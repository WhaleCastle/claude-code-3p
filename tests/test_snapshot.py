import json
import subprocess
import sys
from pathlib import Path


def run_3p(script_path: Path, cwd: Path, *args):
    return subprocess.run([sys.executable, str(script_path), *args],
                          capture_output=True, text=True, cwd=cwd)


def setup_repo_with_files(repo: Path, **files):
    for relpath, content in files.items():
        p = repo / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def test_snapshot_captures_tracked_and_untracked(script_path, tmp_git_repo):
    setup_repo_with_files(tmp_git_repo, **{
        "src/a.py": "# a\n",
        "src/b.py": "# b\n",
        "README.md": "readme",
    })
    subprocess.run(["git", "add", "src/a.py", "README.md"], cwd=tmp_git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_git_repo, check=True)
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    r = run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "pre-build")
    assert r.returncode == 0, r.stderr
    snap = tmp_git_repo / ".3p" / "x-20260603-1430" / "baselines" / "pre-build"
    assert (snap / "src" / "a.py").exists()
    assert (snap / "src" / "b.py").exists()
    assert (snap / "README.md").exists()


def test_snapshot_excludes_hardcoded_secrets(script_path, tmp_git_repo):
    setup_repo_with_files(tmp_git_repo, **{
        ".env": "SECRET=hunter2",
        "config/prod.pem": "----BEGIN----",
        "code.py": "ok",
    })
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "pre-build")
    snap = tmp_git_repo / ".3p" / "x-20260603-1430" / "baselines" / "pre-build"
    assert (snap / "code.py").exists()
    assert not (snap / ".env").exists()
    assert not (snap / "config" / "prod.pem").exists()


def test_snapshot_excludes_bloat(script_path, tmp_git_repo):
    setup_repo_with_files(tmp_git_repo, **{
        "node_modules/foo/index.js": "// huge",
        "src/keep.py": "ok",
    })
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "pre-build")
    snap = tmp_git_repo / ".3p" / "x-20260603-1430" / "baselines" / "pre-build"
    assert (snap / "src" / "keep.py").exists()
    assert not (snap / "node_modules").exists()


def test_snapshot_records_state(script_path, tmp_git_repo):
    setup_repo_with_files(tmp_git_repo, **{"a.py": "x"})
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "pre-build")
    state = json.loads((tmp_git_repo / ".3p" / "x-20260603-1430" / "state.json").read_text())
    assert "pre-build" in state["baselines"]
    assert state["baselines"]["pre-build"]["path"].endswith("baselines/pre-build")
    assert "fileManifest" in state["baselines"]["pre-build"]
    assert "capturedGitignoreRules" in state["baselines"]["pre-build"]
    assert "capturedIgnoredPaths" in state["baselines"]["pre-build"]


def test_snapshot_non_git_mode(script_path, tmp_non_git):
    setup_repo_with_files(tmp_non_git, **{"a.py": "x", ".env": "secret"})
    run_3p(script_path, tmp_non_git, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_non_git, "snapshot", "capture", "x-20260603-1430", "pre-build")
    snap = tmp_non_git / ".3p" / "x-20260603-1430" / "baselines" / "pre-build"
    assert (snap / "a.py").exists()
    assert not (snap / ".env").exists()


def test_snapshot_excludes_3p_dir(script_path, tmp_git_repo):
    setup_repo_with_files(tmp_git_repo, **{"a.py": "x"})
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "pre-build")
    snap = tmp_git_repo / ".3p" / "x-20260603-1430" / "baselines" / "pre-build"
    assert not (snap / ".3p").exists()


def test_snapshot_excludes_git_dir(script_path, tmp_git_repo):
    setup_repo_with_files(tmp_git_repo, **{"src/a.py": "x"})
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "pre-build")
    snap = tmp_git_repo / ".3p" / "x-20260603-1430" / "baselines" / "pre-build"
    assert (snap / "src" / "a.py").exists()
    assert not (snap / ".git").exists()


def test_snapshot_uses_resolved_config_not_cli(script_path, tmp_git_repo):
    setup_repo_with_files(tmp_git_repo, **{"src/a.py": "x", "data/big.bin": "BIG"})
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430", "--exclude", "data/")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "pre-build")
    snap = tmp_git_repo / ".3p" / "x-20260603-1430" / "baselines" / "pre-build"
    assert (snap / "src" / "a.py").exists()
    assert not (snap / "data").exists()


def test_snapshot_excludes_root_level_starstar_secrets(script_path, tmp_git_repo):
    """Spec-mandated: **/.aws/credentials must catch root-level .aws/credentials.
    Same for **/credentials.json and **/.aws/config."""
    (tmp_git_repo / ".aws").mkdir()
    (tmp_git_repo / ".aws" / "credentials").write_text("AKIA...")
    (tmp_git_repo / ".aws" / "config").write_text("[default]")
    (tmp_git_repo / "credentials.json").write_text('{"key":"secret"}')
    (tmp_git_repo / "src.py").write_text("ok")
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "pre-build")
    snap = tmp_git_repo / ".3p" / "x-20260603-1430" / "baselines" / "pre-build"
    assert (snap / "src.py").exists()
    assert not (snap / ".aws" / "credentials").exists()
    assert not (snap / ".aws" / "config").exists()
    assert not (snap / "credentials.json").exists()


def test_gitignore_negation_re_includes(script_path, tmp_non_git):
    setup_repo_with_files(tmp_non_git, **{
        "build/keep.txt": "keep",
        "build/discard.txt": "discard",
    })
    (tmp_non_git / ".gitignore").write_text("build/\n!build/keep.txt\n")
    run_3p(script_path, tmp_non_git, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_non_git, "snapshot", "capture", "x-20260603-1430", "pre-build")
    snap = tmp_non_git / ".3p" / "x-20260603-1430" / "baselines" / "pre-build"
    assert (snap / "build" / "keep.txt").exists()
    assert not (snap / "build" / "discard.txt").exists()

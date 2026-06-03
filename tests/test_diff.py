import subprocess
import sys
from pathlib import Path


def run_3p(script_path, cwd, *args):
    return subprocess.run([sys.executable, str(script_path), *args],
                          capture_output=True, text=True, cwd=cwd)


def test_diff_shows_modifications(script_path, tmp_git_repo):
    (tmp_git_repo / "a.py").write_text("# old\n")
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "step-1")
    (tmp_git_repo / "a.py").write_text("# new\n")
    r = run_3p(script_path, tmp_git_repo, "snapshot", "diff", "x-20260603-1430", "step-1")
    assert r.returncode == 0
    assert "old" in r.stdout
    assert "new" in r.stdout
    assert "a.py" in r.stdout


def test_diff_shows_new_files(script_path, tmp_git_repo):
    (tmp_git_repo / "a.py").write_text("x\n")
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "step-1")
    (tmp_git_repo / "b.py").write_text("brand new\n")
    r = run_3p(script_path, tmp_git_repo, "snapshot", "diff", "x-20260603-1430", "step-1")
    assert "b.py" in r.stdout
    assert "brand new" in r.stdout


def test_diff_excludes_secrets_even_if_they_appear(script_path, tmp_git_repo):
    """Secret files appearing in live tree but not snapshot must not show up."""
    (tmp_git_repo / "a.py").write_text("ok\n")
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "step-1")
    (tmp_git_repo / ".env").write_text("SECRET=hunter2\n")
    r = run_3p(script_path, tmp_git_repo, "snapshot", "diff", "x-20260603-1430", "step-1")
    assert "SECRET=hunter2" not in r.stdout
    assert ".env" not in r.stdout


def test_diff_excludes_bloat_dirs(script_path, tmp_git_repo):
    (tmp_git_repo / "a.py").write_text("ok\n")
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "step-1")
    (tmp_git_repo / "node_modules").mkdir()
    (tmp_git_repo / "node_modules" / "huge.js").write_text("// huge\n")
    r = run_3p(script_path, tmp_git_repo, "snapshot", "diff", "x-20260603-1430", "step-1")
    assert "node_modules" not in r.stdout

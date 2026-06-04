import subprocess
import sys
from pathlib import Path


def run_3p(script_path, cwd, *args):
    return subprocess.run([sys.executable, str(script_path), *args],
                          capture_output=True, text=True, cwd=cwd)


def test_list_returns_runs(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "a", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "init", "b", "20260603-1500")
    r = run_3p(script_path, tmp_git_repo, "list")
    assert r.returncode == 0
    assert "a-20260603-1430" in r.stdout
    assert "b-20260603-1500" in r.stdout


def test_clean_removes_run_dir(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "a", "20260603-1430")
    run_dir = tmp_git_repo / ".3p" / "a-20260603-1430"
    assert run_dir.exists()
    r = run_3p(script_path, tmp_git_repo, "clean", "a-20260603-1430")
    assert r.returncode == 0
    assert not run_dir.exists()


def test_clean_rejects_path_traversal(script_path, tmp_git_repo):
    """run-id with .. or / must be rejected before shutil.rmtree."""
    victim_dir = tmp_git_repo.parent / "victim"
    victim_dir.mkdir(exist_ok=True)
    (victim_dir / "important.txt").write_text("do not delete")
    r = run_3p(script_path, tmp_git_repo, "clean", "../../victim")
    assert r.returncode != 0, f"Should reject path traversal; got: {r.stdout!r} {r.stderr!r}"
    assert victim_dir.exists()
    assert (victim_dir / "important.txt").exists()


def test_clean_rejects_malformed_run_id(script_path, tmp_git_repo):
    r = run_3p(script_path, tmp_git_repo, "clean", "not-a-real-runid")
    assert r.returncode != 0

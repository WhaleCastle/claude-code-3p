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


def test_diff_includes_new_file_added_to_gitignore_mid_run(script_path, tmp_git_repo):
    """Even if a task adds new.txt to .gitignore, a new.txt CREATED after baseline
    must still appear in the reviewer diff (spec: symmetric filtering against
    CAPTURE-TIME rules, not current rules)."""
    (tmp_git_repo / "a.py").write_text("# a\n")
    (tmp_git_repo / ".gitignore").write_text("# initial\n")
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "step-1")
    # Mid-task: task modifies .gitignore to ignore new.txt, then creates new.txt
    (tmp_git_repo / ".gitignore").write_text("# updated\nnew.txt\n")
    (tmp_git_repo / "new.txt").write_text("important new content\n")
    r = run_3p(script_path, tmp_git_repo, "snapshot", "diff", "x-20260603-1430", "step-1")
    assert r.returncode == 0, r.stderr
    assert "new.txt" in r.stdout, "new.txt must appear in diff (capture-time rules didn't exclude it)"
    assert "important new content" in r.stdout


def test_diff_excludes_bloat_dirs(script_path, tmp_git_repo):
    (tmp_git_repo / "a.py").write_text("ok\n")
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "step-1")
    (tmp_git_repo / "node_modules").mkdir()
    (tmp_git_repo / "node_modules" / "huge.js").write_text("// huge\n")
    r = run_3p(script_path, tmp_git_repo, "snapshot", "diff", "x-20260603-1430", "step-1")
    assert "node_modules" not in r.stdout

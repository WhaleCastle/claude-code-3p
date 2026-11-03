"""The end-of-run summary warns when tracked files link into `.3p/`."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

RUN = "x-20260603-1430"


def run_3p(script_path, cwd, *args):
    return subprocess.run([sys.executable, str(script_path), *args],
                          capture_output=True, text=True, cwd=cwd)


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def commit(repo: Path, rel: str, content: str):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", rel)


def summary_of(script_path, repo: Path, *init_args) -> tuple:
    assert run_3p(script_path, repo, "init", "x", "20260603-1430", *init_args).returncode == 0
    r = run_3p(script_path, repo, "summary", RUN)
    assert r.returncode == 0, r.stderr
    return (repo / ".3p" / RUN / "summary.md").read_text(), r.stderr


def test_summary_warns_about_tracked_links(script_path, tmp_git_repo):
    commit(tmp_git_repo, "docs/report.md", "evidence in .3p/old-run-20260101-0000/dashboard.md\n")
    text, err = summary_of(script_path, tmp_git_repo)
    assert "## ⚠ Tracked files reference .3p/" in text
    assert "`docs/report.md:1`" in text
    assert "1 tracked line(s) reference .3p/" in err


def test_summary_has_no_warning_without_links(script_path, tmp_git_repo):
    commit(tmp_git_repo, "README.md", "nothing to see\n")
    text, err = summary_of(script_path, tmp_git_repo)
    assert "Tracked files reference .3p/" not in text
    assert "reference .3p/" not in err


def test_gitignore_rule_is_not_reported(script_path, tmp_git_repo):
    commit(tmp_git_repo, ".gitignore", ".3p/\n")
    text, _ = summary_of(script_path, tmp_git_repo)
    assert "Tracked files reference .3p/" not in text


def test_untracked_files_are_not_reported(script_path, tmp_git_repo):
    commit(tmp_git_repo, "README.md", "x\n")
    (tmp_git_repo / "scratch.md").write_text("see .3p/old/x.md\n")
    text, _ = summary_of(script_path, tmp_git_repo)
    assert "Tracked files reference .3p/" not in text


def test_long_lists_are_capped(script_path, tmp_git_repo):
    commit(tmp_git_repo, "many.md", "".join(f".3p/r{i}/a.md\n" for i in range(25)))
    text, _ = summary_of(script_path, tmp_git_repo)
    assert "…and 5 more" in text


def test_think_summary_warns_too(script_path, tmp_git_repo):
    commit(tmp_git_repo, "notes.md", "see .3p/some-run/memo.md\n")
    text, _ = summary_of(script_path, tmp_git_repo, "--mode", "think")
    assert "## ⚠ Tracked files reference .3p/" in text


def test_grep_config_does_not_hide_links(script_path, tmp_git_repo):
    commit(tmp_git_repo, "docs/a.md", "x .3p/run/a.md\n")
    git(tmp_git_repo, "config", "color.grep", "always")
    git(tmp_git_repo, "config", "grep.column", "true")
    text, _ = summary_of(script_path, tmp_git_repo)
    assert "`docs/a.md:1`" in text


def test_secret_file_lines_are_named_but_not_quoted(script_path, tmp_git_repo):
    commit(tmp_git_repo, "app/credentials.json",
           '{"token": "sk-test-SYNTHETIC-123", "see": ".3p/run/a"}\n')
    for mode in ((), ("--mode", "think")):
        text, _ = summary_of(script_path, tmp_git_repo, *mode)
        assert "`app/credentials.json:1`" in text
        assert "SYNTHETIC" not in text
        assert "excerpt hidden" in text
        shutil.rmtree(tmp_git_repo / ".3p" / RUN)


def test_incomplete_scan_is_reported(script_path, tmp_git_repo):
    commit(tmp_git_repo, "docs/a.md", "x .3p/run/a.md\n")
    (tmp_git_repo / "docs/a.md").chmod(0o000)
    try:
        text, err = summary_of(script_path, tmp_git_repo)
    finally:
        (tmp_git_repo / "docs/a.md").chmod(0o644)
    assert "could not check tracked files for .3p links" in err
    assert "Tracked files reference .3p/" not in text

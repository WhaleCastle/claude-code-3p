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
    """Secret files appearing in live tree must not have their content in the diff.
    The filename may appear in the WARNING comment block (that is spec-compliant)."""
    (tmp_git_repo / "a.py").write_text("ok\n")
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "step-1")
    (tmp_git_repo / ".env").write_text("SECRET=hunter2\n")
    r = run_3p(script_path, tmp_git_repo, "snapshot", "diff", "x-20260603-1430", "step-1")
    assert "SECRET=hunter2" not in r.stdout
    # .env content must not appear in any diff hunk (only in comment lines is OK)
    for line in r.stdout.splitlines():
        if not line.startswith("#"):
            assert ".env" not in line, f"secret filename leaked into diff hunk: {line!r}"


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


def test_diff_handles_spaces_in_filenames(script_path, tmp_git_repo):
    """Diff parser must handle file paths containing spaces."""
    (tmp_git_repo / "my file.py").write_text("# v1\n")
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "step-1")
    (tmp_git_repo / "my file.py").write_text("# v2\n")
    r = run_3p(script_path, tmp_git_repo, "snapshot", "diff", "x-20260603-1430", "step-1")
    assert "my file.py" in r.stdout
    assert "v1" in r.stdout
    assert "v2" in r.stdout


def test_diff_summary_handles_spaces_in_filenames(script_path, tmp_git_repo):
    """_parse_diff_header_paths (used by summary) must use rfind so it picks the
    correct split when the filename itself contains the anchor base string."""
    import importlib.util, os as _os
    spec = importlib.util.spec_from_file_location("threep", str(script_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Construct a rest-string where find() would pick the wrong split
    snap_str = "/snap"
    anchor_str = "/anc"
    # filename "x /anc/y.py" contains " /anc/" — find() hits that first
    rest = "/snap/x /anc/y.py /anc/x /anc/y.py"
    result = mod._parse_diff_header_paths(rest, snap_str, anchor_str)
    expected = _os.path.join("x /anc", "y.py")
    assert result == expected, f"rfind fix needed: got {result!r}, want {expected!r}"


def test_diff_warns_about_dropped_secret_files(script_path, tmp_git_repo):
    """Spec: skipped secrets get a warning so the user knows what was excluded."""
    (tmp_git_repo / "a.py").write_text("ok\n")
    (tmp_git_repo / ".env").write_text("SECRET=hunter2\n")
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "step-1")
    (tmp_git_repo / ".env").write_text("SECRET=changed\n")
    r = run_3p(script_path, tmp_git_repo, "snapshot", "diff", "x-20260603-1430", "step-1")
    assert "hunter2" not in r.stdout
    assert "changed" not in r.stdout
    assert ("secret pattern match" in r.stdout.lower() or "WARNING" in r.stdout)
    assert ".env" in r.stdout


def test_diff_excludes_bloat_dirs(script_path, tmp_git_repo):
    (tmp_git_repo / "a.py").write_text("ok\n")
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "step-1")
    (tmp_git_repo / "node_modules").mkdir()
    (tmp_git_repo / "node_modules" / "huge.js").write_text("// huge\n")
    r = run_3p(script_path, tmp_git_repo, "snapshot", "diff", "x-20260603-1430", "step-1")
    assert "node_modules" not in r.stdout


def test_diff_honors_captured_info_exclude_for_new_files(script_path, tmp_git_repo):
    """Spec: live-side diff must honor captured-time .git/info/exclude rules.
    A file added to info/exclude AND created after baseline must NOT appear in diff."""
    (tmp_git_repo / "a.py").write_text("ok\n")
    info_exclude = tmp_git_repo / ".git" / "info" / "exclude"
    info_exclude.parent.mkdir(parents=True, exist_ok=True)
    info_exclude.write_text("local-only.txt\n")
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "step-1")
    # File didn't exist at capture; gets created mid-task; should be excluded by frozen info/exclude
    (tmp_git_repo / "local-only.txt").write_text("LAPTOP-SECRET-CONFIG=xyz\n")
    r = run_3p(script_path, tmp_git_repo, "snapshot", "diff", "x-20260603-1430", "step-1")
    assert r.returncode == 0, r.stderr
    assert "LAPTOP-SECRET-CONFIG" not in r.stdout, \
        ".git/info/exclude rule must keep this file out of the diff"


def test_diff_honors_captured_nested_gitignore(script_path, tmp_git_repo):
    """Nested .gitignore at pkg/foo/.gitignore should exclude pkg/foo/build/* even
    for files created after baseline."""
    import subprocess
    (tmp_git_repo / "pkg" / "foo").mkdir(parents=True)
    (tmp_git_repo / "pkg" / "foo" / ".gitignore").write_text("build/\n")
    (tmp_git_repo / "pkg" / "foo" / "src.py").write_text("ok\n")
    # Capture baseline now
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture", "x-20260603-1430", "step-1")
    # Mid-task: create files under pkg/foo/build/ — frozen nested rule should exclude
    (tmp_git_repo / "pkg" / "foo" / "build").mkdir()
    (tmp_git_repo / "pkg" / "foo" / "build" / "output.bin").write_text("BUILD-ARTIFACT\n")
    r = run_3p(script_path, tmp_git_repo, "snapshot", "diff", "x-20260603-1430", "step-1")
    assert r.returncode == 0, r.stderr
    assert "BUILD-ARTIFACT" not in r.stdout, \
        "Nested .gitignore must keep pkg/foo/build/output.bin out of the diff"


def test_diff_honors_relative_core_excludesfile(script_path, tmp_git_repo):
    """Spec: relative core.excludesFile paths must be resolved against the
    repo anchor, not the process CWD, so the global excludes source is
    captured correctly regardless of where the user invoked from."""
    import subprocess
    # Set a RELATIVE core.excludesFile that lives at the repo root
    (tmp_git_repo / ".local-ignore").write_text("notes-*.txt\n")
    subprocess.run(["git", "config", "core.excludesFile", ".local-ignore"],
                   cwd=tmp_git_repo, check=True)
    (tmp_git_repo / "src.py").write_text("ok\n")
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "snapshot", "capture",
           "x-20260603-1430", "step-1")
    # Mid-task: create a file matching the relative excludesFile rule
    (tmp_git_repo / "notes-private.txt").write_text("LOCAL-NOTES-CONTENT=foo\n")
    r = run_3p(script_path, tmp_git_repo, "snapshot", "diff",
               "x-20260603-1430", "step-1")
    assert r.returncode == 0, r.stderr
    assert "LOCAL-NOTES-CONTENT" not in r.stdout, \
        "Relative core.excludesFile rule must exclude this file from the diff"

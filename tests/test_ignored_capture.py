"""capturedIgnoredPaths stays small (no .3p/.git entries, wholly-ignored dirs
collapsed to `dir/`) while `snapshot diff` output is unchanged."""
import json
import re
import subprocess
import sys
from pathlib import Path

RUN = "x-20260603-1430"


def run_3p(script_path, cwd, *args):
    return subprocess.run([sys.executable, str(script_path), *args],
                          capture_output=True, text=True, cwd=cwd)


def write(repo: Path, rel: str, content: str = "x\n"):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def git(repo: Path, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _without_mtimes(diff_text: str) -> str:
    """`diff -u` headers carry file mtimes; compare everything else."""
    return re.sub(r"^((?:---|\+\+\+) .*?)\t.*$", r"\1", diff_text, flags=re.M)


def capture(script_path, repo: Path, key: str = "step-1") -> dict:
    r = run_3p(script_path, repo, "init", "x", "20260603-1430")
    assert r.returncode == 0, r.stderr
    r = run_3p(script_path, repo, "snapshot", "capture", RUN, key)
    assert r.returncode == 0, r.stderr
    return json.loads((repo / ".3p" / RUN / "state.json").read_text())["baselines"][key]


def diff(script_path, repo: Path, key: str = "step-1") -> str:
    r = run_3p(script_path, repo, "snapshot", "diff", RUN, key)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_excluded_dirs_never_recorded_at_any_depth(script_path, tmp_git_repo):
    write(tmp_git_repo, ".gitignore", ".3p/\n")
    write(tmp_git_repo, ".3p/old-run-20260101-0000/state.json", "{}")
    write(tmp_git_repo, "pkg/.3p/old/notes.md")
    write(tmp_git_repo, "src/a.py")
    ignored = capture(script_path, tmp_git_repo)["capturedIgnoredPaths"]
    assert not [p for p in ignored if ".3p" in p.rstrip("/").split("/")]
    assert not [p for p in ignored if ".git" in p.rstrip("/").split("/")]


def test_pattern_ignored_dir_collapses_to_one_entry(script_path, tmp_git_repo):
    write(tmp_git_repo, ".gitignore", "node_modules/\n")
    for i in range(5):
        write(tmp_git_repo, f"node_modules/pkg{i}/index.js")
    ignored = capture(script_path, tmp_git_repo)["capturedIgnoredPaths"]
    assert "node_modules/" in ignored
    assert not [p for p in ignored if p.startswith("node_modules/") and p != "node_modules/"]


def test_dir_of_only_ignored_files_is_not_collapsed(script_path, tmp_git_repo):
    # git reports `logs/` under --directory although logs/ itself is not ignored.
    write(tmp_git_repo, ".gitignore", "*.log\n")
    write(tmp_git_repo, "logs/a.log")
    ignored = capture(script_path, tmp_git_repo)["capturedIgnoredPaths"]
    assert "logs/" not in ignored
    assert "logs/a.log" in ignored
    write(tmp_git_repo, "logs/new.txt", "fresh\n")
    assert "logs/new.txt" in diff(script_path, tmp_git_repo)


def test_negated_child_dir_is_not_collapsed(script_path, tmp_git_repo):
    # `logs/*` matches the slash form `logs/` in check-ignore but not `logs`.
    write(tmp_git_repo, ".gitignore", "logs/*\n!logs/keep.txt\n")
    write(tmp_git_repo, "logs/a.log")
    ignored = capture(script_path, tmp_git_repo)["capturedIgnoredPaths"]
    assert "logs/" not in ignored
    write(tmp_git_repo, "logs/keep.txt", "keep me\n")
    assert "logs/keep.txt" in diff(script_path, tmp_git_repo)


def test_new_file_inside_collapsed_dir_stays_out_of_diff(script_path, tmp_git_repo):
    write(tmp_git_repo, ".gitignore", "node_modules/\n")
    write(tmp_git_repo, "node_modules/a/index.js")
    capture(script_path, tmp_git_repo)
    write(tmp_git_repo, "node_modules/b/new.js", "added later\n")
    assert "node_modules" not in diff(script_path, tmp_git_repo)


def test_tracked_file_inside_ignored_dir_still_diffs(script_path, tmp_git_repo):
    write(tmp_git_repo, ".gitignore", "dist/\n")
    write(tmp_git_repo, "dist/tracked.js", "old\n")
    git(tmp_git_repo, "add", "-f", "dist/tracked.js", ".gitignore")
    git(tmp_git_repo, "commit", "-q", "-m", "init")
    write(tmp_git_repo, "dist/untracked.js")
    ignored = capture(script_path, tmp_git_repo)["capturedIgnoredPaths"]
    assert "dist/" not in ignored
    assert "dist/untracked.js" in ignored


def test_new_and_legacy_lists_give_identical_diff(script_path, tmp_git_repo):
    write(tmp_git_repo, ".gitignore", "node_modules/\n*.log\nbuild\nlogs/*\n!logs/keep.txt\n")
    write(tmp_git_repo, "src/app.py", "v1\n")
    git(tmp_git_repo, "add", ".")
    git(tmp_git_repo, "commit", "-q", "-m", "init")
    write(tmp_git_repo, "node_modules/a/index.js")
    write(tmp_git_repo, "build/out/o.txt")
    write(tmp_git_repo, "logs/a.log")
    write(tmp_git_repo, "tmp/only.log")
    capture(script_path, tmp_git_repo)
    # Changes after capture: tracked edit, new plain file, new files inside
    # collapsed dirs, new non-ignored files beside ignored ones.
    write(tmp_git_repo, "src/app.py", "v2\n")
    write(tmp_git_repo, "src/new.py", "new\n")
    write(tmp_git_repo, "node_modules/b/late.js")
    write(tmp_git_repo, "build/late.txt")
    write(tmp_git_repo, "logs/keep.txt", "kept\n")
    write(tmp_git_repo, "tmp/note.txt", "note\n")
    new_diff = diff(script_path, tmp_git_repo)

    state_path = tmp_git_repo / ".3p" / RUN / "state.json"
    state = json.loads(state_path.read_text())
    legacy = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        cwd=tmp_git_repo, capture_output=True, check=True,
    ).stdout.decode().split("\x00")
    # The legacy list was taken at capture time; drop files created since.
    late = {"node_modules/b/late.js", "build/late.txt"}
    state["baselines"]["step-1"]["capturedIgnoredPaths"] = sorted(
        p for p in legacy if p and p not in late)
    state_path.write_text(json.dumps(state))
    assert _without_mtimes(diff(script_path, tmp_git_repo)) == _without_mtimes(new_diff)
    for expected in ("src/app.py", "src/new.py", "logs/keep.txt", "tmp/note.txt"):
        assert expected in new_diff
    for hidden in ("late.js", "build/late.txt"):
        assert hidden not in new_diff

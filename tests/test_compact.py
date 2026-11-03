"""A finished run compacts itself: baseline copies and bulky state fields go
(only when final-diff.txt exists), build-output dirs go (never inside
baselines/), and anything that needs a compacted baseline fails clearly."""
import json
import subprocess
import sys
from pathlib import Path

RUN = "x-20260603-1430"


def run_3p(script_path, cwd, *args):
    return subprocess.run([sys.executable, str(script_path), *args],
                          capture_output=True, text=True, cwd=cwd)


def write(path: Path, content: str = "x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def state_of(repo: Path) -> dict:
    return json.loads((repo / ".3p" / RUN / "state.json").read_text())


def started_run(script_path, repo: Path, *, final_diff: bool = True) -> Path:
    """init + pre-build/step-1 baselines + a change, optionally final-diff.txt."""
    write(repo / "src/app.py", "v1\n")
    write(repo / "notes.md", "base\n")
    # The baseline's git ref comes from `git stash create`, which needs a
    # commit and an uncommitted change to a tracked file.
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    write(repo / "notes.md", "work in progress\n")
    assert run_3p(script_path, repo, "init", "x", "20260603-1430").returncode == 0
    for key in ("pre-build", "step-1"):
        r = run_3p(script_path, repo, "snapshot", "capture", RUN, key)
        assert r.returncode == 0, r.stderr
    write(repo / "src/app.py", "v2\n")
    run_dir = repo / ".3p" / RUN
    if final_diff:
        d = run_3p(script_path, repo, "snapshot", "diff", RUN, "pre-build")
        (run_dir / "final-diff.txt").write_text(d.stdout)
    return run_dir


def finish(script_path, repo: Path):
    r = run_3p(script_path, repo, "state-write", RUN, "phase", '"done"')
    assert r.returncode == 0, r.stderr
    return r


def test_done_transition_compacts_baselines(script_path, tmp_git_repo):
    run_dir = started_run(script_path, tmp_git_repo)
    assert (run_dir / "baselines/pre-build/src/app.py").exists()
    r = finish(script_path, tmp_git_repo)
    assert "compacted" in r.stderr
    assert r.stdout == ""
    assert not (run_dir / "baselines/pre-build").exists()
    assert not (run_dir / "baselines/step-1").exists()
    st = state_of(tmp_git_repo)
    assert st["compactedAt"]
    for key in ("pre-build", "step-1"):
        entry = st["baselines"][key]
        assert entry["compacted"] is True
        for gone in ("fileManifest", "capturedIgnoredPaths",
                     "capturedIgnoreSources", "capturedGitignoreRules"):
            assert gone not in entry
        assert entry["path"] and entry["gitRef"] and entry["gitSha"]
    # The git ref still holds the exact tree.
    show = subprocess.run(["git", "show", f"{st['baselines']['pre-build']['gitSha']}:src/app.py"],
                          cwd=tmp_git_repo, capture_output=True, text=True)
    assert show.stdout == "v1\n"


def test_audit_trail_files_are_kept(script_path, tmp_git_repo):
    run_dir = started_run(script_path, tmp_git_repo)
    for name in ("task.txt", "plan.md", "plan-round-1-codex.md", "step-1-summary.md",
                 "final-test-output.txt", "dashboard.md", "summary.md",
                 "plan-round-1-codex.raw.txt"):
        write(run_dir / name, f"{name}\n")
    finish(script_path, tmp_git_repo)
    for name in ("task.txt", "plan.md", "plan-round-1-codex.md", "step-1-summary.md",
                 "final-test-output.txt", "dashboard.md", "summary.md",
                 "plan-round-1-codex.raw.txt", "final-diff.txt"):
        assert (run_dir / name).exists(), name


def test_unfinished_run_is_refused(script_path, tmp_git_repo):
    run_dir = started_run(script_path, tmp_git_repo)
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "phase", '"final"')
    write(run_dir / "evidence/node_modules/pkg/index.js")
    before = state_of(tmp_git_repo)
    r = run_3p(script_path, tmp_git_repo, "compact", RUN)
    assert r.returncode == 2
    assert "only finished runs" in r.stderr
    assert (run_dir / "baselines/pre-build/src/app.py").exists()
    assert (run_dir / "evidence/node_modules/pkg/index.js").exists()
    assert state_of(tmp_git_repo) == before


def test_missing_final_diff_keeps_baselines_fully_intact(script_path, tmp_git_repo):
    run_dir = started_run(script_path, tmp_git_repo, final_diff=False)
    write(run_dir / "baselines/pre-build/dist/tracked.js", "keep me\n")
    before = state_of(tmp_git_repo)["baselines"]
    r = finish(script_path, tmp_git_repo)
    assert "final-diff.txt is missing" in r.stderr
    assert (run_dir / "baselines/pre-build/src/app.py").read_text() == "v1\n"
    assert (run_dir / "baselines/pre-build/dist/tracked.js").read_text() == "keep me\n"
    assert state_of(tmp_git_repo)["baselines"] == before
    # The intact baseline still diffs.
    d = run_3p(script_path, tmp_git_repo, "snapshot", "diff", RUN, "pre-build")
    assert d.returncode == 0 and "v2" in d.stdout


def test_heavy_dirs_removed_outside_baselines_only(script_path, tmp_git_repo):
    run_dir = started_run(script_path, tmp_git_repo)
    write(run_dir / "step5/node_modules/a/index.js")
    write(run_dir / "evidence/app/.next/cache/x.bin")
    write(run_dir / "builds/deep/nested/dist/bundle.js")
    write(run_dir / "notes/distribution.md", "not a dist dir\n")
    finish(script_path, tmp_git_repo)
    assert not (run_dir / "step5/node_modules").exists()
    assert not (run_dir / "evidence/app/.next").exists()
    assert not (run_dir / "builds/deep/nested/dist").exists()
    assert (run_dir / "notes/distribution.md").exists()
    assert (run_dir / "step5").exists()


def test_symlinked_heavy_dir_is_not_followed(script_path, tmp_git_repo, tmp_path):
    run_dir = started_run(script_path, tmp_git_repo)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    write(outside / "precious.txt", "do not delete\n")
    (run_dir / "node_modules").symlink_to(outside, target_is_directory=True)
    (run_dir / "link-dir").symlink_to(outside, target_is_directory=True)
    finish(script_path, tmp_git_repo)
    assert (outside / "precious.txt").read_text() == "do not delete\n"


def test_symlinked_baselines_dir_is_left_alone(script_path, tmp_git_repo, tmp_path):
    run_dir = started_run(script_path, tmp_git_repo)
    outside = tmp_path.parent / f"{tmp_path.name}-baselines"
    (run_dir / "baselines").rename(outside)
    (run_dir / "baselines").symlink_to(outside, target_is_directory=True)
    r = finish(script_path, tmp_git_repo)
    assert "it is a symlink" in r.stderr
    assert (outside / "pre-build/src/app.py").read_text() == "v1\n"
    assert (outside / "step-1/src/app.py").exists()


def test_failed_deletion_never_leaves_a_usable_looking_baseline(script_path, tmp_git_repo):
    run_dir = started_run(script_path, tmp_git_repo)
    locked = run_dir / "baselines/step-1/src"
    locked.chmod(0o500)  # removing step-1 now fails partway
    try:
        r = finish(script_path, tmp_git_repo)
        assert r.returncode == 0
        assert "warning: could not compact" in r.stderr
        st = state_of(tmp_git_repo)
        assert all(st["baselines"][k]["compacted"] for k in ("pre-build", "step-1"))
        d = run_3p(script_path, tmp_git_repo, "snapshot", "diff", RUN, "pre-build")
        assert d.returncode == 2 and d.stdout == ""
    finally:
        locked.chmod(0o700)
    # A retry finishes the deletions.
    assert run_3p(script_path, tmp_git_repo, "compact", RUN).returncode == 0
    assert not (run_dir / "baselines/step-1").exists()


def test_compaction_error_does_not_fail_state_write(script_path, tmp_git_repo):
    run_dir = started_run(script_path, tmp_git_repo)
    # A read-only heavy dir makes removal raise mid-compaction.
    locked = run_dir / "evidence/node_modules"
    write(locked / "f.js")
    locked.chmod(0o500)
    try:
        r = run_3p(script_path, tmp_git_repo, "state-write", RUN, "phase", '"done"')
        assert r.returncode == 0
        assert "warning: could not compact" in r.stderr
        assert state_of(tmp_git_repo)["phase"] == "done"
    finally:
        locked.chmod(0o700)


def test_compacted_baseline_fails_clearly(script_path, tmp_git_repo):
    started_run(script_path, tmp_git_repo)
    finish(script_path, tmp_git_repo)
    d = run_3p(script_path, tmp_git_repo, "snapshot", "diff", RUN, "pre-build")
    assert d.returncode == 2
    assert "compacted" in d.stderr and "refs/3p/" in d.stderr
    assert d.stdout == ""
    c = run_3p(script_path, tmp_git_repo, "snapshot", "capture", RUN, "step-1")
    assert c.returncode == 2 and "refusing to capture" in c.stderr


def _load_module(script_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("threep_under_test", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _finish_in_background(script_path, repo):
    """Start `state-write phase "done"` in another process (as a second agent
    would) and confirm it is blocked by the lifecycle lock."""
    import time
    proc = subprocess.Popen(
        [sys.executable, str(script_path), "state-write", RUN, "phase", '"done"'],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(1.0)
    assert proc.poll() is None, "finish should wait for the in-flight operation"
    assert state_of(repo)["phase"] != "done"
    return proc


def test_finish_waits_for_in_flight_capture(script_path, tmp_git_repo, monkeypatch):
    """A run cannot finish (and compact) while a capture is copying files, so
    the capture completes, its ref matches its recorded SHA, and compaction
    then covers it."""
    run_dir = started_run(script_path, tmp_git_repo)
    mod = _load_module(script_path)
    monkeypatch.chdir(tmp_git_repo)
    real_copy = mod.shutil.copy2
    procs = []

    def copy_and_race(src, dst, *a, **k):
        out = real_copy(src, dst, *a, **k)
        if not procs:
            procs.append(_finish_in_background(script_path, tmp_git_repo))
        return out

    monkeypatch.setattr(mod.shutil, "copy2", copy_and_race)
    assert mod.cmd_snapshot_capture([RUN, "step-2"]) == 0
    proc = procs[0]
    assert proc.wait(timeout=60) == 0, proc.stderr.read()
    st = state_of(tmp_git_repo)
    assert st["phase"] == "done"
    entry = st["baselines"]["step-2"]
    assert entry["compacted"] is True
    ref_sha = subprocess.run(["git", "rev-parse", entry["gitRef"]], cwd=tmp_git_repo,
                             capture_output=True, text=True).stdout.strip()
    assert ref_sha == entry["gitSha"]
    assert not (run_dir / "baselines/step-2").exists()


def test_finish_waits_for_in_flight_diff(script_path, tmp_git_repo, monkeypatch, capsys):
    """Compaction cannot delete baseline copies while a diff is reading them,
    so the in-flight diff stays correct (no spurious additions)."""
    started_run(script_path, tmp_git_repo)
    mod = _load_module(script_path)
    monkeypatch.chdir(tmp_git_repo)
    real_walk = mod.enumerate_files_nongit
    procs = []

    def walk_and_race(*a, **k):
        if not procs:
            procs.append(_finish_in_background(script_path, tmp_git_repo))
        return real_walk(*a, **k)

    monkeypatch.setattr(mod, "enumerate_files_nongit", walk_and_race)
    assert mod.cmd_snapshot_diff([RUN, "step-1"]) == 0
    out = capsys.readouterr().out
    assert "src/app.py" in out and "notes.md" not in out  # only the real change
    assert "Only in" not in out
    assert procs[0].wait(timeout=60) == 0
    d = run_3p(script_path, tmp_git_repo, "snapshot", "diff", RUN, "step-1")
    assert d.returncode == 2 and d.stdout == ""


def test_failed_ref_pin_is_not_recorded(script_path, tmp_git_repo):
    """If git cannot move the ref, state must not advertise a SHA the ref
    does not hold."""
    started_run(script_path, tmp_git_repo)
    ref = f"refs/3p/{RUN}/pre-build"
    old = subprocess.run(["git", "rev-parse", ref], cwd=tmp_git_repo,
                         capture_output=True, text=True).stdout.strip()
    lock = tmp_git_repo / ".git" / f"{ref}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("stale\n")  # a stale ref lock makes update-ref fail
    try:
        write(tmp_git_repo / "notes.md", "another change\n")
        r = run_3p(script_path, tmp_git_repo, "snapshot", "capture", RUN, "pre-build")
        assert r.returncode == 0, r.stderr
        assert "could not pin git ref" in r.stderr
    finally:
        lock.unlink()
    entry = state_of(tmp_git_repo)["baselines"]["pre-build"]
    assert "gitSha" not in entry and "gitRef" not in entry
    now = subprocess.run(["git", "rev-parse", ref], cwd=tmp_git_repo,
                         capture_output=True, text=True).stdout.strip()
    assert now == old


def test_refused_capture_never_moves_the_recovery_ref(script_path, tmp_git_repo, monkeypatch):
    """Defensive path: if state already says done when capture records its
    baseline, the capture is refused and the existing ref is untouched."""
    started_run(script_path, tmp_git_repo)
    finish(script_path, tmp_git_repo)
    before = subprocess.run(["git", "for-each-ref", f"refs/3p/{RUN}/"], cwd=tmp_git_repo,
                            capture_output=True, text=True).stdout
    mod = _load_module(script_path)
    monkeypatch.chdir(tmp_git_repo)
    # Bypass the up-front refusal to exercise the locked re-check.
    real_read = mod.read_state
    calls = []

    def first_read_unfinished(run_dir):
        s = real_read(run_dir)
        if not calls:
            calls.append(True)
            s = {**s, "phase": "final", "baselines": {}}
        return s

    monkeypatch.setattr(mod, "read_state", first_read_unfinished)
    write(tmp_git_repo / "notes.md", "changed again\n")
    assert mod.cmd_snapshot_capture([RUN, "pre-build"]) == 2
    after = subprocess.run(["git", "for-each-ref", f"refs/3p/{RUN}/"], cwd=tmp_git_repo,
                           capture_output=True, text=True).stdout
    assert after == before


def test_capture_on_finished_run_is_refused(script_path, tmp_git_repo):
    started_run(script_path, tmp_git_repo)
    finish(script_path, tmp_git_repo)
    r = run_3p(script_path, tmp_git_repo, "snapshot", "capture", RUN, "step-9")
    assert r.returncode == 2 and "refusing to capture" in r.stderr
    assert "step-9" not in state_of(tmp_git_repo)["baselines"]


def test_summary_lists_changed_files_after_compaction(script_path, tmp_git_repo):
    run_dir = started_run(script_path, tmp_git_repo)
    finish(script_path, tmp_git_repo)
    assert run_3p(script_path, tmp_git_repo, "summary", RUN).returncode == 0
    assert "- `src/app.py`" in (run_dir / "summary.md").read_text()


def test_summary_notes_unavailable_changes(script_path, tmp_git_repo):
    run_dir = started_run(script_path, tmp_git_repo)
    finish(script_path, tmp_git_repo)
    (run_dir / "final-diff.txt").unlink()
    assert run_3p(script_path, tmp_git_repo, "summary", RUN).returncode == 0
    assert "Unavailable: the pre-build baseline was compacted" in (run_dir / "summary.md").read_text()


def test_second_compact_is_a_no_op(script_path, tmp_git_repo):
    started_run(script_path, tmp_git_repo)
    finish(script_path, tmp_git_repo)
    first = state_of(tmp_git_repo)
    r = run_3p(script_path, tmp_git_repo, "compact", RUN)
    assert r.returncode == 0, r.stderr
    assert "freed 0 B" in r.stdout
    assert state_of(tmp_git_repo) == first


def test_think_run_compacts_without_baselines(script_path, tmp_git_repo):
    assert run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430",
                  "--mode", "think").returncode == 0
    run_dir = tmp_git_repo / ".3p" / RUN
    write(run_dir / "memo.md", "memo\n")
    write(run_dir / "scratch/node_modules/a.js")
    r = finish(script_path, tmp_git_repo)
    assert "warning" not in r.stderr
    assert not (run_dir / "scratch/node_modules").exists()
    assert (run_dir / "memo.md").exists()
    assert state_of(tmp_git_repo)["compactedAt"]


def test_compacted_diff_error_states_what_the_ref_keeps(script_path, tmp_git_repo):
    """The recovery ref comes from `git stash create`, which records tracked
    files only; the message must not promise more than that."""
    started_run(script_path, tmp_git_repo)
    write(tmp_git_repo / "untracked.txt", "u\n")
    finish(script_path, tmp_git_repo)
    d = run_3p(script_path, tmp_git_repo, "snapshot", "diff", RUN, "pre-build")
    assert d.returncode == 2
    assert "Tracked files can be recovered" in d.stderr
    assert "untracked files are not kept" in d.stderr

"""Old finished runs are pruned (with their refs); anything uncertain is kept
and reported. Bare `prune` changes nothing; `init` auto-prunes quietly."""
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def run_3p(script_path, cwd, *args):
    return subprocess.run([sys.executable, str(script_path), *args],
                          capture_output=True, text=True, cwd=cwd)


def write(path: Path, content: str = "x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def make_run(script_path, repo: Path, slug: str, *, phase="done", days_ago=0,
             with_ref=True) -> str:
    """A run in `phase`, finished `days_ago` days ago, optionally with a ref."""
    ts = "20260101-0000"
    run_id = f"{slug}-{ts}"
    assert run_3p(script_path, repo, "init", slug, ts, "--config",
                  str(repo / "no-prune.json")).returncode == 0
    run_dir = repo / ".3p" / run_id
    if with_ref:
        sha = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "update-ref", f"refs/3p/{run_id}/pre-build", sha)
    st = json.loads((run_dir / "state.json").read_text())
    st["phase"] = phase
    finished = datetime.now(timezone.utc) - timedelta(days=days_ago)
    st["timeline"].append({"ts": finished.replace(microsecond=0).isoformat(),
                           "kind": "phase", "label": phase})
    (run_dir / "state.json").write_text(json.dumps(st))
    write(run_dir / "evidence/blob.bin", "z" * 4096)
    return run_id


def repo_with_commit(repo: Path) -> Path:
    write(repo / "README.md", "readme\n")
    # Fixture runs are created with auto-prune disabled so setup never prunes.
    write(repo / "no-prune.json", json.dumps({"retention": {"days": 0}}))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def refs_for(repo, run_id):
    return git(repo, "for-each-ref", f"refs/3p/{run_id}/", "--format=%(refname)").stdout.split()


def snapshot_tree(repo: Path) -> dict:
    out = {}
    for p in sorted((repo / ".3p").rglob("*")):
        if p.is_file() and p.name != ".state.lock":
            out[str(p.relative_to(repo))] = p.read_bytes()
    out["refs"] = git(repo, "for-each-ref", "refs/3p/").stdout
    return out


def test_bare_prune_and_dry_run_change_nothing(script_path, tmp_git_repo):
    repo = repo_with_commit(tmp_git_repo)
    old = make_run(script_path, repo, "old", days_ago=45)
    before = snapshot_tree(repo)
    for args in ([], ["--dry-run"], ["--compact"]):
        r = run_3p(script_path, repo, "prune", *args)
        assert r.returncode == 0, r.stderr
        assert f"would delete {old}" in r.stdout
        assert "no changes made" in r.stdout
        assert snapshot_tree(repo) == before


def test_apply_deletes_old_finished_run_and_its_refs(script_path, tmp_git_repo):
    repo = repo_with_commit(tmp_git_repo)
    old = make_run(script_path, repo, "old", days_ago=45)
    assert refs_for(repo, old)
    r = run_3p(script_path, repo, "prune", "--apply")
    assert r.returncode == 0, r.stderr
    assert f"deleted {old}" in r.stdout
    assert not (repo / ".3p" / old).exists()
    assert refs_for(repo, old) == []


def test_young_run_kept_and_compacted_only_on_request(script_path, tmp_git_repo):
    repo = repo_with_commit(tmp_git_repo)
    young = make_run(script_path, repo, "young", days_ago=3)
    write(repo / ".3p" / young / "scratch/node_modules/a.js")
    r = run_3p(script_path, repo, "prune", "--apply")
    assert young not in r.stdout
    assert (repo / ".3p" / young / "scratch/node_modules/a.js").exists()
    r = run_3p(script_path, repo, "prune", "--compact")
    assert f"would compact {young}" in r.stdout
    assert (repo / ".3p" / young / "scratch/node_modules/a.js").exists()
    r = run_3p(script_path, repo, "prune", "--apply", "--compact")
    assert f"compacted {young}" in r.stdout
    assert (repo / ".3p" / young).exists()
    assert not (repo / ".3p" / young / "scratch/node_modules").exists()


def test_protected_folders_are_kept_with_reasons(script_path, tmp_git_repo):
    repo = repo_with_commit(tmp_git_repo)
    unfinished = make_run(script_path, repo, "unfinished", phase="build", days_ago=90)
    kept = make_run(script_path, repo, "pinned", days_ago=90)
    write(repo / ".3p" / kept / ".keep", "")
    linked = make_run(script_path, repo, "linked", days_ago=90)
    write(repo / "docs/notes.md", f"see .3p/{linked}/summary.md\n")
    git(repo, "add", "docs/notes.md")
    git(repo, "commit", "-q", "-m", "doc")
    write(repo / ".3p/review-artifacts/old.md", "agent scratch\n")
    r = run_3p(script_path, repo, "prune", "--apply", "--days", "1")
    assert r.returncode == 0, r.stderr
    assert f"kept {unfinished}: unfinished ('build')" in r.stdout.replace("phase ", "")
    assert f"kept {kept}: has a .keep file" in r.stdout
    assert f"kept {linked}: linked from tracked file(s): docs/notes.md" in r.stdout
    assert "kept review-artifacts: not a 3p run" in r.stdout
    for name in (unfinished, kept, linked, "review-artifacts"):
        assert (repo / ".3p" / name).exists(), name
    assert refs_for(repo, unfinished) and refs_for(repo, kept) and refs_for(repo, linked)


def test_retention_zero_disables(script_path, tmp_git_repo):
    repo = repo_with_commit(tmp_git_repo)
    old = make_run(script_path, repo, "old", days_ago=400)
    write(repo / ".3p/config.json", json.dumps({"retention": {"days": 0}}))
    r = run_3p(script_path, repo, "prune", "--apply")
    assert r.returncode == 0 and "disabled" in r.stdout
    assert (repo / ".3p" / old).exists()
    assert run_3p(script_path, repo, "init", "new", "20260601-0000").returncode == 0
    assert (repo / ".3p" / old).exists()


def test_invalid_retention_is_rejected(script_path, tmp_git_repo):
    repo = repo_with_commit(tmp_git_repo)
    for bad in ({"days": -1}, {"days": "30"}, {"days": True}, 30):
        write(repo / ".3p/config.json", json.dumps({"retention": bad}))
        r = run_3p(script_path, repo, "config-load")
        assert r.returncode != 0
        assert "retention" in (r.stderr + r.stdout)


def test_init_auto_prunes_quietly(script_path, tmp_git_repo):
    repo = repo_with_commit(tmp_git_repo)
    old = make_run(script_path, repo, "old", days_ago=45)
    young = make_run(script_path, repo, "young", days_ago=2)
    write(repo / ".3p" / young / "scratch/node_modules/a.js")
    r = run_3p(script_path, repo, "init", "new", "20260601-0000")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "new-20260601-0000\n"
    assert f"deleted {old}" in r.stderr
    assert not (repo / ".3p" / old).exists()
    assert refs_for(repo, old) == []
    # Auto-prune never compacts other runs.
    assert (repo / ".3p" / young / "scratch/node_modules/a.js").exists()
    assert (repo / ".3p/new-20260601-0000/state.json").exists()


def test_init_survives_a_failing_prune(script_path, tmp_git_repo, monkeypatch, capsys):
    import importlib.util
    repo = repo_with_commit(tmp_git_repo)
    spec = importlib.util.spec_from_file_location("threep_under_test", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.chdir(repo)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(mod, "prune_runs", boom)
    assert mod.cmd_init(["new", "20260601-0000"]) == 0
    out = capsys.readouterr()
    assert out.out == "new-20260601-0000\n"
    assert "auto-prune skipped: disk on fire" in out.err


def test_non_git_anchor_prunes_without_ref_checks(script_path, tmp_non_git):
    run_dir = tmp_non_git / ".3p" / "old-20260101-0000"
    assert run_3p(script_path, tmp_non_git, "init", "old", "20260101-0000",
                  "--mode", "think").returncode == 0
    st = json.loads((run_dir / "state.json").read_text())
    st["phase"] = "done"
    st["timeline"].append({"ts": "2025-01-01T00:00:00+00:00", "kind": "phase", "label": "done"})
    (run_dir / "state.json").write_text(json.dumps(st))
    r = run_3p(script_path, tmp_non_git, "prune", "--apply")
    assert r.returncode == 0, r.stderr
    assert not run_dir.exists()


def test_prune_waits_for_lock_and_rechecks_eligibility(script_path, tmp_git_repo):
    """A run that leaves `done` while prune waits on its lifecycle lock is kept."""
    import importlib.util
    import os
    import time
    repo = repo_with_commit(tmp_git_repo)
    old = make_run(script_path, repo, "old", days_ago=45)
    spec = importlib.util.spec_from_file_location("threep_under_test", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    run_dir = repo / ".3p" / old
    with mod.run_lifecycle_lock(run_dir):
        proc = subprocess.Popen([sys.executable, str(script_path), "prune", "--apply"],
                                cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=os.environ.copy())
        time.sleep(1.0)
        assert proc.poll() is None, "prune should wait for the run's lock"
        st = json.loads((run_dir / "state.json").read_text())
        st["phase"] = "build"
        (run_dir / "state.json").write_text(json.dumps(st))
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    assert f"kept {old}: unfinished" in out
    assert (run_dir / "state.json").exists() and refs_for(repo, old)


def test_invalid_state_is_kept_and_does_not_stop_the_scan(script_path, tmp_git_repo):
    import os
    repo = repo_with_commit(tmp_git_repo)
    fake = repo / ".3p/fake-20250101-0000"
    write(fake / "state.json", json.dumps({"phase": "done"}))
    write(fake / "precious.txt", "not 3p data\n")
    os.utime(fake / "state.json", (0, 0))
    listy = repo / ".3p/listy-20250101-0000"
    write(listy / "state.json", "[]")
    old = make_run(script_path, repo, "old", days_ago=45)
    r = run_3p(script_path, repo, "prune", "--apply")
    assert r.returncode == 0, r.stderr
    assert "kept fake-20250101-0000: invalid state.json" in r.stdout
    assert "kept listy-20250101-0000: invalid state.json" in r.stdout
    assert (fake / "precious.txt").exists() and listy.exists()
    assert not (repo / ".3p" / old).exists()


def test_links_are_found_despite_color_config_and_in_gitignore(script_path, tmp_git_repo):
    repo = repo_with_commit(tmp_git_repo)
    in_readme = make_run(script_path, repo, "readme", days_ago=45)
    in_ignore = make_run(script_path, repo, "ignore", days_ago=45)
    write(repo / "README.md", f"evidence: .3p/{in_readme}/summary.md\n")
    gi = (repo / ".gitignore").read_text()
    write(repo / ".gitignore", gi + f"# kept for audit: .3p/{in_ignore}\n")
    git(repo, "add", "README.md", ".gitignore")
    git(repo, "commit", "-q", "-m", "links")
    git(repo, "config", "color.grep", "always")
    git(repo, "config", "color.ui", "always")
    r = run_3p(script_path, repo, "prune", "--apply")
    assert r.returncode == 0, r.stderr
    assert f"kept {in_readme}: linked from tracked file(s): README.md" in r.stdout
    assert f"kept {in_ignore}: linked from tracked file(s): .gitignore" in r.stdout
    assert (repo / ".3p" / in_readme).exists() and (repo / ".3p" / in_ignore).exists()


def test_failed_ref_deletion_keeps_the_folder_for_a_retry(script_path, tmp_git_repo):
    repo = repo_with_commit(tmp_git_repo)
    old = make_run(script_path, repo, "old", days_ago=45)
    lock = repo / ".git" / "refs" / "3p" / old / "pre-build.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("stale\n")
    try:
        r = run_3p(script_path, repo, "prune", "--apply")
        assert r.returncode == 0, r.stderr
        assert f"kept {old}: could not delete git ref(s)" in r.stdout
        assert (repo / ".3p" / old / "state.json").exists()
    finally:
        lock.unlink()
    r = run_3p(script_path, repo, "prune", "--apply")
    assert f"deleted {old}" in r.stdout
    assert not (repo / ".3p" / old).exists() and refs_for(repo, old) == []


def test_skips_are_reported_including_at_init(script_path, tmp_git_repo):
    repo = repo_with_commit(tmp_git_repo)
    pinned = make_run(script_path, repo, "pinned", days_ago=90)
    write(repo / ".3p" / pinned / ".keep", "")
    write(repo / ".3p/wha8-final-diff.txt", "loose agent file\n")
    r = run_3p(script_path, repo, "prune")
    assert "kept wha8-final-diff.txt: loose file" in r.stdout
    assert "config.json" not in r.stdout
    r = run_3p(script_path, repo, "init", "new", "20260601-0000")
    assert r.stdout == "new-20260601-0000\n"
    assert "3p auto-prune: kept 2 item(s)" in r.stderr
    assert "1 .keep" in r.stderr and "1 not a run" in r.stderr


def test_links_found_with_line_number_and_column_config(script_path, tmp_git_repo):
    repo = repo_with_commit(tmp_git_repo)
    linked = make_run(script_path, repo, "linked", days_ago=45)
    write(repo / "README.md", f"evidence: .3p/{linked}/summary.md\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "link")
    git(repo, "config", "grep.lineNumber", "true")
    git(repo, "config", "grep.column", "true")
    r = run_3p(script_path, repo, "prune", "--apply")
    assert f"kept {linked}: linked from tracked file(s): README.md" in r.stdout
    assert (repo / ".3p" / linked).exists()


def test_partial_deletion_keeps_state_so_prune_retries(script_path, tmp_git_repo):
    repo = repo_with_commit(tmp_git_repo)
    old = make_run(script_path, repo, "old", days_ago=45)
    stuck = repo / ".3p" / old / "evidence"
    write(stuck / "inner/file.txt")
    (stuck / "inner").chmod(0o500)
    try:
        r = run_3p(script_path, repo, "prune", "--apply")
        assert f"kept {old}: skipped after error" in r.stdout
        assert (repo / ".3p" / old / "state.json").exists()
    finally:
        (stuck / "inner").chmod(0o700)
    r = run_3p(script_path, repo, "prune", "--apply")
    assert f"deleted {old}" in r.stdout
    assert not (repo / ".3p" / old).exists()


def test_failed_ref_listing_keeps_the_run(script_path, tmp_git_repo):
    repo = repo_with_commit(tmp_git_repo)
    old = make_run(script_path, repo, "old", days_ago=45)
    packed = repo / ".git" / "packed-refs"
    git(repo, "pack-refs", "--all")
    original = packed.read_text()
    packed.write_text(original + "this is not a valid packed ref line\n")
    try:
        r = run_3p(script_path, repo, "prune", "--apply")
        assert f"kept {old}: could not delete git ref(s)" in r.stdout
        assert "listing failed" in r.stdout
        assert (repo / ".3p" / old / "state.json").exists()
    finally:
        packed.write_text(original)
    assert refs_for(repo, old)


def test_unreadable_tracked_file_blocks_pruning(script_path, tmp_git_repo):
    repo = repo_with_commit(tmp_git_repo)
    linked = make_run(script_path, repo, "linked", days_ago=45)
    write(repo / "README.md", f"evidence: .3p/{linked}/summary.md\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "link")
    (repo / "README.md").chmod(0o000)
    try:
        r = run_3p(script_path, repo, "prune", "--apply")
        assert r.returncode != 0
        assert "could not scan tracked files" in r.stderr
        r = run_3p(script_path, repo, "init", "new", "20260601-0000")
        assert r.returncode == 0 and r.stdout == "new-20260601-0000\n"
        assert "auto-prune skipped: could not scan tracked files" in r.stderr
    finally:
        (repo / "README.md").chmod(0o644)
    assert (repo / ".3p" / linked).exists() and refs_for(repo, linked)


def test_interrupted_prune_leaves_baselines_unusable(script_path, tmp_git_repo):
    repo = repo_with_commit(tmp_git_repo)
    write(repo / "src/app.py", "v1\n")
    git(repo, "add", "src/app.py")
    git(repo, "commit", "-q", "-m", "app")
    old = make_run(script_path, repo, "old", days_ago=45)
    run_dir = repo / ".3p" / old
    st = json.loads((run_dir / "state.json").read_text())
    st["phase"] = "build"
    (run_dir / "state.json").write_text(json.dumps(st))
    assert run_3p(script_path, repo, "snapshot", "capture", old, "pre-build").returncode == 0
    st = json.loads((run_dir / "state.json").read_text())
    st["phase"] = "done"
    (run_dir / "state.json").write_text(json.dumps(st))
    stuck = run_dir / "baselines/pre-build/src"
    stuck.chmod(0o500)
    try:
        r = run_3p(script_path, repo, "prune", "--apply")
        assert f"kept {old}: skipped after error" in r.stdout
        st = json.loads((run_dir / "state.json").read_text())
        assert st["baselines"]["pre-build"]["compacted"] is True
        d = run_3p(script_path, repo, "snapshot", "diff", old, "pre-build")
        assert d.returncode == 2 and d.stdout == ""
    finally:
        stuck.chmod(0o700)
    assert f"deleted {old}" in run_3p(script_path, repo, "prune", "--apply").stdout


def test_interrupted_prune_of_legacy_run_is_retried(script_path, tmp_git_repo):
    """A run with no `done` timeline event is dated by state.json's mtime;
    marking it for deletion must not make it look newly finished."""
    import os
    repo = repo_with_commit(tmp_git_repo)
    old = make_run(script_path, repo, "legacy", days_ago=0)
    run_dir = repo / ".3p" / old
    st = json.loads((run_dir / "state.json").read_text())
    st["timeline"] = [e for e in st["timeline"] if e.get("label") != "done"]
    (run_dir / "state.json").write_text(json.dumps(st))
    os.utime(run_dir / "state.json", (0, 0))
    stuck = run_dir / "evidence/inner"
    write(stuck / "f.txt")
    stuck.chmod(0o500)
    try:
        assert "skipped after error" in run_3p(script_path, repo, "prune", "--apply").stdout
    finally:
        stuck.chmod(0o700)
    assert f"deleted {old}" in run_3p(script_path, repo, "prune", "--apply").stdout
    assert not run_dir.exists()


def test_clean_still_removes_a_run_with_corrupt_state(script_path, tmp_git_repo):
    repo = repo_with_commit(tmp_git_repo)
    run_id = make_run(script_path, repo, "broken", days_ago=1)
    (repo / ".3p" / run_id / "state.json").write_text('{"truncated": ')
    r = run_3p(script_path, repo, "clean", run_id)
    assert r.returncode == 0, r.stderr
    assert not (repo / ".3p" / run_id).exists() and refs_for(repo, run_id) == []

"""Step 2 — dashboard subcommand."""
import json
import subprocess
import sys
from pathlib import Path


def run_3p(script_path, cwd, *args):
    return subprocess.run([sys.executable, str(script_path), *args],
                          capture_output=True, text=True, cwd=cwd)


RUN = "x-20260603-1430"


def _dash(repo):
    return (repo / ".3p" / RUN / "dashboard.md").read_text()


def _state_write(script_path, repo, key, val):
    run_3p(script_path, repo, "state-write", RUN, key, json.dumps(val))


def _finding(title, location, reviewer, verdict="accepted"):
    return {"reviewer": reviewer, "status": "findings", "durationSeconds": 40,
            "findings": [{"severity": "Important", "title": title, "location": location,
                          "issue": "i", "rationale": "r", "verdict": verdict,
                          "verdictReason": "vr"}], "rebuttals": []}


def test_dashboard_fresh_run_renders_placeholders(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    _state_write(script_path, tmp_git_repo, "northStar", "Ship the thing")
    r = run_3p(script_path, tmp_git_repo, "dashboard", RUN)
    assert r.returncode == 0, r.stderr
    md = _dash(tmp_git_repo)
    assert RUN in md
    assert "Ship the thing" in md
    assert "Alignment:" in md
    assert "Codex" in md and "Antigravity" in md          # scoreboard rows
    assert "None open in the current scope" in md


def test_dashboard_shows_open_findings_and_is_idempotent(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    _state_write(script_path, tmp_git_repo, "phase", "build")
    _state_write(script_path, tmp_git_repo, "currentScope", "step-1")
    _state_write(script_path, tmp_git_repo, "currentRound", 1)
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "1", "codex",
           json.dumps(_finding("Null deref", "a.py:10", "codex")))
    run_3p(script_path, tmp_git_repo, "dashboard", RUN)
    md1 = _dash(tmp_git_repo)
    assert "F-01" in md1
    assert "Null deref" in md1
    # idempotent: regenerating yields identical output
    run_3p(script_path, tmp_git_repo, "dashboard", RUN)
    assert _dash(tmp_git_repo) == md1


def test_dashboard_detects_cross_reviewer_agreement(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    _state_write(script_path, tmp_git_repo, "phase", "build")
    _state_write(script_path, tmp_git_repo, "currentScope", "step-1")
    _state_write(script_path, tmp_git_repo, "currentRound", 1)
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "1", "codex",
           json.dumps(_finding("Race A", "lock.py:5", "codex")))
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "1", "antigravity",
           json.dumps(_finding("Race condition", "lock.py:5", "antigravity")))
    run_3p(script_path, tmp_git_repo, "dashboard", RUN)
    md = _dash(tmp_git_repo)
    assert "Both flagged" in md
    assert "lock.py:5" in md


def test_dashboard_closed_finding_not_open_after_later_responded_round(script_path, tmp_git_repo):
    """A finding raised R1 is closed once the same reviewer responds in R2."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    _state_write(script_path, tmp_git_repo, "phase", "build")
    _state_write(script_path, tmp_git_repo, "currentScope", "step-1")
    _state_write(script_path, tmp_git_repo, "currentRound", 2)
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "1", "codex",
           json.dumps(_finding("Transient", "x.py:1", "codex")))
    # codex responded again in round 2 with no findings -> R1 finding now closed
    run_3p(script_path, tmp_git_repo, "availability-append", RUN,
           json.dumps({"phase": "build", "step": "1", "round": 2, "reviewer": "codex",
                       "status": "responded", "durationSeconds": 5}))
    run_3p(script_path, tmp_git_repo, "dashboard", RUN)
    md = _dash(tmp_git_repo)
    assert "None open in the current scope" in md


def test_dashboard_scope_fallback_when_currentscope_unset(script_path, tmp_git_repo):
    """No currentScope (plan phase / legacy) must not render 'Scope None'."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "dashboard", RUN)
    md = _dash(tmp_git_repo)
    assert "Scope None" not in md
    assert "Scope `plan`" in md


def test_dashboard_coverage_gap_vs_conflict(script_path, tmp_git_repo):
    """One reviewer open + the other never responded in scope => coverage gap,
    NOT a conflict (which would falsely imply a clean opposing review)."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    _state_write(script_path, tmp_git_repo, "phase", "build")
    _state_write(script_path, tmp_git_repo, "currentScope", "step-1")
    _state_write(script_path, tmp_git_repo, "currentRound", 1)
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "1", "codex",
           json.dumps(_finding("Bug", "z.py:1", "codex")))
    run_3p(script_path, tmp_git_repo, "dashboard", RUN)
    md = _dash(tmp_git_repo)
    assert "Coverage gap" in md
    assert "Conflict" not in md
    # Now antigravity responds clean in the same scope -> genuine conflict.
    run_3p(script_path, tmp_git_repo, "availability-append", RUN,
           json.dumps({"phase": "build", "step": "1", "round": 1, "reviewer": "antigravity",
                       "status": "responded", "durationSeconds": 9}))
    run_3p(script_path, tmp_git_repo, "dashboard", RUN)
    md2 = _dash(tmp_git_repo)
    assert "Conflict" in md2


def test_dashboard_stale_approval_is_not_conflict(script_path, tmp_git_repo):
    """Reviewer approved R1, the other raises an open finding in R2 -> coverage
    gap (the R1 approval never saw the R2 revision), NOT a conflict."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    _state_write(script_path, tmp_git_repo, "phase", "build")
    _state_write(script_path, tmp_git_repo, "currentScope", "step-1")
    _state_write(script_path, tmp_git_repo, "currentRound", 2)
    # antigravity responded clean in round 1 only
    run_3p(script_path, tmp_git_repo, "availability-append", RUN,
           json.dumps({"phase": "build", "step": "1", "round": 1, "reviewer": "antigravity",
                       "status": "responded", "durationSeconds": 9}))
    # codex raises an open finding in round 2
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "2", "codex",
           json.dumps(_finding("New in r2", "y.py:3", "codex")))
    run_3p(script_path, tmp_git_repo, "dashboard", RUN)
    md = _dash(tmp_git_repo)
    assert "Coverage gap" in md
    assert "Conflict" not in md


def test_dashboard_includes_full_ledger_section(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    _state_write(script_path, tmp_git_repo, "currentScope", "step-1")
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "1", "codex",
           json.dumps(_finding("Ledgered", "q.py:2", "codex")))
    run_3p(script_path, tmp_git_repo, "dashboard", RUN)
    md = _dash(tmp_git_repo)
    assert "Findings ledger (all scopes)" in md
    assert "F-01" in md and "Ledgered" in md
    assert "Open?" in md          # ledger marks open/closed explicitly
    assert "open" in md           # this finding has no later responded round


def test_dashboard_stdout_echoes_full_markdown(script_path, tmp_git_repo):
    """--stdout prints the rendered markdown (so the skill can relay scoreboard +
    ledger into chat) AND still writes the file; the two must match."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    _state_write(script_path, tmp_git_repo, "currentScope", "step-1")
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "1", "codex",
           json.dumps(_finding("Echoed", "e.py:1", "codex")))
    r = run_3p(script_path, tmp_git_repo, "dashboard", RUN, "--stdout")
    assert r.returncode == 0, r.stderr
    assert "Findings ledger (all scopes)" in r.stdout
    assert "F-01" in r.stdout and "Echoed" in r.stdout
    assert r.stdout.rstrip("\n") == _dash(tmp_git_repo).rstrip("\n")   # stdout == file
    # default (no flag) still prints just the path, not the markdown
    r2 = run_3p(script_path, tmp_git_repo, "dashboard", RUN)
    assert "dashboard.md" in r2.stdout
    assert "Findings ledger" not in r2.stdout


def test_hud_renders_compact_block(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    _state_write(script_path, tmp_git_repo, "phase", "build")
    _state_write(script_path, tmp_git_repo, "currentScope", "step-1")
    _state_write(script_path, tmp_git_repo, "currentRound", 2)
    _state_write(script_path, tmp_git_repo, "currentStep", {"index": 1, "description": "d"})
    _state_write(script_path, tmp_git_repo, "alignment",
                 {"status": "green", "note": "ok", "checkedAtPhase": "build-step-1"})
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "2", "codex",
           json.dumps(_finding("Open one", "h.py:1", "codex")))
    r = run_3p(script_path, tmp_git_repo, "hud", RUN)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert lines[0].startswith("┌ /3p " + RUN)
    assert "Phase B step 1/" in lines[0]
    assert "Round 2/10" in lines[0]
    assert "Alignment 🟢" in lines[0]
    assert "Codex" in lines[1] and "Antigravity" in lines[1]
    assert "F-01 [Important] open" in lines[1]   # first open finding surfaced
    assert lines[2] == "└"


def test_hud_zero_open_findings(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    r = run_3p(script_path, tmp_git_repo, "hud", RUN)
    assert r.returncode == 0, r.stderr
    assert "open findings: 0" in r.stdout
    assert "Phase A Plan" in r.stdout       # plan-phase fallback label


def test_hud_latency_shows_zero_not_stale(script_path, tmp_git_repo):
    """A latest responded round with 0s latency (missing PAL metadata) must render
    '0s' — consistent with the scoreboard — not fall through to an older round's
    latency or '—'. Regression guard for the _last_latency truthiness bug."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    _state_write(script_path, tmp_git_repo, "phase", "build")
    _state_write(script_path, tmp_git_repo, "currentScope", "step-1")
    _state_write(script_path, tmp_git_repo, "currentRound", 2)
    # round 1: codex responded in 9s; round 2: codex responded with 0s (no metadata)
    run_3p(script_path, tmp_git_repo, "availability-append", RUN,
           json.dumps({"phase": "build", "step": "1", "round": 1, "reviewer": "codex",
                       "status": "responded", "durationSeconds": 9}))
    run_3p(script_path, tmp_git_repo, "availability-append", RUN,
           json.dumps({"phase": "build", "step": "1", "round": 2, "reviewer": "codex",
                       "status": "responded", "durationSeconds": 0}))
    r = run_3p(script_path, tmp_git_repo, "hud", RUN)
    assert r.returncode == 0, r.stderr
    assert "Codex 🟢 0s" in r.stdout       # latest=0s, not the stale 9s
    assert "Codex 🟢 9s" not in r.stdout


def test_dashboard_resilient_to_prelegacy_state(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    sp = tmp_git_repo / ".3p" / RUN / "state.json"
    state = json.loads(sp.read_text())
    for k in ("ledger", "northStar", "alignment"):
        state.pop(k, None)
    sp.write_text(json.dumps(state))
    r = run_3p(script_path, tmp_git_repo, "dashboard", RUN)
    assert r.returncode == 0, r.stderr
    assert "None open" in _dash(tmp_git_repo)

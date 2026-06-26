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

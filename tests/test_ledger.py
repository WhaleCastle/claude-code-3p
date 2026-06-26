"""Step 1 — ledger + timing data model.

Covers: new state keys at init, stable-id upsert + history across rounds,
per-reviewer separation, rebuttal/pushback recording, and resilience for runs
that predate the ledger keys.
"""
import json
import subprocess
import sys
from pathlib import Path


def run_3p(script_path, cwd, *args):
    return subprocess.run([sys.executable, str(script_path), *args],
                          capture_output=True, text=True, cwd=cwd)


RUN = "x-20260603-1430"


def _read_state(repo):
    return json.loads((repo / ".3p" / RUN / "state.json").read_text())


def _finding(title, location="state.py:10", severity="Important",
             verdict="accepted", reason="r"):
    return {"severity": severity, "title": title, "location": location,
            "issue": "i", "rationale": "rat", "verdict": verdict,
            "verdictReason": reason}


def _verdicts(reviewer, findings, rebuttals=None, duration=42):
    return {"reviewer": reviewer, "status": "findings" if findings else "approved",
            "durationSeconds": duration, "findings": findings,
            "rebuttals": rebuttals or []}


def test_init_writes_new_state_keys(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    state = _read_state(tmp_git_repo)
    assert state["northStar"] is None
    assert state["alignment"] == {"status": "unknown", "note": "", "checkedAtPhase": None}
    assert state["ledger"] == {"nextId": 1, "findings": [], "rebuttals": []}


def test_reraise_same_finding_collapses_to_one_entry_with_history(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    # Round 1: rejected. Round 2: same finding (reworded markdown) now accepted.
    v1 = _verdicts("codex", [_finding("Race in state lock", verdict="rejected", reason="single-writer")])
    v2 = _verdicts("codex", [_finding("**Race** in `state lock`", verdict="accepted", reason="fixing")])
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "1", "codex", json.dumps(v1))
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "2", "codex", json.dumps(v2))
    findings = _read_state(tmp_git_repo)["ledger"]["findings"]
    assert len(findings) == 1, findings
    e = findings[0]
    assert e["id"] == "F-01"
    assert e["scope"] == "step-1"
    assert e["lastRound"] == 2
    assert e["firstRound"] == 1
    assert e["status"] == "accepted"          # latest verdict wins
    assert len(e["history"]) == 2
    assert [h["verdict"] for h in e["history"]] == ["rejected", "accepted"]


def test_different_reviewer_same_location_gets_separate_id(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    vc = _verdicts("codex", [_finding("Race in state lock")])
    va = _verdicts("antigravity", [_finding("Race in state lock")])
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "1", "codex", json.dumps(vc))
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "1", "antigravity", json.dumps(va))
    findings = _read_state(tmp_git_repo)["ledger"]["findings"]
    assert {f["id"] for f in findings} == {"F-01", "F-02"}
    assert {f["reviewer"] for f in findings} == {"codex", "antigravity"}


def test_rebuttal_outcome_recorded(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    v = {"reviewer": "antigravity", "status": "findings", "durationSeconds": 30,
         "findings": [_finding("Missing wiring")],
         "rebuttals": [{"originalRound": 1, "originalTitle": "Auth bypass",
                        "claudeReasonPrior": "constrained", "reviewerPushback": "no",
                        "claudeReasonNow": "valid", "outcome": "now-accepted"}]}
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "2", "2", "antigravity", json.dumps(v))
    rebuttals = _read_state(tmp_git_repo)["ledger"]["rebuttals"]
    assert len(rebuttals) == 1
    assert rebuttals[0]["outcome"] == "now-accepted"
    assert rebuttals[0]["reviewer"] == "antigravity"
    assert rebuttals[0]["scope"] == "step-2"


def test_approved_round_is_noop_for_ledger(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    v = {"reviewer": "codex", "status": "approved", "findings": [], "rebuttals": []}
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "plan", "-", "2", "codex", json.dumps(v))
    assert _read_state(tmp_git_repo)["ledger"]["findings"] == []


def test_round_write_is_idempotent_per_round(script_path, tmp_git_repo):
    """Re-running round-write for the same round must not duplicate history or
    rebuttals (retry/resume safety) — both reviewers flagged this in step-1 R1."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    v = {"reviewer": "codex", "status": "findings", "durationSeconds": 10,
         "findings": [_finding("Race in state lock", verdict="accepted")],
         "rebuttals": [{"originalRound": 1, "originalTitle": "old", "outcome": "sustained"}]}
    for _ in range(3):  # same round written 3x
        run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "1", "codex", json.dumps(v))
    ledger = _read_state(tmp_git_repo)["ledger"]
    assert len(ledger["findings"]) == 1
    assert len(ledger["findings"][0]["history"]) == 1   # not 3
    assert len(ledger["rebuttals"]) == 1                 # not 3


def test_round_rewrite_drops_phantom_finding(script_path, tmp_git_repo):
    """Re-writing the SAME round with a finding dropped (a correction) must remove
    the phantom; a finding that only existed in that round disappears entirely."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    two = {"reviewer": "codex", "status": "findings", "durationSeconds": 5,
           "findings": [_finding("Bug A", "a.py:1"), _finding("Bug B", "b.py:2")]}
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "1", "codex", json.dumps(two))
    assert len(_read_state(tmp_git_repo)["ledger"]["findings"]) == 2
    # corrected re-write of the same round with only Bug A
    one = {"reviewer": "codex", "status": "findings", "durationSeconds": 5,
           "findings": [_finding("Bug A", "a.py:1")]}
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "1", "codex", json.dumps(one))
    findings = _read_state(tmp_git_repo)["ledger"]["findings"]
    assert [f["title"] for f in findings] == ["Bug A"]   # Bug B phantom removed


def test_round_rewrite_keeps_earlier_round_history(script_path, tmp_git_repo):
    """A finding raised across rounds 1 and 2, then round 2 re-written empty, must
    keep its round-1 history (lastRound rolls back to 1), not vanish."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    v1 = {"reviewer": "codex", "status": "findings", "durationSeconds": 5,
          "findings": [_finding("Bug A", "a.py:1", verdict="rejected")]}
    v2 = {"reviewer": "codex", "status": "findings", "durationSeconds": 5,
          "findings": [_finding("Bug A", "a.py:1", verdict="accepted")]}
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "1", "codex", json.dumps(v1))
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "2", "codex", json.dumps(v2))
    # round 2 re-written as approved/empty (correction): drop round-2 contribution
    empty = {"reviewer": "codex", "status": "approved", "findings": []}
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "2", "codex", json.dumps(empty))
    findings = _read_state(tmp_git_repo)["ledger"]["findings"]
    assert len(findings) == 1
    e = findings[0]
    assert e["lastRound"] == 1
    assert [h["round"] for h in e["history"]] == [1]
    assert e["status"] == "rejected"   # recomputed from remaining history


def test_ledger_resilient_to_prelegacy_state(script_path, tmp_git_repo):
    """A run whose state.json predates the ledger key must not crash round-write."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    sp = tmp_git_repo / ".3p" / RUN / "state.json"
    state = json.loads(sp.read_text())
    del state["ledger"]
    sp.write_text(json.dumps(state))
    v = _verdicts("codex", [_finding("New issue")])
    r = run_3p(script_path, tmp_git_repo, "round-write", RUN, "plan", "-", "1", "codex", json.dumps(v))
    assert r.returncode == 0, r.stderr
    assert _read_state(tmp_git_repo)["ledger"]["findings"][0]["id"] == "F-01"

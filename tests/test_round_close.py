"""round-close + phase-end — render the close-of-round block and the full
dashboard as a side effect of mandatory commands, so the scoreboard/ledger/HUD
reach chat even on a zero-finding happy path (the case that previously produced
no visible output at all)."""
import json
import subprocess
import sys


def run_3p(script_path, cwd, *args):
    return subprocess.run([sys.executable, str(script_path), *args],
                          capture_output=True, text=True, cwd=cwd)


RUN = "x-20260603-1430"


def _sw(script_path, repo, key, val):
    run_3p(script_path, repo, "state-write", RUN, key, json.dumps(val))


def _finding(title, location, reviewer, verdict="accepted", reason="vr"):
    return {"reviewer": reviewer, "status": "findings", "durationSeconds": 40,
            "findings": [{"severity": "Important", "title": title, "location": location,
                          "issue": "i", "rationale": "r", "verdict": verdict,
                          "verdictReason": reason}], "rebuttals": []}


def _approved(reviewer, dur=12):
    return {"reviewer": reviewer, "status": "approved", "durationSeconds": dur, "findings": []}


def _avail(script_path, repo, rnd, reviewer, status="responded", reason=None, dur=12, phase="plan", step="-"):
    entry = {"phase": phase, "step": step, "round": rnd, "reviewer": reviewer,
             "status": status, "durationSeconds": dur}
    if reason:
        entry["reason"] = reason
    run_3p(script_path, repo, "availability-append", RUN, json.dumps(entry))


def _plan_round1_clean(script_path, repo):
    run_3p(script_path, repo, "init", "x", "20260603-1430")
    _sw(script_path, repo, "northStar", "Fix the blank page")
    _sw(script_path, repo, "currentScope", "plan")
    _sw(script_path, repo, "currentRound", 1)
    run_3p(script_path, repo, "round-write", RUN, "plan", "-", "1", "codex", json.dumps(_approved("codex")))
    run_3p(script_path, repo, "round-write", RUN, "plan", "-", "1", "antigravity", json.dumps(_approved("antigravity", 22)))
    _avail(script_path, repo, 1, "codex", dur=12)
    _avail(script_path, repo, 1, "antigravity", dur=22)


def test_round_close_zero_findings_still_renders(script_path, tmp_git_repo):
    """The exact failed-run shape: plan, round 1, both APPROVED, no findings.
    round-close must STILL emit the approval lines, the recap, and the HUD box."""
    _plan_round1_clean(script_path, tmp_git_repo)
    r = run_3p(script_path, tmp_git_repo, "round-close", RUN)
    assert r.returncode == 0, r.stderr
    assert "Codex ✓ APPROVED" in r.stdout
    assert "Antigravity ✓ APPROVED" in r.stdout
    assert "Round 1: Codex 0/0/0 · Antigravity 0/0/0" in r.stdout
    assert "┌ /3p " + RUN in r.stdout          # close-of-round HUD box
    assert "open findings: 0" in r.stdout
    # side effect: the persistent dashboard file is refreshed
    assert (tmp_git_repo / ".3p" / RUN / "dashboard.md").exists()


def test_round_close_renders_per_finding_lines(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    _sw(script_path, tmp_git_repo, "phase", "build")
    _sw(script_path, tmp_git_repo, "currentScope", "step-1")
    _sw(script_path, tmp_git_repo, "currentRound", 1)
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "1", "codex",
           json.dumps(_finding("Null deref", "a.py:10", "codex", "accepted", "real bug, fixing")))
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "build", "1", "1", "antigravity",
           json.dumps(_finding("Style nit", "b.py:2", "antigravity", "ignored", "out of scope")))
    _avail(script_path, tmp_git_repo, 1, "codex", phase="build", step="1")
    _avail(script_path, tmp_git_repo, 1, "antigravity", phase="build", step="1")
    r = run_3p(script_path, tmp_git_repo, "round-close", RUN)
    assert r.returncode == 0, r.stderr
    assert "Codex [Important] Null deref → accepted: real bug, fixing" in r.stdout
    assert "Antigravity [Important] Style nit → ignored: out of scope" in r.stdout
    assert "Round 1: Codex 1/0/0 · Antigravity 0/0/1" in r.stdout


def test_round_close_marks_unavailable_reviewer(script_path, tmp_git_repo):
    """A reviewer that timed out this round renders as ⚠ unavailable, not APPROVED."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    _sw(script_path, tmp_git_repo, "currentScope", "plan")
    _sw(script_path, tmp_git_repo, "currentRound", 1)
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "plan", "-", "1", "codex", json.dumps(_approved("codex")))
    _avail(script_path, tmp_git_repo, 1, "codex")
    _avail(script_path, tmp_git_repo, 1, "antigravity", status="unavailable", reason="timeout")
    r = run_3p(script_path, tmp_git_repo, "round-close", RUN)
    assert r.returncode == 0, r.stderr
    assert "Codex ✓ APPROVED" in r.stdout
    assert "Antigravity ⚠ unavailable (timeout)" in r.stdout
    assert "Antigravity ✓ APPROVED" not in r.stdout


def test_round_close_missing_availability_is_not_approved(script_path, tmp_git_repo):
    """A reviewer with no findings AND no availabilityLog 'responded' record must
    NOT be rendered as ✓ APPROVED — a dropped availability-append would otherwise
    print a false approval and hide the adherence failure this layer surfaces."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    _sw(script_path, tmp_git_repo, "currentScope", "plan")
    _sw(script_path, tmp_git_repo, "currentRound", 1)
    # codex responded clean (has a record); antigravity has NO availability record
    run_3p(script_path, tmp_git_repo, "round-write", RUN, "plan", "-", "1", "codex", json.dumps(_approved("codex")))
    _avail(script_path, tmp_git_repo, 1, "codex")
    r = run_3p(script_path, tmp_git_repo, "round-close", RUN)
    assert r.returncode == 0, r.stderr
    assert "Codex ✓ APPROVED" in r.stdout                       # has a responded record
    assert "Antigravity ✓ APPROVED" not in r.stdout             # NO record -> not approved
    assert "Antigravity ⚠ no availability record" in r.stdout


def test_phase_end_matches_dashboard_and_writes_file(script_path, tmp_git_repo):
    """phase-end prints the same markdown dashboard --stdout would, and refreshes
    the file — it is the mandatory pre-stop / phase-boundary render."""
    _plan_round1_clean(script_path, tmp_git_repo)
    r = run_3p(script_path, tmp_git_repo, "phase-end", RUN)
    assert r.returncode == 0, r.stderr
    assert "# /3p Dashboard" in r.stdout
    assert "Findings ledger (all scopes)" in r.stdout
    assert "Fix the blank page" in r.stdout            # north-star goal
    # identical to the file it just wrote, and to dashboard --stdout
    dash = (tmp_git_repo / ".3p" / RUN / "dashboard.md").read_text()
    assert r.stdout.rstrip("\n") == dash.rstrip("\n")
    r2 = run_3p(script_path, tmp_git_repo, "dashboard", RUN, "--stdout")
    assert r2.stdout.rstrip("\n") == r.stdout.rstrip("\n")

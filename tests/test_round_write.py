import json
import subprocess
import sys
from pathlib import Path


def run_3p(script_path, cwd, *args):
    return subprocess.run([sys.executable, str(script_path), *args],
                          capture_output=True, text=True, cwd=cwd)


def test_round_write_plan(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    verdicts = {
        "reviewer": "codex",
        "status": "findings",
        "findings": [{
            "severity": "Important", "title": "T",
            "location": "L", "issue": "I", "rationale": "R",
            "verdict": "accepted", "verdictReason": "valid",
        }],
        "rebuttals": [],
        "durationSeconds": 47,
    }
    r = run_3p(script_path, tmp_git_repo, "round-write", "x-20260603-1430",
               "plan", "-", "1", "codex", json.dumps(verdicts))
    assert r.returncode == 0, r.stderr
    f = tmp_git_repo / ".3p" / "x-20260603-1430" / "plan-round-1-codex.md"
    assert f.exists()
    md = f.read_text()
    assert "codex" in md
    assert "Important" in md
    assert "accepted" in md


def test_round_write_step(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    verdicts = {"reviewer": "antigravity", "status": "approved", "findings": [],
                "rebuttals": [], "durationSeconds": 30}
    r = run_3p(script_path, tmp_git_repo, "round-write", "x-20260603-1430",
               "build", "2", "1", "antigravity", json.dumps(verdicts))
    assert r.returncode == 0
    f = tmp_git_repo / ".3p" / "x-20260603-1430" / "step-2-round-1-antigravity.md"
    assert f.exists()
    assert "APPROVED" in f.read_text()


def test_round_write_per_reviewer_files(script_path, tmp_git_repo):
    """Per-reviewer files: each call writes its own file, no merge, no race."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    v1 = {"reviewer": "codex", "status": "findings",
          "findings": [{"severity": "Critical", "title": "C1",
                        "location": "L", "issue": "I", "rationale": "R",
                        "verdict": "accepted", "verdictReason": "valid"}],
          "rebuttals": [], "durationSeconds": 47}
    v2 = {"reviewer": "antigravity", "status": "approved", "findings": [],
          "rebuttals": [], "durationSeconds": 30}
    run_3p(script_path, tmp_git_repo, "round-write", "x-20260603-1430",
           "plan", "-", "1", "codex", json.dumps(v1))
    run_3p(script_path, tmp_git_repo, "round-write", "x-20260603-1430",
           "plan", "-", "1", "antigravity", json.dumps(v2))
    run_dir = tmp_git_repo / ".3p" / "x-20260603-1430"
    codex_md = (run_dir / "plan-round-1-codex.md").read_text()
    antigravity_md = (run_dir / "plan-round-1-antigravity.md").read_text()
    assert "C1" in codex_md
    assert "APPROVED" in antigravity_md


def test_round_write_with_rebuttals(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    verdicts = {
        "reviewer": "codex",
        "status": "findings",
        "findings": [],
        "rebuttals": [{
            "originalRound": 1,
            "originalTitle": "X is unsafe",
            "claudeReasonPrior": "single-writer by design",
            "reviewerPushback": "consider concurrent reads",
            "claudeReasonNow": "reads are read-only, no risk",
            "outcome": "withdrawn"
        }],
        "durationSeconds": 47,
    }
    r = run_3p(script_path, tmp_git_repo, "round-write", "x-20260603-1430",
               "plan", "-", "2", "codex", json.dumps(verdicts))
    assert r.returncode == 0
    md = (tmp_git_repo / ".3p" / "x-20260603-1430" / "plan-round-2-codex.md").read_text()
    assert "Rebuttal" in md
    assert "X is unsafe" in md
    assert "withdrawn" in md


def test_round_write_injects_missing_reviewer_and_defaults(script_path, tmp_git_repo):
    """Omitting the reviewer key and the descriptive fields must not crash:
    reviewer is injected from the CLI arg, rationale/verdictReason default to ''."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    verdicts = {
        "status": "findings",
        "findings": [{"severity": "Important", "title": "T", "verdict": "accepted"}],
    }
    r = run_3p(script_path, tmp_git_repo, "round-write", "x-20260603-1430",
               "plan", "-", "1", "codex", json.dumps(verdicts))
    assert r.returncode == 0, r.stderr
    md = (tmp_git_repo / ".3p" / "x-20260603-1430" / "plan-round-1-codex.md").read_text()
    assert "codex" in md and "Important" in md


def test_round_write_missing_verdict_is_actionable_error(script_path, tmp_git_repo):
    """A missing required field yields a clear message naming it, not a traceback."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    verdicts = {"reviewer": "codex", "findings": [{"severity": "Important", "title": "T"}]}
    r = run_3p(script_path, tmp_git_repo, "round-write", "x-20260603-1430",
               "plan", "-", "1", "codex", json.dumps(verdicts))
    assert r.returncode != 0
    assert "missing required field 'verdict'" in r.stderr
    assert "Traceback" not in r.stderr


def test_round_write_reviewer_mismatch_errors(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    verdicts = {"reviewer": "antigravity", "status": "approved", "findings": []}
    r = run_3p(script_path, tmp_git_repo, "round-write", "x-20260603-1430",
               "plan", "-", "1", "codex", json.dumps(verdicts))
    assert r.returncode != 0
    assert "!= CLI arg 'codex'" in r.stderr


def test_round_write_falsey_nonlist_findings_errors(script_path, tmp_git_repo):
    """A malformed falsey non-list `findings` ({}) must error, not be coerced to
    an empty round — including when `status` is absent (must not infer approved)."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    for payload in ({"reviewer": "codex", "status": "findings", "findings": {}},
                    {"reviewer": "codex", "findings": {}}):
        r = run_3p(script_path, tmp_git_repo, "round-write", "x-20260603-1430",
                   "plan", "-", "1", "codex", json.dumps(payload))
        assert r.returncode != 0, f"should reject {payload}"
        assert "'findings' must be a JSON array" in r.stderr
        assert "Traceback" not in r.stderr


def test_round_write_rebuttal_missing_key_does_not_traceback(script_path, tmp_git_repo):
    """A hand-assembled rebuttal missing a descriptive field must default it (no
    crash); missing the required `outcome` must be an actionable error, not a
    raw KeyError from _append_rebuttals."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    # Missing descriptive field (originalTitle) -> defaulted, write succeeds.
    ok = {
        "reviewer": "codex", "status": "findings",
        "findings": [{"severity": "Important", "title": "T", "verdict": "rejected"}],
        "rebuttals": [{"originalRound": 1, "claudeReasonPrior": "a",
                       "reviewerPushback": "b", "claudeReasonNow": "c",
                       "outcome": "sustained"}],
    }
    r = run_3p(script_path, tmp_git_repo, "round-write", "x-20260603-1430",
               "plan", "-", "1", "codex", json.dumps(ok))
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
    md = (tmp_git_repo / ".3p" / "x-20260603-1430" / "plan-round-1-codex.md").read_text()
    assert "Rebuttal" in md and "sustained" in md
    # Missing required `outcome` -> actionable error, no traceback.
    bad = dict(ok)
    bad["rebuttals"] = [{"originalRound": 1, "originalTitle": "X"}]
    r = run_3p(script_path, tmp_git_repo, "round-write", "x-20260603-1430",
               "plan", "-", "2", "codex", json.dumps(bad))
    assert r.returncode != 0
    assert "rebuttals[0] missing required field 'outcome'" in r.stderr
    assert "Traceback" not in r.stderr


def test_round_write_invalid_status_rejected(script_path, tmp_git_repo):
    """A typo'd status must error loudly, not silently write an empty record."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    r = run_3p(script_path, tmp_git_repo, "round-write", "x-20260603-1430",
               "plan", "-", "1", "codex",
               json.dumps({"reviewer": "codex", "status": "approve", "findings": []}))
    assert r.returncode != 0
    assert "invalid status 'approve'" in r.stderr
    assert "Traceback" not in r.stderr


def test_round_write_approved_with_findings_rejected(script_path, tmp_git_repo):
    """Findings on an approved record would be silently dropped by the renderer;
    reject so real review data is never lost from the audit trail."""
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    payload = {"reviewer": "codex", "status": "approved",
               "findings": [{"severity": "Important", "title": "REAL BUG",
                             "verdict": "accepted"}]}
    r = run_3p(script_path, tmp_git_repo, "round-write", "x-20260603-1430",
               "plan", "-", "1", "codex", json.dumps(payload))
    assert r.returncode != 0
    assert "must not carry findings" in r.stderr
    assert "Traceback" not in r.stderr

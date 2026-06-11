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

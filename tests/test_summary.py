import json
import subprocess
import sys
from pathlib import Path


def run_3p(script_path, cwd, *args):
    return subprocess.run([sys.executable, str(script_path), *args],
                          capture_output=True, text=True, cwd=cwd)


def test_summary_contains_task_and_round_records(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_dir = tmp_git_repo / ".3p" / "x-20260603-1430"
    (run_dir / "task.txt").write_text("Add login form")
    (run_dir / "plan.md").write_text("# Plan\nstep 1\nstep 2\n")
    (run_dir / "plan-round-1-codex.md").write_text("# Plan round 1 (codex)\n\n**APPROVED**\n")
    (run_dir / "plan-round-1-gemini.md").write_text("# Plan round 1 (gemini)\n\n**APPROVED**\n")
    (run_dir / "step-1-summary.md").write_text("Step 1 done.")
    r = run_3p(script_path, tmp_git_repo, "summary", "x-20260603-1430")
    assert r.returncode == 0, r.stderr
    summary = (run_dir / "summary.md").read_text()
    assert "Add login form" in summary
    assert "plan-round-1-codex.md" in summary or "plan-round-1-gemini.md" in summary
    assert "Step 1" in summary
    assert "Uncommitted-state notice" in summary


def test_consolidate_final_produces_artifact(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_dir = tmp_git_repo / ".3p" / "x-20260603-1430"
    (run_dir / "final-round-1-codex.md").write_text("# Final round 1 (codex)\n\n**APPROVED**\n")
    (run_dir / "final-round-1-gemini.md").write_text("# Final round 1 (gemini)\n\n**APPROVED**\n")
    r = run_3p(script_path, tmp_git_repo, "consolidate-final", "x-20260603-1430")
    assert r.returncode == 0, r.stderr
    fr = (run_dir / "final-review.md").read_text()
    assert "Phase C" in fr
    assert "approved" in fr.lower()
    assert "final-round-1-codex.md" in fr
    assert "final-round-1-gemini.md" in fr

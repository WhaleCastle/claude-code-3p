"""Run timing — wall-clock is recorded on an append-only state.timeline (seeded
at init, auto-stamped on phase transitions, bracketed for tests via `mark`) and
rolled into the summary's `## Timing` table. cmd_summary reports total time and
time-per-part (Plan/Build/Final/Review/Test) so the final summary can surface it.
Duration math is unit-tested against synthetic timestamps; the CLI path is tested
for the plumbing (timeline seeded, phase transitions stamped once, marks land)."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def run_3p(script_path, cwd, *args):
    return subprocess.run([sys.executable, str(script_path), *args],
                          capture_output=True, text=True, cwd=cwd)


def _load_module(script_path):
    spec = importlib.util.spec_from_file_location("p3_timing", str(script_path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# --- pure duration math ----------------------------------------------------

SYNTHETIC = {
    "startedAt": "2026-07-04T10:00:00+00:00",
    "phase": "done",   # a completed run: end is taken from recorded events, not now
    "timeline": [
        {"ts": "2026-07-04T10:00:00+00:00", "kind": "run-start", "label": None},
        {"ts": "2026-07-04T10:00:00+00:00", "kind": "phase", "label": "plan"},
        {"ts": "2026-07-04T10:02:30+00:00", "kind": "phase", "label": "build"},
        {"ts": "2026-07-04T10:03:00+00:00", "kind": "test-start", "label": "step-1"},
        {"ts": "2026-07-04T10:03:45+00:00", "kind": "test-end", "label": "step-1"},
        {"ts": "2026-07-04T10:07:00+00:00", "kind": "phase", "label": "final"},
        {"ts": "2026-07-04T10:09:00+00:00", "kind": "phase", "label": "done"},
    ],
    "availabilityLog": [
        {"phase": "plan", "step": "-", "round": 1, "reviewer": "codex", "durationSeconds": 30},
        {"phase": "plan", "step": "-", "round": 1, "reviewer": "antigravity", "durationSeconds": 45},
        {"phase": "build", "step": 1, "round": 1, "reviewer": "codex", "durationSeconds": 20},
        {"phase": "build", "step": 1, "round": 1, "reviewer": "antigravity", "durationSeconds": 25},
    ],
}


def test_compute_timing_durations(script_path):
    m = _load_module(script_path)
    tm = m._compute_timing(SYNTHETIC)
    assert tm["total"] == 540                      # 10:00:00 -> 10:09:00
    assert tm["phases"]["plan"] == 150             # anchored at startedAt -> build
    assert tm["phases"]["build"] == 270
    assert tm["phases"]["final"] == 120
    assert "done" not in tm["phases"]              # terminal marker excluded
    assert tm["reviewWall"] == 70                  # parallel: max(30,45)+max(20,25)
    assert tm["reviewRaw"] == 120                  # 30+45+20+25
    assert tm["testTotal"] == 45
    assert tm["perTest"] == [("step-1", 45)]


def test_in_progress_run_extends_end_to_now(script_path):
    """A run not yet marked 'done' (early-stop, or summary generated before the
    done stamp) must NOT truncate the current phase at its start event — the end
    is extended to now, so the final phase and total reflect elapsed wall-clock."""
    m = _load_module(script_path)
    # final phase started but 'done' never stamped, and no event after it.
    state = {
        "startedAt": "2026-07-04T10:00:00+00:00",
        "phase": "final",
        "timeline": [
            {"ts": "2026-07-04T10:00:00+00:00", "kind": "phase", "label": "plan"},
            {"ts": "2026-07-04T10:05:00+00:00", "kind": "phase", "label": "final"},
        ],
    }
    tm = m._compute_timing(state)
    # 'final' would be 0s if truncated at its own start event; now-extension makes
    # it positive, and total exceeds the 5m of plan.
    assert tm["phases"]["final"] > 0
    assert tm["total"] > 300


def test_review_key_coercion_int_vs_str(script_path):
    """int vs str round/step across reviewers must still map to one logical round
    so the parallel max() holds instead of summing both durations."""
    m = _load_module(script_path)
    state = {
        "startedAt": "2026-07-04T10:00:00+00:00",
        "phase": "done",
        "timeline": [{"ts": "2026-07-04T10:00:00+00:00", "kind": "phase", "label": "final"}],
        "availabilityLog": [
            {"phase": "final", "step": "-", "round": 1, "reviewer": "codex", "durationSeconds": 30},
            {"phase": "final", "step": "-", "round": "1", "reviewer": "antigravity", "durationSeconds": 50},
        ],
    }
    tm = m._compute_timing(state)
    assert tm["reviewWall"] == 50   # max(30,50), NOT 80
    assert tm["reviewRaw"] == 80


def test_fmt_dur_shapes(script_path):
    m = _load_module(script_path)
    assert m._fmt_dur(None) == "—"
    assert m._fmt_dur(9) == "9s"
    assert m._fmt_dur(185) == "3m 05s"
    assert m._fmt_dur(3723) == "1h 02m 03s"
    assert m._fmt_dur(-5) == "0s"


def test_render_timing_table_legacy_state_degrades(script_path):
    m = _load_module(script_path)
    lines = "\n".join(m.render_timing_table({}))          # no startedAt/timeline
    assert "Total (run wall-clock)" in lines
    assert "—" in lines
    assert "predates the timing layer" in lines


# --- CLI plumbing ----------------------------------------------------------

RUN = "x-20260603-1430"


def _timeline(run_dir):
    return json.loads((run_dir / "state.json").read_text())["timeline"]


def test_init_seeds_started_at_and_timeline(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    state = json.loads((tmp_git_repo / ".3p" / RUN / "state.json").read_text())
    assert state["startedAt"]
    kinds = [(e["kind"], e["label"]) for e in state["timeline"]]
    assert kinds == [("run-start", None), ("phase", "plan")]


def test_phase_transitions_stamped_once_each(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_dir = tmp_git_repo / ".3p" / RUN
    # plan is already seeded by init, so re-writing it must NOT add a segment.
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "phase", '"plan"')
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "phase", '"build"')
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "phase", '"build"')   # duplicate
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "phase", '"final"')
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "phase", '"done"')
    phases = [e["label"] for e in _timeline(run_dir) if e["kind"] == "phase"]
    assert phases == ["plan", "build", "final", "done"]


def test_mark_appends_event(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_dir = tmp_git_repo / ".3p" / RUN
    run_3p(script_path, tmp_git_repo, "mark", RUN, "test-start", "step-1")
    run_3p(script_path, tmp_git_repo, "mark", RUN, "test-end", "step-1")
    events = [(e["kind"], e["label"]) for e in _timeline(run_dir)]
    assert ("test-start", "step-1") in events
    assert ("test-end", "step-1") in events


def test_summary_has_timing_section(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_dir = tmp_git_repo / ".3p" / RUN
    (run_dir / "task.txt").write_text("Do the thing")
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "phase", '"build"')
    r = run_3p(script_path, tmp_git_repo, "summary", RUN)
    assert r.returncode == 0, r.stderr
    summary = (run_dir / "summary.md").read_text()
    assert "## Timing" in summary
    assert "Total (run wall-clock)" in summary
    assert "Phase A · Plan" in summary
    assert "Review (reviewer wall-clock, parallel-adjusted)" in summary

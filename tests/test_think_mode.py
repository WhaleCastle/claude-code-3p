import json
import subprocess
import sys
from pathlib import Path


def run_3p(script_path, cwd, *args, env=None):
    return subprocess.run([sys.executable, str(script_path), *args],
                          capture_output=True, text=True, cwd=cwd, env=env)


RUN = "q-20261007-1200"


def init_think(script_path, cwd, *extra):
    r = run_3p(script_path, cwd, "init", "q", "20261007-1200", "--mode", "think", *extra)
    assert r.returncode == 0, r.stderr
    return cwd / ".3p" / RUN


def read_state(run_dir: Path) -> dict:
    return json.loads((run_dir / "state.json").read_text())


# --- Step 1: init, config, guards -------------------------------------------

def test_think_init_in_git_repo(script_path, tmp_git_repo):
    run_dir = init_think(script_path, tmp_git_repo)
    s = read_state(run_dir)
    assert s["mode"] == "think"
    assert s["phase"] == "think"
    assert s["contextFiles"] == []
    assert s["resolvedConfig"]["roundCap"] == 5
    assert {"ts": s["timeline"][1]["ts"], "kind": "phase", "label": "think"} == s["timeline"][1]
    refs = subprocess.run(["git", "for-each-ref", "refs/3p/"], cwd=tmp_git_repo,
                          capture_output=True, text=True).stdout
    assert refs.strip() == ""


def test_think_init_non_git(script_path, tmp_non_git):
    run_dir = init_think(script_path, tmp_non_git)
    s = read_state(run_dir)
    assert s["gitMode"] is False
    assert s["mode"] == "think"
    assert not (tmp_non_git / ".gitignore").exists()


def test_full_init_records_mode_and_keeps_defaults(script_path, tmp_git_repo):
    r = run_3p(script_path, tmp_git_repo, "init", "q", "20261007-1200")
    assert r.returncode == 0, r.stderr
    s = read_state(tmp_git_repo / ".3p" / RUN)
    assert s["mode"] == "full"
    assert s["phase"] == "plan"
    assert s["contextFiles"] == []
    assert s["resolvedConfig"]["roundCap"] == 10
    assert "thinkRoundCap" not in s["resolvedConfig"]
    assert s["timeline"][1]["label"] == "plan"


def _caps(script_path, repo, project_cfg=None, user_cfg=None, tmp_path=None):
    """Return (think_cap, full_cap) for the given config layers."""
    import os
    env = dict(os.environ)
    if project_cfg is not None:
        (repo / ".3p").mkdir(exist_ok=True)
        (repo / ".3p" / "config.json").write_text(json.dumps(project_cfg))
    if user_cfg is not None:
        uc = tmp_path / "user-config.json"
        uc.write_text(json.dumps(user_cfg))
        env["CLAUDE_CODE_3P_USER_CONFIG"] = str(uc)
    r = run_3p(script_path, repo, "init", "t", "20261007-1200", "--mode", "think", env=env)
    assert r.returncode == 0, r.stderr
    r = run_3p(script_path, repo, "init", "f", "20261007-1200", env=env)
    assert r.returncode == 0, r.stderr
    think = read_state(repo / ".3p" / "t-20261007-1200")["resolvedConfig"]["roundCap"]
    full = read_state(repo / ".3p" / "f-20261007-1200")["resolvedConfig"]["roundCap"]
    return think, full


def test_cap_precedence_think_round_cap(script_path, tmp_git_repo):
    assert _caps(script_path, tmp_git_repo, {"thinkRoundCap": 3}) == (3, 10)


def test_cap_precedence_explicit_round_cap_controls_think(script_path, tmp_git_repo):
    assert _caps(script_path, tmp_git_repo, {"roundCap": 7}) == (7, 7)


def test_cap_precedence_both_set(script_path, tmp_git_repo):
    assert _caps(script_path, tmp_git_repo, {"roundCap": 7, "thinkRoundCap": 3}) == (3, 7)


def test_cap_precedence_explicit_default_value_counts(script_path, tmp_git_repo):
    assert _caps(script_path, tmp_git_repo, {"roundCap": 10}) == (10, 10)


def test_cap_precedence_machine_wide_round_cap(script_path, tmp_git_repo, tmp_path):
    assert _caps(script_path, tmp_git_repo, user_cfg={"roundCap": 8},
                 tmp_path=tmp_path) == (8, 8)


def test_cap_precedence_via_config_flag(script_path, tmp_git_repo, tmp_path):
    cfg = tmp_path / "custom.json"
    cfg.write_text(json.dumps({"thinkRoundCap": 2}))
    r = run_3p(script_path, tmp_git_repo, "init", "q", "20261007-1200",
               "--mode", "think", "--config", str(cfg))
    assert r.returncode == 0, r.stderr
    assert read_state(tmp_git_repo / ".3p" / RUN)["resolvedConfig"]["roundCap"] == 2


def test_cap_malformed_project_config_falls_back_to_default(script_path, tmp_git_repo):
    (tmp_git_repo / ".3p").mkdir()
    (tmp_git_repo / ".3p" / "config.json").write_text("{not json")
    run_dir = init_think(script_path, tmp_git_repo)
    assert read_state(run_dir)["resolvedConfig"]["roundCap"] == 5


def test_invalid_think_round_cap_rejected(script_path, tmp_git_repo):
    for bad in (0, -1, "5", True):
        (tmp_git_repo / ".3p").mkdir(exist_ok=True)
        (tmp_git_repo / ".3p" / "config.json").write_text(json.dumps({"thinkRoundCap": bad}))
        r = run_3p(script_path, tmp_git_repo, "init", "q", "20261007-1200", "--mode", "think")
        assert r.returncode != 0, bad
        assert "thinkRoundCap" in r.stderr
        assert not (tmp_git_repo / ".3p" / RUN).exists()


def test_context_file_stored_absolute(script_path, tmp_git_repo):
    (tmp_git_repo / "notes.md").write_text("background")
    run_dir = init_think(script_path, tmp_git_repo, "--context", "notes.md")
    files = read_state(run_dir)["contextFiles"]
    assert files == [str((tmp_git_repo / "notes.md").resolve())]


def test_context_file_outside_anchor_allowed(script_path, tmp_git_repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "brief.txt"
    outside.write_text("x")
    run_dir = init_think(script_path, tmp_git_repo, "--context", str(outside))
    assert read_state(run_dir)["contextFiles"] == [str(outside.resolve())]


def test_context_rejections_leave_no_run_dir(script_path, tmp_git_repo):
    (tmp_git_repo / ".env").write_text("SECRET=1")
    (tmp_git_repo / "adir").mkdir()
    for bad in ("missing.md", "adir", ".env"):
        r = run_3p(script_path, tmp_git_repo, "init", "q", "20261007-1200",
                   "--mode", "think", "--context", bad)
        assert r.returncode == 2, (bad, r.stderr)
        assert "--context" in r.stderr
        assert not (tmp_git_repo / ".3p" / RUN).exists()


def test_context_requires_think_mode(script_path, tmp_git_repo):
    (tmp_git_repo / "notes.md").write_text("x")
    r = run_3p(script_path, tmp_git_repo, "init", "q", "20261007-1200", "--context", "notes.md")
    assert r.returncode == 2
    assert "--context requires --mode think" in r.stderr


def test_unknown_mode_rejected(script_path, tmp_git_repo):
    r = run_3p(script_path, tmp_git_repo, "init", "q", "20261007-1200", "--mode", "bogus")
    assert r.returncode == 2
    assert "Unknown mode" in r.stderr


def test_mode_flag_missing_value(script_path, tmp_git_repo):
    r = run_3p(script_path, tmp_git_repo, "init", "q", "20261007-1200", "--mode")
    assert r.returncode == 2


def test_snapshot_capture_refused_in_think_mode(script_path, tmp_git_repo):
    run_dir = init_think(script_path, tmp_git_repo)
    r = run_3p(script_path, tmp_git_repo, "snapshot", "capture", RUN, "pre-build")
    assert r.returncode == 2
    assert "think-mode" in r.stderr
    assert read_state(run_dir)["baselines"] == {}


def test_context_secret_outside_anchor_rejected(script_path, tmp_git_repo, tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    cred = home / ".aws" / "credentials"
    cred.parent.mkdir()
    cred.write_text("[default]\naws_secret_access_key=x\n")
    r = run_3p(script_path, tmp_git_repo, "init", "q", "20261007-1200",
               "--mode", "think", "--context", str(cred))
    assert r.returncode == 2, r.stderr
    assert "secret pattern" in r.stderr
    assert not (tmp_git_repo / ".3p" / RUN).exists()


def test_context_secret_nested_inside_anchor_rejected(script_path, tmp_git_repo):
    cred = tmp_git_repo / "sub" / ".aws" / "credentials"
    cred.parent.mkdir(parents=True)
    cred.write_text("x")
    r = run_3p(script_path, tmp_git_repo, "init", "q", "20261007-1200",
               "--mode", "think", "--context", str(cred))
    assert r.returncode == 2
    assert "secret pattern" in r.stderr


def test_context_symlink_named_like_secret_rejected(script_path, tmp_git_repo):
    (tmp_git_repo / "store").mkdir()
    (tmp_git_repo / "store" / "opaque").write_text("SECRET=1")
    (tmp_git_repo / ".env").symlink_to(tmp_git_repo / "store" / "opaque")
    r = run_3p(script_path, tmp_git_repo, "init", "q", "20261007-1200",
               "--mode", "think", "--context", ".env")
    assert r.returncode == 2, r.stderr
    assert "secret pattern" in r.stderr
    assert not (tmp_git_repo / ".3p" / RUN).exists()


def test_context_symlink_to_secret_rejected(script_path, tmp_git_repo, tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    cred = home / ".aws" / "credentials"
    cred.parent.mkdir()
    cred.write_text("x")
    (tmp_git_repo / "notes.md").symlink_to(cred)
    r = run_3p(script_path, tmp_git_repo, "init", "q", "20261007-1200",
               "--mode", "think", "--context", "notes.md")
    assert r.returncode == 2, r.stderr
    assert "secret pattern" in r.stderr


# --- Step 2: think phase plumbing --------------------------------------------

def _load_module(script_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("threep", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _finding(title="Weak assumption", verdict="rejected", reason="memo addresses it"):
    return {"severity": "Important", "title": title, "location": "memo §Options",
            "issue": "The memo assumes X.", "rationale": "If not X, the answer flips.",
            "verdict": verdict, "verdictReason": reason}


def _think_round(script_path, cwd, rnd, reviewer, findings=(), rebuttals=()):
    status = "findings" if findings else "approved"
    r = run_3p(script_path, cwd, "round-write", RUN, "think", "-", str(rnd), reviewer,
               json.dumps({"reviewer": reviewer, "status": status, "durationSeconds": 5,
                           "findings": list(findings), "rebuttals": list(rebuttals)}))
    assert r.returncode == 0, r.stderr
    r = run_3p(script_path, cwd, "availability-append", RUN,
               json.dumps({"phase": "think", "step": None, "round": rnd,
                           "reviewer": reviewer, "status": "responded",
                           "durationSeconds": 5}))
    assert r.returncode == 0, r.stderr


def test_review_type_for_think_is_reasoning(script_path):
    mod = _load_module(script_path)
    assert mod._review_type_for_phase("think") == "reasoning"
    assert mod._review_type_for_phase("build") == "code"
    assert mod.round_filename("think", "-", 2, "codex") == "think-round-2-codex.md"
    assert mod.scope_for("think", "-") == "think"
    assert mod._availability_scope({"phase": "think"}) == "think"


def test_think_round_write_and_ledger(script_path, tmp_git_repo):
    run_dir = init_think(script_path, tmp_git_repo)
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "currentRound", "1")
    _think_round(script_path, tmp_git_repo, 1, "codex", [_finding()])
    f = run_dir / "think-round-1-codex.md"
    assert f.exists()
    assert f.read_text().startswith("# Think round 1 (codex)")
    entry = read_state(run_dir)["ledger"]["findings"][0]
    assert entry["scope"] == "think"
    assert entry["issue"] == "The memo assumes X."
    assert entry["rationale"] == "If not X, the answer flips."


def test_think_rebuttal_arguments_recorded(script_path, tmp_git_repo):
    run_dir = init_think(script_path, tmp_git_repo)
    _think_round(script_path, tmp_git_repo, 2, "codex", [_finding()], [{
        "originalRound": 1, "originalTitle": "Weak assumption",
        "claudeReasonPrior": "addressed", "reviewerPushback": "It is not addressed.",
        "claudeReasonNow": "Section 2 covers it.", "outcome": "sustained"}])
    reb = read_state(run_dir)["ledger"]["rebuttals"][0]
    assert reb["scope"] == "think"
    assert reb["reviewerPushback"] == "It is not addressed."
    assert reb["claudeReasonNow"] == "Section 2 covers it."


def test_think_round_close_and_hud(script_path, tmp_git_repo):
    init_think(script_path, tmp_git_repo)
    r = run_3p(script_path, tmp_git_repo, "hud", RUN)
    assert r.returncode == 0, r.stderr
    assert "· Think ·" in r.stdout and "Round 0/5" in r.stdout
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "currentScope", '"think"')
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "currentRound", "1")
    _think_round(script_path, tmp_git_repo, 1, "codex")
    _think_round(script_path, tmp_git_repo, 1, "antigravity", [_finding(verdict="accepted")])
    r = run_3p(script_path, tmp_git_repo, "round-close", RUN)
    assert r.returncode == 0, r.stderr
    assert "Codex ✓ APPROVED" in r.stdout
    assert "Antigravity [Important] Weak assumption → accepted" in r.stdout


def test_think_phase_end_progress(script_path, tmp_git_repo):
    init_think(script_path, tmp_git_repo)
    r = run_3p(script_path, tmp_git_repo, "phase-end", RUN)
    assert r.returncode == 0, r.stderr
    assert "[Think ▶]" in r.stdout
    assert "Plan" not in r.stdout.split("**Progress:**")[1].splitlines()[0]
    assert "Scope `think`" in r.stdout
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "phase", '"done"')
    r = run_3p(script_path, tmp_git_repo, "phase-end", RUN)
    assert "[Think ✓]" in r.stdout
    assert "Scope `think`" in r.stdout


def test_full_mode_progress_unchanged(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "q", "20261007-1200")
    r = run_3p(script_path, tmp_git_repo, "phase-end", RUN)
    assert "[Plan ▶] → [Build ·] → [Final ·]" in r.stdout
    assert "Scope `plan`" in r.stdout


def test_think_timing_row(script_path, tmp_git_repo):
    mod = _load_module(script_path)
    state = {"startedAt": "2026-10-07T10:00:00Z", "phase": "done", "mode": "think",
             "timeline": [{"ts": "2026-10-07T10:00:00Z", "kind": "run-start", "label": None},
                          {"ts": "2026-10-07T10:00:00Z", "kind": "phase", "label": "think"},
                          {"ts": "2026-10-07T10:05:00Z", "kind": "phase", "label": "done"}]}
    lines = mod.render_timing_table(state)
    row = [l for l in lines if l.startswith("| Think |")]
    assert row and "5m" in row[0]
    assert not any("Phase · think" in l for l in lines)


# --- Step 3: think summary and promote ---------------------------------------

def _summary(script_path, cwd, run_dir):
    r = run_3p(script_path, cwd, "summary", RUN)
    assert r.returncode == 0, r.stderr
    return (run_dir / "summary.md").read_text()


def _section(text, heading):
    body = text.split(f"## {heading}\n", 1)[1]
    return body.split("\n## ", 1)[0]


def test_think_summary_lists_open_rejection(script_path, tmp_git_repo):
    run_dir = init_think(script_path, tmp_git_repo)
    (run_dir / "task.txt").write_text("Per-repo or machine-wide config?")
    (run_dir / "memo.md").write_text("# Memo\nRecommend machine-wide.")
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "currentRound", "1")
    _think_round(script_path, tmp_git_repo, 1, "codex", [_finding()])
    _think_round(script_path, tmp_git_repo, 1, "antigravity")
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "exitReason", '"cap-reached"')
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "phase", '"done"')
    text = _summary(script_path, tmp_git_repo, run_dir)
    assert "Per-repo or machine-wide config?" in text
    assert "Recommend machine-wide." in text
    dis = _section(text, "Unresolved disagreements")
    assert "Weak assumption" in dis
    assert "The memo assumes X. If not X, the answer flips." in dis
    assert "memo addresses it" in dis
    assert "normal think-mode outcome" in _section(text, "Outcome")
    assert "| Think |" in _section(text, "Timing")
    assert "think-round-1-codex.md" in text
    assert "Uncommitted-state notice" not in text
    assert "Final approved plan" not in text
    assert "Per-step summaries" not in text


def test_think_summary_uses_latest_pushback(script_path, tmp_git_repo):
    run_dir = init_think(script_path, tmp_git_repo)
    _think_round(script_path, tmp_git_repo, 1, "codex", [_finding()])
    _think_round(script_path, tmp_git_repo, 2, "codex", [_finding(reason="still covered")], [{
        "originalRound": 1, "originalTitle": "Weak assumption",
        "reviewerPushback": "Section 2 never tests X.",
        "claudeReasonNow": "X is the stated premise of the question.",
        "outcome": "sustained"}])
    dis = _section(_summary(script_path, tmp_git_repo, run_dir), "Unresolved disagreements")
    assert "Section 2 never tests X." in dis
    assert "X is the stated premise of the question." in dis
    assert "The memo assumes X." not in dis


def test_think_summary_dropped_rejection_is_resolved(script_path, tmp_git_repo):
    run_dir = init_think(script_path, tmp_git_repo)
    _think_round(script_path, tmp_git_repo, 1, "codex", [_finding()])
    _think_round(script_path, tmp_git_repo, 2, "codex")  # responded, did not re-raise
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "exitReason", '"approved"')
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "currentRound", "2")
    text = _summary(script_path, tmp_git_repo, run_dir)
    assert "every objection was resolved" in _section(text, "Unresolved disagreements")
    assert "Unanimous approval at round 2" in _section(text, "Outcome")


def test_think_summary_accepted_open_is_not_disagreement(script_path, tmp_git_repo):
    run_dir = init_think(script_path, tmp_git_repo)
    _think_round(script_path, tmp_git_repo, 1, "codex", [_finding(verdict="accepted")])
    text = _summary(script_path, tmp_git_repo, run_dir)
    assert "every objection was resolved" in _section(text, "Unresolved disagreements")
    assert "Not recorded." in _section(text, "Outcome")


def test_full_summary_has_no_think_sections(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "q", "20261007-1200")
    run_dir = tmp_git_repo / ".3p" / RUN
    text = _summary(script_path, tmp_git_repo, run_dir)
    assert "Unresolved disagreements" not in text
    assert "Uncommitted-state notice" in text


def test_promote_done_think_run(script_path, tmp_git_repo):
    run_dir = init_think(script_path, tmp_git_repo)
    (run_dir / "task.txt").write_text("Per-repo or machine-wide config?")
    (run_dir / "memo.md").write_text("Recommend machine-wide.")
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "phase", '"done"')
    r = run_3p(script_path, tmp_git_repo, "promote", RUN)
    assert r.returncode == 0, r.stderr
    assert "Per-repo or machine-wide config?" in r.stdout
    assert "Recommend machine-wide." in r.stdout
    assert RUN in r.stdout


def test_promote_refusals(script_path, tmp_git_repo):
    run_dir = init_think(script_path, tmp_git_repo)
    (run_dir / "memo.md").write_text("m")
    r = run_3p(script_path, tmp_git_repo, "promote", RUN)  # not done
    assert r.returncode == 2 and "has not finished" in r.stderr
    run_3p(script_path, tmp_git_repo, "state-write", RUN, "phase", '"done"')
    (run_dir / "memo.md").unlink()
    r = run_3p(script_path, tmp_git_repo, "promote", RUN)  # no memo
    assert r.returncode == 2 and "no memo.md" in r.stderr
    run_3p(script_path, tmp_git_repo, "init", "f", "20261007-1200")
    full = tmp_git_repo / ".3p" / "f-20261007-1200"
    (full / "memo.md").write_text("m")
    run_3p(script_path, tmp_git_repo, "state-write", "f-20261007-1200", "phase", '"done"')
    r = run_3p(script_path, tmp_git_repo, "promote", "f-20261007-1200")
    assert r.returncode == 2 and "not a think-mode run" in r.stderr
    r = run_3p(script_path, tmp_git_repo, "promote", "nope-20261007-1200")
    assert r.returncode == 2


def _two_same_title_findings(script_path, cwd, rebuttal):
    cost = dict(_finding(reason="cost covered"), location="memo §Cost",
                issue="Assumes hosting is free.", rationale="It is not.")
    priv = dict(_finding(reason="privacy covered"), location="memo §Privacy",
                issue="Assumes data stays local.", rationale="It syncs.")
    _think_round(script_path, cwd, 1, "codex", [cost, priv])
    _think_round(script_path, cwd, 2, "codex", [cost, priv], [rebuttal])


def test_rebuttal_with_location_matches_only_its_finding(script_path, tmp_git_repo):
    run_dir = init_think(script_path, tmp_git_repo)
    _two_same_title_findings(script_path, tmp_git_repo, {
        "originalRound": 1, "originalTitle": "Weak assumption",
        "originalLocation": "memo §Cost", "reviewerPushback": "Hosting costs $X/mo.",
        "claudeReasonNow": "Self-hosting is out of scope.", "outcome": "sustained"})
    dis = _section(_summary(script_path, tmp_git_repo, run_dir), "Unresolved disagreements")
    cost_block, priv_block = dis.split("### ")[1:3]
    assert "Hosting costs $X/mo." in cost_block
    assert "Self-hosting is out of scope." in cost_block
    assert "Hosting costs" not in priv_block
    assert "Assumes data stays local. It syncs." in priv_block
    assert "privacy covered" in priv_block


def test_ambiguous_rebuttal_without_location_is_not_attached(script_path, tmp_git_repo):
    run_dir = init_think(script_path, tmp_git_repo)
    _two_same_title_findings(script_path, tmp_git_repo, {
        "originalRound": 1, "originalTitle": "Weak assumption",
        "reviewerPushback": "Hosting costs $X/mo.",
        "claudeReasonNow": "Self-hosting is out of scope.", "outcome": "sustained"})
    dis = _section(_summary(script_path, tmp_git_repo, run_dir), "Unresolved disagreements")
    assert "Hosting costs" not in dis
    assert "Assumes hosting is free. It is not." in dis
    assert "Assumes data stays local. It syncs." in dis


# --- Step 4: reviewer prompt --------------------------------------------------

def test_think_review_prompt_template(script_path):
    text = (Path(script_path).parent.parent / "prompts" / "think-review.md").read_text()
    for ph in ("{{task}}", "{{north_star}}", "{{memo}}", "{{context_files}}",
               "{{rebuttal_section}}"):
        assert ph in text, ph
    assert "The literal token `APPROVED` on its own line" in text
    assert "[<Blocker|Critical|Important|Risk>] <one-line title>" in text
    assert "no substantive objections remain" in text


def test_think_review_prompt_findings_parse(script_path, tmp_path):
    raw = tmp_path / "r.txt"
    raw.write_text("[Important] Missing option: do nothing\nLocation: memo §Options\n"
                   "Issue: The memo never weighs keeping the status quo.\n"
                   "Rationale: It may dominate both options.\n")
    r = run_3p(script_path, tmp_path, "parse-response", str(raw))
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed["status"] == "findings"
    assert parsed["findings"][0]["title"] == "Missing option: do nothing"

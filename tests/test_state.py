import json
import subprocess
import sys
from pathlib import Path


def run_3p(script_path: Path, cwd: Path, *args, env=None):
    return subprocess.run(
        [sys.executable, str(script_path), *args],
        capture_output=True, text=True, cwd=cwd, env=env,
    )


def test_init_creates_run_dir_and_state(script_path, tmp_git_repo):
    r = run_3p(script_path, tmp_git_repo, "init", "test-slug", "20260603-1430")
    assert r.returncode == 0, r.stderr
    run_dir = tmp_git_repo / ".3p" / "test-slug-20260603-1430"
    assert run_dir.is_dir()
    state = json.loads((run_dir / "state.json").read_text())
    assert state["taskSlug"] == "test-slug"
    assert state["phase"] == "plan"
    assert state["currentRound"] == 0
    assert state["gitMode"] is True
    assert "resolvedConfig" in state
    assert state["resolvedConfig"]["roundCap"] == 10
    assert ".env" in state["resolvedConfig"]["secretPatterns"]
    assert state["availabilityLog"] == []


def test_init_persists_cli_config_flags(script_path, tmp_git_repo):
    r = run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430",
               "--exclude", "secret_dir/", "--exclude", "data/")
    assert r.returncode == 0, r.stderr
    state = json.loads((tmp_git_repo / ".3p" / "x-20260603-1430" / "state.json").read_text())
    assert "secret_dir/" in state["resolvedConfig"]["excludes"]
    assert "data/" in state["resolvedConfig"]["excludes"]
    assert "node_modules/" in state["resolvedConfig"]["excludes"]


def test_init_persists_model_power_and_reviewer_role(script_path, tmp_git_repo, tmp_path):
    run_3p(script_path, tmp_git_repo, "model-power", "low")
    r = run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    assert r.returncode == 0, r.stderr
    state = json.loads((tmp_git_repo / ".3p" / "x-20260603-1430" / "state.json").read_text())
    assert state["resolvedConfig"]["modelPower"] == "low"
    fake_home = tmp_path / "home"
    env = {"HOME": str(fake_home), "PATH": "/usr/bin:/bin"}
    role = run_3p(script_path, tmp_git_repo, "reviewer-role", "x-20260603-1430",
                  "antigravity", "reasoning", env=env)
    assert role.returncode == 0, role.stderr
    role_name = role.stdout.strip()
    assert role_name.startswith("codereviewer-low-reasoning-")
    # antigravity is stored under its PAL cli_name, agy.json.
    agy_pal = json.loads((fake_home / ".pal" / "cli_clients" / "agy.json").read_text())
    assert agy_pal["roles"][role_name]["role_args"] == ["--model", "Gemini 3.1 Pro (Low)"]
    # The code-review slot resolves to the Flash model instead.
    code_role = run_3p(script_path, tmp_git_repo, "reviewer-role", "x-20260603-1430",
                       "antigravity", "code", env=env)
    assert code_role.returncode == 0, code_role.stderr
    code_name = code_role.stdout.strip()
    assert code_name.startswith("codereviewer-low-code-")
    agy_pal = json.loads((fake_home / ".pal" / "cli_clients" / "agy.json").read_text())
    assert agy_pal["roles"][code_name]["role_args"] == ["--model", "Gemini 3.5 Flash (Low)"]


def test_init_bootstraps_gitignore(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    gi = (tmp_git_repo / ".gitignore").read_text()
    assert ".3p/" in gi


def test_init_non_git_sets_gitmode_false(script_path, tmp_non_git):
    run_3p(script_path, tmp_non_git, "init", "x", "20260603-1430")
    state = json.loads((tmp_non_git / ".3p" / "x-20260603-1430" / "state.json").read_text())
    assert state["gitMode"] is False
    assert not (tmp_non_git / ".gitignore").exists()


def test_state_read_write_roundtrip(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    r = run_3p(script_path, tmp_git_repo, "state-write", "x-20260603-1430",
               "currentRound", "3")
    assert r.returncode == 0, r.stderr
    r2 = run_3p(script_path, tmp_git_repo, "state-read", "x-20260603-1430", "currentRound")
    assert r2.stdout.strip() == "3"


def test_state_write_no_tmp_residue(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    run_3p(script_path, tmp_git_repo, "state-write", "x-20260603-1430",
           "phase", '"build"')
    run_dir = tmp_git_repo / ".3p" / "x-20260603-1430"
    assert not (run_dir / "state.json.tmp").exists()


def test_availability_log_appends(script_path, tmp_git_repo):
    run_3p(script_path, tmp_git_repo, "init", "x", "20260603-1430")
    e1 = {"phase": "plan", "step": None, "round": 1, "reviewer": "codex",
          "status": "responded", "durationSeconds": 47}
    e2 = {"phase": "plan", "step": None, "round": 1, "reviewer": "antigravity",
          "status": "unavailable", "reason": "timeout", "durationSeconds": 120}
    run_3p(script_path, tmp_git_repo, "availability-append", "x-20260603-1430",
           json.dumps(e1))
    run_3p(script_path, tmp_git_repo, "availability-append", "x-20260603-1430",
           json.dumps(e2))
    state = json.loads((tmp_git_repo / ".3p" / "x-20260603-1430" / "state.json").read_text())
    assert len(state["availabilityLog"]) == 2
    assert state["availabilityLog"][0]["reviewer"] == "codex"
    assert state["availabilityLog"][1]["status"] == "unavailable"

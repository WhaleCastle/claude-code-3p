import json
import subprocess
import sys
from pathlib import Path


def run_config_load(script_path: Path, cwd: Path, extra_args=None, env=None):
    args = [sys.executable, str(script_path), "config-load"] + (extra_args or [])
    result = subprocess.run(args, capture_output=True, text=True, cwd=cwd, check=True, env=env)
    return json.loads(result.stdout)


def run_3p(script_path: Path, cwd: Path, *args, env=None):
    return subprocess.run(
        [sys.executable, str(script_path), *args],
        capture_output=True, text=True, cwd=cwd, env=env,
    )


def test_defaults_present(script_path, tmp_path):
    cfg = run_config_load(script_path, tmp_path)
    assert cfg["timeoutSeconds"] == 120
    assert cfg["roundCap"] == 10
    assert cfg["consecutiveFailuresForDowngrade"] == 3
    assert cfg["modelPower"] == "high"
    assert cfg["models"]["codex"]["high"] == {"reasoning": "gpt-5.5", "code": "gpt-5.5"}
    assert cfg["models"]["codex"]["low"] == {
        "reasoning": "gpt-5.4-mini", "code": "gpt-5.4-mini"}
    assert cfg["models"]["antigravity"]["high"] == {
        "reasoning": "Gemini 3.1 Pro (High)", "code": "Gemini 3.5 Flash (High)"}
    assert cfg["models"]["antigravity"]["low"] == {
        "reasoning": "Gemini 3.1 Pro (Low)", "code": "Gemini 3.5 Flash (Low)"}
    assert "node_modules/" in cfg["excludes"]


def test_legacy_flat_model_config_upconverts(script_path, tmp_path):
    """A legacy flat {power: "model"} config is up-converted to
    {reasoning, code} so older .3p configs keep working."""
    config_dir = tmp_path / ".3p"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({
        "models": {"codex": {"high": "gpt-legacy"}},
    }))
    cfg = run_config_load(script_path, tmp_path)
    assert cfg["models"]["codex"]["high"] == {
        "reasoning": "gpt-legacy", "code": "gpt-legacy"}


def test_secret_patterns_always_present(script_path, tmp_path):
    cfg = run_config_load(script_path, tmp_path)
    assert ".env" in cfg["secretPatterns"]
    assert "*.pem" in cfg["secretPatterns"]
    assert "**/.aws/credentials" in cfg["secretPatterns"]


def test_config_file_excludes_replaces_defaults(script_path, tmp_path):
    """Per spec: default bloat list is user-overridable. File's `excludes`
    REPLACES the defaults (so users can intentionally include `dist/` etc.)."""
    config_dir = tmp_path / ".3p"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({
        "roundCap": 12,
        "excludes": ["custom_dir/"],
    }))
    cfg = run_config_load(script_path, tmp_path)
    assert cfg["roundCap"] == 12
    assert "custom_dir/" in cfg["excludes"]
    assert "node_modules/" not in cfg["excludes"]


def test_config_file_extraExcludes_appends(script_path, tmp_path):
    config_dir = tmp_path / ".3p"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({
        "extraExcludes": ["extra_dir/"],
    }))
    cfg = run_config_load(script_path, tmp_path)
    assert "extra_dir/" in cfg["excludes"]
    assert "node_modules/" in cfg["excludes"]


def test_secret_patterns_cannot_be_removed(script_path, tmp_path):
    config_dir = tmp_path / ".3p"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({
        "secretPatterns": [],
    }))
    cfg = run_config_load(script_path, tmp_path)
    assert ".env" in cfg["secretPatterns"]
    assert "*.pem" in cfg["secretPatterns"]


def test_cli_exclude_flag_appends(script_path, tmp_path):
    cfg = run_config_load(script_path, tmp_path,
                          ["--exclude", "extra/", "--exclude", "more/"])
    assert "extra/" in cfg["excludes"]
    assert "more/" in cfg["excludes"]
    assert "node_modules/" in cfg["excludes"]


def test_config_path_flag(script_path, tmp_path):
    cfg_file = tmp_path / "custom.json"
    cfg_file.write_text(json.dumps({"roundCap": 99}))
    cfg = run_config_load(script_path, tmp_path, ["--config", str(cfg_file)])
    assert cfg["roundCap"] == 99


def test_missing_config_flag_value_returns_usage(script_path, tmp_path):
    r = run_3p(script_path, tmp_path, "config-load", "--config")
    assert r.returncode == 2
    assert "Usage:" in r.stderr


def test_missing_init_exclude_value_returns_usage(script_path, tmp_path):
    r = run_3p(script_path, tmp_path, "init", "x", "20260603-1430", "--exclude")
    assert r.returncode == 2
    assert "Usage:" in r.stderr


def test_config_rejects_string_excludes(script_path, tmp_path):
    cfg_file = tmp_path / "custom.json"
    cfg_file.write_text(json.dumps({"excludes": "dist/"}))
    r = run_3p(script_path, tmp_path, "config-load", "--config", str(cfg_file))
    assert r.returncode != 0
    assert "Invalid excludes" in r.stderr


def test_config_rejects_string_extra_excludes(script_path, tmp_path):
    cfg_file = tmp_path / "custom.json"
    cfg_file.write_text(json.dumps({"extraExcludes": "dist/"}))
    r = run_3p(script_path, tmp_path, "config-load", "--config", str(cfg_file))
    assert r.returncode != 0
    assert "Invalid extraExcludes" in r.stderr


def test_model_power_command_sets_project_config(script_path, tmp_path):
    r = run_3p(script_path, tmp_path, "model-power", "low")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "low"
    cfg = run_config_load(script_path, tmp_path)
    assert cfg["modelPower"] == "low"
    assert json.loads((tmp_path / ".3p" / "config.json").read_text())["modelPower"] == "low"


def test_model_power_rejects_invalid_value(script_path, tmp_path):
    r = run_3p(script_path, tmp_path, "model-power", "medium")
    assert r.returncode == 2
    assert "model-power" in r.stderr


def test_models_set_updates_config_and_pal_roles(script_path, tmp_path):
    fake_home = tmp_path / "home"
    env = {"HOME": str(fake_home), "PATH": "/usr/bin:/bin"}
    r = run_3p(script_path, tmp_path,
               "models", "set", "codex", "high", "reasoning", "gpt-6.0", env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0] == "codex.high.reasoning=gpt-6.0"
    assert "Restart Claude Code so PAL MCP reloads reviewer roles" in r.stdout
    cfg = run_config_load(script_path, tmp_path, env=env)
    # Only the reasoning slot changed; code keeps the default.
    assert cfg["models"]["codex"]["high"]["reasoning"] == "gpt-6.0"
    assert cfg["models"]["codex"]["high"]["code"] == "gpt-5.5"
    codex_pal = json.loads((fake_home / ".pal" / "cli_clients" / "codex.json").read_text())
    assert codex_pal["roles"]["codereviewer-high-reasoning"]["role_args"] == ["--model", "gpt-6.0"]
    assert codex_pal["roles"]["codereviewer-high-code"]["role_args"] == ["--model", "gpt-5.5"]
    assert codex_pal["roles"]["codereviewer-low-reasoning"]["role_args"] == ["--model", "gpt-5.4-mini"]
    stable_roles = [
        name for name in codex_pal["roles"]
        if name.startswith("codereviewer-high-reasoning-")
    ]
    assert stable_roles
    assert codex_pal["roles"][stable_roles[0]]["role_args"] == ["--model", "gpt-6.0"]


def test_models_set_preserves_legacy_flat_sibling_slot(script_path, tmp_path):
    """Overriding one review type on a legacy flat {power: "model"} config must
    keep the user's model for the untouched review type (up-convert, not drop)."""
    fake_home = tmp_path / "home"
    env = {"HOME": str(fake_home), "PATH": "/usr/bin:/bin"}
    config_dir = tmp_path / ".3p"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({
        "models": {"codex": {"high": "gpt-legacy"}},
    }))
    r = run_3p(script_path, tmp_path,
               "models", "set", "codex", "high", "code", "gpt-new", env=env)
    assert r.returncode == 0, r.stderr
    cfg = run_config_load(script_path, tmp_path, env=env)
    assert cfg["models"]["codex"]["high"]["code"] == "gpt-new"
    assert cfg["models"]["codex"]["high"]["reasoning"] == "gpt-legacy"


def test_models_set_rejects_missing_review_type(script_path, tmp_path):
    """The legacy 4-arg form (no reviewType) must be rejected."""
    fake_home = tmp_path / "home"
    env = {"HOME": str(fake_home), "PATH": "/usr/bin:/bin"}
    r = run_3p(script_path, tmp_path, "models", "set", "codex", "high", "gpt-6.0", env=env)
    assert r.returncode == 2
    assert "reasoning|code" in r.stderr


def test_pal_config_install_preserves_existing_client_args(script_path, tmp_path):
    fake_home = tmp_path / "home"
    pal_dir = fake_home / ".pal" / "cli_clients"
    pal_dir.mkdir(parents=True)
    # The Antigravity reviewer is stored under its PAL cli_name, agy.json.
    (pal_dir / "agy.json").write_text(json.dumps({
        "name": "agy",
        "command": "agy",
        "additional_args": ["--add-dir", "/tmp/work"],
        "env": {},
        "roles": {
            "codereviewer": {
                "prompt_path": "systemprompts/clink/default_codereviewer.txt",
                "role_args": [],
            }
        },
    }))
    env = {"HOME": str(fake_home), "PATH": "/usr/bin:/bin"}
    r = run_3p(script_path, tmp_path, "pal-config", "install", env=env)
    assert r.returncode == 0, r.stderr
    assert "Restart Claude Code so PAL MCP reloads reviewer roles" in r.stdout
    agy_pal = json.loads((pal_dir / "agy.json").read_text())
    # User customizations preserved; PAL injects --dangerously-skip-permissions
    # itself, so it is intentionally absent here.
    assert agy_pal["name"] == "agy"
    assert agy_pal["additional_args"] == ["--add-dir", "/tmp/work"]
    roles = agy_pal["roles"]
    assert roles["codereviewer-high-reasoning"]["role_args"] == ["--model", "Gemini 3.1 Pro (High)"]
    assert roles["codereviewer-high-code"]["role_args"] == ["--model", "Gemini 3.5 Flash (High)"]
    assert roles["codereviewer-low-reasoning"]["role_args"] == ["--model", "Gemini 3.1 Pro (Low)"]
    assert roles["codereviewer-low-code"]["role_args"] == ["--model", "Gemini 3.5 Flash (Low)"]

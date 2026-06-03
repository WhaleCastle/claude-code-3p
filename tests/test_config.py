import json
import subprocess
import sys
from pathlib import Path


def run_config_load(script_path: Path, cwd: Path, extra_args=None):
    args = [sys.executable, str(script_path), "config-load"] + (extra_args or [])
    result = subprocess.run(args, capture_output=True, text=True, cwd=cwd, check=True)
    return json.loads(result.stdout)


def test_defaults_present(script_path, tmp_path):
    cfg = run_config_load(script_path, tmp_path)
    assert cfg["timeoutSeconds"] == 120
    assert cfg["roundCap"] == 10
    assert cfg["consecutiveFailuresForDowngrade"] == 3
    assert "node_modules/" in cfg["excludes"]


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

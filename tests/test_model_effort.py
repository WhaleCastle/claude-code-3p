"""A Codex reviewer slot can pin a reasoning effort as <model>@<effort>."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "3p.py"


def _module():
    spec = importlib.util.spec_from_file_location("p3_effort", str(SCRIPT))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_codex_effort_suffix_becomes_a_config_override():
    m = _module()
    assert m.model_role_args("codex", "gpt-6.2-sol@high") == [
        "--model", "gpt-6.2-sol", "-c", "model_reasoning_effort=high"]
    assert m.model_role_args("codex", "gpt-6.2-sol") == ["--model", "gpt-6.2-sol"]
    assert m.model_role_args("agy", "gemini-4-neon-high") == ["--model", "gemini-4-neon-high"]


@pytest.mark.parametrize("cli, spec", [
    ("agy", "gemini-4-neon@high"),       # tier belongs in the agy id
    ("claude", "opus@high"),
    ("codex", "@high"),
    ("codex", "gpt-6.2-sol@"),
    ("codex", "gpt-6.2-sol@high,x=1"),   # nothing but an effort word
    ("codex", "gpt-6.2-sol@HIGH"),
])
def test_bad_effort_specs_are_rejected(cli, spec):
    with pytest.raises(SystemExit):
        _module().model_role_args(cli, spec)


def _models_set(tmp_path, *args):
    home = tmp_path / "home"
    return home, subprocess.run(
        [sys.executable, str(SCRIPT), "models", "set", "--global", *args],
        capture_output=True, text=True, cwd=tmp_path,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin",
             "CLAUDE_CODE_3P_USER_CONFIG": str(tmp_path / "user.json"),
             "CODEX_3P_USER_CONFIG": str(tmp_path / "user.json")})


def test_models_set_rejects_effort_for_antigravity_without_writing(tmp_path):
    home, r = _models_set(tmp_path, "antigravity", "high", "code", "gemini-4-neon@high")
    assert r.returncode == 2
    assert "only supported for the Codex reviewer" in r.stderr
    assert not (tmp_path / "user.json").exists()


def test_models_set_codex_effort_reaches_pal_roles(tmp_path):
    if "codex" not in _module().MODEL_REVIEWERS:
        pytest.skip("this edition has no Codex reviewer")
    home, r = _models_set(tmp_path, "codex", "high", "reasoning", "gpt-6.2-sol@high")
    assert r.returncode == 0, r.stderr
    roles = json.loads((home / ".pal" / "cli_clients" / "codex.json").read_text())["roles"]
    assert roles["codereviewer-high-reasoning"]["role_args"] == [
        "--model", "gpt-6.2-sol", "-c", "model_reasoning_effort=high"]

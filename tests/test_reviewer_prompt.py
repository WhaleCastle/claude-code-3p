"""3p reviewer roles carry their own PAL system prompt.

PAL's stock codereviewer prompt asks for a Critical/High/Medium/Low report that
ends in a <SUMMARY> block, which contradicts the Response Contract, and
Antigravity followed it often enough that its replies were rejected as
unparseable. 3p's roles point at a prompt that agrees with the contract.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "scripts" / "3p.py"


def _module():
    spec = importlib.util.spec_from_file_location("p3_prompt", str(SCRIPT))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _install(tmp_path):
    fake_home = tmp_path / "home"
    r = subprocess.run([sys.executable, str(SCRIPT), "pal-config", "install"],
                       capture_output=True, text=True, cwd=tmp_path,
                       env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    return fake_home


def _role_prompts(fake_home):
    out = {}
    for path in sorted((fake_home / ".pal" / "cli_clients").glob("*.json")):
        roles = json.loads(path.read_text())["roles"]
        out[path.stem] = {name: role["prompt_path"] for name, role in roles.items()}
    return out


def test_reviewer_roles_use_the_3p_system_prompt(tmp_path):
    fake_home = _install(tmp_path)
    prompt = fake_home / ".pal" / "3p" / "reviewer-system.txt"
    assert prompt.read_text() == (REPO / "prompts" / "reviewer-system.txt").read_text()
    clients = _role_prompts(fake_home)
    m = _module()
    assert set(clients) == {m.REVIEWER_CLI[r] for r in m.MODEL_REVIEWERS}
    for cli, roles in clients.items():
        generated = {n: p for n, p in roles.items() if n.startswith("codereviewer-")}
        assert len(generated) >= 8, cli          # 4 slot roles + 4 model-pinned roles
        assert set(generated.values()) == {str(prompt)}, cli
        # The user's own role keeps PAL's stock prompt for that CLI.
        reviewer = next(r for r, c in m.REVIEWER_CLI.items() if c == cli)
        stock = m.DEFAULT_CLI_CLIENTS[reviewer]["roles"]["codereviewer"]["prompt_path"]
        assert roles["codereviewer"] == stock


def test_install_is_idempotent(tmp_path):
    fake_home = _install(tmp_path)
    before = {p: p.read_text() for p in (fake_home / ".pal").rglob("*") if p.is_file()}
    _install(tmp_path)
    after = {p: p.read_text() for p in (fake_home / ".pal").rglob("*") if p.is_file()}
    assert before == after


def test_missing_prompt_source_keeps_stock_prompt(tmp_path, monkeypatch):
    """A role must never point at a missing file: PAL would then fail to load
    every CLI client, not just the reviewers."""
    m = _module()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(m, "REVIEWER_SYSTEM_PROMPT_SOURCE", tmp_path / "absent.txt")
    m.install_pal_config(m.DEFAULTS)
    assert not m.reviewer_system_prompt_path().exists()
    agy = json.loads(m.cli_client_path("antigravity").read_text())
    assert agy["roles"]["codereviewer-high-code"]["prompt_path"] == (
        m.DEFAULT_CLI_CLIENTS["antigravity"]["roles"]["codereviewer"]["prompt_path"])


def test_review_prompts_forbid_markdown_and_override_other_formats():
    for name in ("plan", "step", "final", "think"):
        text = (REPO / "prompts" / f"{name}-review.md").read_text()
        assert "summary block is fine" not in text, name
        assert "overrides any other output format" in text, name


def test_format_repair_prompt_has_reply_slot():
    text = (REPO / "prompts" / "format-repair.md").read_text()
    assert text.count("{{previous_reply}}") == 1
    assert "APPROVED" in text


def test_install_heals_stale_pinned_roles_only(tmp_path):
    """A model-pinned role from an earlier run moves to the 3p prompt (keeping
    its model); roles 3p did not generate are left alone."""
    fake_home = tmp_path / "home"
    pal_dir = fake_home / ".pal" / "cli_clients"
    pal_dir.mkdir(parents=True)
    stock = "systemprompts/clink/default_codereviewer.txt"
    (pal_dir / "agy.json").write_text(json.dumps({
        "name": "agy", "command": "agy", "additional_args": [], "env": {},
        "roles": {
            "codereviewer": {"prompt_path": stock, "role_args": []},
            "codereviewer-high-code-0123456789": {
                "prompt_path": stock, "role_args": ["--model", "old-model"]},
            "codereviewer-mine": {"prompt_path": stock, "role_args": []},
        },
    }))
    _install(tmp_path)
    roles = json.loads((pal_dir / "agy.json").read_text())["roles"]
    prompt = str(fake_home / ".pal" / "3p" / "reviewer-system.txt")
    assert roles["codereviewer-high-code-0123456789"] == {
        "prompt_path": prompt, "role_args": ["--model", "old-model"]}
    assert roles["codereviewer"]["prompt_path"] == stock
    assert roles["codereviewer-mine"]["prompt_path"] == stock


def test_missing_prompt_source_also_repoints_stale_roles(tmp_path, monkeypatch):
    """With the fallback in use, an old pinned role that points at the 3p
    prompt file must not be left on a path that may no longer exist."""
    m = _module()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(m, "REVIEWER_SYSTEM_PROMPT_SOURCE", tmp_path / "absent.txt")
    pal_dir = home / ".pal" / "cli_clients"
    pal_dir.mkdir(parents=True)
    gone = str(home / ".pal" / "3p" / "reviewer-system.txt")
    (pal_dir / "agy.json").write_text(json.dumps({
        "name": "agy", "command": "agy", "additional_args": [], "env": {},
        "roles": {"codereviewer-high-code-0123456789": {
            "prompt_path": gone, "role_args": ["--model", "old-model"]}},
    }))
    m.install_pal_config(m.DEFAULTS)
    roles = json.loads((pal_dir / "agy.json").read_text())["roles"]
    stock = m.DEFAULT_CLI_CLIENTS["antigravity"]["roles"]["codereviewer"]["prompt_path"]
    assert roles["codereviewer-high-code-0123456789"] == {
        "prompt_path": stock, "role_args": ["--model", "old-model"]}

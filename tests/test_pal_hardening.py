"""PAL reviewer-client hardening — the generated ~/.pal/cli_clients/*.json must
carry a bounded `timeout_seconds` (an unset value falls back to PAL's 1800s
default, which is how a wedged `clink` reviewer call hangs ~30 min) and must not
carry the deprecated `--enable web_search_request` flag (it trips PAL's JSON
parser). _harden_cli_client heals both, idempotently, on every install so stale
configs get upgraded."""
import importlib.util
from pathlib import Path


def _load_module():
    p = Path(__file__).parent.parent / "scripts" / "3p.py"
    spec = importlib.util.spec_from_file_location("p3_pal", str(p))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_default_codex_template_is_clean():
    m = _load_module()
    codex = m.DEFAULT_CLI_CLIENTS["codex"]
    assert codex["timeout_seconds"] == m.REVIEWER_TIMEOUT_BACKSTOP_SECONDS
    assert "web_search_request" not in codex["additional_args"]
    assert "--enable" not in codex["additional_args"]
    # agy template also carries a backstop.
    assert m.DEFAULT_CLI_CLIENTS["antigravity"]["timeout_seconds"] == m.REVIEWER_TIMEOUT_BACKSTOP_SECONDS


def test_strip_flag_pair():
    m = _load_module()
    args = ["--json", "--enable", "web_search_request", "--skip-git-repo-check"]
    assert m._strip_flag_pair(args, "--enable", "web_search_request") == [
        "--json", "--skip-git-repo-check"]
    # no-op when the pair is absent
    assert m._strip_flag_pair(["--json"], "--enable", "web_search_request") == ["--json"]
    # a dangling flag with no following value is left intact
    assert m._strip_flag_pair(["--enable"], "--enable", "web_search_request") == ["--enable"]


def test_harden_heals_stale_codex_config():
    m = _load_module()
    # The exact stale shape observed in the wild: no timeout, deprecated flag.
    stale = {
        "timeout_seconds": None,
        "additional_args": ["--skip-git-repo-check", "--json",
                            "--dangerously-bypass-approvals-and-sandbox",
                            "--enable", "web_search_request"],
    }
    m._harden_cli_client("codex", stale)
    assert stale["timeout_seconds"] == m.REVIEWER_TIMEOUT_BACKSTOP_SECONDS
    assert "web_search_request" not in stale["additional_args"]
    # user-added flags (e.g. cold-boot -c overrides) are preserved
    custom = {"timeout_seconds": None,
              "additional_args": ["--json", "-c", "features.hooks=false"]}
    m._harden_cli_client("codex", custom)
    assert custom["additional_args"] == ["--json", "-c", "features.hooks=false"]


def test_harden_respects_user_timeout_and_is_idempotent():
    m = _load_module()
    chosen = {"timeout_seconds": 1500, "additional_args": []}
    m._harden_cli_client("codex", chosen)
    assert chosen["timeout_seconds"] == 1500          # positive user value respected
    # zero/unset both get the backstop
    for bad in (0, None):
        c = {"timeout_seconds": bad, "additional_args": []}
        m._harden_cli_client("agy", c)
        assert c["timeout_seconds"] == m.REVIEWER_TIMEOUT_BACKSTOP_SECONDS
    # idempotent: a second pass changes nothing
    once = {"timeout_seconds": None, "additional_args": ["--enable", "web_search_request"]}
    m._harden_cli_client("codex", once)
    snapshot = dict(once)
    m._harden_cli_client("codex", once)
    assert once == snapshot

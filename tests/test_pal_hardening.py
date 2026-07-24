"""PAL reviewer-client hardening — the generated ~/.pal/cli_clients/*.json must
carry a bounded `timeout_seconds` (an unset value falls back to PAL's 1800s
default, which is how a wedged `clink` reviewer call hangs ~30 min) and must not
carry the deprecated `--enable web_search_request` flag (it trips PAL's JSON
parser). agy's config must also carry `--print-timeout` above agy's own 5m0s
default, with the PAL wrapper set above that print-timeout. _harden_cli_client
heals all of these, idempotently, on every install so stale configs get
upgraded."""
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
    # agy template carries its own (higher) backstop, above its print-timeout.
    agy = m.DEFAULT_CLI_CLIENTS["antigravity"]
    assert agy["timeout_seconds"] == m.AGY_TIMEOUT_BACKSTOP_SECONDS
    assert agy["additional_args"] == [m.AGY_PRINT_TIMEOUT_FLAG, m.AGY_PRINT_TIMEOUT]
    # wrapper must sit strictly above agy's own print-timeout
    assert m.AGY_TIMEOUT_BACKSTOP_SECONDS > m._parse_go_duration_seconds(m.AGY_PRINT_TIMEOUT)


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
    # zero/unset both get the shared backstop for a reviewer that doesn't raise
    # it further (agy's own higher backstop is covered separately below)
    for bad in (0, None):
        c = {"timeout_seconds": bad, "additional_args": []}
        m._harden_cli_client("codex", c)
        assert c["timeout_seconds"] == m.REVIEWER_TIMEOUT_BACKSTOP_SECONDS
    # idempotent: a second pass changes nothing
    once = {"timeout_seconds": None, "additional_args": ["--enable", "web_search_request"]}
    m._harden_cli_client("codex", once)
    snapshot = dict(once)
    m._harden_cli_client("codex", once)
    assert once == snapshot


def test_parse_go_duration_seconds():
    m = _load_module()
    assert m._parse_go_duration_seconds("1200s") == 1200
    assert m._parse_go_duration_seconds("5m0s") == 300
    assert m._parse_go_duration_seconds("20m") == 1200
    assert m._parse_go_duration_seconds("1h30m") == 5400
    # sub-second units must NOT collapse to minutes: '500ms' is 0.5s, not 500m
    assert m._parse_go_duration_seconds("500ms") == 0.5
    assert m._parse_go_duration_seconds("2m30s") == 150
    # a real 0s parses to 0.0 (falsy but VALID) — distinct from None/unparseable
    assert m._parse_go_duration_seconds("0s") == 0.0
    assert m._parse_go_duration_seconds("0s") is not None
    # Go accepts a bare "0" (no unit) as zero; a bare non-zero number does not
    assert m._parse_go_duration_seconds("0") == 0.0
    assert m._parse_go_duration_seconds("5") is None
    # the WHOLE string must be a valid Go duration — partial/garbage → None,
    # never a half-parse (Go rejects these too)
    assert m._parse_go_duration_seconds("1200sbad") is None
    assert m._parse_go_duration_seconds("foo1200s") is None
    assert m._parse_go_duration_seconds("1h 30m") is None      # internal space
    # unparseable / wrong type → None (caller falls back to the default)
    assert m._parse_go_duration_seconds("") is None
    assert m._parse_go_duration_seconds(None) is None
    assert m._parse_go_duration_seconds("forever") is None


def _print_timeout_flag_count(args, flag="--print-timeout"):
    return sum(1 for a in args if a == flag or a.startswith(flag + "="))


def test_harden_agy_preserves_eq_syntax_value():
    """A user value in `--print-timeout=VALUE` form must be detected and NOT
    clobbered by an appended duplicate default (reviewer: Antigravity R1)."""
    m = _load_module()
    c = {"timeout_seconds": 600, "additional_args": ["--print-timeout=3000s"]}
    m._harden_cli_client("agy", c)
    assert c["additional_args"] == ["--print-timeout=3000s"]      # preserved, no duplicate
    assert _print_timeout_flag_count(c["additional_args"]) == 1
    assert c["timeout_seconds"] > 3000                            # wrapper above the user's value
    snap = {"timeout_seconds": c["timeout_seconds"],
            "additional_args": list(c["additional_args"])}
    m._harden_cli_client("agy", c)                                # idempotent
    assert c == snap


def test_harden_agy_repairs_missing_value():
    """A dangling `--print-timeout` (or one whose next token is another flag)
    leaves an invalid agy invocation — repair it with the default value
    (reviewer: Codex R1)."""
    m = _load_module()
    dangling = {"timeout_seconds": 600, "additional_args": ["--print-timeout"]}
    m._harden_cli_client("agy", dangling)
    assert dangling["additional_args"] == ["--print-timeout", "1200s"]
    assert dangling["timeout_seconds"] == m.AGY_TIMEOUT_BACKSTOP_SECONDS
    # value-shaped-as-flag: the real value is missing → insert the default before it
    nextflag = {"timeout_seconds": 600,
                "additional_args": ["--print-timeout", "--add-dir", "/w"]}
    m._harden_cli_client("agy", nextflag)
    assert nextflag["additional_args"] == ["--print-timeout", "1200s", "--add-dir", "/w"]
    # idempotent on the repaired form
    snap = list(nextflag["additional_args"])
    m._harden_cli_client("agy", nextflag)
    assert nextflag["additional_args"] == snap


def test_harden_agy_raises_on_equal_wrapper():
    """When timeout_seconds == the effective print-timeout the wrapper must be
    raised strictly above it (reviewers: Codex Blocker + Antigravity R1)."""
    m = _load_module()
    c = {"timeout_seconds": 1200, "additional_args": ["--print-timeout", "1200s"]}
    m._harden_cli_client("agy", c)
    assert c["timeout_seconds"] > 1200
    # and idempotent afterwards (no further raise)
    raised = c["timeout_seconds"]
    m._harden_cli_client("agy", c)
    assert c["timeout_seconds"] == raised


def test_harden_agy_zero_print_timeout_not_clobbered():
    """`--print-timeout 0s` (disable agy's internal timeout) is a valid Go
    duration; the `is None` fallback must NOT treat 0.0 as unparseable and bump
    the user's wrapper (reviewers: Codex Risk + Antigravity R1)."""
    m = _load_module()
    c = {"timeout_seconds": 1000, "additional_args": ["--print-timeout", "0s"]}
    m._harden_cli_client("agy", c)
    assert c["additional_args"] == ["--print-timeout", "0s"]   # value preserved
    assert c["timeout_seconds"] == 1000                        # wrapper left alone (1000 > 0)


def test_harden_agy_duplicate_flags_size_above_max():
    """Duplicate --print-timeout flags: Go flag parsing is last-wins, so the
    wrapper must sit above the LARGEST (effective) value, not the first
    occurrence — otherwise PAL kills agy before the value it actually honors
    (reviewer: Codex R2)."""
    m = _load_module()
    dup = {"timeout_seconds": 600,
           "additional_args": ["--print-timeout", "300s", "--print-timeout", "3000s"]}
    m._harden_cli_client("agy", dup)
    assert dup["additional_args"] == [
        "--print-timeout", "300s", "--print-timeout", "3000s"]   # both preserved
    assert dup["timeout_seconds"] > 3000     # above the max, not the first (300s)
    # mixed space + = forms, and idempotent
    mixed = {"timeout_seconds": 600,
             "additional_args": ["--print-timeout=300s", "--print-timeout", "3000s"]}
    m._harden_cli_client("agy", mixed)
    assert mixed["timeout_seconds"] > 3000
    snap = {"timeout_seconds": mixed["timeout_seconds"],
            "additional_args": list(mixed["additional_args"])}
    m._harden_cli_client("agy", mixed)
    assert mixed == snap


def test_harden_agy_repairs_unparseable_value():
    """A present but unparseable value (e.g. `forever`) would make agy error out,
    so heal it to the default rather than leave the invalid arg (reviewer:
    Codex R3). Both space and = forms."""
    m = _load_module()
    space = {"timeout_seconds": 600, "additional_args": ["--print-timeout", "forever"]}
    m._harden_cli_client("agy", space)
    assert space["additional_args"] == ["--print-timeout", "1200s"]   # repaired
    assert space["timeout_seconds"] == m.AGY_TIMEOUT_BACKSTOP_SECONDS
    eqform = {"timeout_seconds": 600, "additional_args": ["--print-timeout=nonsense"]}
    m._harden_cli_client("agy", eqform)
    assert eqform["additional_args"] == ["--print-timeout=1200s"]     # repaired
    # a partial/half-parseable value ('1200sbad') is invalid to Go → also repaired
    partial = {"timeout_seconds": 600, "additional_args": ["--print-timeout", "1200sbad"]}
    m._harden_cli_client("agy", partial)
    assert partial["additional_args"] == ["--print-timeout", "1200s"]
    # idempotent after repair
    snap = list(space["additional_args"])
    m._harden_cli_client("agy", space)
    assert space["additional_args"] == snap


def test_harden_agy_bare_zero_preserved():
    """A bare `--print-timeout 0` is a VALID Go zero duration (disable) and must
    not be clobbered by the unparseable-repair path — the parser now recognizes
    bare '0'."""
    m = _load_module()
    c = {"timeout_seconds": 1000, "additional_args": ["--print-timeout", "0"]}
    m._harden_cli_client("agy", c)
    assert c["additional_args"] == ["--print-timeout", "0"]   # preserved, not repaired
    assert c["timeout_seconds"] == 1000                       # wrapper left alone (1000 > 0)


def test_harden_heals_stale_agy_config():
    m = _load_module()
    # The exact stale shape every already-provisioned server has: empty args
    # (so agy uses its own 5m0s print-timeout) and the old shared 600s wrapper,
    # which is BELOW the print-timeout we inject — so it must be raised.
    stale = {"timeout_seconds": 600, "additional_args": []}
    m._harden_cli_client("agy", stale)
    # print-timeout injected...
    assert m.AGY_PRINT_TIMEOUT_FLAG in stale["additional_args"]
    idx = stale["additional_args"].index(m.AGY_PRINT_TIMEOUT_FLAG)
    assert stale["additional_args"][idx + 1] == m.AGY_PRINT_TIMEOUT
    # ...and the wrapper raised strictly above the print-timeout
    assert stale["timeout_seconds"] == m.AGY_TIMEOUT_BACKSTOP_SECONDS
    assert stale["timeout_seconds"] > m._parse_go_duration_seconds(m.AGY_PRINT_TIMEOUT)

    # idempotent: a second pass must NOT duplicate the flag or move the wrapper
    snapshot = {"timeout_seconds": stale["timeout_seconds"],
                "additional_args": list(stale["additional_args"])}
    m._harden_cli_client("agy", stale)
    assert stale == snapshot
    assert stale["additional_args"].count(m.AGY_PRINT_TIMEOUT_FLAG) == 1


def test_harden_agy_preserves_user_print_timeout():
    m = _load_module()
    # A user-chosen --print-timeout is never clobbered, and user flags survive.
    custom = {"timeout_seconds": 600,
              "additional_args": ["--add-dir", "/w", "--print-timeout", "2000s"]}
    m._harden_cli_client("agy", custom)
    assert custom["additional_args"] == ["--add-dir", "/w", "--print-timeout", "2000s"]
    # wrapper is lifted above the *user's* larger print-timeout, not just the default
    assert custom["timeout_seconds"] > 2000
    # already-safe wrapper (above the injected default) is left untouched
    safe = {"timeout_seconds": 1500, "additional_args": []}
    m._harden_cli_client("agy", safe)
    assert safe["timeout_seconds"] == 1500
    assert safe["additional_args"] == [m.AGY_PRINT_TIMEOUT_FLAG, m.AGY_PRINT_TIMEOUT]

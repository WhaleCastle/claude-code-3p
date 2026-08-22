#!/usr/bin/env python3
"""3p — three-party review skill helper. Subcommand-based CLI."""
import sys
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess as _sp
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl  # POSIX
    _IS_POSIX = True
except ImportError:
    _IS_POSIX = False
    import msvcrt

import re as _re_validation

_RUN_ID_RE = _re_validation.compile(r"^[a-z0-9][a-z0-9-]*-\d{8}-\d{4}$")


def validate_run_id(run_id: str) -> None:
    """Reject anything that isn't a well-formed run id to prevent path traversal."""
    if not _RUN_ID_RE.match(run_id):
        raise SystemExit(
            f"Invalid run_id: {run_id!r}. Expected format: <slug>-<YYYYMMDD>-<HHMM> "
            f"where slug uses only [a-z0-9-]."
        )


# --- Timing helpers --------------------------------------------------------
# Wall-clock is recorded as ISO-8601 UTC strings on a per-run `timeline` event
# log (see cmd_init / cmd_state_write / cmd_mark). All readers use .get with
# defaults so runs predating the timeline keys never crash.
def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(s):
    """Parse an ISO-8601 timestamp (tolerating a trailing 'Z'); None on failure."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_dur(seconds) -> str:
    """Human-readable duration, e.g. '1h 02m 03s', '3m 05s', '12s'. '—' if unknown."""
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    if seconds < 0:
        seconds = 0
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


HARDCODED_SECRET_PATTERNS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "**/.aws/credentials",
    "**/.aws/config",
    ".npmrc",
    ".netrc",
    "secrets.*",
    "**/credentials.json",
]

DEFAULT_BLOAT_EXCLUDES = [
    "node_modules/",
    "__pycache__/",
    ".venv/",
    "venv/",
    ".tox/",
    "dist/",
    "build/",
    "target/",
    ".next/",
    ".nuxt/",
    ".cache/",
    "*.log",
    "*.pyc",
    ".DS_Store",
]

DEFAULTS = {
    "timeoutSeconds": 120,
    "roundCap": 10,
    "consecutiveFailuresForDowngrade": 3,
    "modelPower": "high",
    "models": {
        # models[reviewer][power][reviewType]. Codex uses the same model for
        # both review types (the user only differentiated Antigravity); the
        # schema is kept uniform so role resolution is a single code path.
        "codex": {
            "high": {"reasoning": "gpt-5.5", "code": "gpt-5.5"},
            "low": {"reasoning": "gpt-5.4-mini", "code": "gpt-5.4-mini"},
        },
        "antigravity": {
            "high": {
                "reasoning": "Gemini 3.1 Pro (High)",
                "code": "Gemini 3.5 Flash (High)",
            },
            "low": {
                "reasoning": "Gemini 3.1 Pro (Low)",
                "code": "Gemini 3.5 Flash (Low)",
            },
        },
    },
    "excludes": list(DEFAULT_BLOAT_EXCLUDES),
    "secretPatterns": list(HARDCODED_SECRET_PATTERNS),
}

MODEL_POWERS = {"low", "high"}
MODEL_REVIEWERS = {"codex", "antigravity"}
REVIEW_TYPES = {"reasoning", "code"}
# A 3p reviewer key -> the PAL clink cli_name (and ~/.pal/cli_clients/<name>.json
# filename). PAL only supports a fixed set of cli names (codex, gemini, claude,
# agy) because the name binds the output parser, so the Antigravity reviewer
# talks to PAL as `agy` even though 3p presents it as "Antigravity".
REVIEWER_CLI = {"codex": "codex", "antigravity": "agy"}
# Human-facing display name for each reviewer key.
REVIEWER_LABEL = {"codex": "Codex", "antigravity": "Antigravity"}
PAL_RESTART_MESSAGE = (
    "Restart Claude Code so PAL MCP reloads reviewer roles. "
    "If you run PAL MCP as a separate process, restart that process instead."
)


def ensure_string_list(value, key: str) -> list:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise SystemExit(f"Invalid {key}: expected an array of strings.")
    return list(value)


def normalize_config(cfg: dict) -> dict:
    """Validate and fill derived defaults for the persisted user config."""
    power = cfg.get("modelPower", DEFAULTS["modelPower"])
    if power not in MODEL_POWERS:
        raise SystemExit(
            f"Invalid modelPower: {power!r}. Expected one of: low, high."
        )
    cfg["modelPower"] = power

    models = cfg.get("models")
    if not isinstance(models, dict):
        models = {}
    normalized_models = json.loads(json.dumps(DEFAULTS["models"]))
    for reviewer in MODEL_REVIEWERS:
        reviewer_models = models.get(reviewer)
        if not isinstance(reviewer_models, dict):
            continue
        for pwr in MODEL_POWERS:
            value = reviewer_models.get(pwr)
            if value is None:
                continue
            # Legacy flat shape {power: "model"} -> {reasoning: m, code: m}.
            if isinstance(value, str):
                if not value.strip():
                    raise SystemExit(
                        f"Invalid models.{reviewer}.{pwr}: expected a non-empty string."
                    )
                model = value.strip()
                normalized_models[reviewer][pwr] = {
                    "reasoning": model, "code": model,
                }
                continue
            if not isinstance(value, dict):
                raise SystemExit(
                    f"Invalid models.{reviewer}.{pwr}: expected a string or "
                    f"a {{reasoning, code}} object."
                )
            for rtype in REVIEW_TYPES:
                rvalue = value.get(rtype)
                if rvalue is None:
                    continue
                if not isinstance(rvalue, str) or not rvalue.strip():
                    raise SystemExit(
                        f"Invalid models.{reviewer}.{pwr}.{rtype}: "
                        f"expected a non-empty string."
                    )
                normalized_models[reviewer][pwr][rtype] = rvalue.strip()
    cfg["models"] = normalized_models
    return cfg


def _merge_models(dst: dict, src) -> None:
    """Deep-merge a `models` block slot by slot.

    Whole-dict replacement would make a project config that names one slot
    silently drop the machine-wide value for its siblings (they would fall back
    to DEFAULTS, not to the layer below). Merging per
    reviewer/power/reviewType keeps each layer authoritative only for the slots
    it actually names. Shapes this does not understand are passed through
    untouched so normalize_config stays the single validator.
    """
    if not isinstance(src, dict):
        return
    for reviewer, powers in src.items():
        if not isinstance(powers, dict):
            dst[reviewer] = powers
            continue
        dst_reviewer = dst.get(reviewer)
        if not isinstance(dst_reviewer, dict):
            dst_reviewer = dst[reviewer] = {}
        for power, value in powers.items():
            if isinstance(value, str):
                # Legacy flat shape {power: "model"} overrides both reviewTypes.
                dst_reviewer[power] = {"reasoning": value, "code": value}
            elif isinstance(value, dict):
                dst_power = dst_reviewer.get(power)
                if isinstance(dst_power, str):
                    dst_power = {"reasoning": dst_power, "code": dst_power}
                elif not isinstance(dst_power, dict):
                    dst_power = {}
                dst_power.update(value)
                dst_reviewer[power] = dst_power
            else:
                dst_reviewer[power] = value


def _merge_config_file(cfg: dict, file_path: Path) -> None:
    """Apply one config layer onto cfg in place. Missing files are a no-op."""
    if not file_path.exists():
        return
    try:
        file_cfg = json.loads(file_path.read_text())
    except json.JSONDecodeError as e:
        print(f"Warning: ignoring malformed {file_path}: {e}", file=sys.stderr)
        return
    if not isinstance(file_cfg, dict):
        print(f"Warning: ignoring {file_path}: expected a JSON object.",
              file=sys.stderr)
        return
    for k, v in file_cfg.items():
        if k == "excludes":
            cfg["excludes"] = ensure_string_list(v, "excludes")  # replace defaults
        elif k == "extraExcludes":
            for x in ensure_string_list(v, "extraExcludes"):
                if x not in cfg["excludes"]:
                    cfg["excludes"].append(x)
        elif k == "secretPatterns":
            merged = list(HARDCODED_SECRET_PATTERNS)
            for x in ensure_string_list(v, "secretPatterns"):
                if x not in merged:
                    merged.append(x)
            cfg["secretPatterns"] = merged
        elif k == "models":
            _merge_models(cfg.setdefault("models", {}), v)
        else:
            cfg[k] = v


def load_config(anchor: Path, config_path=None, cli_excludes=None) -> dict:
    """Merge defaults <- machine config <- project config file <- CLI flags.
    - The machine-wide config (see user_config_path) applies in every repo, so
      reviewer models can be set once per machine instead of per anchor.
    - `excludes` in a config file REPLACES defaults (user-overridable bloat list).
    - `extraExcludes` in a config file APPENDS to defaults.
    - CLI `--exclude` flags always APPEND on top.
    - Secret patterns are NEVER overridable.
    """
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    project_path = config_path or (anchor / ".3p" / "config.json")
    layers = [user_config_path(), project_path]
    seen = set()
    for file_path in layers:
        try:
            key = file_path.resolve()
        except OSError:
            key = file_path
        if key in seen:  # $THREEP_USER_CONFIG pointed at the project file
            continue
        seen.add(key)
        _merge_config_file(cfg, file_path)
    if cli_excludes:
        for x in cli_excludes:
            if x not in cfg["excludes"]:
                cfg["excludes"].append(x)
    for p in HARDCODED_SECRET_PATTERNS:
        if p not in cfg["secretPatterns"]:
            cfg["secretPatterns"].append(p)
    return normalize_config(cfg)


USER_CONFIG_ENV = "THREEP_USER_CONFIG"


def user_config_path() -> Path:
    """Machine-wide config, applied under every project's .3p/config.json.

    Project config is anchored to the git root, so without this layer a setting
    like the reviewer model map would have to be repeated in every repo. Point
    $THREEP_USER_CONFIG elsewhere to relocate it (tests rely on this).
    """
    override = os.environ.get(USER_CONFIG_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "3p" / "config.json"


def read_user_config() -> dict:
    path = user_config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"Malformed {path}: {e}") from e


def write_user_config(data: dict) -> None:
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data)


def project_config_path(anchor: Path) -> Path:
    return anchor / ".3p" / "config.json"


def read_project_config(anchor: Path) -> dict:
    path = project_config_path(anchor)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"Malformed {path}: {e}") from e


def write_project_config(anchor: Path, data: dict) -> None:
    path = project_config_path(anchor)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data)


def reviewer_role_name(power: str, review_type: str) -> str:
    if power not in MODEL_POWERS:
        raise SystemExit(f"Invalid model power: {power!r}")
    if review_type not in REVIEW_TYPES:
        raise SystemExit(f"Invalid review type: {review_type!r}")
    return f"codereviewer-{power}-{review_type}"


def stable_model_role_name(power: str, reviewer: str, review_type: str,
                           model_name: str) -> str:
    if power not in MODEL_POWERS or reviewer not in MODEL_REVIEWERS:
        raise SystemExit(f"Invalid reviewer/model power: {reviewer!r}/{power!r}")
    if review_type not in REVIEW_TYPES:
        raise SystemExit(f"Invalid review type: {review_type!r}")
    digest = hashlib.sha256(model_name.encode("utf-8")).hexdigest()[:10]
    return f"codereviewer-{power}-{review_type}-{digest}"


def parse_config_flags(args: list, *, usage: str):
    config_path = None
    cli_excludes = []
    i = 0
    while i < len(args):
        if args[i] == "--config":
            if i + 1 >= len(args):
                print(usage, file=sys.stderr)
                return None, None, 2
            config_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--exclude":
            if i + 1 >= len(args):
                print(usage, file=sys.stderr)
                return None, None, 2
            cli_excludes.append(args[i + 1])
            i += 2
        else:
            print(f"Unknown flag: {args[i]}", file=sys.stderr)
            return None, None, 2
    return config_path, cli_excludes, 0


def find_anchor():
    """Return (anchor_dir, is_git). Walks up to find .git, else CWD."""
    cwd = Path.cwd()
    cur = cwd
    while cur != cur.parent:
        if (cur / ".git").exists():
            return cur, True
        cur = cur.parent
    return cwd, False


def run_dir_path(anchor: Path, run_id: str) -> Path:
    validate_run_id(run_id)
    base = (anchor / ".3p").resolve()
    candidate = (anchor / ".3p" / run_id).resolve()
    if candidate != base and base not in candidate.parents:
        raise SystemExit(
            f"run_id {run_id!r} resolves outside the .3p/ directory. Aborting."
        )
    return anchor / ".3p" / run_id


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def read_state(run_dir: Path) -> dict:
    return json.loads((run_dir / "state.json").read_text())


def write_state(run_dir: Path, state: dict) -> None:
    atomic_write_json(run_dir / "state.json", state)


@contextlib.contextmanager
def state_lock(run_dir: Path):
    lock_path = run_dir / ".state.lock"
    lock_path.touch(exist_ok=True)
    f = open(lock_path, "r+")
    try:
        if _IS_POSIX:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        else:
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        if _IS_POSIX:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        else:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        f.close()


def mutate_state(run_dir: Path, mutator) -> None:
    with state_lock(run_dir):
        state = read_state(run_dir)
        mutator(state)
        write_state(run_dir, state)


def append_availability_log(run_dir: Path, entry: dict) -> None:
    def _mutator(state):
        state.setdefault("availabilityLog", []).append(entry)
    mutate_state(run_dir, _mutator)


def cmd_init(args: list) -> int:
    if len(args) < 2:
        print("Usage: 3p.py init <slug> <timestamp> [--config <p>] [--exclude <pat>]...",
              file=sys.stderr)
        return 2
    slug, ts = args[0], args[1]
    config_path, cli_excludes, status = parse_config_flags(
        args[2:],
        usage="Usage: 3p.py init <slug> <timestamp> [--config <p>] [--exclude <pat>]...",
    )
    if status:
        return status
    run_id = f"{slug}-{ts}"
    anchor, is_git = find_anchor()
    if is_git:
        verify_git_ref_format(f"refs/3p/{run_id}/pre-build")
    run_dir = run_dir_path(anchor, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "baselines").mkdir(exist_ok=True)
    resolved_cfg = load_config(anchor, config_path, cli_excludes)
    now = _now_iso()
    state = {
        "taskSlug": slug,
        "taskDir": str(run_dir),
        "repoRoot": str(anchor) if is_git else None,
        "cwdAnchor": str(anchor) if not is_git else None,
        "gitMode": is_git,
        "phase": "plan",
        "currentStep": None,
        "currentScope": None,
        "currentRound": 0,
        "reviewerHealth": {
            "codex": {"lastStatus": None, "consecutiveFailures": 0},
            "antigravity": {"lastStatus": None, "consecutiveFailures": 0},
        },
        "consecutiveBothDownRounds": 0,
        "downgradeMode": None,
        "baselines": {},
        "pausedReason": None,
        "resolvedConfig": resolved_cfg,
        "availabilityLog": [],
        # Interface/observability layer (additive). northStar: one-line goal the
        # dashboard and reviewers anchor to. alignment: latest per-phase drift
        # self-check. ledger: stable-id findings + pushback-rebuttal log; all
        # readers use .get/.setdefault so runs predating these keys never crash.
        "northStar": None,
        "alignment": {"status": "unknown", "note": "", "checkedAtPhase": None},
        "ledger": {"nextId": 1, "findings": [], "rebuttals": []},
        # Timing layer (additive). startedAt anchors the run wall-clock; timeline
        # is an append-only event log ({ts, kind, label}). Phase transitions are
        # stamped automatically by state-write; test brackets and any other
        # granular events go through `mark`. cmd_summary rolls these into the
        # end-of-run Timing section.
        "startedAt": now,
        # Seed the plan-phase event here: init sets phase="plan", so Phase A's
        # `state-write phase "plan"` is a no-op that would never stamp the timeline
        # — without this seed, plan-phase wall-clock would collapse into build.
        "timeline": [
            {"ts": now, "kind": "run-start", "label": None},
            {"ts": now, "kind": "phase", "label": "plan"},
        ],
    }
    write_state(run_dir, state)
    if is_git:
        gi = anchor / ".gitignore"
        existing = gi.read_text() if gi.exists() else ""
        if ".3p/" not in existing.splitlines():
            sep = "" if existing.endswith("\n") or existing == "" else "\n"
            gi.write_text(existing + sep + ".3p/\n")
    print(run_id)
    return 0


def cmd_state_read(args: list) -> int:
    if len(args) != 2:
        print("Usage: 3p.py state-read <run-id> <key>", file=sys.stderr)
        return 2
    run_id, key = args
    anchor, _ = find_anchor()
    state = read_state(run_dir_path(anchor, run_id))
    val = state.get(key)
    if isinstance(val, (dict, list)):
        print(json.dumps(val))
    else:
        print(val)
    return 0


def cmd_state_write(args: list) -> int:
    if len(args) != 3:
        print("Usage: 3p.py state-write <run-id> <key> <value-json>", file=sys.stderr)
        return 2
    run_id, key, value_json = args
    value = json.loads(value_json)
    anchor, _ = find_anchor()
    run_dir = run_dir_path(anchor, run_id)

    def _mutator(s):
        # Auto-stamp phase transitions onto the timeline so the summary can derive
        # per-phase wall-clock without the skill having to record it explicitly.
        # Only stamp on an actual change so resume (which re-writes the same phase)
        # doesn't inject spurious zero-length segments.
        if key == "phase" and s.get("phase") != value:
            s.setdefault("timeline", []).append(
                {"ts": _now_iso(), "kind": "phase", "label": value}
            )
        s[key] = value

    mutate_state(run_dir, _mutator)
    return 0


def cmd_mark(args: list) -> int:
    """Append a timestamped event to the run timeline: mark <run-id> <kind> [label].
    Used to bracket granular work (e.g. test-start/test-end) that isn't a phase
    transition, so cmd_summary can report time spent in it."""
    if len(args) < 2 or len(args) > 3:
        print("Usage: 3p.py mark <run-id> <kind> [label]", file=sys.stderr)
        return 2
    run_id, kind = args[0], args[1]
    label = args[2] if len(args) == 3 else None
    anchor, _ = find_anchor()
    run_dir = run_dir_path(anchor, run_id)
    mutate_state(
        run_dir,
        lambda s: s.setdefault("timeline", []).append(
            {"ts": _now_iso(), "kind": kind, "label": label}
        ),
    )
    return 0


def cmd_availability_append(args: list) -> int:
    if len(args) != 2:
        print("Usage: 3p.py availability-append <run-id> <entry-json>", file=sys.stderr)
        return 2
    run_id, entry_json = args
    entry = json.loads(entry_json)
    anchor, _ = find_anchor()
    append_availability_log(run_dir_path(anchor, run_id), entry)
    return 0


def cmd_config_load(args: list) -> int:
    config_path, cli_excludes, status = parse_config_flags(
        args,
        usage="Usage: 3p.py config-load [--config <p>] [--exclude <pat>]...",
    )
    if status:
        return status
    anchor = Path.cwd()
    cfg = load_config(anchor, config_path, cli_excludes)
    print(json.dumps(cfg, indent=2))
    return 0


def cmd_model_power(args: list) -> int:
    if len(args) > 1:
        print("Usage: 3p.py model-power [low|high]", file=sys.stderr)
        return 2
    anchor, _ = find_anchor()
    if not args:
        cfg = load_config(anchor)
        print(cfg["modelPower"])
        return 0
    power = args[0]
    if power not in MODEL_POWERS:
        print("Usage: 3p.py model-power [low|high]", file=sys.stderr)
        return 2
    raw = read_project_config(anchor)
    raw["modelPower"] = power
    normalize_config(json.loads(json.dumps({**DEFAULTS, **raw})))
    write_project_config(anchor, raw)
    print(power)
    return 0


# --- reviewer model discovery (`models available`) -------------------------
# Bounded discovery of the models each reviewer CLI currently offers, used by
# the `/3p models` interactive picker. Fail-soft per reviewer: a missing,
# hung, failing, or garbled CLI becomes a per-reviewer error entry — never a
# traceback — so one broken CLI can't hide the other's catalog.

MODELS_CLI_TIMEOUT_ENV = "THREEP_MODELS_CLI_TIMEOUT"
MODELS_CLI_TIMEOUT_DEFAULT = 30.0
CLAUDE_MODEL_WARNING = ("Claude-family model — picking it undercuts 3p's "
                        "cross-vendor reviewer independence")


def _models_cli_timeout() -> float:
    try:
        value = float(os.environ.get(MODELS_CLI_TIMEOUT_ENV, ""))
    except ValueError:
        return MODELS_CLI_TIMEOUT_DEFAULT
    return value if value > 0 else MODELS_CLI_TIMEOUT_DEFAULT


def _run_discovery_cli(argv: list):
    """Run a reviewer CLI for discovery. Returns (stdout, error): exactly one
    is None. Never raises for the expected failure modes (missing binary,
    timeout, non-zero exit)."""
    timeout = _models_cli_timeout()
    try:
        proc = _sp.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, f"{argv[0]}: command not found (is the CLI installed?)"
    except _sp.TimeoutExpired:
        return None, f"{' '.join(argv)}: timed out after {timeout:g}s"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        suffix = f": {detail[0]}" if detail else ""
        return None, f"{' '.join(argv)}: exit {proc.returncode}{suffix}"
    return proc.stdout, None


def _discover_codex_models() -> dict:
    """`codex debug models` renders the raw model catalog as JSON. Keep only
    entries listed in the picker UI (visibility == "list"); this also drops
    internal entries like codex-auto-review. The catalog embeds each model's
    base instructions (~262KB total) so only the distilled fields are kept."""
    source = "codex debug models"
    stdout, err = _run_discovery_cli(["codex", "debug", "models"])
    if err:
        return {"source": source, "status": "error", "error": err, "models": []}
    try:
        models = []
        for entry in json.loads(stdout)["models"]:
            if entry.get("visibility") != "list":
                continue
            levels = [lvl.get("effort")
                      for lvl in entry.get("supported_reasoning_levels", [])
                      if isinstance(lvl, dict) and lvl.get("effort")]
            models.append({
                "id": entry["slug"],
                "displayName": entry.get("display_name"),
                "reasoningLevels": levels,
                "warning": None,
            })
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
        return {"source": source, "status": "error",
                "error": f"{source}: unparseable output ({type(exc).__name__})",
                "models": []}
    return {"source": source, "status": "ok", "models": models}


def _discover_agy_models() -> dict:
    """`agy models` prints one model per line as `<id>\t<display name>` (e.g.
    `gemini-3.1-pro-high\tGemini 3.1 Pro (High)`). The id embeds the reasoning
    tier, which maps onto 3p's high/low powers. Split on the first whitespace
    run so both the tab-delimited form and a bare-id line parse correctly —
    model ids never contain whitespace, display names usually do. The display
    name is optional; `agy --model` accepts either spelling.
    Claude-family ids are annotated (not excluded): 3p's value comes from
    cross-vendor review, and a Claude reviewer would review Claude's work."""
    source = "agy models"
    stdout, err = _run_discovery_cli(["agy", "models"])
    if err:
        return {"source": source, "status": "error", "error": err, "models": []}
    models = []
    for line in stdout.splitlines():
        parts = line.split(None, 1)
        if not parts:
            continue
        mid = parts[0]
        display = parts[1].strip() if len(parts) > 1 else ""
        models.append({
            "id": mid,
            "displayName": display or None,
            "reasoningLevels": [],
            "warning": (CLAUDE_MODEL_WARNING
                        if mid.lower().startswith("claude") else None),
        })
    if not models:
        return {"source": source, "status": "error",
                "error": f"{source}: no models in output", "models": []}
    return {"source": source, "status": "ok", "models": models}


def cmd_models(args: list) -> int:
    anchor, _ = find_anchor()
    if not args or args == ["list"]:
        cfg = load_config(anchor)
        print(json.dumps(cfg["models"], indent=2))
        return 0
    if args == ["available"]:
        cfg = load_config(anchor)
        reviewers = {
            "codex": _discover_codex_models(),
            "antigravity": _discover_agy_models(),
        }
        print(json.dumps({"reviewers": reviewers, "current": cfg["models"]},
                         indent=2))
        return 0 if any(r["status"] == "ok" for r in reviewers.values()) else 1
    if args and args[0] == "set":
        set_args = [a for a in args[1:] if a not in ("--global", "-g")]
        is_global = len(set_args) != len(args) - 1
        if len(set_args) != 4:
            print("Usage: 3p.py models set [--global] <codex|antigravity> "
                  "<low|high> <reasoning|code> <model>", file=sys.stderr)
            return 2
        reviewer, power, review_type, model_name = set_args
        if (reviewer not in MODEL_REVIEWERS or power not in MODEL_POWERS
                or review_type not in REVIEW_TYPES or not model_name.strip()):
            print("Usage: 3p.py models set [--global] <codex|antigravity> "
                  "<low|high> <reasoning|code> <model>", file=sys.stderr)
            return 2
        raw = read_user_config() if is_global else read_project_config(anchor)
        if not isinstance(raw.get("models", {}), dict):
            raw["models"] = {}
        raw_models = raw.setdefault("models", {})
        if not isinstance(raw_models.get(reviewer, {}), dict):
            raw_models[reviewer] = {}
        raw_reviewer = raw_models.setdefault(reviewer, {})
        existing = raw_reviewer.get(power)
        if isinstance(existing, str):
            # Up-convert a legacy flat value {power: "model"} into both review
            # types first, so overriding one review type does not silently drop
            # the user's model for the sibling review type.
            raw_reviewer[power] = {"reasoning": existing, "code": existing}
        elif not isinstance(existing, dict):
            raw_reviewer[power] = {}
        raw_power = raw_reviewer[power]
        raw_power[review_type] = model_name.strip()
        normalize_config(json.loads(json.dumps({**DEFAULTS, **raw})))
        if is_global:
            write_user_config(raw)
        else:
            write_project_config(anchor, raw)
        install_pal_config(load_config(anchor))
        # First line is the stable machine-readable contract; scope goes after.
        print(f"{reviewer}.{power}.{review_type}={model_name.strip()}")
        if is_global:
            print(f"scope: machine-wide ({user_config_path()}) — applies in every repo")
        else:
            print(f"scope: project ({project_config_path(anchor)})")
            if user_config_path().exists():
                print("note: overrides the machine-wide value under this anchor "
                      "only; use --global to change it everywhere.")
        print(PAL_RESTART_MESSAGE)
        return 0
    print("""Usage: 3p.py models [list]
       3p.py models available
       3p.py models set [--global] <codex|antigravity> <low|high> <reasoning|code> <model>

--global writes the machine-wide config (%s) instead of
this project's .3p/config.json, so the models apply in every repo.""" % user_config_path(),
          file=sys.stderr)
    return 2


def cmd_reviewer_role(args: list) -> int:
    usage = "Usage: 3p.py reviewer-role <run-id> <codex|antigravity> <reasoning|code>"
    if len(args) != 3:
        print(usage, file=sys.stderr)
        return 2
    run_id, reviewer, review_type = args
    if reviewer not in MODEL_REVIEWERS or review_type not in REVIEW_TYPES:
        print(usage, file=sys.stderr)
        return 2
    anchor, _ = find_anchor()
    state = read_state(run_dir_path(anchor, run_id))
    cfg = normalize_config(state.get("resolvedConfig", {}))
    power = cfg["modelPower"]
    model_name = cfg["models"][reviewer][power][review_type]
    install_pal_config(cfg)
    print(stable_model_role_name(power, reviewer, review_type, model_name))
    return 0


# PAL enforces a per-client hard timeout (asyncio.wait_for) from the client
# JSON's `timeout_seconds`. When it is unset, PAL falls back to its own default
# (1800s / 30 min) — which is how a wedged reviewer `clink` call hangs for half
# an hour with no output. We stamp a much tighter backstop into every generated
# client so a hung reviewer is killed and reported as a timeout instead of
# looking frozen. Round-1 reviews here were 61s / 153s, so 600s is comfortably
# above a legitimate slow review while bounding the pathological case.
REVIEWER_TIMEOUT_BACKSTOP_SECONDS = 600

# agy (the Antigravity CLI) has its OWN internal `--print-timeout` flag whose
# default is 5m0s. 3p used to leave `additional_args` empty, so agy fell back to
# that 5-minute default and any review slower than ~5 min (large diff or a slow
# high-reasoning model) self-aborted with stderr `Error: timeout waiting for
# response` and exit 1 — a reviewer that only *looks* failed when it was just
# slow. PAL's outer `timeout_seconds` wrapper does NOT stop agy's own print-
# timeout (which fires first), so the intended 600s bound was effectively 300s.
# Fix: hand agy a larger print-timeout and put PAL's wrapper ABOVE it, so agy's
# own clean timeout fires first and PAL's harder kill is only the last resort.
# The values are a starting recommendation (a hang-vs-throughput trade-off) —
# tune them here. AGY_TIMEOUT_BACKSTOP_SECONDS must stay above AGY_PRINT_TIMEOUT
# so codex's tighter 600s bound is never weakened by a shared constant.
AGY_PRINT_TIMEOUT_FLAG = "--print-timeout"
AGY_PRINT_TIMEOUT = "1200s"           # agy's internal wait; overrides the 5m0s default
AGY_TIMEOUT_BACKSTOP_SECONDS = 1500   # PAL outer-wrapper bound for agy; sits ABOVE AGY_PRINT_TIMEOUT
AGY_WRAPPER_MARGIN_SECONDS = 300      # keep the wrapper at least this far above the print-timeout

DEFAULT_CLI_CLIENTS = {
    "codex": {
        "name": "codex",
        "command": "codex",
        # NOTE: the deprecated `--enable web_search_request` flag is intentionally
        # NOT here — web search is on by default and that pair injects an `error`
        # item into codex's `--json` stream that can trip PAL's parser (a suspected
        # cause of wedged calls). _harden_cli_client also strips it from existing
        # configs on install.
        "additional_args": [
            "--skip-git-repo-check",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
        ],
        "timeout_seconds": REVIEWER_TIMEOUT_BACKSTOP_SECONDS,
        "env": {},
        "roles": {
            "default": {
                "prompt_path": "systemprompts/clink/default.txt",
                "role_args": [],
            },
            "planner": {
                "prompt_path": "systemprompts/clink/default_planner.txt",
                "role_args": [],
            },
            "codereviewer": {
                "prompt_path": "systemprompts/clink/codex_codereviewer.txt",
                "role_args": [],
            },
        },
    },
    "antigravity": {
        # The Antigravity reviewer talks to PAL as `agy`. PAL's `agy` internal
        # defaults already inject `--dangerously-skip-permissions` for
        # non-interactive auto-approval, so it is deliberately NOT repeated here
        # (it would be passed twice). additional_args carries `--print-timeout`
        # (agy's own default is 5m0s, too low for large diffs) — users may add
        # more `agy` flags (e.g. --add-dir) and install_pal_config preserves them.
        "name": "agy",
        "command": "agy",
        "additional_args": [AGY_PRINT_TIMEOUT_FLAG, AGY_PRINT_TIMEOUT],
        "timeout_seconds": AGY_TIMEOUT_BACKSTOP_SECONDS,
        "env": {},
        "roles": {
            "default": {
                "prompt_path": "systemprompts/clink/default.txt",
                "role_args": [],
            },
            "planner": {
                "prompt_path": "systemprompts/clink/default_planner.txt",
                "role_args": [],
            },
            "codereviewer": {
                "prompt_path": "systemprompts/clink/default_codereviewer.txt",
                "role_args": [],
            },
        },
    },
}


def cli_client_path(reviewer: str) -> Path:
    """PAL cli_clients file for a 3p reviewer key (antigravity -> agy.json)."""
    cli_name = REVIEWER_CLI[reviewer]
    return Path.home() / ".pal" / "cli_clients" / f"{cli_name}.json"


def load_cli_client_config(reviewer: str) -> dict:
    path = cli_client_path(reviewer)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise SystemExit(f"Malformed PAL CLI config {path}: {e}") from e
    return json.loads(json.dumps(DEFAULT_CLI_CLIENTS[reviewer]))


def write_cli_client_config(reviewer: str, data: dict) -> None:
    path = cli_client_path(reviewer)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data)


def _strip_flag_pair(args: list, flag: str, value: str) -> list:
    """Return args with every adjacent [flag, value] pair removed."""
    out = []
    i = 0
    while i < len(args):
        if args[i] == flag and i + 1 < len(args) and args[i + 1] == value:
            i += 2
            continue
        out.append(args[i])
        i += 1
    return out


_DURATION_UNIT_SECONDS = {
    "h": 3600, "m": 60, "s": 1, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9,
}
# One number+unit token. Multi-char units first so 'ms'/'us'/'ns' win over the
# single-char 'm'/'s' in the alternation — otherwise '500ms' would parse as 500
# *minutes*.
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|us|µs|ns|h|m|s)")
# A valid Go duration is one-or-more such tokens back-to-back and nothing else.
# Used to reject partial/garbage values like '1200sbad', 'foo1200s', or
# '1h 30m' that findall() alone would happily half-match into a bogus number.
_DURATION_FULL_RE = re.compile(r"(?:\d+(?:\.\d+)?(?:ms|us|µs|ns|h|m|s))+")


def _parse_go_duration_seconds(value):
    """Parse a Go-style duration string ('1200s', '5m0s', '20m', '1h30m',
    '500ms') to a number of seconds. The WHOLE string must be a valid duration
    (Go semantics): a partial/garbage value like '1200sbad' returns None, not a
    half-parse. Returns None when unparseable so callers can tell it apart from a
    real 0/0s (which parse to 0.0 — a falsy but valid value)."""
    if not isinstance(value, str):
        return None
    if value == "0":                       # Go accepts a bare "0" (no unit) as zero
        return 0.0
    if not _DURATION_FULL_RE.fullmatch(value):
        return None
    return sum(float(num) * _DURATION_UNIT_SECONDS[unit]
               for num, unit in _DURATION_RE.findall(value))


def _ensure_agy_print_timeout(args: list) -> float:
    """Ensure `args` carries at least one well-formed agy `--print-timeout` and
    return the effective print-timeout in SECONDS that PAL's wrapper must sit
    above — the LARGEST parseable value across ALL occurrences. Go flag parsing
    is last-wins, but sizing above the max is safe whichever duplicate agy
    honors, so a stray second flag can't leave the wrapper below the value agy
    actually uses. Handles both `--print-timeout X` and `--print-timeout=X`;
    preserves user values; repairs a dangling or flag-shaped (missing) value
    with the default; appends the flag when none is present. Mutates `args` in
    place. Idempotent: well-formed flag+value token(s) are left untouched."""
    flag = AGY_PRINT_TIMEOUT_FLAG
    eq_prefix = flag + "="
    raws = []            # every print-timeout value string found (post-repair)
    found = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == flag:
            found = True
            nxt = args[i + 1] if i + 1 < len(args) else None
            # Repair with the default when the value is absent (a bare
            # `--print-timeout`, or a following token that is itself a flag) OR
            # present but unparseable (e.g. `forever`) — either way agy would
            # error out on it, so the healed config must not carry it.
            if isinstance(nxt, str) and not nxt.startswith("-") \
                    and _parse_go_duration_seconds(nxt) is not None:
                raws.append(nxt)
            elif isinstance(nxt, str) and not nxt.startswith("-"):
                args[i + 1] = AGY_PRINT_TIMEOUT           # present but unparseable → replace
                raws.append(AGY_PRINT_TIMEOUT)
            else:
                args[i + 1:i + 1] = [AGY_PRINT_TIMEOUT]   # missing → insert default
                raws.append(AGY_PRINT_TIMEOUT)
            i += 2
            continue
        if isinstance(a, str) and a.startswith(eq_prefix):
            found = True
            val = a[len(eq_prefix):]
            if not val or _parse_go_duration_seconds(val) is None:
                args[i] = eq_prefix + AGY_PRINT_TIMEOUT   # bare/unparseable '=value' → repair
                val = AGY_PRINT_TIMEOUT
            raws.append(val)
        i += 1
    if not found:
        args += [flag, AGY_PRINT_TIMEOUT]
        raws.append(AGY_PRINT_TIMEOUT)
    parsed = [s for s in (_parse_go_duration_seconds(r) for r in raws) if s is not None]
    # Nothing parseable (all user values were garbage) → fall back to the default.
    return max(parsed) if parsed else _parse_go_duration_seconds(AGY_PRINT_TIMEOUT)


def _harden_cli_client(cli_name: str, client: dict) -> None:
    """Heal a (possibly stale) reviewer client config in place so it can't hang.

    Idempotent and preserves user customizations. Applied on every install so
    configs generated before these safeguards existed get upgraded:
      - stamp a bounded `timeout_seconds` when unset (None/0 → PAL's 1800s default,
        the 30-min hang) — a user-chosen positive value is respected.
      - for codex, strip the deprecated `--enable web_search_request` flag pair.
      - for agy, inject `--print-timeout` when absent (agy's own 5m0s default
        self-aborts long reviews) and raise `timeout_seconds` above the print-
        timeout so PAL's wrapper is the outer bound and agy's clean timeout fires
        first. A user-chosen `--print-timeout` value is never clobbered.
    """
    if not isinstance(client.get("timeout_seconds"), (int, float)) or not client.get("timeout_seconds"):
        client["timeout_seconds"] = REVIEWER_TIMEOUT_BACKSTOP_SECONDS
    if cli_name == "codex":
        args = client.get("additional_args") or []
        client["additional_args"] = _strip_flag_pair(args, "--enable", "web_search_request")
    elif cli_name == "agy":
        args = list(client.get("additional_args") or [])
        # Effective print-timeout in seconds (largest across all occurrences);
        # the helper also repairs/appends the flag in place.
        print_secs = _ensure_agy_print_timeout(args)
        client["additional_args"] = args
        # PAL's outer wrapper must outlast agy's own print-timeout so agy self-
        # aborts cleanly first. Raise on equality (`<=`) so the wrapper sits
        # STRICTLY above the print-timeout instead of racing it.
        current = client.get("timeout_seconds")
        if not isinstance(current, (int, float)) or current <= print_secs:
            client["timeout_seconds"] = max(
                AGY_TIMEOUT_BACKSTOP_SECONDS, int(print_secs) + AGY_WRAPPER_MARGIN_SECONDS)


def install_pal_config(cfg: dict) -> None:
    for reviewer in sorted(MODEL_REVIEWERS):
        cli_name = REVIEWER_CLI[reviewer]
        client = load_cli_client_config(reviewer)
        # PAL binds its output parser to the cli `name`, which must be one of
        # PAL's supported names — so write the PAL cli_name, not the 3p key.
        client["name"] = cli_name
        client.setdefault("command", cli_name)
        client.setdefault("additional_args", [])
        client.setdefault("env", {})
        roles = client.setdefault("roles", {})
        base_role = roles.get("codereviewer") or DEFAULT_CLI_CLIENTS[reviewer]["roles"]["codereviewer"]
        prompt_path = base_role.get("prompt_path") or DEFAULT_CLI_CLIENTS[reviewer]["roles"]["codereviewer"]["prompt_path"]
        for power in sorted(MODEL_POWERS):
            for review_type in sorted(REVIEW_TYPES):
                model_name = cfg["models"][reviewer][power][review_type]
                role = {
                    "prompt_path": prompt_path,
                    "role_args": ["--model", model_name],
                }
                roles[reviewer_role_name(power, review_type)] = role
                roles[stable_model_role_name(
                    power, reviewer, review_type, model_name)] = role
        _harden_cli_client(cli_name, client)
        write_cli_client_config(reviewer, client)


def cmd_pal_config(args: list) -> int:
    if args != ["install"]:
        print("Usage: 3p.py pal-config install", file=sys.stderr)
        return 2
    anchor, _ = find_anchor()
    install_pal_config(load_config(anchor))
    print("installed PAL codereviewer-low/codereviewer-high roles")
    print(PAL_RESTART_MESSAGE)
    return 0


def cmd_update(args: list) -> int:
    if args:
        print("Usage: 3p.py update", file=sys.stderr)
        return 2
    skill_root = Path(__file__).resolve().parents[1]
    meta_path = skill_root / "install.json"
    source = skill_root
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            if meta.get("source"):
                source = Path(meta["source"]).expanduser()
        except json.JSONDecodeError:
            pass
    if not (source / ".git").exists():
        print(f"Cannot auto-update: {source} is not a git checkout.", file=sys.stderr)
        return 1
    for command in (["git", "fetch", "--quiet"], ["git", "pull", "--ff-only"], ["./install.sh"]):
        result = _sp.run(command, cwd=source, capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stdout, end="")
            print(result.stderr, end="", file=sys.stderr)
            return result.returncode
    print("updated 3p skill")
    print(PAL_RESTART_MESSAGE)
    return 0


USAGE = """\
Usage: 3p.py <subcommand> [args...]

Subcommands:
  slug <task-description>
  init <slug> <timestamp> [--config <p>] [--exclude <pat>]...
  config-load
  model-power [low|high]
  models [list]
  models available
  models set [--global] <codex|antigravity> <low|high> <reasoning|code> <model>
  reviewer-role <run-id> <codex|antigravity> <reasoning|code>
  pal-config install
  update
  state-read <run-id> <key>
  state-write <run-id> <key> <value-json>
  mark <run-id> <kind> [label]
  availability-append <run-id> <entry-json>
  snapshot capture <run-id> <key>
  snapshot diff <run-id> <key>
  parse-response <file>
  round-write <run-id> <phase> <step|-> <round> <reviewer> <verdicts-json>
  round-close <run-id>
  phase-end <run-id>
  dashboard <run-id> [--stdout]
  hud <run-id>
  summary <run-id>
  consolidate-final <run-id>
  list
  clean <run-id>
"""


def cmd_slug(args: list) -> int:
    if len(args) != 1:
        print("Usage: 3p.py slug <task-description>", file=sys.stderr)
        return 2
    task = args[0]
    slug = task.lower()
    # whitespace -> dashes
    slug = re.sub(r"\s+", "-", slug)
    # strip any char not [a-z0-9-]
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    # collapse consecutive dashes
    slug = re.sub(r"-+", "-", slug)
    # trim edges
    slug = slug.strip("-")
    # cap at 50 chars, trim any new trailing dash
    if len(slug) > 50:
        slug = slug[:50].rstrip("-")
    # remove '..' (defensive)
    slug = slug.replace("..", "-")
    # never start with '.'
    if slug.startswith("."):
        slug = slug.lstrip(".")
    # empty -> hash fallback
    if not slug:
        slug = hashlib.sha256(task.encode("utf-8")).hexdigest()[:8]
    print(slug)
    return 0


def verify_git_ref_format(ref_path: str) -> None:
    """Spec mandate: verify constructed git ref passes git check-ref-format
    before any git update-ref call. Aborts with a clear error if invalid.
    Caller must handle non-git mode (skip this check)."""
    result = _sp.run(
        ["git", "check-ref-format", ref_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"git check-ref-format rejected ref {ref_path!r}: "
            f"{result.stderr.strip() or 'invalid'}. "
            f"This is a spec-mandated safety check before any git update-ref call. "
            f"Aborting to avoid corrupting git refs."
        )


ALWAYS_EXCLUDED_DIRS = {".3p", ".git"}


def live_path_allowed(anchor: Path, rel_path: str) -> bool:
    path = anchor / rel_path
    try:
        resolved = path.resolve()
        anchor_resolved = anchor.resolve()
    except OSError:
        return False
    return resolved == anchor_resolved or anchor_resolved in resolved.parents


def pattern_matches(rel_path: str, pattern: str) -> bool:
    """fnmatch-based matcher supporting trailing `/` for dir-only,
    `**` for any depth, leading `/` for anchored-at-root."""
    rel = rel_path.replace(os.sep, "/")
    if pattern.startswith("/"):
        anchored = True
        pattern = pattern[1:]
    else:
        anchored = False
    if pattern.endswith("/"):
        p = pattern.rstrip("/")
        if anchored:
            return rel == p or rel.startswith(p + "/")
        segments = rel.split("/")
        if p in segments[:-1]:
            return True
        if rel == p or rel.startswith(p + "/"):
            return True
        return False
    if "**" in pattern:
        # Also test the bare suffix (after "**/") at root level so that
        # e.g. "**/.aws/credentials" matches the root-level ".aws/credentials".
        if pattern.startswith("**/"):
            suffix = pattern[3:]
            if pattern_matches(rel_path, suffix):
                return True
        regex = fnmatch.translate(pattern)
        return re.match(regex, rel) is not None
    if anchored:
        return fnmatch.fnmatch(rel, pattern)
    base = rel.rsplit("/", 1)[-1]
    return fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(base, pattern)


def should_exclude(rel_path: str, patterns: list) -> bool:
    return any(pattern_matches(rel_path, p) for p in patterns)


def collect_gitignore_sources(anchor: Path, is_git: bool) -> list:
    """Collect all gitignore-format ignore sources at capture time:
    - Every `.gitignore` in the working tree (scoped to their containing dir)
    - `.git/info/exclude` (repo-scoped)
    - Global `core.excludesFile` (repo-scoped)

    Each entry: `{kind: "gitignore"|"info-exclude"|"global", dir: "<scope-dir-or-empty>", content: <text>}`.

    The `dir` field is the path RELATIVE TO ANCHOR where the source's patterns apply
    (empty string = repo-wide). Diff-time logic uses this to scope nested rules
    correctly: a nested `.gitignore` at `pkg/foo/.gitignore` only applies to paths
    under `pkg/foo/`.
    """
    if not is_git:
        return []
    sources = []
    # Every nested .gitignore in the tree
    for root, dirs, files in os.walk(anchor):
        # Skip .git and .3p — never recurse into them
        if ".git" in dirs:
            dirs.remove(".git")
        if ".3p" in dirs:
            dirs.remove(".3p")
        for f in files:
            if f == ".gitignore":
                rel_dir = os.path.relpath(root, anchor)
                full = Path(root) / f
                try:
                    sources.append({
                        "kind": "gitignore",
                        "dir": "" if rel_dir == "." else rel_dir.replace(os.sep, "/"),
                        "content": full.read_text(),
                    })
                except OSError:
                    pass
    # .git/info/exclude
    info_exclude = anchor / ".git" / "info" / "exclude"
    if info_exclude.exists():
        try:
            sources.append({
                "kind": "info-exclude",
                "dir": "",
                "content": info_exclude.read_text(),
            })
        except OSError:
            pass
    # Global excludesFile (from `git config core.excludesFile`)
    try:
        r = _sp.run(
            ["git", "config", "--get", "core.excludesFile"],
            cwd=anchor, capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            raw = os.path.expanduser(r.stdout.strip())
            # Git accepts relative paths for core.excludesFile; resolve them
            # against the repo anchor (not the process CWD) so the source is
            # captured correctly regardless of where the user invoked from.
            global_path = Path(raw) if os.path.isabs(raw) else (anchor / raw)
            if global_path.exists():
                sources.append({
                    "kind": "global",
                    "dir": "",
                    "content": global_path.read_text(),
                })
    except (OSError, _sp.CalledProcessError):
        pass
    return sources


def _parse_source_rules(content: str) -> list:
    """Parse gitignore-format content into ordered (negate, pattern) tuples.
    Same syntax as `gitignore_rules`, just from arbitrary content text."""
    rules = []
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("!"):
            rules.append((True, s[1:]))
        else:
            rules.append((False, s))
    return rules


def rel_path_excluded_by_sources(rel_path: str, sources: list) -> bool:
    """Apply each captured ignore source to rel_path with directory scoping.

    For each source, if `dir` is non-empty, the source applies ONLY to paths
    at or below that directory. The path is matched against the source's
    patterns relative to that directory. Returns True if any rule excludes
    the path (last-match-wins within a source; later sources override earlier
    ones for the same path).
    """
    rel = rel_path.replace(os.sep, "/")
    excluded = False
    for src in sources:
        src_dir = src.get("dir", "")
        if src_dir:
            scope_prefix = src_dir.rstrip("/") + "/"
            if not (rel == src_dir or rel.startswith(scope_prefix)):
                continue
            scoped_rel = rel[len(scope_prefix):] if rel.startswith(scope_prefix) else rel
        else:
            scoped_rel = rel
        for negate, pattern in _parse_source_rules(src["content"]):
            if pattern_matches(scoped_rel, pattern):
                excluded = not negate
    return excluded


def gitignore_rules(anchor: Path):
    """Parse anchor `.gitignore` into ordered (negate, pattern) tuples.
    Best-effort: handles comments, blanks, negations, trailing /."""
    gi = anchor / ".gitignore"
    if not gi.exists():
        return []
    rules = []
    for line in gi.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("!"):
            rules.append((True, s[1:]))
        else:
            rules.append((False, s))
    return rules


def gitignore_excludes(rel_path: str, rules) -> bool:
    """Apply ordered .gitignore rules; True if excluded."""
    excluded = False
    for negate, pattern in rules:
        if pattern_matches(rel_path, pattern):
            excluded = not negate
    return excluded


def enumerate_files_git(anchor: Path, user_excludes: list, secret_patterns: list):
    result = _sp.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=anchor, capture_output=True, check=True,
    )
    paths = [p for p in result.stdout.decode("utf-8").split("\x00") if p]
    out = []
    for rel in paths:
        path = anchor / rel
        if path.is_symlink() or not live_path_allowed(anchor, rel):
            continue
        top = rel.split("/", 1)[0]
        if top in ALWAYS_EXCLUDED_DIRS:
            continue
        if should_exclude(rel, secret_patterns):
            continue
        if should_exclude(rel, user_excludes):
            continue
        out.append(rel)
    return out


def enumerate_files_nongit(anchor: Path, user_excludes: list,
                           secret_patterns: list, gi_rules):
    """Walk filtered by ALWAYS_EXCLUDED_DIRS; prune safely-excluded dirs
    when no negation rule could match a descendant; collect-then-filter
    per file so negations can re-include below pruned trees."""
    negation_prefixes = [pat for negate, pat in gi_rules if negate]

    def has_negation_descendant(rel_dir: str) -> bool:
        prefix = rel_dir.rstrip("/") + "/"
        for npat in negation_prefixes:
            n = npat.lstrip("/").rstrip("/")
            # exact dir match or a path under this dir
            if n == rel_dir or n.startswith(prefix):
                return True
            # negation pattern itself starts with this dir (file inside)
            if n.startswith(rel_dir + "/"):
                return True
            if "**" in npat:
                return True
        return False

    candidates = []
    for root, dirs, files in os.walk(anchor):
        rel_root = os.path.relpath(root, anchor)
        if rel_root == ".":
            rel_root = ""
        new_dirs = []
        for d in dirs:
            if d in ALWAYS_EXCLUDED_DIRS:
                continue
            full_dir = Path(root) / d
            if full_dir.is_symlink():
                continue
            rel_d = f"{rel_root}/{d}" if rel_root else d
            positively_excluded = (
                should_exclude(rel_d + "/", secret_patterns)
                or should_exclude(rel_d + "/", user_excludes)
                or any(
                    pattern_matches(rel_d + "/", pat)
                    for negate, pat in gi_rules if not negate
                )
            )
            if positively_excluded and not has_negation_descendant(rel_d):
                continue
            new_dirs.append(d)
        dirs[:] = new_dirs
        for f in files:
            full_file = Path(root) / f
            if full_file.is_symlink():
                continue
            rel = f"{rel_root}/{f}" if rel_root else f
            candidates.append(rel)
    out = []
    for rel in candidates:
        if not live_path_allowed(anchor, rel):
            continue
        top = rel.split("/", 1)[0]
        if top in ALWAYS_EXCLUDED_DIRS:
            continue
        if should_exclude(rel, secret_patterns):
            continue
        # Apply gitignore rules; if a negation explicitly re-includes this file,
        # skip the user_excludes check so negations can override bloat defaults.
        gi_excluded = False
        gi_negated = False
        for negate, pattern in gi_rules:
            if pattern_matches(rel, pattern):
                if negate:
                    gi_negated = True
                    gi_excluded = False
                else:
                    gi_negated = False
                    gi_excluded = True
        if gi_excluded:
            continue
        # Only apply user excludes when gitignore did NOT explicitly negate this file
        if not gi_negated and should_exclude(rel, user_excludes):
            continue
        out.append(rel)
    return out


def enumerate_files(anchor: Path, user_excludes: list, secret_patterns: list,
                    gi_rules, is_git: bool):
    if is_git:
        base = enumerate_files_git(anchor, user_excludes, secret_patterns)
        if gi_rules:
            base = [f for f in base if not gitignore_excludes(f, gi_rules)]
        return base
    return enumerate_files_nongit(anchor, user_excludes, secret_patterns, gi_rules)


_FINDING_HEADER = re.compile(
    r"^\s*\*{0,2}\[(Blocker|Critical|Important|Risk)\]\*{0,2}\s+(.+?)\s*$",
    re.M,
)


# Caps that keep a bloated reviewer response (e.g. an agent CLI that echoes its
# entire chain-of-thought / tool-call transcript around a one-line verdict) from
# polluting round files and parsed output. The structured verdict is *extracted*
# (findings headers / APPROVED token), and each field is bounded; only a
# genuinely unparseable response keeps raw text, and even that is windowed.
_MAX_FIELD_CHARS = 2000
_MAX_RAW_CHARS = 8000


def _truncate(s: str, limit: int = _MAX_FIELD_CHARS) -> str:
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + f" …[truncated {len(s) - limit} chars]"


def _cap_raw(text: str, limit: int = _MAX_RAW_CHARS) -> str:
    """Window an unparseable response to head+tail. An agent CLI's actual verdict
    usually lands at the very end (after its transcript), so keep both ends rather
    than chopping the tail and losing the conclusion."""
    if len(text) <= limit:
        return text
    half = limit // 2
    elided = len(text) - 2 * half
    return f"{text[:half]}\n…[{elided} chars elided]…\n{text[-half:]}"


def _extract_field(block: str, name: str) -> str:
    m = re.search(rf"^{name}:\s*(.+?)(?:\n[A-Z][a-z]+:|\Z)", block, re.M | re.S)
    return m.group(1).strip() if m else ""


def parse_response(text: str) -> dict:
    findings = []
    matches = list(_FINDING_HEADER.finditer(text))
    for i, m in enumerate(matches):
        severity = m.group(1)
        title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end]
        # Each field is bounded: a trailing transcript with no further field
        # label would otherwise let the last finding's Rationale absorb it all.
        findings.append({
            "severity": severity,
            "title": _truncate(title),
            "location": _truncate(_extract_field(block, "Location")),
            "issue": _truncate(_extract_field(block, "Issue")),
            "rationale": _truncate(_extract_field(block, "Rationale")),
        })
    if findings:
        return {"status": "findings", "findings": findings}
    if re.search(r"^APPROVED\s*$", text, re.M):
        return {"status": "approved", "findings": []}
    return {"status": "unavailable", "raw": _cap_raw(text), "findings": []}


def cmd_parse_response(args: list) -> int:
    if len(args) != 1:
        print("Usage: 3p.py parse-response <file>", file=sys.stderr)
        return 2
    text = Path(args[0]).read_text()
    print(json.dumps(parse_response(text), indent=2))
    return 0


def cmd_snapshot(args: list) -> int:
    if len(args) < 1:
        print("Usage: 3p.py snapshot {capture|diff} ...", file=sys.stderr)
        return 2
    sub = args[0]
    if sub == "capture":
        return cmd_snapshot_capture(args[1:])
    if sub == "diff":
        return cmd_snapshot_diff(args[1:])
    print(f"Unknown snapshot subcommand: {sub}", file=sys.stderr)
    return 2


def cmd_snapshot_diff(args: list) -> int:
    """Symmetric per-file diff. Snapshot side uses persisted fileManifest
    (stable across mid-task .gitignore changes). Live side uses
    capturedGitignoreRules + capturedIgnoredPaths (also stable). Secret
    patterns enforced at diff time as a non-overridable safety net."""
    if len(args) != 2:
        print("Usage: 3p.py snapshot diff <run-id> <key>", file=sys.stderr)
        return 2
    run_id, key = args
    anchor, is_git = find_anchor()
    run_dir = run_dir_path(anchor, run_id)
    state = read_state(run_dir)
    baseline_meta = state["baselines"][key]
    snap_path = Path(baseline_meta["path"])
    cfg = state["resolvedConfig"]
    if "fileManifest" in baseline_meta:
        snap_files = set(baseline_meta["fileManifest"])
    else:
        gi_rules_legacy = gitignore_rules(anchor)
        snap_files = set(enumerate_files_nongit(
            snap_path, cfg["excludes"], cfg["secretPatterns"], gi_rules_legacy
        ))
    captured_gi = [tuple(t) for t in baseline_meta.get("capturedGitignoreRules", [])]
    if captured_gi:
        gi_rules = captured_gi
    else:
        gi_rules = gitignore_rules(anchor)
    # Use nongit filesystem walk for the live side so that mid-task .gitignore
    # changes don't silently drop newly-created files. Captured rules give
    # symmetric filtering against snapshot-time state.
    live_files = set(enumerate_files_nongit(
        anchor, cfg["excludes"], cfg["secretPatterns"], gi_rules
    ))
    captured_ignored = set(baseline_meta.get("capturedIgnoredPaths", []))
    if captured_ignored:
        live_files -= captured_ignored
    # Apply captured-time non-root ignore sources (nested .gitignore, .git/info/exclude,
    # global excludesFile) so newly-created files matching those frozen rules stay out
    # of the live side. This restores full ignore-stack symmetry that root-only
    # capturedGitignoreRules cannot provide.
    captured_sources = baseline_meta.get("capturedIgnoreSources", [])
    if captured_sources:
        live_files = {f for f in live_files
                      if not rel_path_excluded_by_sources(f, captured_sources)}
    snap_files = {p for p in snap_files
                  if not should_exclude(p, cfg["secretPatterns"])}
    union = sorted(snap_files | live_files)
    out_lines = []
    for rel in union:
        snap_file = snap_path / rel
        live_file = anchor / rel
        if live_file.is_symlink() or not live_path_allowed(anchor, rel):
            continue
        snap_exists = snap_file.exists()
        live_exists = live_file.exists()
        if snap_exists and live_exists:
            # Quick equality check to avoid spawning diff for unchanged files.
            if snap_file.stat().st_size == live_file.stat().st_size:
                if snap_file.read_bytes() == live_file.read_bytes():
                    continue  # identical, skip diff entirely
            r = _sp.run(["diff", "-u", str(snap_file), str(live_file)],
                        capture_output=True, text=True)
            if r.stdout:
                out_lines.append(f"diff -ruN {snap_file} {live_file}\n")
                out_lines.append(r.stdout)
        elif live_exists:
            r = _sp.run(["diff", "-uN", "/dev/null", str(live_file)],
                        capture_output=True, text=True)
            out_lines.append(f"Only in {anchor}: {rel}\n")
            if r.stdout:
                out_lines.append(r.stdout)
        elif snap_exists:
            r = _sp.run(["diff", "-uN", str(snap_file), "/dev/null"],
                        capture_output=True, text=True)
            out_lines.append(f"Only in {snap_path}: {rel}\n")
            if r.stdout:
                out_lines.append(r.stdout)
    # Enumerate live-tree paths that matched secret patterns and were excluded.
    # Warn so the user knows what was silently dropped from the diff.
    dropped_secrets = []
    for root, dirs, files in os.walk(anchor):
        rel_root = os.path.relpath(root, anchor)
        if rel_root == ".":
            rel_root = ""
        dirs[:] = [d for d in dirs if d not in ALWAYS_EXCLUDED_DIRS]
        for f in files:
            full_file = Path(root) / f
            if full_file.is_symlink():
                continue
            rel = f"{rel_root}/{f}" if rel_root else f
            if should_exclude(rel, cfg["secretPatterns"]):
                dropped_secrets.append(rel)
    dropped_secrets = sorted(set(dropped_secrets))
    if dropped_secrets:
        out_lines.append("\n# ============================================================\n")
        out_lines.append("# WARNING: the following paths matched hardcoded secret patterns\n")
        out_lines.append("# and were excluded from this diff. If these are legitimate files\n")
        out_lines.append("# you want reviewers to see, the secret-pattern list cannot be\n")
        out_lines.append("# disabled — consider renaming the files instead.\n")
        out_lines.append("# ============================================================\n")
        for p in dropped_secrets:
            out_lines.append(f"# skipped (secret pattern match): {p}\n")
    sys.stdout.write("".join(out_lines))
    return 0


def _parse_diff_header_paths(rest: str, snap_str: str, anchor_str: str) -> str:
    """Extract relative path from a 'diff -ruN PATH_A PATH_B' line,
    robust to spaces. PATH_A starts with snap_str or anchor_str.

    Strategy: since we KNOW both paths mirror the same relative path, they are
    structured as '<base_a>/<rel> <base_b>/<rel>'. We find the unique space
    that acts as separator by scanning for all occurrences of ' <base_b>' and
    choosing the one where rest[idx+1:] is exactly '<base_b>/<same_rel>'.
    """
    for base_a in (snap_str, anchor_str):
        if not rest.startswith(base_a + os.sep) and not rest.startswith(base_a + " "):
            continue
        if not rest.startswith(base_a):
            continue
        other = anchor_str if base_a == snap_str else snap_str
        needle = " " + other
        # Walk all occurrences of needle; for each, check that the remainder
        # starts with other + os.sep (or is exactly other) and that the
        # relative tails are equal — this pins the correct split.
        start = 0
        while True:
            idx = rest.find(needle, start)
            if idx == -1:
                break
            path_a = rest[:idx]
            path_b = rest[idx + 1:]
            # path_b must start with other followed by a separator or end
            if path_b == other or path_b.startswith(other + os.sep):
                rel_a = os.path.relpath(path_a, base_a)
                rel_b = os.path.relpath(path_b, other)
                # The two relative paths must agree (mirrored layout)
                if rel_a == rel_b:
                    return rel_a
            start = idx + 1
    return ""


def cmd_snapshot_capture(args: list) -> int:
    if len(args) != 2:
        print("Usage: 3p.py snapshot capture <run-id> <key>", file=sys.stderr)
        return 2
    run_id, key = args
    anchor, is_git = find_anchor()
    run_dir = run_dir_path(anchor, run_id)
    state = read_state(run_dir)
    cfg = state["resolvedConfig"]
    gi_rules = gitignore_rules(anchor)
    files = enumerate_files(
        anchor, cfg["excludes"], cfg["secretPatterns"], gi_rules, is_git
    )
    snap_dir = run_dir / "baselines" / key
    snap_dir.mkdir(parents=True, exist_ok=True)
    for rel in files:
        src = anchor / rel
        # Skip symlinks, disallowed paths, and tracked-but-deleted files (a
        # normal git state: `git status` "D" — the file is enumerated from the
        # index but absent on disk; copying it would raise FileNotFoundError).
        if src.is_symlink() or not src.is_file() or not live_path_allowed(anchor, rel):
            continue
        dst = snap_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    # Capture-time ignored paths (git mode only; honors full git ignore stack)
    captured_ignored = []
    if is_git:
        try:
            res = _sp.run(
                ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
                cwd=anchor, capture_output=True, check=True,
            )
            captured_ignored = [
                p for p in res.stdout.decode("utf-8").split("\x00") if p
            ]
        except _sp.CalledProcessError:
            captured_ignored = []
    baseline_entry = {
        "path": str(snap_dir),
        "fileManifest": sorted(files),
        "capturedGitignoreRules": [list(t) for t in gi_rules],
        "capturedIgnoredPaths": sorted(captured_ignored),
        "capturedIgnoreSources": collect_gitignore_sources(anchor, is_git),  # NEW
    }
    if is_git:
        ref = f"refs/3p/{run_id}/{key}"
        verify_git_ref_format(ref)
        try:
            sha = _sp.run(["git", "stash", "create", "-u"], cwd=anchor,
                          capture_output=True, text=True, check=True).stdout.strip()
            if sha:
                _sp.run(["git", "update-ref", ref, sha], cwd=anchor, check=True)
                baseline_entry["gitSha"] = sha
                baseline_entry["gitRef"] = ref
        except _sp.CalledProcessError:
            pass

    def _mutator(s):
        s["baselines"][key] = baseline_entry
    mutate_state(run_dir, _mutator)
    return 0


def round_filename(phase: str, step: str, rnd: int, reviewer: str) -> str:
    """Per-reviewer naming eliminates merge race."""
    if phase == "plan":
        return f"plan-round-{rnd}-{reviewer}.md"
    if phase == "build":
        return f"step-{step}-round-{rnd}-{reviewer}.md"
    if phase == "final":
        return f"final-round-{rnd}-{reviewer}.md"
    raise ValueError(f"Unknown phase: {phase}")


def render_reviewer_section(v: dict) -> str:
    out = [f"## {v['reviewer']}", "", f"_Duration: {v.get('durationSeconds', 0)}s_", ""]
    if v["status"] == "approved":
        out.append("**APPROVED**")
        out.append("")
        return _append_rebuttals(out, v) if v.get("rebuttals") else "\n".join(out)
    if v["status"] == "unavailable":
        out.append("**UNAVAILABLE** (raw response below)")
        out.append("")
        out.append("```")
        out.append(v.get("raw", "").strip())
        out.append("```")
        return "\n".join(out)
    for f in v["findings"]:
        out += [
            f"### [{f['severity']}] {f['title']}",
            f"- **Location:** {f['location']}",
            f"- **Issue:** {f['issue']}",
            f"- **Rationale:** {f['rationale']}",
            f"- **Claude's verdict:** `{f['verdict']}` — {f['verdictReason']}",
            "",
        ]
    return _append_rebuttals(out, v) if v.get("rebuttals") else "\n".join(out)


def _append_rebuttals(out: list, v: dict) -> str:
    if not v.get("rebuttals"):
        return "\n".join(out)
    out += ["### Rebuttal exchanges", ""]
    for r in v["rebuttals"]:
        out += [
            f"- **From round {r['originalRound']}** — _{r['originalTitle']}_",
            f"  - Claude's prior reason: {r['claudeReasonPrior']}",
            f"  - Reviewer pushback: {r['reviewerPushback']}",
            f"  - Claude's reconsideration: {r['claudeReasonNow']}",
            f"  - Outcome: **{r['outcome']}**",
            "",
        ]
    return "\n".join(out)


# The complete set of statuses render_reviewer_section knows how to render.
_VALID_STATUSES = ("approved", "findings", "unavailable")
# Per-finding fields the orchestrator must decide (hard error if absent).
_FINDING_REQUIRED = ("severity", "title", "verdict")
# Descriptive fields that default to "" when absent rather than crashing. These
# come from parse-response (location/issue/rationale) or are Claude's one-liner
# (verdictReason); a missing one should never abort a round-write.
_FINDING_OPTIONAL = ("location", "issue", "rationale", "verdictReason")
# Rebuttal-exchange fields are hand-assembled too (SKILL.md step 7), so they get
# the same treatment as findings: one required decision field, the rest default.
_REBUTTAL_REQUIRED = ("outcome",)
_REBUTTAL_OPTIONAL = ("originalRound", "originalTitle", "claudeReasonPrior",
                      "reviewerPushback", "claudeReasonNow")


class VerdictsError(ValueError):
    """Actionable, user-facing validation error for a hand-assembled
    verdicts-json. Caught in cmd_round_write and printed without a traceback."""


def _normalize_rebuttals(v) -> None:
    """Validate/default the rebuttals array so _append_rebuttals never raw-
    KeyErrors on a hand-assembled entry — the same failure class normalize_
    verdicts exists to eliminate for findings."""
    rebuttals = v.get("rebuttals")
    if rebuttals is None:
        return
    if not isinstance(rebuttals, list):
        raise VerdictsError("round-write: 'rebuttals' must be a JSON array")
    for i, r in enumerate(rebuttals):
        if not isinstance(r, dict):
            raise VerdictsError(f"round-write: rebuttals[{i}] must be a JSON object")
        for key in _REBUTTAL_REQUIRED:
            if not r.get(key):
                raise VerdictsError(
                    f"round-write: rebuttals[{i}] missing required field {key!r} "
                    f"(needs: {', '.join(_REBUTTAL_REQUIRED)})"
                )
        for key in _REBUTTAL_OPTIONAL:
            r.setdefault(key, "")


def normalize_verdicts(v, reviewer: str) -> dict:
    """Validate + fill the hand-assembled verdicts-JSON so round-write fails
    with an actionable VerdictsError (naming the missing field) instead of a raw
    KeyError/AssertionError traceback. The CLI `reviewer` arg is authoritative:
    a missing `reviewer` key is injected; a mismatched one is a clear error."""
    if not isinstance(v, dict):
        raise VerdictsError(
            f"round-write: verdicts-json must be a JSON object, got {type(v).__name__}"
        )
    got = v.get("reviewer")
    if got in (None, ""):
        v["reviewer"] = reviewer
    elif got != reviewer:
        raise VerdictsError(
            f"round-write: verdicts-json reviewer {got!r} != CLI arg {reviewer!r}"
        )
    # Rebuttals can ride along on approved OR findings status, so normalize them
    # before the approved/unavailable early-return below.
    _normalize_rebuttals(v)
    findings = v.get("findings")
    if findings is None:
        findings = []
    # Validate the type BEFORE coercion or status inference. Using `or []`
    # here would silently turn a malformed falsey non-list ({}, "", 0, False)
    # into an empty round, and an absent status would then be inferred as
    # "approved" — both hiding the assembly error this function exists to surface.
    if not isinstance(findings, list):
        raise VerdictsError("round-write: 'findings' must be a JSON array")
    if "status" not in v or not v["status"]:
        v["status"] = "findings" if findings else "approved"
    if v["status"] not in _VALID_STATUSES:
        raise VerdictsError(
            f"round-write: invalid status {v['status']!r} "
            f"(expected one of {', '.join(_VALID_STATUSES)})"
        )
    if v["status"] in ("approved", "unavailable"):
        # Findings on an approved/unavailable record are silently dropped by the
        # renderer, which loses real review data from the audit trail — reject it.
        if findings:
            raise VerdictsError(
                f"round-write: status {v['status']!r} must not carry findings "
                f"(got {len(findings)}); use status 'findings' to record them"
            )
        v["findings"] = findings
        return v
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            raise VerdictsError(f"round-write: findings[{i}] must be a JSON object")
        for key in _FINDING_REQUIRED:
            if not f.get(key):
                raise VerdictsError(
                    f"round-write: findings[{i}] missing required field {key!r} "
                    f"(needs: {', '.join(_FINDING_REQUIRED)}). For findings copied "
                    f"from parse-response, add 'verdict' and 'verdictReason'."
                )
        for key in _FINDING_OPTIONAL:
            f.setdefault(key, "")
    v["findings"] = findings
    return v


def scope_for(phase: str, step: str) -> str:
    """The canonical review-scope id used by the ledger and dashboard.
    plan -> 'plan'; build step N -> 'step-N'; final -> 'final'. Mirrors the
    round-file naming so a finding's scope lines up with currentScope."""
    if phase == "build":
        return f"step-{step}"
    if phase == "final":
        return "final"
    return "plan"


def _normalize_finding_key(location: str, title: str) -> str:
    """Dedup key for 're-raised same finding' detection. Lowercased, markdown-
    stripped, whitespace-collapsed location+title. Two rounds raising the same
    issue (even if the reviewer reworded asterisks/backticks/spacing) collapse to
    one ledger entry; a genuinely different title or location stays separate."""
    raw = f"{location or ''} {title or ''}"
    raw = raw.replace("*", "").replace("`", "").replace("_", "")
    return re.sub(r"\s+", " ", raw).strip().lower()


def _ledger_upsert(state: dict, scope: str, rnd: int, v: dict) -> None:
    """Fold one reviewer's round verdicts into state['ledger'] (idempotent per
    (scope, reviewer, normalized-finding) across rounds). Resilient to runs that
    predate the ledger via setdefault. No-op-safe for approved/unavailable
    rounds (no findings). Rebuttal outcomes are always recorded for pushback W/L."""
    ledger = state.setdefault("ledger", {"nextId": 1, "findings": [], "rebuttals": []})
    ledger.setdefault("nextId", 1)
    ledger.setdefault("findings", [])
    ledger.setdefault("rebuttals", [])
    reviewer = v.get("reviewer", "")
    duration = v.get("durationSeconds", 0)
    for f in v.get("findings", []) or []:
        key = _normalize_finding_key(f.get("location", ""), f.get("title", ""))
        entry = next(
            (e for e in ledger["findings"]
             if e.get("scope") == scope and e.get("reviewer") == reviewer
             and _normalize_finding_key(e.get("location", ""), e.get("title", "")) == key),
            None,
        )
        hist = {
            "round": rnd,
            "verdict": f.get("verdict", ""),
            "verdictReason": f.get("verdictReason", ""),
            "durationSeconds": duration,
        }
        if entry is None:
            ledger["findings"].append({
                "id": f"F-{ledger['nextId']:02d}",
                "scope": scope,
                "reviewer": reviewer,
                "severity": f.get("severity", ""),
                "title": f.get("title", ""),
                "location": f.get("location", ""),
                "status": f.get("verdict", ""),
                "firstRound": rnd,
                "lastRound": rnd,
                "history": [hist],
            })
            ledger["nextId"] += 1
        else:
            # Idempotent per round: re-running round-write for the same round
            # (retry / resume) must update that round's history item in place,
            # not append a duplicate that would overcount the review event.
            existing_hist = next(
                (h for h in entry["history"] if h.get("round") == rnd), None)
            if existing_hist is not None:
                existing_hist.update(hist)
            else:
                entry["history"].append(hist)
            entry["lastRound"] = max(entry.get("lastRound", rnd), rnd)
            entry["status"] = f.get("verdict", entry.get("status", ""))
            # Refresh descriptive fields to the latest wording.
            entry["severity"] = f.get("severity", entry.get("severity", ""))
            entry["title"] = f.get("title", entry.get("title", ""))
            entry["location"] = f.get("location", entry.get("location", ""))
    # Replace (not merely augment) this (scope, reviewer, round) contribution: a
    # re-run of round-write for the same round with a finding DROPPED — a
    # correction, or a later approved/empty write — must remove that finding's
    # round-`rnd` history so no phantom finding lingers in the ledger (final-phase
    # integration review). Findings keep their stable id and any earlier-round
    # history; an entry left with no history at all is dropped entirely.
    new_keys = {
        _normalize_finding_key(f.get("location", ""), f.get("title", ""))
        for f in v.get("findings", []) or []
    }
    for entry in list(ledger["findings"]):
        if (entry.get("scope") != scope or entry.get("reviewer") != reviewer
                or _normalize_finding_key(entry.get("location", ""),
                                          entry.get("title", "")) in new_keys):
            continue
        kept = [h for h in entry["history"] if h.get("round") != rnd]
        if len(kept) == len(entry["history"]):
            continue  # had no contribution at round rnd; leave it untouched
        if not kept:
            ledger["findings"].remove(entry)
        else:
            entry["history"] = kept
            entry["lastRound"] = max(h.get("round", 0) for h in kept)
            entry["status"] = max(
                kept, key=lambda h: h.get("round", 0)).get("verdict", entry.get("status", ""))
    rebuttals = v.get("rebuttals", []) or []
    if rebuttals:
        # Same idempotency guarantee for rebuttals: drop any already-recorded for
        # this (reviewer, scope, round) before re-appending this round's set.
        ledger["rebuttals"] = [
            r for r in ledger["rebuttals"]
            if not (r.get("reviewer") == reviewer and r.get("scope") == scope
                    and r.get("round") == rnd)
        ]
        for r in rebuttals:
            ledger["rebuttals"].append({
                "reviewer": reviewer,
                "scope": scope,
                "round": rnd,
                "originalTitle": r.get("originalTitle", ""),
                "outcome": r.get("outcome", ""),
            })


def cmd_round_write(args: list) -> int:
    if len(args) != 6:
        print("Usage: 3p.py round-write <run-id> <phase> <step|-> <round> <reviewer> <verdicts-json>",
              file=sys.stderr)
        return 2
    run_id, phase, step, rnd_s, reviewer, verdicts_json = args
    rnd = int(rnd_s)
    try:
        v = json.loads(verdicts_json)
    except json.JSONDecodeError as e:
        print(f"round-write: verdicts-json is not valid JSON: {e}", file=sys.stderr)
        return 2
    try:
        v = normalize_verdicts(v, reviewer)
    except VerdictsError as e:
        print(str(e), file=sys.stderr)
        return 2
    anchor, _ = find_anchor()
    run_dir = run_dir_path(anchor, run_id)
    path = run_dir / round_filename(phase, step, rnd, reviewer)
    header = (
        f"# {phase.title()} round {rnd}"
        + (f" — step {step}" if phase == "build" else "")
        + f" ({reviewer})\n"
    )
    section = render_reviewer_section(v)
    path.write_text(header + "\n" + section + "\n")
    # Fold this round's findings/rebuttals into the persistent ledger. Done after
    # the markdown write so a ledger hiccup never costs the audit-trail file; the
    # round file remains the source of truth and the ledger is a derived index.
    mutate_state(run_dir, lambda s: _ledger_upsert(s, scope_for(phase, step), rnd, v))
    return 0


# ---------------------------------------------------------------------------
# Interface/observability rendering (dashboard + scoreboard + ledger views).
# All helpers are pure reads of state.json + run dir; safe to call any time and
# defensive against runs that predate the ledger/alignment/northStar keys.
# ---------------------------------------------------------------------------

def _review_type_for_phase(phase: str) -> str:
    """Phase B (build) is code review; plan and final are reasoning review."""
    return "code" if phase == "build" else "reasoning"


def _availability_scope(entry: dict) -> str:
    """Map an availabilityLog entry to the same scope id the ledger uses."""
    ph = entry.get("phase", "")
    if ph == "build":
        return f"step-{entry.get('step')}"
    if ph == "final":
        return "final"
    return "plan"


def _responded_in_scope_since(reviewer: str, scope: str, min_round: int,
                              availability_log: list) -> bool:
    """True if this reviewer `responded` for the scope at a round >= min_round.
    Conflict detection uses min_round = the open finding's round so a stale
    earlier approval (which never saw the current revision) is not reported as a
    clean opposing review — it stays a coverage gap until the reviewer responds
    again for the newer round (step-2 R2)."""
    for a in availability_log or []:
        if (_availability_scope(a) == scope and a.get("reviewer") == reviewer
                and a.get("status") == "responded"):
            try:
                rnd = int(a.get("round", 0))
            except (TypeError, ValueError):
                rnd = 0
            if rnd >= min_round:
                return True
    return False


def _finding_is_open(entry: dict, availability_log: list) -> bool:
    """Timing-independent open test (plan-phase reviewer consensus, R1): a finding
    is open unless the SAME reviewer has `responded` in a LATER round for that
    scope (which re-reviewed the revised artifact and did not re-raise it). This
    does not compare against currentRound (incremented before reviewers reply) and
    keeps a finding open across rounds where its reviewer was unavailable."""
    scope = entry.get("scope")
    reviewer = entry.get("reviewer")
    last = entry.get("lastRound", 0)
    for a in availability_log or []:
        if (_availability_scope(a) == scope and a.get("reviewer") == reviewer
                and a.get("status") == "responded"):
            try:
                rnd = int(a.get("round", 0))
            except (TypeError, ValueError):
                rnd = 0
            if rnd > last:
                return False
    return True


def _scoreboard(state: dict) -> dict:
    """Per-reviewer aggregates derived from the ledger + availabilityLog.
    Latency comes from PAL's metadata.duration_seconds (recorded into the log),
    not Claude wall-clock (parallel tool calls are atomic — plan-phase R1)."""
    cfg = state.get("resolvedConfig", {}) or {}
    models = cfg.get("models", {}) or {}
    power = cfg.get("modelPower", "high")
    rt = _review_type_for_phase(state.get("phase", "plan"))
    ledger = state.get("ledger") or {}
    findings = ledger.get("findings", []) or []
    rebuttals = ledger.get("rebuttals", []) or []
    log = state.get("availabilityLog", []) or []
    res = {}
    for r in ("codex", "antigravity"):
        rf = [e for e in findings if e.get("reviewer") == r]
        durs = [a.get("durationSeconds", 0) for a in log
                if a.get("reviewer") == r and a.get("status") == "responded"
                and isinstance(a.get("durationSeconds"), (int, float))]
        last = [a for a in log if a.get("reviewer") == r]
        avail = "⚪ —"
        if last:
            avail = "🟢 up" if last[-1].get("status") == "responded" else "🔴 down"
        try:
            model = models.get(r, {}).get(power, {}).get(rt, "?")
        except AttributeError:
            model = "?"
        res[r] = {
            "model": model or "?",
            "avail": avail,
            "latency": f"{round(sum(durs) / len(durs))}s" if durs else "—",
            "raised": len(rf),
            "accepted": sum(1 for e in rf if e.get("status") == "accepted"),
            "rejected": sum(1 for e in rf if e.get("status") == "rejected"),
            "ignored": sum(1 for e in rf if e.get("status") == "ignored"),
            "win": sum(1 for x in rebuttals
                       if x.get("reviewer") == r and x.get("outcome") == "now-accepted"),
            "loss": sum(1 for x in rebuttals if x.get("reviewer") == r
                        and x.get("outcome") in ("withdrawn", "sustained")),
        }
    return res


def render_scoreboard_table(state: dict) -> list:
    sb = _scoreboard(state)
    lines = [
        "| Reviewer | Model | Now | Avg latency | Raised | ✓Acc | ✗Rej | –Ign | Pushback W/L |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in ("codex", "antigravity"):
        s = sb[r]
        lines.append(
            f"| {REVIEWER_LABEL[r]} | {s['model']} | {s['avail']} | {s['latency']} | "
            f"{s['raised']} | {s['accepted']} | {s['rejected']} | {s['ignored']} | "
            f"{s['win']}/{s['loss']} |"
        )
    return lines


def render_ledger_table(state: dict) -> list:
    ledger = state.get("ledger") or {}
    findings = ledger.get("findings", []) or []
    if not findings:
        return ["_No findings recorded._"]
    log = state.get("availabilityLog", []) or []
    lines = [
        "| ID | Scope | Sev | Title | Reviewer | Status | Open? | Rounds |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for e in findings:
        rounds = ",".join(str(h.get("round")) for h in e.get("history", []))
        # Surface open/closed explicitly so the summary (and dashboard) never hide
        # an unresolved finding behind a bare verdict like "accepted" — e.g. a
        # finding left open by a round-cap exit (final-phase integration review).
        openness = "🔴 open" if _finding_is_open(e, log) else "✅ closed"
        lines.append(
            f"| {e['id']} | {e.get('scope', '')} | {e.get('severity', '')} | "
            f"{e.get('title', '')} | {REVIEWER_LABEL.get(e.get('reviewer'), e.get('reviewer'))} | "
            f"{e.get('status', '')} | {openness} | {rounds} |"
        )
    return lines


def _compute_timing(state: dict) -> dict:
    """Roll the run timeline + availability log into wall-clock durations.

    Returns: {
      total,               # run wall-clock (startedAt → last event), seconds or None
      phases,              # {phase_label: seconds} for plan/build/final/... (done excluded)
      reviewWall,          # reviewer wall-clock, parallel-adjusted (max per round), seconds
      reviewRaw,           # aggregate reviewer-seconds across both reviewers, seconds
      testTotal,           # sum of test-start→test-end brackets, seconds
      perTest,             # [(label, seconds)] per bracketed test
    }
    All fields degrade gracefully to None/0/{} for runs predating the timeline.
    """
    tl = state.get("timeline") or []
    parsed = []
    for e in tl:
        t = _parse_iso(e.get("ts"))
        if t is not None:
            parsed.append((t, e.get("kind"), e.get("label")))
    parsed.sort(key=lambda x: x[0])

    started = _parse_iso(state.get("startedAt"))
    end_t = parsed[-1][0] if parsed else None
    if started is None and parsed:
        started = parsed[0][0]
    # If the run has NOT reached the terminal "done" phase (an early-stop, a
    # summary computed mid-run, or a summary generated before the done stamp is
    # written), extend the end to now so elapsed wall-clock — and the current
    # phase's duration — aren't truncated at the last recorded event. A finished
    # run (phase == "done") uses its recorded events verbatim, so re-reading a
    # completed run later stays stable/deterministic.
    if state.get("phase") != "done":
        now_t = _parse_iso(_now_iso())
        if now_t is not None:
            end_t = max(end_t, now_t) if end_t is not None else now_t
    total = (end_t - started).total_seconds() if (started and end_t) else None

    # Per-phase wall-clock from consecutive phase segments. A phase runs until the
    # next distinct phase transition (or the final event for the last one).
    phase_events = [(t, label) for (t, kind, label) in parsed if kind == "phase"]
    segments = []  # [(label, start_t)]
    for t, label in phase_events:
        if segments and segments[-1][0] == label:
            continue  # collapse repeats (e.g. resume re-writing the same phase)
        segments.append([label, t])
    # Anchor the first phase at the run start so phase time isn't lost to setup gap.
    if segments and started and started < segments[0][1]:
        segments[0][1] = started
    phases = {}
    for i, (label, t) in enumerate(segments):
        nxt = segments[i + 1][1] if i + 1 < len(segments) else end_t
        if nxt is None:
            continue
        dur = max(0.0, (nxt - t).total_seconds())
        phases[label] = phases.get(label, 0.0) + dur
    phases.pop("done", None)  # terminal marker, not a phase with meaningful duration

    # Review time from the availability log. Reviewers run in parallel within a
    # round, so wall-clock per round ≈ max of the two; reviewRaw sums both.
    log = state.get("availabilityLog", []) or []
    review_raw = 0.0
    round_max = {}
    for e in log:
        d = e.get("durationSeconds") or 0
        if not isinstance(d, (int, float)):
            d = 0
        review_raw += d
        # Coerce to str so an int vs. string discrepancy in a round/step field
        # (e.g. round logged as 1 for one reviewer and "1" for the other) can't
        # split one logical round into two keys and defeat the parallel max().
        key = (str(e.get("phase")), str(e.get("step")), str(e.get("round")))
        round_max[key] = max(round_max.get(key, 0), d)
    review_wall = sum(round_max.values())

    # Test brackets: pair test-start with the next test-end sharing its label.
    open_starts = {}
    test_total = 0.0
    per_test = []
    for t, kind, label in parsed:
        if kind == "test-start":
            open_starts[label] = t
        elif kind == "test-end":
            st = open_starts.pop(label, None)
            if st is not None:
                d = max(0.0, (t - st).total_seconds())
                test_total += d
                per_test.append((label, d))

    return {
        "total": total,
        "phases": phases,
        "reviewWall": review_wall,
        "reviewRaw": review_raw,
        "testTotal": test_total,
        "perTest": per_test,
    }


_PHASE_TIMING_LABELS = {
    "plan": "Phase A · Plan",
    "build": "Phase B · Build",
    "final": "Phase C · Final",
}


def render_timing_table(state: dict) -> list:
    """Markdown lines for the summary's Timing section."""
    tm = _compute_timing(state)
    lines = [
        "| Part | Wall-clock |",
        "|---|---|",
        f"| **Total (run wall-clock)** | {_fmt_dur(tm['total'])} |",
    ]
    phases = tm["phases"]
    for label in ("plan", "build", "final"):
        if label in phases:
            lines.append(f"| {_PHASE_TIMING_LABELS[label]} | {_fmt_dur(phases[label])} |")
    for label, dur in phases.items():
        if label not in _PHASE_TIMING_LABELS:
            lines.append(f"| Phase · {label} | {_fmt_dur(dur)} |")
    lines.append(
        f"| Review (reviewer wall-clock, parallel-adjusted) | {_fmt_dur(tm['reviewWall'])} |"
    )
    if tm["reviewRaw"] and round(tm["reviewRaw"]) != round(tm["reviewWall"]):
        lines.append(
            f"| Review (aggregate reviewer-seconds, both reviewers) | {_fmt_dur(tm['reviewRaw'])} |"
        )
    if tm["testTotal"] or tm["perTest"]:
        lines.append(f"| Test (build test commands) | {_fmt_dur(tm['testTotal'])} |")
        for label, dur in tm["perTest"]:
            lines.append(f"| &nbsp;&nbsp;↳ {label or 'test'} | {_fmt_dur(dur)} |")
    if tm["total"] is None:
        lines.append("")
        lines.append(
            "_Timing was not recorded for this run (it predates the timing layer)._"
        )
    return lines


def _plan_step_count(run_dir: Path):
    p = run_dir / "plan.md"
    if not p.exists():
        return "?"
    nums = re.findall(r"(?mi)^#+\s*Step\s+(\d+)\b", p.read_text())
    return max(int(n) for n in nums) if nums else "?"


def _phase_progress(state: dict, step_count) -> str:
    phase = state.get("phase", "plan")
    idx = (state.get("currentStep") or {}).get("index")
    plan_m = "✓" if phase in ("build", "final", "done") else ("▶" if phase == "plan" else "·")
    if phase == "build":
        build_m = f"▶ {idx if idx is not None else '?'}/{step_count}"
    elif phase in ("final", "done"):
        build_m = "✓"
    else:
        build_m = "·"
    final_m = "✓" if phase == "done" else ("▶" if phase == "final" else "·")
    return f"[Plan {plan_m}] → [Build {build_m}] → [Final {final_m}]"


def _align_badge(alignment: dict) -> str:
    a = alignment or {}
    status = a.get("status", "unknown")
    emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(status, "❔")
    note = a.get("note", "")
    cp = a.get("checkedAtPhase")
    tail = f" — {note}" if note else ""
    cpt = f"  _(checked: {cp})_" if cp else ""
    return f"{emoji} {status}{tail}{cpt}"


def _resolve_scope(state: dict) -> str:
    """The live review scope id. Falls back to the canonical scope for the phase
    so plan/final phases and legacy runs predating currentScope never render
    "Scope None" with empty open-findings/agreement sections."""
    scope = state.get("currentScope")
    if scope:
        return scope
    phase = state.get("phase", "plan")
    if phase == "final":
        return "final"
    if phase == "build":
        idx = (state.get("currentStep") or {}).get("index")
        return f"step-{idx}" if idx is not None else "plan"
    return "plan"


def _phase_label(state: dict, step_count) -> str:
    """Compact one-token phase descriptor for the HUD line."""
    phase = state.get("phase", "plan")
    if phase == "plan":
        return "Phase A Plan"
    if phase == "build":
        idx = (state.get("currentStep") or {}).get("index")
        idx = idx if idx is not None else "?"
        return f"Phase B step {idx}/{step_count}"
    if phase == "final":
        return "Phase C Final"
    return "Done"


def _last_latency(reviewer: str, availability_log: list) -> str:
    """Most-recent responded latency for a reviewer (the HUD shows 'now', not avg)."""
    for a in reversed(availability_log or []):
        if (a.get("reviewer") == reviewer and a.get("status") == "responded"
                and isinstance(a.get("durationSeconds"), (int, float))):
            return f"{round(a['durationSeconds'])}s"
    return "—"


def _hud_lines(run_dir: Path, state: dict) -> list:
    """The compact one-glance HUD box as a list of lines. Shared by `hud` and
    `round-close` so the round-open and round-close boxes render identically."""
    run_id = run_dir.name
    cfg = state.get("resolvedConfig", {}) or {}
    round_cap = cfg.get("roundCap", DEFAULTS["roundCap"])
    cur_round = state.get("currentRound", 0)
    scope = _resolve_scope(state)
    step_count = _plan_step_count(run_dir)
    log = state.get("availabilityLog", []) or []
    sb = _scoreboard(state)
    align = (state.get("alignment") or {}).get("status", "unknown")
    align_emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(align, "❔")

    ledger = state.get("ledger") or {}
    findings = ledger.get("findings", []) or []
    open_f = [e for e in findings if e.get("scope") == scope and _finding_is_open(e, log)]
    if open_f:
        first = open_f[0]
        open_tail = (f"open findings: {len(open_f)} · "
                     f"{first['id']} [{first.get('severity', '')}] open")
    else:
        open_tail = "open findings: 0"

    reviewers = " · ".join(
        f"{REVIEWER_LABEL[r]} {sb[r]['avail'].split()[0]} {_last_latency(r, log)}"
        for r in ("codex", "antigravity")
    )
    lines = [
        f"┌ /3p {run_id} · {_phase_label(state, step_count)} · "
        f"Round {cur_round}/{round_cap} · Alignment {align_emoji}",
        f"│ {reviewers} · {open_tail}",
        "└",
    ]
    return lines


def cmd_hud(args: list) -> int:
    """Emit the compact one-glance HUD block to stdout, deterministically, so the
    skill relays tool output each round instead of hand-rebuilding the box."""
    if len(args) != 1:
        print("Usage: 3p.py hud <run-id>", file=sys.stderr)
        return 2
    run_id = args[0]
    anchor, _ = find_anchor()
    run_dir = run_dir_path(anchor, run_id)
    state = read_state(run_dir)
    print("\n".join(_hud_lines(run_dir, state)))
    return 0


def _as_round_int(v) -> int:
    """Coerce a round value (ledger history stores int; availabilityLog may carry
    a string) to int for comparison. Unparseable -> -1, which never matches a real
    round (rounds are >= 1)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def _round_review_lines(state: dict, scope: str, rnd: int) -> list:
    """Per-finding chat lines for one (scope, round): one line per finding each
    reviewer raised that round, plus a ✓ APPROVED / ⚠ unavailable summary line per
    reviewer. Reconstructed from the ledger + availabilityLog so the relayed block
    matches the round files — the skill never hand-types these."""
    findings = (state.get("ledger") or {}).get("findings", []) or []
    log = state.get("availabilityLog", []) or []
    raised = {"codex": [], "antigravity": []}
    for e in findings:
        if e.get("scope") != scope or e.get("reviewer") not in raised:
            continue
        h = next((x for x in e.get("history", []) if _as_round_int(x.get("round")) == rnd), None)
        if h is not None:
            raised[e["reviewer"]].append((e, h))
    avail = {}
    for a in log:
        if _availability_scope(a) == scope and _as_round_int(a.get("round")) == rnd:
            avail[a.get("reviewer")] = a
    lines = []
    for rv in ("codex", "antigravity"):
        label = REVIEWER_LABEL[rv]
        a = avail.get(rv)
        if a is not None and a.get("status") == "unavailable":
            lines.append(f"{label} ⚠ unavailable ({a.get('reason') or 'no response'})")
            continue
        items = sorted(raised[rv], key=lambda t: t[0].get("id", ""))
        if not items:
            # APPROVED is only truthful when the reviewer actually responded this
            # round. A missing availabilityLog record (a dropped/lost
            # availability-append) must NOT render as approval — that would print a
            # false positive to chat and hide the very adherence failure this
            # observability layer exists to surface. Render an explicit warning.
            if a is not None and a.get("status") == "responded":
                lines.append(f"{label} ✓ APPROVED")
            else:
                lines.append(f"{label} ⚠ no availability record (did not respond this round?)")
            continue
        for e, h in items:
            verdict = h.get("verdict") or e.get("status") or ""
            reason = h.get("verdictReason") or ""
            tail = f": {reason}" if reason else ""
            lines.append(f"{label} [{e.get('severity', '')}] {e.get('title', '')} → {verdict}{tail}")
    return lines


def _round_recap_line(state: dict, scope: str, rnd: int) -> str:
    """`Round N: Codex a/r/i · Antigravity a/r/i (accepted/rejected/ignored)`."""
    findings = (state.get("ledger") or {}).get("findings", []) or []
    counts = {r: {"accepted": 0, "rejected": 0, "ignored": 0}
              for r in ("codex", "antigravity")}
    for e in findings:
        if e.get("scope") != scope or e.get("reviewer") not in counts:
            continue
        h = next((x for x in e.get("history", []) if _as_round_int(x.get("round")) == rnd), None)
        if h is None:
            continue
        verdict = (h.get("verdict") or e.get("status") or "").lower()
        if verdict in counts[e["reviewer"]]:
            counts[e["reviewer"]][verdict] += 1

    def fmt(rv):
        c = counts[rv]
        return f"{REVIEWER_LABEL[rv]} {c['accepted']}/{c['rejected']}/{c['ignored']}"

    return f"Round {rnd}: {fmt('codex')} · {fmt('antigravity')} (accepted/rejected/ignored)"


def cmd_round_close(args: list) -> int:
    """One mandatory command the skill runs after both round files are written;
    its stdout IS the close-of-round chat block. Regenerates dashboard.md and
    prints the per-finding lines + recap + close-of-round HUD, so the round result
    reaches chat as a side effect of a command the skill cannot skip — never as a
    separate ceremony the model can collapse away on a trivial happy path."""
    if len(args) != 1:
        print("Usage: 3p.py round-close <run-id>", file=sys.stderr)
        return 2
    run_id = args[0]
    anchor, _ = find_anchor()
    run_dir = run_dir_path(anchor, run_id)
    state = read_state(run_dir)
    _write_dashboard(run_dir, state)          # keep the persistent file fresh
    scope = _resolve_scope(state)
    rnd = state.get("currentRound", 0)
    out = _round_review_lines(state, scope, rnd)
    out += ["", _round_recap_line(state, scope, rnd), ""]
    out += _hud_lines(run_dir, state)
    print("\n".join(out))
    return 0


def cmd_phase_end(args: list) -> int:
    """Mandatory at every phase boundary AND before any early stop (e.g. a
    plan-only run that halts after Phase A). Regenerates dashboard.md and prints
    the full dashboard markdown (scoreboard + open findings + agreement/conflict +
    ledger + alignment) so the skill relays it verbatim instead of stopping
    silently. Decoupled from 'proceed to the next phase' on purpose: stopping is
    not an excuse to skip the render."""
    if len(args) != 1:
        print("Usage: 3p.py phase-end <run-id>", file=sys.stderr)
        return 2
    run_id = args[0]
    anchor, _ = find_anchor()
    run_dir = run_dir_path(anchor, run_id)
    state = read_state(run_dir)
    _, text = _write_dashboard(run_dir, state)
    print(text)
    return 0


def _dashboard_lines(run_dir: Path, state: dict) -> list:
    """Full dashboard markdown as a list of lines. The single source of truth for
    the at-a-glance view — `dashboard`, `phase-end`, and `round-close` all render
    through this so the file, the phase-boundary chat relay, and the per-round
    relay can never diverge. run_id is taken from the run dir name."""
    run_id = run_dir.name
    cfg = state.get("resolvedConfig", {}) or {}
    round_cap = cfg.get("roundCap", DEFAULTS["roundCap"])
    scope = _resolve_scope(state)
    cur_round = state.get("currentRound", 0)
    north = state.get("northStar") or "_(not set)_"
    ledger = state.get("ledger") or {}
    findings = ledger.get("findings", []) or []
    log = state.get("availabilityLog", []) or []
    step_count = _plan_step_count(run_dir)

    out = [
        f"# /3p Dashboard — {state.get('taskSlug', '')} · {run_id}",
        "",
        f"🎯 **Goal:** {north}",
        "",
        f"**Progress:** {_phase_progress(state, step_count)}  ·  Round {cur_round}/{round_cap}"
        f"  ·  Scope `{scope}`",
        f"**Alignment:** {_align_badge(state.get('alignment'))}",
        "",
        "## Reviewers",
        "",
    ]
    out += render_scoreboard_table(state)

    open_f = [e for e in findings if e.get("scope") == scope and _finding_is_open(e, log)]
    out += ["", f"## Open findings — `{scope}`", ""]
    if open_f:
        out += ["| ID | Sev | Title | Reviewer | Status |", "|---|---|---|---|---|"]
        for e in open_f:
            out.append(
                f"| {e['id']} | {e.get('severity', '')} | {e.get('title', '')} | "
                f"{REVIEWER_LABEL.get(e.get('reviewer'), e.get('reviewer'))} | "
                f"{e.get('status', '')} |"
            )
    else:
        out.append("_None open in the current scope._")

    # Agreement / conflict — independent reviewers flagging the same location is a
    # high-confidence signal; one approving while the other has open findings is a
    # conflict worth surfacing.
    out += ["", "## Agreement / conflict", ""]
    scope_findings = [e for e in findings if e.get("scope") == scope]
    by_loc = {}
    for e in scope_findings:
        loc = re.sub(r"\s+", " ", (e.get("location", "") or "")).strip().lower()
        if loc:
            by_loc.setdefault(loc, []).append(e)
    agreed = False
    for loc, group in by_loc.items():
        reviewers = {e.get("reviewer") for e in group}
        if len(reviewers) > 1:
            agreed = True
            ids = ", ".join(e["id"] for e in group)
            out.append(f"- ✓ **Both flagged** `{group[0].get('location')}` ({ids}) — high confidence")
    reviewers_with_open = {e.get("reviewer") for e in open_f}
    if len(reviewers_with_open) == 1:
        has = next(iter(reviewers_with_open))
        other = "antigravity" if has == "codex" else "codex"
        n = sum(1 for e in open_f if e.get("reviewer") == has)
        open_round = max((e.get("lastRound", 0) for e in open_f), default=0)
        if _responded_in_scope_since(other, scope, open_round, log):
            # The other reviewer actually reviewed this scope and is clean — a
            # genuine disagreement between the two reviewers.
            out.append(
                f"- ⚠ **Conflict:** {REVIEWER_LABEL.get(other, other)} approved (no open findings) "
                f"while {REVIEWER_LABEL.get(has, has)} has {n} open in `{scope}`"
            )
        else:
            # The other reviewer has not returned a review for this scope; do NOT
            # imply it approved (it may be unavailable or not run yet).
            out.append(
                f"- ⚠ **Coverage gap:** {REVIEWER_LABEL.get(has, has)} has {n} open in `{scope}`; "
                f"{REVIEWER_LABEL.get(other, other)} has not returned a review for this scope yet"
            )
    elif not agreed:
        out.append("_No cross-reviewer agreement or conflict in the current scope._")

    # Full findings ledger (all scopes) — stable IDs, status, and round history,
    # so the dashboard reflects the whole run at a glance, not just the live scope.
    out += ["", "## Findings ledger (all scopes)", ""]
    out += render_ledger_table(state)

    # Last activity
    out += ["", "## Last activity", ""]
    if log:
        a = log[-1]
        out.append(
            f"- {a.get('phase', '')} {(a.get('step') or '-')} round {a.get('round', '')}: "
            f"{REVIEWER_LABEL.get(a.get('reviewer'), a.get('reviewer'))} "
            f"{a.get('status', '')}"
            + (f" ({a.get('reason')})" if a.get("reason") else "")
            + (f", {a.get('durationSeconds')}s" if a.get("durationSeconds") else "")
        )
    out.append(f"- Open findings in scope: {len(open_f)}")
    out.append("")
    return out


def _write_dashboard(run_dir: Path, state: dict) -> tuple:
    """Regenerate dashboard.md and return (dest_path, rendered_text)."""
    out = _dashboard_lines(run_dir, state)
    text = "\n".join(out)
    dest = run_dir / "dashboard.md"
    dest.write_text(text)
    return dest, text


def cmd_dashboard(args: list) -> int:
    to_stdout = "--stdout" in args
    args = [a for a in args if a != "--stdout"]
    if len(args) != 1:
        print("Usage: 3p.py dashboard <run-id> [--stdout]", file=sys.stderr)
        return 2
    run_id = args[0]
    anchor, _ = find_anchor()
    run_dir = run_dir_path(anchor, run_id)
    state = read_state(run_dir)
    dest, text = _write_dashboard(run_dir, state)
    # Default prints the path (back-compat). --stdout additionally echoes the full
    # rendered markdown so the skill can relay the scoreboard+ledger straight into
    # chat at phase boundaries instead of reconstructing the tables by hand.
    if to_stdout:
        print(text)
    else:
        print(str(dest))
    return 0


def cmd_summary(args: list) -> int:
    if len(args) != 1:
        print("Usage: 3p.py summary <run-id>", file=sys.stderr)
        return 2
    run_id = args[0]
    anchor, _ = find_anchor()
    run_dir = run_dir_path(anchor, run_id)
    state = read_state(run_dir)
    task = (run_dir / "task.txt").read_text() if (run_dir / "task.txt").exists() else "(no task.txt)"
    plan = (run_dir / "plan.md").read_text() if (run_dir / "plan.md").exists() else "(no plan.md)"

    rounds = sorted(run_dir.glob("plan-round-*.md")) \
        + sorted(run_dir.glob("step-*-round-*.md")) \
        + sorted(run_dir.glob("final-round-*.md"))
    step_summaries = sorted(run_dir.glob("step-*-summary.md"))
    changed_files = _enumerate_diff_paths(anchor, state, run_id)

    out = [
        f"# /3p Run Summary — {run_id}",
        "",
        "## Original task",
        "",
        f"> {task.strip()}",
        "",
        "## Timing",
        "",
    ]
    out += render_timing_table(state)
    out += [
        "",
        "## Final approved plan",
        "",
        plan.strip(),
        "",
        "## Per-step summaries",
        "",
    ]
    for s in step_summaries:
        out += [f"### {s.name}", "", s.read_text().strip(), ""]
    # Observability roll-up — same helpers the live dashboard uses, so the
    # end-of-run summary and the dashboard can never diverge.
    out += ["## Goal-alignment", "",
            f"- {_align_badge(state.get('alignment'))}", ""]
    out += ["## Reviewer scoreboard", ""]
    out += render_scoreboard_table(state)
    out += ["", "## Findings ledger", ""]
    out += render_ledger_table(state)
    out += [""]
    out += ["## Round-by-round audit trail", ""]
    for r in rounds:
        out += [f"### {r.name}", "", r.read_text().strip(), ""]
    fr = run_dir / "final-review.md"
    if fr.exists():
        out += ["## Phase C consolidated final-review.md", "", fr.read_text().strip(), ""]
    out += [
        "## Reviewer availability log (full history)",
        "",
        "| Phase | Step | Round | Reviewer | Status | Reason | Duration (s) |",
        "|---|---|---|---|---|---|---|",
    ]
    for e in state.get("availabilityLog", []):
        out.append(
            f"| {e.get('phase','')} | {e.get('step','-') or '-'} | "
            f"{e.get('round','')} | {e.get('reviewer','')} | "
            f"{e.get('status','')} | {e.get('reason','-') or '-'} | "
            f"{e.get('durationSeconds','')} |"
        )
    out += ["",
            "Current reviewer health counters:",
            "",
            f"```json\n{json.dumps(state.get('reviewerHealth', {}), indent=2)}\n```",
            ""]
    if state.get("downgradeMode"):
        out += ["## Downgrade mode", "",
                f"Active: {json.dumps(state['downgradeMode'], indent=2)}", ""]
    out += [
        "## Uncommitted-state notice",
        "",
        "The following files were modified during Phase B and remain on disk uncommitted. "
        "`/3p` does not touch git history — you are responsible for staging/committing/reverting.",
        "",
        "Changed/new files:",
        "",
    ]
    for p in changed_files:
        out.append(f"- `{p}`")
    out += ["", f"Audit trail location: `{run_dir}`", ""]
    (run_dir / "summary.md").write_text("\n".join(out))
    return 0


def _enumerate_diff_paths(anchor: Path, state: dict, run_id: str) -> list:
    if "pre-build" not in state.get("baselines", {}):
        return []
    snap_str = str(Path(state["baselines"]["pre-build"]["path"]))
    anchor_str = str(anchor)
    run_dir = run_dir_path(anchor, run_id)
    final_diff = run_dir / "final-diff.txt"
    if final_diff.exists():
        diff_text = final_diff.read_text()
    else:
        proc = _sp.run(
            [sys.executable, __file__, "snapshot", "diff", run_id, "pre-build"],
            cwd=anchor, capture_output=True, text=True,
        )
        diff_text = proc.stdout
    paths = set()
    for line in diff_text.splitlines():
        if line.startswith("diff -ruN "):
            rest = line[len("diff -ruN "):]
            rel = _parse_diff_header_paths(rest, snap_str, anchor_str)
            if rel:
                paths.add(rel)
        elif line.startswith("Only in "):
            rest = line[len("Only in "):]
            sep = rest.rfind(": ")
            if sep == -1:
                continue
            dir_part = rest[:sep]
            name = rest[sep + 2:]
            if dir_part == snap_str or dir_part.startswith(snap_str + os.sep):
                rel = os.path.relpath(os.path.join(dir_part, name), snap_str)
            else:
                rel = os.path.relpath(os.path.join(dir_part, name), anchor_str)
            paths.add(rel)
    return sorted(paths)


def cmd_list(args: list) -> int:
    anchor, _ = find_anchor()
    base = anchor / ".3p"
    if not base.exists():
        return 0
    for run_dir in sorted(base.iterdir()):
        state_f = run_dir / "state.json"
        if state_f.exists():
            state = json.loads(state_f.read_text())
            print(f"{run_dir.name}\t{state.get('phase')}\t{state.get('taskSlug')}")
    return 0


def cmd_clean(args: list) -> int:
    if len(args) != 1:
        print("Usage: 3p.py clean <run-id>", file=sys.stderr)
        return 2
    run_id = args[0]
    anchor, is_git = find_anchor()
    run_dir = run_dir_path(anchor, run_id)
    if run_dir.exists():
        shutil.rmtree(run_dir)
    if is_git:
        refs = _sp.run(
            ["git", "for-each-ref", f"refs/3p/{run_id}/", "--format=%(refname)"],
            cwd=anchor, capture_output=True, text=True,
        ).stdout.splitlines()
        for ref in refs:
            _sp.run(["git", "update-ref", "-d", ref], cwd=anchor)
    return 0


def cmd_consolidate_final(args: list) -> int:
    """Consolidate per-reviewer final-round-*.md files into final-review.md."""
    if len(args) != 1:
        print("Usage: 3p.py consolidate-final <run-id>", file=sys.stderr)
        return 2
    run_id = args[0]
    anchor, _ = find_anchor()
    run_dir = run_dir_path(anchor, run_id)
    state = read_state(run_dir)
    round_files = sorted(run_dir.glob("final-round-*.md"))
    rounds_by_num = {}
    for rf in round_files:
        parts = rf.stem.split("-")
        try:
            n = int(parts[2])
        except (IndexError, ValueError):
            continue
        rounds_by_num.setdefault(n, []).append(rf)
    phase_c_log = [e for e in state.get("availabilityLog", []) if e.get("phase") == "final"]
    exit_status = "cap-reached"
    if rounds_by_num:
        last_n = max(rounds_by_num)
        last_files = rounds_by_num[last_n]
        approvals = sum("**APPROVED**" in p.read_text() for p in last_files)
        if approvals >= 2:
            exit_status = "approved"
        elif state.get("downgradeMode") and approvals >= 1:
            exit_status = "approved (downgrade-mode)"
    out = [
        "# Phase C — Final Review",
        "",
        f"_Exit: **{exit_status}** after {len(rounds_by_num)} round(s)_",
        "",
    ]
    for r in round_files:
        out += [f"## {r.name}", "", r.read_text().strip(), ""]
    out += [
        "## Phase C reviewer availability",
        "",
        "| Round | Reviewer | Status | Reason | Duration (s) |",
        "|---|---|---|---|---|",
    ]
    for e in phase_c_log:
        out.append(
            f"| {e.get('round','')} | {e.get('reviewer','')} | "
            f"{e.get('status','')} | {e.get('reason','-') or '-'} | "
            f"{e.get('durationSeconds','')} |"
        )
    (run_dir / "final-review.md").write_text("\n".join(out) + "\n")
    return 0


def main(argv: list) -> int:
    if len(argv) < 2:
        print(USAGE, file=sys.stderr)
        return 2
    cmd = argv[1]
    args = argv[2:]
    dispatcher = {
        "slug": cmd_slug,
        "config-load": cmd_config_load,
        "model-power": cmd_model_power,
        "models": cmd_models,
        "reviewer-role": cmd_reviewer_role,
        "pal-config": cmd_pal_config,
        "update": cmd_update,
        "init": cmd_init,
        "state-read": cmd_state_read,
        "state-write": cmd_state_write,
        "mark": cmd_mark,
        "availability-append": cmd_availability_append,
        "snapshot": cmd_snapshot,
        "parse-response": cmd_parse_response,
        "round-write": cmd_round_write,
        "round-close": cmd_round_close,
        "phase-end": cmd_phase_end,
        "dashboard": cmd_dashboard,
        "hud": cmd_hud,
        "summary": cmd_summary,
        "consolidate-final": cmd_consolidate_final,
        "list": cmd_list,
        "clean": cmd_clean,
    }
    if cmd not in dispatcher:
        print(f"Unknown subcommand: {cmd}\n\n{USAGE}", file=sys.stderr)
        return 2
    return dispatcher[cmd](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

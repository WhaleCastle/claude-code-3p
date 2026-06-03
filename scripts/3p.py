#!/usr/bin/env python3
"""3p — three-party review skill helper. Subcommand-based CLI."""
import sys
import contextlib
import hashlib
import json
import re
import subprocess as _sp
from pathlib import Path

try:
    import fcntl  # POSIX
    _IS_POSIX = True
except ImportError:
    _IS_POSIX = False
    import msvcrt


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
    "excludes": list(DEFAULT_BLOAT_EXCLUDES),
    "secretPatterns": list(HARDCODED_SECRET_PATTERNS),
}


def load_config(anchor: Path, config_path=None, cli_excludes=None) -> dict:
    """Merge defaults <- config file <- CLI flags.
    - `excludes` in config file REPLACES defaults (user-overridable bloat list).
    - `extraExcludes` in config file APPENDS to defaults.
    - CLI `--exclude` flags always APPEND on top.
    - Secret patterns are NEVER overridable.
    """
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    file_path = config_path or (anchor / ".3p" / "config.json")
    if file_path.exists():
        try:
            file_cfg = json.loads(file_path.read_text())
        except json.JSONDecodeError as e:
            print(f"Warning: ignoring malformed {file_path}: {e}", file=sys.stderr)
            file_cfg = {}
        for k, v in file_cfg.items():
            if k == "excludes":
                cfg["excludes"] = list(v)  # replace defaults
            elif k == "extraExcludes":
                for x in v:
                    if x not in cfg["excludes"]:
                        cfg["excludes"].append(x)
            elif k == "secretPatterns":
                merged = list(HARDCODED_SECRET_PATTERNS)
                for x in v:
                    if x not in merged:
                        merged.append(x)
                cfg["secretPatterns"] = merged
            else:
                cfg[k] = v
    if cli_excludes:
        for x in cli_excludes:
            if x not in cfg["excludes"]:
                cfg["excludes"].append(x)
    for p in HARDCODED_SECRET_PATTERNS:
        if p not in cfg["secretPatterns"]:
            cfg["secretPatterns"].append(p)
    return cfg


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
    config_path = None
    cli_excludes = []
    i = 2
    while i < len(args):
        if args[i] == "--config":
            config_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--exclude":
            cli_excludes.append(args[i + 1])
            i += 2
        else:
            print(f"Unknown flag: {args[i]}", file=sys.stderr)
            return 2
    run_id = f"{slug}-{ts}"
    anchor, is_git = find_anchor()
    if is_git:
        verify_git_ref_format(f"refs/3p/{run_id}/pre-build")
    run_dir = run_dir_path(anchor, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "baselines").mkdir(exist_ok=True)
    resolved_cfg = load_config(anchor, config_path, cli_excludes)
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
            "gemini": {"lastStatus": None, "consecutiveFailures": 0},
        },
        "consecutiveBothDownRounds": 0,
        "downgradeMode": None,
        "baselines": {},
        "pausedReason": None,
        "resolvedConfig": resolved_cfg,
        "availabilityLog": [],
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
    mutate_state(run_dir, lambda s: s.update({key: value}))
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
    config_path = None
    cli_excludes = []
    i = 0
    while i < len(args):
        if args[i] == "--config":
            config_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--exclude":
            cli_excludes.append(args[i + 1])
            i += 2
        else:
            print(f"Unknown flag: {args[i]}", file=sys.stderr)
            return 2
    anchor = Path.cwd()
    cfg = load_config(anchor, config_path, cli_excludes)
    print(json.dumps(cfg, indent=2))
    return 0


USAGE = """\
Usage: 3p.py <subcommand> [args...]

Subcommands:
  slug <task-description>
  init <slug> <timestamp> [--config <p>] [--exclude <pat>]...
  config-load
  state-read <run-id> <key>
  state-write <run-id> <key> <value-json>
  availability-append <run-id> <entry-json>
  snapshot capture <run-id> <key>
  snapshot diff <run-id> <key>
  parse-response <file>
  round-write <run-id> <phase> <step|-> <round> <reviewer> <verdicts-json>
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


def main(argv: list) -> int:
    if len(argv) < 2:
        print(USAGE, file=sys.stderr)
        return 2
    cmd = argv[1]
    args = argv[2:]
    dispatcher = {
        "slug": cmd_slug,
        "config-load": cmd_config_load,
        "init": cmd_init,
        "state-read": cmd_state_read,
        "state-write": cmd_state_write,
        "availability-append": cmd_availability_append,
    }
    if cmd not in dispatcher:
        print(f"Unknown subcommand: {cmd}\n\n{USAGE}", file=sys.stderr)
        return 2
    return dispatcher[cmd](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

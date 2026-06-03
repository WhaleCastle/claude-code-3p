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


ALWAYS_EXCLUDED_DIRS = {".3p", ".git"}


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
        regex = fnmatch.translate(pattern)
        return re.match(regex, rel) is not None
    if anchored:
        return fnmatch.fnmatch(rel, pattern)
    base = rel.rsplit("/", 1)[-1]
    return fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(base, pattern)


def should_exclude(rel_path: str, patterns: list) -> bool:
    return any(pattern_matches(rel_path, p) for p in patterns)


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
            rel = f"{rel_root}/{f}" if rel_root else f
            candidates.append(rel)
    out = []
    for rel in candidates:
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


def cmd_snapshot(args: list) -> int:
    if len(args) < 1:
        print("Usage: 3p.py snapshot {capture|diff} ...", file=sys.stderr)
        return 2
    sub = args[0]
    if sub == "capture":
        return cmd_snapshot_capture(args[1:])
    if sub == "diff":
        print("Not yet implemented in Task 2.4", file=sys.stderr)
        return 2
    print(f"Unknown snapshot subcommand: {sub}", file=sys.stderr)
    return 2


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
        "snapshot": cmd_snapshot,
    }
    if cmd not in dispatcher:
        print(f"Unknown subcommand: {cmd}\n\n{USAGE}", file=sys.stderr)
        return 2
    return dispatcher[cmd](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

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

import re as _re_validation

_RUN_ID_RE = _re_validation.compile(r"^[a-z0-9][a-z0-9-]*-\d{8}-\d{4}$")


def validate_run_id(run_id: str) -> None:
    """Reject anything that isn't a well-formed run id to prevent path traversal."""
    if not _RUN_ID_RE.match(run_id):
        raise SystemExit(
            f"Invalid run_id: {run_id!r}. Expected format: <slug>-<YYYYMMDD>-<HHMM> "
            f"where slug uses only [a-z0-9-]."
        )


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


_FINDING_HEADER = re.compile(
    r"^\s*\*{0,2}\[(Blocker|Critical|Important|Risk)\]\*{0,2}\s+(.+?)\s*$",
    re.M,
)


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
        findings.append({
            "severity": severity,
            "title": title,
            "location": _extract_field(block, "Location"),
            "issue": _extract_field(block, "Issue"),
            "rationale": _extract_field(block, "Rationale"),
        })
    if findings:
        return {"status": "findings", "findings": findings}
    if re.search(r"^APPROVED\s*$", text, re.M):
        return {"status": "approved", "findings": []}
    return {"status": "unavailable", "raw": text, "findings": []}


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


def cmd_round_write(args: list) -> int:
    if len(args) != 6:
        print("Usage: 3p.py round-write <run-id> <phase> <step|-> <round> <reviewer> <verdicts-json>",
              file=sys.stderr)
        return 2
    run_id, phase, step, rnd_s, reviewer, verdicts_json = args
    rnd = int(rnd_s)
    v = json.loads(verdicts_json)
    assert v["reviewer"] == reviewer
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
        "## Final approved plan",
        "",
        plan.strip(),
        "",
        "## Per-step summaries",
        "",
    ]
    for s in step_summaries:
        out += [f"### {s.name}", "", s.read_text().strip(), ""]
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
        "init": cmd_init,
        "state-read": cmd_state_read,
        "state-write": cmd_state_write,
        "availability-append": cmd_availability_append,
        "snapshot": cmd_snapshot,
        "parse-response": cmd_parse_response,
        "round-write": cmd_round_write,
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

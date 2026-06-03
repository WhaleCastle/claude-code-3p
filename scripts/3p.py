#!/usr/bin/env python3
"""3p — three-party review skill helper. Subcommand-based CLI."""
import sys
import hashlib
import json
import re
import subprocess as _sp
from pathlib import Path


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
    }
    if cmd not in dispatcher:
        print(f"Unknown subcommand: {cmd}\n\n{USAGE}", file=sys.stderr)
        return 2
    return dispatcher[cmd](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

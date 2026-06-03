#!/usr/bin/env python3
"""3p — three-party review skill helper. Subcommand-based CLI."""
import sys


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


def main(argv: list) -> int:
    if len(argv) < 2:
        print(USAGE, file=sys.stderr)
        return 2
    cmd = argv[1]
    args = argv[2:]
    dispatcher = {
        # subcommands wired in by later tasks
    }
    if cmd not in dispatcher:
        print(f"Unknown subcommand: {cmd}\n\n{USAGE}", file=sys.stderr)
        return 2
    return dispatcher[cmd](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

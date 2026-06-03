#!/usr/bin/env bash
# Installs the 3p skill to ~/.claude/skills/3p/
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST="${HOME}/.claude/skills/3p"

# Sanity checks
command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }
command -v git >/dev/null || { echo "git not found" >&2; exit 1; }
command -v diff >/dev/null || { echo "diff not found" >&2; exit 1; }

# Verify python version >= 3.8
python3 -c "import sys; assert sys.version_info >= (3, 8), sys.version_info" \
  || { echo "Python 3.8+ required" >&2; exit 1; }

mkdir -p "$DST"
cp -r "$SRC/SKILL.md" "$SRC/scripts" "$SRC/prompts" "$DST/"
chmod +x "$DST/scripts/3p.py"

echo "Installed 3p skill to $DST"
echo
echo "Verify by running in Claude Code: /3p --list"

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

(cd "$SRC" && python3 scripts/3p.py pal-config install >/dev/null)
python3 - "$DST/install.json" "$SRC" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
source = Path(sys.argv[2]).resolve()
path.write_text(json.dumps({"source": str(source)}, indent=2) + "\n")
PY

echo "Installed 3p skill to $DST"
echo "Installed PAL clink reviewer roles: codereviewer-{low,high}-{reasoning,code} (codex + antigravity/agy)"
echo "Restart Claude Code so PAL MCP reloads the reviewer roles."
echo "If you run PAL MCP as a separate process, restart that process instead."
echo
echo "Verify by running in Claude Code: /3p --list"

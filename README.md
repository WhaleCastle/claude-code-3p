# 3p — Three-Party Review Skill for Claude Code

A standalone Claude Code skill that runs a Plan → Build → Review workflow with Codex and Gemini as independent reviewers and Claude as worker/judge. Catches important/critical/blocker issues through triangulation across three models with explicit verification and full audit trail.

## Install

```bash
./install.sh
```

Installs to `~/.claude/skills/3p/`. Requires Python 3.8+, git, diff, and PAL MCP configured with codex + gemini backends.

## Usage

```
/3p <task description>         # run the full workflow
/3p --resume <task-slug>       # resume an interrupted run
/3p --list                     # list recent runs
/3p --clean <task-slug>        # remove a run's artifacts and git refs
```

## Design

See `docs/superpowers/specs/2026-06-03-3p-skill-design.md` (in your projects, not in this install) for the full specification.

## License

MIT

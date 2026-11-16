# Agent skills

Skills interpret the JSON reports produced by the scripts and turn them into bug reports.

| Skill | Status | Covers |
|---|---|---|
| [`qa-bug-hunter`](qa-bug-hunter/SKILL.md) | published | test design (Kaner), bot quality, layout matrix, code regressions |
| `qa-security` | being prepared for publication | prompt injection, secrets, RLS, endpoints |

Each skill is a folder with a `SKILL.md` file, the format used by Claude Code and compatible agents.

## Install
Claude Code: copy the folder into `~/.claude/skills/` (or `.claude/skills/` in a project). Other agents: point them at `SKILL.md` as instructions.

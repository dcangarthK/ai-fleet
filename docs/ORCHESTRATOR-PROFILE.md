# Lean orchestrator profile

Launch: `scripts/orchestrator.sh [claude args]`. It changes to the repo root and runs `claude --strict-mcp-config
--mcp-config orchestrator/mcp.json --settings orchestrator/settings.json`. Only the `local-llm` MCP server loads, and
the design/UX/superpowers plugins listed in `orchestrator/settings.json` are off. `claude-mem` stays on.

Desktop app: it can't take these flags. To get the plugin part there, copy `enabledPlugins` from
`orchestrator/settings.json` into the repo's `.claude/settings.local.json` (per machine, not committed); don't use the shared `.claude/settings.json`, which would also turn these plugins off for fleet worker sessions. The MCP part stays as is.

Baseline startup context: about 38k tokens (CLI), about 90k (desktop app). Target: under 25k.
Measured 17 Nov 2026 with `scripts/orchestrator.sh -p --model haiku "/context"`: 35.5k tokens (system prompt 3.5k, system tools 9.9k, skills 9.9k, memory files 5.5k, custom agents 3k; deferred MCP and system tools are listed but not loaded). The CLI saving is small, about 2.5k: most of what remains is the user-level skills in ~/.claude/skills and agents in ~/.claude/agents, which plugin settings do not cover. The large saving is against the desktop app (about 90k), so run orchestrator sessions from this launcher in a terminal rather than the desktop app.

The under-25k target was not met on the CLI (35.5k) before this change.

## Keeping user-level skills and agents out (`--setting-sources`)

Mechanism: `claude --setting-sources project,local` (listed in `claude --help`: "setting sources to load (user, project, local)"). The launcher now passes it, so `~/.claude` settings (user source) are not loaded. No new config dir, no new login, nothing under `~/.claude*` is touched or copied; auth is unaffected because it is not a setting. A dedicated `CLAUDE_CONFIG_DIR` was rejected: it would need a fresh login (owner step) and would hide the installed plugins.

What this should drop: user-level skills/agents/CLAUDE.md/hooks. What is kept: project skill `.claude/skills/fleet-batch` and the project CLAUDE.md (project source), the `local-llm` MCP server (`--mcp-config`), and claude-mem, which is now enabled explicitly in `orchestrator/settings.json` (`claude-mem@thedotmack`) because its user-settings entry no longer loads.

Side effect: the user-level master instructions (`~/.claude/CLAUDE.md`, caveman mode, SessionStart hook) are also not loaded in these sessions.

**UNVERIFIED.** The task session could not run a nested `claude -p ... "/context"` (command refused, not retried), so there is no after-measurement, and I did not confirm that `--setting-sources` also stops `~/.claude/skills` and `~/.claude/agents` loading, nor that claude-mem loads from `--settings`. Owner step: run
`scripts/orchestrator.sh -p --model haiku "/context"` and record the numbers here.
If skills (9.9k) and agents (3k) drop out, expect about 22k (35.5k - 9.9k - 3k, plus memory-file savings), under 25k. If they do not drop, the fallback needing an owner step is a dedicated `CLAUDE_CONFIG_DIR` with a one-time `claude` login in it; 25k is then reachable only by that route.

| | total | skills | agents | memory |
|---|---|---|---|---|
| Before | 35.5k | 9.9k | 3k | 5.5k |
| After | not measured | | | |

The launcher cds to the repo root, so relative paths in your own args resolve from there. Run it by its real path, not a symlink.

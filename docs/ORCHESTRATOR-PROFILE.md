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

What it drops: user-level skills and agents (no custom agents listed any more). Kept, confirmed in the measurement below: project skill `fleet-batch`, the `local-llm` MCP server (29 tools), and claude-mem (its skills load), the last now enabled explicitly in `orchestrator/settings.json` (`claude-mem@thedotmack`) because its user-settings entry no longer loads.

Correction to the earlier expectation: `~/.claude/CLAUDE.md` (1.1k) still loads, listed as a "Project" memory file, so the master instructions stay. Whether the SessionStart hook (caveman) still runs was not checked.

Measured 17 Nov 2026 with `scripts/orchestrator.sh -p --model haiku "/context"`: **19.8k tokens**, under the 25k target. System prompt 3.4k, system tools 3.1k, memory files 4.9k, skills 5.2k (fleet-batch, claude-mem and built-ins), messages 3.2k; MCP tools (7.1k) and deferred system tools (14.2k) are listed but not loaded. No new login was needed.

| | total | system tools | skills | agents | memory |
|---|---|---|---|---|---|
| Before | 35.5k | 9.9k | 9.9k | 3k | 5.5k |
| After | 19.8k | 3.1k | 5.2k | none | 4.9k |

The launcher cds to the repo root, so relative paths in your own args resolve from there. Run it by its real path, not a symlink.

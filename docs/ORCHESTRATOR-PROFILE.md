# Lean orchestrator profile

Launch: `scripts/orchestrator.sh [claude args]`. It changes to the repo root and runs `claude --strict-mcp-config
--mcp-config orchestrator/mcp.json --settings orchestrator/settings.json`. Only the `local-llm` MCP server loads, and
the design/UX/superpowers plugins listed in `orchestrator/settings.json` are off. `claude-mem` stays on.

Desktop app: it can't take these flags. To get the plugin part there, copy `enabledPlugins` from
`orchestrator/settings.json` into `~/ai-fleet/.claude/settings.json` (project settings). The MCP part stays as is.

Baseline startup context: about 38k tokens (CLI), about 90k (desktop app). Target: under 25k.
Measured 17 Nov 2026 with `scripts/orchestrator.sh -p --model haiku "/context"`: 35.5k tokens (system prompt 3.5k, system tools 9.9k, skills 9.9k, memory files 5.5k, custom agents 3k; deferred MCP and system tools are listed but not loaded). The CLI saving is small, about 2.5k: most of what remains is the user-level skills in ~/.claude/skills and agents in ~/.claude/agents, which plugin settings do not cover. The large saving is against the desktop app (about 90k), so run orchestrator sessions from this launcher in a terminal rather than the desktop app.

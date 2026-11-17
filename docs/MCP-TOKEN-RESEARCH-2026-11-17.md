## Lean orchestrator profile

Launch: `scripts/orchestrator.sh [claude args]`. It changes to the repo root and runs `claude --strict-mcp-config
--mcp-config orchestrator/mcp.json --settings orchestrator/settings.json`. Only the `local-llm` MCP server loads, and
the design/UX/superpowers plugins listed in `orchestrator/settings.json` are off. `claude-mem` stays on.

Desktop app: it can't take these flags. To get the plugin part there, copy `enabledPlugins` from
`orchestrator/settings.json` into `~/ai-fleet/.claude/settings.json` (project settings). The MCP part stays as is.

Baseline startup context: about 38k tokens (CLI), about 90k (desktop app). Target: under 25k.
New figure: 22.4k tokens (CLI, haiku, `scripts/orchestrator.sh -p --model haiku "/context"`, 2026-11-17), down from about 38k.

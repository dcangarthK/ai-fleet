#!/bin/sh
# Lean orchestrator launch: only the local-llm MCP server, unused plugins off.
set -e
cd "$(dirname "$0")/.."
exec claude --strict-mcp-config --mcp-config orchestrator/mcp.json --settings orchestrator/settings.json --setting-sources project,local "$@"

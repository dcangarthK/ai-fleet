#!/usr/bin/env bash
# Master node (Laptop 1): Tailscale tag, LiteLLM gateway, fleet controller, Claude Code MCP servers.
# Run once; safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CODEX="$(command -v codex || echo /Applications/Codex.app/Contents/Resources/codex)"
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale

command -v uv >/dev/null || brew install uv
UV="$(command -v uv)"

# 1. Tailnet identity: the policy lets only tag:master reach node model ports.
[ -x "$TS" ] || { brew install --cask tailscale; open -a Tailscale; echo "Sign in to Tailscale, then press Enter."; read -r; }
"$TS" up --advertise-tags=tag:master --reset

# 2. Gateway. bootstrap.sh prints a virtual key on success.
"$HERE/litellm/bootstrap.sh" | tee /dev/stderr | sed -n 's/^Virtual key for Laptop 1: //p' > "$HERE/litellm/.vkey" \
  || { rm -f "$HERE/litellm/.vkey"; echo "Gateway setup incomplete (see above). Fix and re-run."; exit 1; }
if [ -s "$HERE/litellm/.vkey" ]; then
  security delete-generic-password -s litellm-laptop1 >/dev/null 2>&1 || true
  security add-generic-password -s litellm-laptop1 -a "$USER" -w "$(cat "$HERE/litellm/.vkey")"
fi
rm -f "$HERE/litellm/.vkey"

# 3. Fleet controller as a login agent (dashboard: http://127.0.0.1:8700).
PLIST="$HOME/Library/LaunchAgents/local.ai-fleet.controller.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.ai-fleet.controller</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>-c</string>
    <string>set -a; . "\$0/../litellm/.env"; set +a; exec "$UV" run --script "\$0/controller.py"</string>
    <string>$HERE/controller</string>
  </array>
  <key>EnvironmentVariables</key><dict><key>TAILSCALE_BIN</key><string>$TS</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/ai-fleet-controller.log</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/ai-fleet-controller.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# 4. Claude Code: Codex as a sub-agent, fleet bridge for local models + fleet management.
claude mcp remove --scope user codex >/dev/null 2>&1 || true
claude mcp remove --scope user local-llm >/dev/null 2>&1 || true
claude mcp add --scope user codex -- "$CODEX" mcp-server
claude mcp add --scope user local-llm -- "$UV" run --script "$HERE/mcp/server.py"
claude mcp list

echo "Master ready. Dashboard: http://127.0.0.1:8700  Logs: ~/Library/Logs/ai-fleet-controller.log"

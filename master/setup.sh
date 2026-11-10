#!/usr/bin/env bash
# Master node (Laptop 1): Tailscale tag, LiteLLM gateway, fleet controller, Claude Code MCP servers.
# Run once; safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CODEX="$(command -v codex || echo /Applications/Codex.app/Contents/Resources/codex)"
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale

# BEGIN launchd plist helper
write_launchd_plist() {
  python3 - "$@" <<'PY'
import plistlib
import sys

path, label, run_at_load, keep_alive, interval, stdout, stderr, env_count, *items = sys.argv[1:]
env_items = items[:int(env_count) * 2]
program_arguments = items[int(env_count) * 2:]
data = {"Label": label, "ProgramArguments": program_arguments}
if env_items:
    data["EnvironmentVariables"] = dict(zip(env_items[::2], env_items[1::2]))
if run_at_load:
    data["RunAtLoad"] = True
if keep_alive:
    data["KeepAlive"] = True
if interval:
    data["StartInterval"] = int(interval)
if stdout:
    data["StandardOutPath"] = stdout
if stderr:
    data["StandardErrorPath"] = stderr
with open(path, "wb") as stream:
    plistlib.dump(data, stream, fmt=plistlib.FMT_XML, sort_keys=False)
PY
}
# END launchd plist helper

command -v python3 >/dev/null || brew install python
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
mkdir -p "$HOME/Library/LaunchAgents"
PLIST="$HOME/Library/LaunchAgents/local.ai-fleet.controller.plist"
write_launchd_plist "$PLIST" "local.ai-fleet.controller" 1 1 "" \
  "$HOME/Library/Logs/ai-fleet-controller.log" "$HOME/Library/Logs/ai-fleet-controller.log" 1 \
  TAILSCALE_BIN "$TS" \
  "/bin/bash" "-c" 'set -a; . "$0/../litellm/.env"; set +a; exec "$1" run --script "$0/controller.py"' \
  "$HERE/controller" "$UV"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# 4. The master's own agent runner (localhost only): lets tasks use this Mac's Claude Code, Codex, Ollama.
mkdir -p "$HOME/.ai-fleet" && chmod 700 "$HOME/.ai-fleet"
[ -f "$HOME/.ai-fleet/runner.json" ] || echo '{"max_mode": "auto-edit"}' > "$HOME/.ai-fleet/runner.json"
RPLIST="$HOME/Library/LaunchAgents/local.ai-fleet.runner.plist"
write_launchd_plist "$RPLIST" "local.ai-fleet.runner" 1 1 "" "" \
  "$HOME/Library/Logs/ai-fleet-runner.log" 4 \
  RUNNER_BIND "127.0.0.1:11436" \
  FLEET_DIR "$HOME/.ai-fleet" \
  OLLAMA_URL "http://127.0.0.1:11434" \
  PATH "$HOME/.local/bin:$HOME/.claude/local:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
  "$(command -v python3)" "$HERE/../nodes/runner.py"
launchctl bootout "gui/$(id -u)" "$RPLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$RPLIST"

# 5. Claude Code: Codex as a sub-agent, fleet bridge for local models + fleet management + tasks.
claude mcp remove --scope user codex >/dev/null 2>&1 || true
claude mcp remove --scope user local-llm >/dev/null 2>&1 || true
claude mcp add --scope user codex -- "$CODEX" mcp-server
claude mcp add --scope user local-llm -- "$UV" run --script "$HERE/mcp/server.py"
claude mcp list

echo "Master ready. Dashboard: http://127.0.0.1:8700  Logs: ~/Library/Logs/ai-fleet-controller.log"

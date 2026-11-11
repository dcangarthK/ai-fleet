#!/usr/bin/env bash
# Master node (Laptop 1): Tailscale tag, LiteLLM gateway, fleet controller, Claude Code MCP servers.
# Run once; safe to re-run.
#   --services-only     (re)install just the controller, runner and Ollama services; skip the Tailscale
#                       re-tag, the gateway bootstrap and the Claude Code MCP registration.
#   --reset-tailscale   allow `tailscale up --reset` if Tailscale refuses to add the tag otherwise. It wipes
#                       this Mac's other Tailscale settings (exit node, routes, DNS, SSH); never done without it.
set -euo pipefail
SERVICES_ONLY=0; RESET_TAILSCALE=0
for arg in "$@"; do
  case "$arg" in
    --services-only) SERVICES_ONLY=1 ;;
    --reset-tailscale) RESET_TAILSCALE=1 ;;
    *) echo "unknown option $arg (use --services-only and/or --reset-tailscale)"; exit 1 ;;
  esac
done
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$HERE/.." && pwd)"
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

# (Re)load a LaunchAgent. `launchctl bootout` returns before the job is gone, so an immediate bootstrap
# can fail with "Bootstrap failed: 5: Input/output error" on a re-run; wait for it and retry instead.
reload_launch_agent() {
  local plist="$1" label domain="gui/$(id -u)" i
  label="$(basename "$plist" .plist)"
  launchctl bootout "$domain" "$plist" 2>/dev/null || true
  for i in 1 2 3 4 5 6 7 8 9 10; do
    launchctl print "$domain/$label" >/dev/null 2>&1 || break
    sleep "${RELOAD_WAIT:-0.5}"
  done
  for i in 1 2 3 4 5; do
    launchctl bootstrap "$domain" "$plist" 2>/dev/null && return 0
    sleep "${RELOAD_WAIT:-1}"
  done
  echo "Could not start $label: run 'launchctl bootstrap $domain $plist' to see why, then re-run."
  return 1
}

# The https address nodes and phones use (tailscale serve on 8443 -> 127.0.0.1:8701), e.g.
# mac.tailnet.ts.net:8443. The controller prints it in enrollment commands and sign-in links.
# Empty when Tailscale isn't installed or signed in; FLEET_PUBLIC_HOST in master/litellm/.env still wins.
public_host() {
  [ -x "$TS" ] || return 0
  "$TS" status --json 2>/dev/null | python3 -c '
import json, sys
name = ((json.load(sys.stdin).get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
print(name + ":8443" if name else "")' 2>/dev/null || true
}

# Tag this Mac tag:master without touching its other Tailscale preferences (exit node, routes, DNS, SSH).
# `tailscale set` changes only the flags given; older CLIs without it get `tailscale up` without --reset, which
# Tailscale refuses if other non-default flags would be dropped. --reset only with --reset-tailscale.
tag_master() {
  local help=""
  help="$("$TS" set --help 2>&1 || true)"
  if [[ "$help" == *advertise-tags* ]]; then
    "$TS" set --advertise-tags=tag:master && return 0
  else
    "$TS" up --advertise-tags=tag:master && return 0
  fi
  if [ "$RESET_TAILSCALE" = 1 ]; then
    echo "--reset-tailscale: retrying with 'tailscale up --reset'. This resets this Mac's other Tailscale"
    echo "settings (exit node, subnet routes, DNS, Tailscale SSH) to defaults."
    "$TS" up --advertise-tags=tag:master --reset && return 0
  fi
  echo "Could not tag this Mac as tag:master. Two usual causes:"
  echo " - the tailnet policy has no tag:master owner: paste tailscale/policy.hujson into the Tailscale admin"
  echo "   console (Access controls) first, then re-run setup;"
  echo " - Tailscale wants this Mac's existing non-default flags repeated (see its message above). Either run"
  echo "   the command it suggests, keeping --advertise-tags=tag:master, then re-run setup; or re-run"
  echo "   'bash master/setup.sh --reset-tailscale' to accept resetting this Mac's other Tailscale settings"
  echo "   (exit node, routes, DNS, SSH)."
  return 1
}

# Tests source this file with SETUP_SOURCE_ONLY=1 to call the helpers above.
[ "${SETUP_SOURCE_ONLY:-0}" = 1 ] && return 0

command -v python3 >/dev/null || brew install python
command -v uv >/dev/null || brew install uv
UV="$(command -v uv)"

if [ "$SERVICES_ONLY" = 0 ]; then
# 1. Tailnet identity: the policy lets only tag:master reach node model ports.
[ -x "$TS" ] || { brew install --cask tailscale; open -a Tailscale; echo "Sign in to Tailscale, then press Enter."; read -r; }
tag_master || exit 1
# Token-protected console port for phones and for node enrollment (never point serve at 8700).
if ! "$TS" serve --bg --https=8443 http://127.0.0.1:8701; then
  echo "tailscale serve failed: enable HTTPS certificates (admin console > DNS), then run"
  echo "  $TS serve --bg --https=8443 http://127.0.0.1:8701"
fi

# 2. Gateway. bootstrap.sh prints a virtual key on success.
"$HERE/litellm/bootstrap.sh" | tee /dev/stderr | sed -n 's/^Virtual key for Laptop 1: //p' > "$HERE/litellm/.vkey" \
  || { rm -f "$HERE/litellm/.vkey"; echo "Gateway setup incomplete (see above). Fix and re-run."; exit 1; }
if [ -s "$HERE/litellm/.vkey" ]; then
  security delete-generic-password -s litellm-laptop1 >/dev/null 2>&1 || true
  security add-generic-password -s litellm-laptop1 -a "$USER" -w "$(cat "$HERE/litellm/.vkey")"
fi
rm -f "$HERE/litellm/.vkey"
fi

# Ollama as a login service so the master's own models stay up (and reachable from LiteLLM's container).
if command -v brew >/dev/null && brew list ollama >/dev/null 2>&1; then
  brew services start ollama >/dev/null || true
fi

# 3. Fleet controller as a login agent (dashboard: http://127.0.0.1:8700).
mkdir -p "$HOME/Library/LaunchAgents"
PLIST="$HOME/Library/LaunchAgents/local.ai-fleet.controller.plist"
CONTROLLER_ENV=(TAILSCALE_BIN "$TS" FLEET_MASTER_MODEL_HOST "host.docker.internal")
PUBLIC_HOST="$(public_host)"
[ -n "$PUBLIC_HOST" ] && CONTROLLER_ENV+=(FLEET_PUBLIC_HOST "$PUBLIC_HOST")
write_launchd_plist "$PLIST" "local.ai-fleet.controller" 1 1 "" \
  "$HOME/Library/Logs/ai-fleet-controller.log" "$HOME/Library/Logs/ai-fleet-controller.log" \
  "$(( ${#CONTROLLER_ENV[@]} / 2 ))" "${CONTROLLER_ENV[@]}" \
  "/bin/bash" "-c" 'set -a; . "$0/../litellm/.env"; set +a; exec "$1" run --script "$0/controller.py"' \
  "$HERE/controller" "$UV"
reload_launch_agent "$PLIST"

# 4. The master's own agent runner (localhost only): lets tasks use this Mac's Claude Code, Codex, Ollama.
mkdir -p "$HOME/.ai-fleet" && chmod 700 "$HOME/.ai-fleet"
python3 - "$HOME/.ai-fleet/runner.json" "$REPO_DIR" <<'PY'
import json
import os
import sys

path, repo = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as handle:
        config = json.load(handle)
except (OSError, ValueError):
    config = {}
if not isinstance(config, dict):
    config = {}
if "allowed_roots" not in config:
    config.update({
        "allowed_roots": [os.path.realpath(repo)],
        "gate_profiles": {
            "pytest": ["python3", "-m", "pytest", "-q"],
            "unittest": ["python3", "-m", "unittest", "-q"],
        },
    })
config.setdefault("max_mode", "auto-edit")
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2, sort_keys=True)
    handle.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
# Fleet Claude tasks use their own Claude Code profile, so they never share or disturb your own sessions.
# Sign it in once: CLAUDE_CONFIG_DIR=~/.claude-fleet claude   (then /login)
mkdir -p "$HOME/.claude-fleet" && chmod 700 "$HOME/.claude-fleet"
# Fleet Codex tasks get a lean profile too (docs/SPEC-paid-budget.md 3.6): medium reasoning effort by default, no
# plugins, memories or notify hook. Effort per task is passed on the command line, never written here.
# Sign it in once: CODEX_HOME=~/.codex-fleet codex login   (until then the runner uses your default Codex home).
# Setup never reads or copies your Codex credentials.
mkdir -p "$HOME/.codex-fleet" && chmod 700 "$HOME/.codex-fleet"
if [ ! -e "$HOME/.codex-fleet/config.toml" ]; then
  {
    # Only the model line is taken from your own Codex config, if it names one.
    grep -m1 -E '^model[[:space:]]*=' "$HOME/.codex/config.toml" 2>/dev/null || true
    echo 'model_reasoning_effort = "medium"'
  } > "$HOME/.codex-fleet/config.toml"
  chmod 600 "$HOME/.codex-fleet/config.toml"
fi
RPLIST="$HOME/Library/LaunchAgents/local.ai-fleet.runner.plist"
write_launchd_plist "$RPLIST" "local.ai-fleet.runner" 1 1 "" "" \
  "$HOME/Library/Logs/ai-fleet-runner.log" 5 \
  RUNNER_BIND "127.0.0.1:11436" \
  CLAUDE_CONFIG_DIR "$HOME/.claude-fleet" \
  FLEET_DIR "$HOME/.ai-fleet" \
  OLLAMA_URL "http://127.0.0.1:11434" \
  PATH "$HOME/.local/bin:$HOME/.claude/local:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
  "$(command -v python3)" "$HERE/../nodes/runner.py"
reload_launch_agent "$RPLIST"

# Store the generated local credential in the controller DB without putting it in argv or logs.
registered=0
for _ in $(seq 1 30); do
  if [ -s "$HOME/.ai-fleet/runner.token" ] \
      && "$UV" run --script "$HERE/controller/controller.py" runner-token set master \
           < "$HOME/.ai-fleet/runner.token" >/dev/null 2>&1; then
    registered=1
    break
  fi
  sleep 1
done
[ "$registered" = 1 ] || { echo "Could not register the master runner token; re-run setup."; exit 1; }

[ "$SERVICES_ONLY" = 1 ] && { echo "Services ready. Console: http://127.0.0.1:8700/app/"; exit 0; }

# 5. Claude Code: Codex as a sub-agent, fleet bridge for local models + fleet management + tasks.
claude mcp remove --scope user codex >/dev/null 2>&1 || true
claude mcp remove --scope user local-llm >/dev/null 2>&1 || true
claude mcp add --scope user codex -- "$CODEX" mcp-server
claude mcp add --scope user local-llm -- "$UV" run --script "$HERE/mcp/server.py"
claude mcp list

echo "Master ready. Dashboard: http://127.0.0.1:8700  Logs: ~/Library/Logs/ai-fleet-controller.log"

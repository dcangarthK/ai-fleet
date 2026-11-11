#!/usr/bin/env bash
# AI fleet onboarding for macOS and Linux. Copy this one file to a machine and run it:
#   ./onboard.sh                  interactive: join fleet, set up Ollama, scan, publish inventory
#   ./onboard.sh --yes            accept all defaults (installs + recommended models)
#   ./onboard.sh --models "qwen3:8b qwen3:4b"   pull these models
#   ./onboard.sh --allow-root ~/code --allow-root /srv/repos
#   ./onboard.sh --scan-only      just rescan and republish (runs hourly after onboarding)
# Run as your normal user; it asks for sudo when it needs it.
set -euo pipefail

FLEET_DIR="${FLEET_DIR:-$HOME/.ai-fleet}"
INFO_PORT=11435
SCAN_ONLY=0; YES=0; MODELS=""; ALLOW_ROOTS_SET=0; ALLOW_ROOTS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --scan-only) SCAN_ONLY=1 ;;
    --yes|-y) YES=1 ;;
    --models) MODELS="$2"; shift ;;
    --allow-root) ALLOW_ROOTS+=("$2"); ALLOW_ROOTS_SET=1; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
  shift
done

OS="$(uname -s)"
[ "$OS" = Darwin ] || [ "$OS" = Linux ] || { echo "Unsupported OS $OS (use onboard.ps1 on Windows)"; exit 1; }
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ask() { [ "$YES" = 1 ] && return 0; read -rp "$1 [Y/n] " a; [[ -z "$a" || "$a" =~ ^[Yy] ]]; }
have() { command -v "$1" >/dev/null 2>&1; }
mkdir -p "$FLEET_DIR/public"

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

systemd_quote() {
  local mode="$1"; shift
  python3 - "$mode" "$@" <<'PY'
import sys

mode, *values = sys.argv[1:]
escapes = {
    "\a": r"\a", "\b": r"\b", "\f": r"\f", "\n": r"\n",
    "\r": r"\r", "\t": r"\t", "\v": r"\v", "\\": r"\\",
    '"': r'\"',
}

def quote(value):
    value = value.replace("%", "%%")
    if mode == "exec":
        value = value.replace("$", "$$")
    escaped = "".join(escapes.get(char, f"\\x{ord(char):02x}" if ord(char) < 32 or ord(char) == 127 else char) for char in value)
    return f'"{escaped}"'

rendered = " ".join(quote(value) for value in values)
sys.stdout.buffer.write(rendered.encode("utf-8") + b"\n")
PY
}

ram_gb() {
  if [ "$OS" = Darwin ]; then echo $(( $(sysctl -n hw.memsize) / 1073741824 ))
  else awk '/MemTotal/ {print int($2/1048576)}' /proc/meminfo; fi
}
gpu_gb() {  # largest NVIDIA GPU memory in GB, 0 if none
  have nvidia-smi && nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null \
    | sort -n | tail -1 | awk '{print int($1/1024)}' || echo 0
}

# ---------- prerequisites ----------
ensure_basics() {
  if [ "$OS" = Darwin ]; then
    have brew || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
    have python3 || brew install python
  else
    local pm=""
    for p in apt-get dnf yum pacman zypper apk; do have "$p" && { pm="$p"; break; }; done
    pkg() {
      case "$pm" in
        apt-get|dnf|yum) $SUDO "$pm" install -y "$@" ;;
        pacman) $SUDO pacman -S --noconfirm "$@" ;;
        zypper) $SUDO zypper --non-interactive install "$@" ;;
        apk) $SUDO apk add "$@" ;;
        *) echo "No supported package manager; install $* yourself and re-run."; exit 1 ;;
      esac
    }
    have curl || pkg curl
    have python3 || pkg python3
    have systemctl || { echo "This script needs systemd on Linux."; exit 1; }
  fi
}

# ---------- Tailscale ----------
TS=""
find_tailscale() {
  if [ "$OS" = Darwin ] && [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then
    TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
  elif have tailscale; then TS="$(command -v tailscale)"; fi
}
ensure_tailscale() {
  find_tailscale
  if [ -z "$TS" ]; then
    say "Installing Tailscale"
    if [ "$OS" = Darwin ]; then
      brew install --cask tailscale; open -a Tailscale
      echo "Sign in to Tailscale from the menu bar (same account as the master), then press Enter."; read -r
    else
      curl -fsSL https://tailscale.com/install.sh | sh
    fi
    find_tailscale
  fi
  local tags
  tags="$("$TS" status --json 2>/dev/null | python3 -c 'import sys,json;print(",".join(json.load(sys.stdin).get("Self",{}).get("Tags") or []))' 2>/dev/null || true)"
  if [[ "$tags" != *tag:llm-node* ]]; then
    say "Joining the tailnet as tag:llm-node (log in if a URL appears)"
    # No --reset: keep the machine's existing Tailscale settings (routes, DNS, exit node).
    local up=("$TS" up --advertise-tags=tag:llm-node); [ "$OS" = Linux ] && up=($SUDO "${up[@]}")
    if ! "${up[@]}"; then
      echo "Tailscale wants the machine's existing non-default flags repeated. Run the command it"
      echo "suggests above, keeping --advertise-tags=tag:llm-node, then re-run this script."; exit 1
    fi
  fi
}
tailnet_ip() { [ -n "$TS" ] && "$TS" ip -4 2>/dev/null | head -1 || true; }

# ---------- Ollama ----------
recommended_models() {
  local ram gpu; ram="$(ram_gb)"; gpu="$(gpu_gb)"
  if [ "$gpu" -ge 24 ] || [ "$ram" -ge 48 ]; then echo "qwen3-coder:30b gpt-oss:20b qwen3:8b"
  elif [ "$ram" -ge 32 ]; then echo "qwen3-coder:30b qwen3:14b qwen3:4b"
  elif [ "$ram" -ge 16 ]; then echo "qwen2.5-coder:14b qwen3:8b qwen3:4b"
  else echo "qwen3:4b"; fi
}

# Runs `ollama serve` from our own LaunchAgent so the tailnet bind survives logout and reboot
# (launchctl setenv, what Ollama's FAQ suggests, is lost on reboot).
run_ollama_mac() {
  local ip="$1" bin la="$HOME/Library/LaunchAgents/local.ai-fleet.ollama.plist"
  local env_vars=(
    OLLAMA_HOST "$ip:11434"
    OLLAMA_KEEP_ALIVE "10m"
    OLLAMA_FLASH_ATTENTION "1"
    OLLAMA_KV_CACHE_TYPE "q8_0"
  )
  bin="$(command -v ollama || echo /Applications/Ollama.app/Contents/Resources/ollama)"
  brew services stop ollama >/dev/null 2>&1 || true
  osascript -e 'quit app "Ollama"' >/dev/null 2>&1 || true
  if [ "$(ram_gb)" -le 16 ]; then
    env_vars+=(
      OLLAMA_MAX_LOADED_MODELS "1"
      OLLAMA_NUM_PARALLEL "1"
      OLLAMA_CONTEXT_LENGTH "16384"
    )
  fi
  mkdir -p "$HOME/Library/LaunchAgents"
  write_launchd_plist "$la" "local.ai-fleet.ollama" 1 1 "" "" "$FLEET_DIR/ollama.log" \
    "$(( ${#env_vars[@]} / 2 ))" "${env_vars[@]}" "$bin" "serve"
  launchctl bootout "gui/$(id -u)" "$la" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$la"
  [ -d /Applications/Ollama.app ] && echo "Ollama.app was quit; remove it from Login Items so it doesn't start a second server."
}

configure_ollama() {
  local ip="$1"
  if ! have ollama && [ ! -d /Applications/Ollama.app ]; then
    ask "Ollama is not installed. Install it to serve local models?" || return 0
    if [ "$OS" = Darwin ]; then brew install ollama; else curl -fsSL https://ollama.com/install.sh | sh; fi
  fi
  say "Configuring Ollama to listen on the tailnet only ($ip:11434)"
  if [ "$OS" = Darwin ]; then
    run_ollama_mac "$ip"
  else
    local env_host env_keep_alive env_flash_attention
    env_host="$(systemd_quote environment "OLLAMA_HOST=$ip:11434")"
    env_keep_alive="$(systemd_quote environment "OLLAMA_KEEP_ALIVE=10m")"
    env_flash_attention="$(systemd_quote environment "OLLAMA_FLASH_ATTENTION=1")"
    $SUDO mkdir -p /etc/systemd/system/ollama.service.d
    $SUDO tee /etc/systemd/system/ollama.service.d/fleet.conf >/dev/null <<EOF
[Unit]
After=tailscaled.service
Wants=tailscaled.service
[Service]
Environment=$env_host
Environment=$env_keep_alive
Environment=$env_flash_attention
Restart=always
RestartSec=5
EOF
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now ollama
    $SUDO systemctl restart ollama
  fi
  for _ in $(seq 1 30); do curl -fsS "http://$ip:11434/api/tags" >/dev/null 2>&1 && break; sleep 1; done

  local have_models want
  have_models="$(curl -fsS "http://$ip:11434/api/tags" 2>/dev/null | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["models"]))' 2>/dev/null || echo 0)"
  want="$MODELS"
  if [ -z "$want" ] && [ "$have_models" = 0 ]; then
    want="$(recommended_models)"
    ask "No models yet. Pull recommended models for $(ram_gb)GB RAM ($want)?" || want=""
  fi
  for m in $want; do OLLAMA_HOST="$ip:11434" ollama pull "$m"; done
}

# ---------- inventory scan ----------
scan() {
  FLEET_DIR="$FLEET_DIR" TS_IP="$1" python3 - <<'PY'
import glob, json, os, platform, shutil, socket, subprocess, time, urllib.request
from pathlib import Path

home = Path.home()
ip = os.environ.get("TS_IP", "")
mac = platform.system() == "Darwin"

def run(cmd, timeout=5):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (out.stdout or out.stderr).strip()
    except Exception:
        return ""

def get_json(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None

def hardware():
    hw = {"os": platform.system(), "os_version": platform.release(), "arch": platform.machine(),
          "hostname": socket.gethostname().split(".")[0].lower()}
    if mac:
        hw["cpu"] = run(["sysctl", "-n", "machdep.cpu.brand_string"])
        hw["ram_gb"] = int(run(["sysctl", "-n", "hw.memsize"]) or 0) // 2**30
        hw["gpu"] = "Apple Silicon (unified memory)" if platform.machine() == "arm64" else ""
    else:
        hw["cpu"] = next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")), "")
        hw["ram_gb"] = int(next(l.split()[1] for l in open("/proc/meminfo") if l.startswith("MemTotal"))) // 2**20
        gpus = run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"]) if shutil.which("nvidia-smi") else ""
        hw["gpu"] = gpus.replace("\n", "; ")
    return hw

# Model servers: port -> (engine, path). Probed on loopback and on the tailnet IP.
# ROUTED ports match the controller probes, Tailscale policy and Windows firewall; others are inventory-only.
ROUTED = {11434, 8000, 1234, 8080}
SERVERS = {11434: ("ollama", "/api/tags"), 1234: ("lmstudio", "/v1/models"), 8000: ("vllm/openai", "/v1/models"),
           8080: ("llama.cpp/openai", "/v1/models"), 1337: ("jan", "/v1/models"), 4891: ("gpt4all", "/v1/models"),
           5000: ("openai-compatible", "/v1/models")}

def servers():
    found = []
    for port, (engine, path) in SERVERS.items():
        for host in ([ip] if ip else []) + ["127.0.0.1"]:
            body = get_json(f"http://{host}:{port}{path}")
            if body is None:
                continue
            if engine == "ollama":
                models = [{"name": m.get("name"), "size_gb": round(m.get("size", 0) / 1e9, 1),
                           "params": (m.get("details") or {}).get("parameter_size"),
                           "quant": (m.get("details") or {}).get("quantization_level")} for m in body.get("models", [])]
            else:
                models = [{"name": m.get("id")} for m in body.get("data", [])]
            found.append({"engine": engine, "port": port, "models": models,
                          "reachable_from_master": bool(ip) and host == ip and port in ROUTED,
                          "hint": ("" if host == ip and port in ROUTED
                                   else f"port {port} isn't routed by the fleet; serve it on 8000, 1234 or 8080" if host == ip
                                   else f"bound to localhost only; bind it to {ip or 'the tailnet IP'} to use it in the fleet")})
            break
    return found

def installed_engines():
    checks = {
        "ollama": shutil.which("ollama") or ("/Applications/Ollama.app" if Path("/Applications/Ollama.app").exists() else None),
        "lmstudio": shutil.which("lms") or next((p for p in ["/Applications/LM Studio.app", str(home / ".lmstudio")] if Path(p).exists()), None),
        "llama.cpp": shutil.which("llama-server") or shutil.which("llama-cli"),
        "vllm": shutil.which("vllm"),
        "mlx-lm": shutil.which("mlx_lm.server") or shutil.which("mlx_lm.generate"),
        "jan": next((p for p in ["/Applications/Jan.app", str(home / "jan")] if Path(p).exists()), None),
        "gpt4all": next(iter(glob.glob("/Applications/GPT4All*")), None),
        "localai": shutil.which("local-ai"),
        "docker": shutil.which("docker"),
    }
    return {k: v for k, v in checks.items() if v}

def model_files():
    files = []
    hf = home / ".cache/huggingface/hub"
    for d in sorted(hf.glob("models--*"))[:200]:
        files.append({"source": "huggingface", "name": d.name[8:].replace("--", "/")})
    lms = home / ".lmstudio/models"
    for f in sorted(lms.glob("*/*/*.gguf"))[:200] if lms.exists() else []:
        files.append({"source": "lmstudio", "name": f"{f.parent.parent.name}/{f.parent.name}/{f.name}", "size_gb": round(f.stat().st_size / 1e9, 1)})
    seen = set()
    for base in [home / "models", home / "Models", home / "Downloads", home / "llama.cpp/models"]:
        for f in list(base.glob("**/*.gguf"))[:100] if base.exists() else []:
            if f.name not in seen:
                seen.add(f.name)
                files.append({"source": "gguf", "name": f.name, "path": str(f), "size_gb": round(f.stat().st_size / 1e9, 1)})
    return files

def docker_llm_containers():
    if not shutil.which("docker"):
        return []
    out = run(["docker", "ps", "--format", "{{.Image}}\t{{.Ports}}\t{{.Names}}"])
    keys = ("ollama", "vllm", "llama", "localai", "text-generation", "tgi", "lmdeploy", "sglang", "litellm", "kimi")
    return [dict(zip(["image", "ports", "name"], l.split("\t"))) for l in out.splitlines() if any(k in l.lower() for k in keys)]

AGENT_CLIS = {"claude": "Claude Code", "codex": "OpenAI Codex", "gemini": "Gemini CLI", "qwen": "Qwen Code",
              "kimi": "Kimi CLI", "aider": "Aider", "opencode": "OpenCode", "goose": "Goose", "crush": "Crush",
              "cursor-agent": "Cursor Agent", "amp": "Amp", "copilot": "GitHub Copilot CLI", "llm": "llm (Simon Willison)"}
DESKTOP_APPS = ["Claude", "Codex", "ChatGPT", "Cursor", "Windsurf", "Zed", "LM Studio", "Ollama", "Jan", "Msty", "AnythingLLM"]

# Per-user install dirs are often missing from PATH in scripts and launchd/systemd jobs.
USER_BIN_DIRS = [home / ".local/bin", home / ".claude/local", home / ".npm-global/bin", home / ".bun/bin",
                 home / ".cargo/bin", home / ".volta/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
SEARCH_PATH = os.pathsep.join([os.environ.get("PATH", "")] + [str(d) for d in USER_BIN_DIRS]
                              + glob.glob(str(home / ".nvm/versions/node/*/bin")))
BUNDLED = {"codex": "/Applications/Codex.app/Contents/Resources/codex"}

def agents():
    found = []
    for cmd, label in AGENT_CLIS.items():
        path = shutil.which(cmd, path=SEARCH_PATH)
        if not path and Path(BUNDLED.get(cmd, "/nonexistent")).exists():
            path = BUNDLED[cmd]
        if path:
            version = run([path, "--version"]).splitlines()[:1]
            found.append({"cli": cmd, "name": label, "path": path, "version": version[0] if version else ""})
    return found

def apps():
    if mac:
        return [a for a in DESKTOP_APPS if Path(f"/Applications/{a}.app").exists() or (home / f"Applications/{a}.app").exists()]
    return [a for a in DESKTOP_APPS if shutil.which(a.lower().replace(" ", "-"))]

srv = servers()
manifest = {
    "schema": 1, "scanned_at": int(time.time()), "tailnet_ip": ip,
    "hardware": hardware(), "servers": srv, "engines": installed_engines(),
    "model_files": model_files(), "docker": docker_llm_containers(),
    "agents": agents(), "apps": apps(),
}
fleet_dir = Path(os.environ["FLEET_DIR"])
(fleet_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
# Published copy: no file paths (they leak usernames/layout).
public = json.loads(json.dumps(manifest))
for f in public["model_files"]:
    f.pop("path", None)
for a in public["agents"]:
    a.pop("path", None)
public["engines"] = sorted(public["engines"])
(fleet_dir / "public" / "fleet.json").write_text(json.dumps(public, indent=2))

hw = manifest["hardware"]
print(f"\n{hw['hostname']}: {hw['os']} {hw['arch']}, {hw['ram_gb']}GB RAM, {hw.get('gpu') or 'no GPU detected'}")
print(f"Tailnet IP: {ip or 'not connected'}")
print("\nModel servers:")
for s in srv:
    flag = "fleet-ready" if s["reachable_from_master"] else "LOCAL ONLY"
    print(f"  {s['engine']:<18} :{s['port']:<6} {len(s['models'])} models  [{flag}]")
    for m in s["models"][:12]:
        print(f"      - {m['name']}")
    if s["hint"]:
        print(f"      hint: {s['hint']}")
if not srv:
    print("  none running")
print("\nInstalled engines: " + (", ".join(manifest["engines"]) or "none"))
print(f"Model files on disk: {len(manifest['model_files'])}")
for f in manifest["model_files"][:10]:
    print(f"  - [{f['source']}] {f['name']}")
if manifest["docker"]:
    print("LLM containers: " + ", ".join(c["image"] for c in manifest["docker"]))
print("Agent CLIs: " + (", ".join(f"{a['name']} {a['version']}".strip() for a in manifest["agents"]) or "none"))
print("Desktop AI apps: " + (", ".join(manifest["apps"]) or "none"))
print(f"\nInventory: {fleet_dir / 'manifest.json'}")
PY
}

# ---------- publish inventory + hourly rescan ----------
install_services() {
  local ip="$1" self="$FLEET_DIR/onboard.sh" py
  py="$(command -v python3)"
  [ "$(cd "$(dirname "$0")" && pwd)/$(basename "$0")" = "$self" ] || cp "$0" "$self"
  chmod +x "$self"
  say "Publishing inventory on $ip:$INFO_PORT (tailnet only) and rescanning hourly"
  if [ "$OS" = Darwin ]; then
    local la="$HOME/Library/LaunchAgents"; mkdir -p "$la"
    write_launchd_plist "$la/local.ai-fleet.info.plist" "local.ai-fleet.info" 1 1 "" "" "/dev/null" 0 \
      "$py" "-m" "http.server" "$INFO_PORT" "--bind" "$ip" "--directory" "$FLEET_DIR/public"
    write_launchd_plist "$la/local.ai-fleet.scan.plist" "local.ai-fleet.scan" "" "" 3600 \
      "$FLEET_DIR/scan.log" "$FLEET_DIR/scan.log" 2 \
      PATH "$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
      FLEET_DIR "$FLEET_DIR" \
      "/bin/bash" "$self" "--scan-only"
    for job in info scan; do
      launchctl bootout "gui/$(id -u)" "$la/local.ai-fleet.$job.plist" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$la/local.ai-fleet.$job.plist"
    done
  else
    local user info_exec scan_exec scan_fleet_env
    user="$(id -un)"
    info_exec="$(systemd_quote exec "$py" "-m" "http.server" "$INFO_PORT" "--bind" "$ip" "--directory" "$FLEET_DIR/public")"
    scan_exec="$(systemd_quote exec "/bin/bash" "$self" "--scan-only")"
    scan_fleet_env="$(systemd_quote environment "FLEET_DIR=$FLEET_DIR")"
    $SUDO tee /etc/systemd/system/ai-fleet-info.service >/dev/null <<EOF
[Unit]
Description=AI fleet node inventory (tailnet only)
After=tailscaled.service
Wants=tailscaled.service
[Service]
User=$user
ExecStart=$info_exec
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
    $SUDO tee /etc/systemd/system/ai-fleet-scan.service >/dev/null <<EOF
[Unit]
Description=AI fleet node rescan
[Service]
Type=oneshot
User=$user
Environment=$scan_fleet_env
ExecStart=$scan_exec
EOF
    $SUDO tee /etc/systemd/system/ai-fleet-scan.timer >/dev/null <<EOF
[Unit]
Description=Hourly AI fleet node rescan
[Timer]
OnBootSec=2min
OnUnitActiveSec=1h
[Install]
WantedBy=timers.target
EOF
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now ai-fleet-info.service ai-fleet-scan.timer
    $SUDO systemctl restart ai-fleet-info.service
  fi
}

# ---------- agent runner ----------
# The runner lets the master start Claude Code / Codex / Ollama jobs here (tailnet only, port 11436).
# Its source is embedded below by nodes/build.py from nodes/runner.py; edit runner.py, then rebuild.
write_runner() {
  cat > "$FLEET_DIR/runner.py" <<'RUNNER_PY'
# >>> runner.py (generated by nodes/build.py; do not edit here)
#!/usr/bin/env python3
"""Small, dependency-free HTTP runner for detached agent jobs.

CLI note: the installed Codex CLI (checked 2026-11-10) accepts ``-s`` on
``codex exec`` but not on ``codex exec resume``.  Resumes therefore use the
equivalent ``-c sandbox_mode=...`` setting.  Claude was not installed on the
development machine; its print/stream-json flags follow the runner spec.

Prompts go to agents on stdin (a 0600 file), never argv: argv is visible to every local user
via ps, and Claude's variadic --allowedTools would otherwise swallow a trailing prompt.
"""

import datetime
import glob
import hmac
import json
import math
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


VERSION = "2"
MODES = ("read-only", "supervised", "auto-edit", "full")
MODE_RANK = {name: rank for rank, name in enumerate(MODES)}
JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
GATE_PROFILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
GATE_ARG = re.compile(r"^[A-Za-z0-9_./:=@+,-]{1,200}$")
MODEL_REF = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}(/[a-z0-9][a-z0-9._-]{0,63})?(:[A-Za-z0-9][A-Za-z0-9._-]{0,63})?$")
DERIVED_MODEL = re.compile(r"^fleet-[a-z0-9][a-z0-9.-]{0,60}$")
KEEP_ALIVE = re.compile(r"^[0-9]+(?:ms|s|m|h)$")
TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\(.{0,200}\))?$")
FINAL_OPERATION_STATES = {"ready", "removed", "failed", "blocked", "cancelled"}
JOB_REQUEST_KEYS = {
    "id", "agent", "prompt", "mode", "repo", "worktree", "cwd", "model", "gate", "gate_args",
    "allowed_tools", "resume_session", "instructions", "policy", "policy_hash",
}
BUILTIN_DENIED = (
    ".ssh", ".aws", ".gnupg", ".config/gcloud", ".kube", ".docker", "Library/Keychains",
)
PUBLIC_JOB_KEYS = (
    "id", "agent", "mode", "state", "exit_code", "session_id", "cwd",
    "worktree", "branch", "started_at", "ended_at", "result", "denials",
    "gate", "runs", "instructions", "policy_hash",
)


def validate_job_policy(value):
    if not isinstance(value, dict):
        raise HttpError(400, "policy must be an object")
    allowed = {"filesystem", "tools", "network", "limits"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise HttpError(400, "unknown policy field: " + unknown[0])
    nested = {"filesystem": {"allowed_roots", "denied"},
              "tools": {"claude_allow", "claude_deny"}, "network": {"agents"},
              "limits": {"task_minutes"}}
    for section, keys in nested.items():
        if section not in value:
            continue
        body = value[section]
        if not isinstance(body, dict):
            raise HttpError(400, section + " policy must be an object")
        extra = sorted(set(body) - keys)
        if extra:
            raise HttpError(400, "unknown %s policy field: %s" % (section, extra[0]))
    for field in ("allowed_roots", "denied"):
        paths = value.get("filesystem", {}).get(field)
        if paths is not None and (not isinstance(paths, list) or any(
                not isinstance(path, str) or not (path.startswith("~/") or os.path.isabs(path)) for path in paths)):
            raise HttpError(400, "filesystem.%s must be an absolute path list" % field)
    for field in ("claude_allow", "claude_deny"):
        tools = value.get("tools", {}).get(field)
        if tools is not None and (not isinstance(tools, list) or any(
                not isinstance(tool, str) or not TOOL_NAME.fullmatch(tool) for tool in tools)):
            raise HttpError(400, "tools.%s is invalid" % field)
    network = value.get("network", {}).get("agents")
    if network is not None and network not in ("allow", "deny"):
        raise HttpError(400, "network.agents must be allow or deny")
    minutes = value.get("limits", {}).get("task_minutes")
    if minutes is not None and (not isinstance(minutes, (int, float)) or isinstance(minutes, bool)
                                or not math.isfinite(minutes) or minutes <= 0):
        raise HttpError(400, "limits.task_minutes must be a positive number")
    return value


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic_json(path, value, mode=None):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def trim(value, limit=20000):
    value = value if isinstance(value, str) else str(value or "")
    return value[-limit:]


def process_alive(pid):
    if not pid:
        return False
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                capture_output=True, text=True, timeout=3,
            )
            return str(pid) in result.stdout
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def boot_time():
    """Seconds since the epoch when this machine booted, or None if unknown."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=3).stdout
            return float(re.search(r"sec = (\d+)", out).group(1))
        if os.name == "nt":
            import ctypes
            ticks = ctypes.windll.kernel32.GetTickCount64
            ticks.restype = ctypes.c_ulonglong  # default int return type would truncate after ~49 days
            return time.time() - ticks() / 1000.0
        if sys.platform.startswith("linux"):
            with open("/proc/stat", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("btime "):
                        return float(line.split()[1])
    except (OSError, AttributeError, ValueError, subprocess.SubprocessError):
        pass
    return None


def started_epoch(job):
    try:
        return datetime.datetime.fromisoformat(job.get("run_started_at") or job["started_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def agent_search_path():
    home = Path.home()
    dirs = [
        home / ".local/bin", home / ".claude/local", home / ".npm-global/bin",
        home / ".bun/bin", home / ".cargo/bin", home / ".volta/bin",
        Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
    ]
    dirs.extend(Path(item) for item in glob.glob(str(home / ".nvm/versions/node/*/bin")))
    return os.pathsep.join([os.environ.get("PATH", "")] + [str(item) for item in dirs])


def find_agents(ollama_url):
    search = agent_search_path()
    codex_bundled = "/Applications/Codex.app/Contents/Resources/codex"
    codex = shutil.which("codex", path=search)
    if not codex and os.path.isfile(codex_bundled) and os.access(codex_bundled, os.X_OK):
        codex = codex_bundled
    claude = shutil.which("claude", path=search)
    if not claude:
        # Claude Code bundled with the Claude desktop app (newest install first).
        bundled = glob.glob(str(Path.home() / "Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude"))
        bundled = [p for p in bundled if os.access(p, os.X_OK)]
        claude = max(bundled, key=os.path.getmtime) if bundled else None
    agents = {
        "claude": claude,
        "codex": codex,
        "ollama": None,
    }
    try:
        with urllib.request.urlopen(ollama_url.rstrip("/") + "/api/tags", timeout=1) as response:
            if 200 <= response.status < 300:
                agents["ollama"] = ollama_url.rstrip("/")
    except (OSError, urllib.error.URLError):
        pass
    return agents


class JobManager:
    def __init__(self, fleet_dir, ollama_url):
        self.root = Path(fleet_dir).expanduser().resolve()
        self.jobs_dir = self.root / "jobs"
        self.worktrees_dir = self.root / "worktrees"
        self.ollama_url = ollama_url.rstrip("/")
        self.lock = threading.RLock()
        self.jobs = {}
        self.gate_threads = set()
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.worktrees_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        config_path = self.root / "runner.json"
        if not config_path.exists():
            atomic_json(config_path, {"max_mode": "auto-edit"}, 0o600)
        config = read_json(config_path, {})
        if not isinstance(config, dict):
            config = {}
        self.max_mode = config.get("max_mode", "auto-edit")
        if self.max_mode not in MODE_RANK:
            self.max_mode = "auto-edit"
        self.allowed_roots = self._path_list(config.get("allowed_roots"))
        configured_denied = self._path_list(config.get("denied_paths"))
        self.denied_paths = [str(self.root)] + [str(Path.home() / item) for item in BUILTIN_DENIED]
        self.denied_paths.extend(configured_denied)
        profiles = config.get("gate_profiles")
        self.gate_profiles = {}
        if isinstance(profiles, dict):
            for name, argv in profiles.items():
                if (isinstance(name, str) and GATE_PROFILE.fullmatch(name)
                        and isinstance(argv, list) and argv
                        and all(isinstance(item, str) and item for item in argv)):
                    self.gate_profiles[name] = list(argv)
        self.allow_raw_gates = config.get("allow_raw_gates", False)
        if not isinstance(self.allow_raw_gates, bool):
            self.allow_raw_gates = False
        max_jobs = config.get("max_jobs", 2)
        self.max_jobs = max_jobs if isinstance(max_jobs, int) and not isinstance(max_jobs, bool) and max_jobs > 0 else 2
        max_minutes = config.get("max_job_minutes", 120)
        self.max_job_minutes = (float(max_minutes)
                                if isinstance(max_minutes, (int, float)) and not isinstance(max_minutes, bool)
                                and math.isfinite(max_minutes) and max_minutes > 0 else 120.0)
        network = config.get("network") if isinstance(config.get("network"), dict) else {}
        self.network = {"agents": "allow" if network.get("agents") == "allow" else "deny"}
        models = config.get("models") if isinstance(config.get("models"), dict) else {}
        remote = models.get("remote_management", False)
        disk = models.get("disk_reserve_gb", 8)
        ram = models.get("ram_reserve_gb", 4)
        loaded = models.get("max_loaded", 1)
        self.models_config = {
            "remote_management": remote if isinstance(remote, bool) else False,
            "disk_reserve_gb": (float(disk) if isinstance(disk, (int, float)) and not isinstance(disk, bool)
                                and math.isfinite(disk) and disk >= 0 else 8),
            "ram_reserve_gb": (float(ram) if isinstance(ram, (int, float)) and not isinstance(ram, bool)
                               and math.isfinite(ram) and ram >= 0 else 4),
            "max_loaded": loaded if isinstance(loaded, int) and not isinstance(loaded, bool) and loaded > 0 else 1,
        }
        self.request_context = threading.local()
        self.agents = find_agents(self.ollama_url)
        self.model_manager = ModelManager(self.root, self.ollama_url, self.models_config)
        self._load_jobs()

    @staticmethod
    def _path_list(value):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return []
        return [item for item in value if os.path.isabs(os.path.expanduser(item))]

    @staticmethod
    def _comparable(path):
        value = os.path.normcase(os.path.realpath(os.path.expanduser(path)))
        return value.casefold() if sys.platform in ("darwin", "win32") else value

    @classmethod
    def _within(cls, path, parent):
        path, parent = cls._comparable(path), cls._comparable(parent)
        try:
            return os.path.commonpath([path, parent]) == parent
        except (ValueError, OSError):
            return False

    def audit(self, event, detail):
        record = {"ts": now(), "event": event, "detail": str(detail),
                  "remote": getattr(self.request_context, "remote", "")}
        path = self.root / "audit.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        finally:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    def check_path(self, value, policy=None):
        expanded = os.path.expanduser(value) if isinstance(value, str) else ""
        if not expanded or not os.path.isabs(expanded):
            raise HttpError(400, "path must be absolute")
        resolved = os.path.realpath(expanded)
        if not os.path.isdir(resolved):
            raise HttpError(400, "path does not exist or is not a directory")
        allowed = any(self._within(resolved, root) for root in self.allowed_roots)
        denied = any(self._within(resolved, path) or self._within(path, resolved) for path in self.denied_paths)
        filesystem = (policy or {}).get("filesystem") or {}
        policy_roots = filesystem.get("allowed_roots")
        if policy_roots is not None:
            allowed = allowed and any(self._within(resolved, root) for root in policy_roots)
        policy_denied = filesystem.get("denied") or []
        denied = denied or any(self._within(resolved, path) or self._within(path, resolved)
                               for path in policy_denied)
        if not allowed or denied:
            self.audit("path_denied", "requested path was outside node policy")
            raise HttpError(403, "path not allowed by this node's runner policy")
        return resolved

    def _check_git_common_dir(self, repo, policy=None):
        found = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--git-common-dir"], capture_output=True,
            text=True, timeout=15,
        )
        if found.returncode == 0:
            common = found.stdout.strip()
            if not os.path.isabs(common):
                common = os.path.join(repo, common)
            self.check_path(common, policy)

    def _gate(self, value, args):
        if not value:
            if args not in (None, []):
                self.audit("gate_denied", "gate_args supplied without a gate")
                raise HttpError(400, "gate_args need a gate profile")
            return None
        if not isinstance(value, str):
            self.audit("gate_denied", "gate was not a string")
            raise HttpError(400, "gate must be a profile name")
        if args is None:
            args = []
        if (not isinstance(args, list) or len(args) > 20
                or any(not isinstance(item, str) or not GATE_ARG.fullmatch(item)
                       or os.path.isabs(item) or ".." in item.split("/") for item in args)):
            self.audit("gate_denied", "invalid gate_args")
            raise HttpError(400, "invalid gate_args")
        if value in self.gate_profiles:
            argv = self.gate_profiles[value] + args
            executable = shutil.which(argv[0], path=agent_search_path())
            if not executable:
                self.audit("gate_denied", "gate profile executable was unavailable")
                raise HttpError(400, "gate profile executable is unavailable")
            argv[0] = executable
            return {"profile": value, "argv": argv, "raw": False, "cmd": shlex.join(argv),
                    "state": "pending", "exit_code": None, "output_tail": ""}
        if self.allow_raw_gates and not args:
            return {"profile": None, "argv": None, "raw": True, "cmd": value,
                    "state": "pending", "exit_code": None, "output_tail": ""}
        self.audit("gate_denied", "unknown gate profile")
        if GATE_PROFILE.fullmatch(value):
            raise HttpError(400, "unknown gate profile %s; raw gate commands are disabled on this node" % value)
        raise HttpError(400, "invalid gate profile; raw gate commands are disabled on this node")

    def _job_dir(self, job):
        return self.jobs_dir / job["id"]

    def _state_path(self, job):
        return self._job_dir(job) / "state.json"

    def _save(self, job):
        atomic_json(self._state_path(job), job, 0o600)

    def _load_jobs(self):
        for state_path in self.jobs_dir.glob("*/state.json"):
            job = read_json(state_path)
            if not isinstance(job, dict) or not JOB_ID.match(str(job.get("id", ""))):
                continue
            self.jobs[job["id"]] = job
        booted = boot_time()
        for job in list(self.jobs.values()):
            # After a reboot the saved pid may belong to an unrelated process: the job is lost.
            started = started_epoch(job)
            if (job.get("state") == "running" and booted and started and started < booted
                    and not (self._job_dir(job) / job.get("exit_file", "exit_code")).exists()):
                job.update({"state": "lost", "ended_at": now()})
                self._parse_events(job)
                self._save(job)
            self.refresh(job)
            if job["state"] == "running":
                self._start_monitor(job)
            elif (job["state"] == "exited" and job.get("exit_code") == 0
                  and job.get("gate") and job["gate"].get("state") in ("pending", "running")):
                # A runner restart can interrupt its (non-agent) gate subprocess.
                gate = job["gate"]
                is_raw = gate.get("raw") is True or not gate.get("argv")
                if is_raw and not self.allow_raw_gates:
                    gate.update({"raw": True, "state": "failed", "exit_code": None,
                                 "output_tail": "raw gate commands are disabled on this node"})
                    self._save(job)
                else:
                    gate["state"] = "pending"
                    self._start_gate(job)

    def public(self, job):
        self.refresh(job)
        return {key: job.get(key) for key in PUBLIC_JOB_KEYS}

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise HttpError(404, "job not found")
            return job

    def list_public(self):
        with self.lock:
            return [self.public(job) for job in sorted(self.jobs.values(), key=lambda item: item["started_at"])]

    def _parse_events(self, job):
        raw_path = self._job_dir(job) / "events.jsonl"
        session_id = None
        result = ""
        denials = []
        try:
            with open(raw_path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            return
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            event_type = event.get("type")
            if event_type in ("system", "result") and event.get("session_id"):
                session_id = event["session_id"]
            if event_type == "thread.started" and event.get("thread_id"):
                session_id = event["thread_id"]
            if event_type == "result":
                if isinstance(event.get("result"), str):
                    result = event["result"]
                current = event.get("permission_denials") or []
                denials = [
                    {"tool": item.get("tool_name") or item.get("tool") or "",
                     "input": item.get("tool_input", item.get("input"))}
                    for item in current if isinstance(item, dict)
                ]
            if event_type in ("item.completed", "item.updated"):
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    result = item["text"]
            if event_type == "ollama.result":
                result = event.get("text") or ""
        job["session_id"] = session_id or job.get("session_id")
        job["result"] = trim(result or job.get("result", ""))
        job["denials"] = denials

    def refresh(self, job):
        with self.lock:
            if job.get("state") != "running":
                return
            exit_path = self._job_dir(job) / job.get("exit_file", "exit_code")
            exit_code = None
            try:
                exit_code = int(exit_path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                pass
            if exit_code is None:
                if not process_alive(job.get("pid")):
                    job["state"] = "lost"
                    job["ended_at"] = now()
                    self._parse_events(job)
                    self._save(job)
                return
            job["state"] = "exited"
            job["exit_code"] = exit_code
            job["ended_at"] = now()
            self._parse_events(job)
            self._save(job)
            if exit_code == 0 and job.get("gate"):
                self._start_gate(job)
            elif job.get("gate"):
                job["gate"]["state"] = "skipped"
                self._save(job)

    def _start_monitor(self, job, process=None):
        def monitor():
            timeout_minutes = min(self.max_job_minutes, job.get("timeout_minutes", self.max_job_minutes))
            deadline = (started_epoch(job) or time.time()) + timeout_minutes * 60
            def alive():
                return process.poll() is None if process is not None else process_alive(job.get("pid"))

            while job.get("state") == "running" and alive():
                if time.time() >= deadline:
                    self._timeout(job)
                    return
                time.sleep(0.2)
            if process is not None:
                try:
                    process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
            # The wrapper writes its code immediately before exiting; allow that write to land.
            for _ in range(20):
                self.refresh(job)
                if job.get("state") != "running":
                    break
                time.sleep(0.05)
        threading.Thread(target=monitor, name="job-" + job["id"], daemon=True).start()

    def _kill_group(self, pid, force=True):
        if not pid:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
        else:
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)

    def _timeout(self, job):
        with self.lock:
            if job.get("state") != "running":
                return
            try:
                self._kill_group(job.get("pid"))
            except (OSError, subprocess.SubprocessError):
                pass
            self._parse_events(job)
            message = "Job timed out after %s minutes" % job.get("timeout_minutes", self.max_job_minutes)
            job.update({"state": "exited", "exit_code": 124, "ended_at": now(),
                        "result": trim(((job.get("result") or "") + "\n" + message).strip())})
            if job.get("gate"):
                job["gate"]["state"] = "skipped"
            self._save(job)

    def _gate_worker(self, job):
        gate = job["gate"]
        try:
            completed = subprocess.run(
                gate["cmd"] if gate.get("raw") else gate["argv"], cwd=job["cwd"],
                shell=bool(gate.get("raw")), capture_output=True,
                text=True, errors="replace", timeout=900,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            with self.lock:
                gate["exit_code"] = completed.returncode
                gate["output_tail"] = trim(output)
                gate["state"] = "passed" if completed.returncode == 0 else "failed"
                self._save(job)
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + (exc.stderr or "")
            with self.lock:
                gate["exit_code"] = None
                gate["output_tail"] = trim(output + "\nGate timed out after 900 seconds")
                gate["state"] = "failed"
                self._save(job)
        finally:
            with self.lock:
                self.gate_threads.discard(job["id"])

    def _start_gate(self, job):
        gate = job["gate"]
        if (gate.get("raw") is True or not gate.get("argv")) and not self.allow_raw_gates:
            gate.update({"raw": True, "state": "failed", "exit_code": None,
                         "output_tail": "raw gate commands are disabled on this node"})
            self._save(job)
            return
        if gate.get("state") == "running" or job["id"] in self.gate_threads:
            return
        gate.update({"state": "running", "exit_code": None, "output_tail": ""})
        self.gate_threads.add(job["id"])
        self._save(job)
        threading.Thread(target=self._gate_worker, args=(job,), name="gate-" + job["id"], daemon=True).start()

    def _make_worktree(self, job_id, repo, policy=None):
        repo = self.check_path(repo, policy)
        self._check_git_common_dir(repo, policy)
        base = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True,
            text=True, timeout=15,
        )
        if base.returncode:
            raise HttpError(400, "repo is not a git repository")
        target = self.worktrees_dir / job_id
        branch = "fleet/" + job_id
        made = subprocess.run(
            ["git", "-C", repo, "worktree", "add", "-b", branch, str(target)],
            capture_output=True, text=True, timeout=60,
        )
        if made.returncode:
            raise HttpError(400, trim(made.stderr.strip() or "could not create worktree"))
        return repo, str(target), branch, base.stdout.strip()

    def create(self, body):
        unknown = sorted(set(body) - JOB_REQUEST_KEYS)
        if unknown:
            self.audit("bad_field", "unknown job field: " + unknown[0])
            raise HttpError(400, "unknown job field: " + unknown[0])
        job_id = body.get("id")
        agent = body.get("agent")
        mode = body.get("mode")
        if not isinstance(job_id, str) or not JOB_ID.match(job_id):
            raise HttpError(400, "invalid job id")
        if agent not in ("claude", "codex", "ollama"):
            raise HttpError(400, "unknown agent")
        if mode not in MODE_RANK:
            raise HttpError(400, "invalid mode")
        if MODE_RANK[mode] > MODE_RANK[self.max_mode]:
            self.audit("mode_denied", "requested mode exceeded max_mode")
            raise HttpError(403, "requested mode exceeds runner max_mode")
        if not isinstance(body.get("prompt"), str):
            raise HttpError(400, "prompt is required")
        instructions = body.get("instructions", "")
        if not isinstance(instructions, str) or len(instructions.encode("utf-8")) > 32 * 1024:
            raise HttpError(400, "instructions must be a string no larger than 32 KB")
        policy = validate_job_policy(body.get("policy", {}))
        policy_hash = body.get("policy_hash")
        if policy_hash is not None and (not isinstance(policy_hash, str) or len(policy_hash) > 128):
            raise HttpError(400, "invalid policy_hash")
        if agent == "ollama" and not body.get("model"):
            raise HttpError(400, "model is required for ollama")
        if body.get("gate") and MODE_RANK[mode] < MODE_RANK["auto-edit"]:
            self.audit("gate_denied", "gate requested below auto-edit mode")
            raise HttpError(400, "a gate needs auto-edit or full mode")
        if not self.agents.get(agent):
            raise HttpError(400, "%s agent is unavailable" % agent)
        gate = self._gate(body.get("gate"), body.get("gate_args"))
        repo = self.check_path(body["repo"], policy) if body.get("repo") else None
        if repo:
            self._check_git_common_dir(repo, policy)
        requested_cwd = self.check_path(body["cwd"], policy) if body.get("cwd") else None
        if requested_cwd:
            self._check_git_common_dir(requested_cwd, policy)
        with self.lock:
            for current in self.jobs.values():
                self.refresh(current)
            if sum(current.get("state") == "running" for current in self.jobs.values()) >= self.max_jobs:
                self.audit("busy", "runner is at max_jobs")
                raise HttpError(429, "runner is busy")
            if job_id in self.jobs or (self.jobs_dir / job_id).exists():
                raise HttpError(409, "duplicate job id")
            job_dir = self.jobs_dir / job_id
            job_dir.mkdir(mode=0o700, parents=False)
            os.chmod(job_dir, 0o700)
            use_worktree = bool(repo and body.get("worktree"))
            branch = None
            base = None
            if use_worktree:
                try:
                    repo, cwd, branch, base = self._make_worktree(job_id, repo, policy)
                except Exception:
                    shutil.rmtree(job_dir, ignore_errors=True)
                    raise
            else:
                cwd = requested_cwd or repo
                if cwd:
                    cwd = os.path.realpath(cwd)
                else:
                    work = job_dir / "work"
                    work.mkdir(mode=0o700)
                    cwd = str(work)
                rev = subprocess.run(
                    ["git", "-C", cwd, "rev-parse", "HEAD"], capture_output=True,
                    text=True,
                )
                if rev.returncode == 0:
                    base = rev.stdout.strip()
            job = {
                "id": job_id, "agent": agent, "mode": mode, "state": "running",
                "exit_code": None, "session_id": body.get("resume_session"), "cwd": cwd,
                "worktree": use_worktree, "branch": branch, "started_at": now(),
                "ended_at": None, "result": "", "denials": [], "gate": gate,
                "runs": 1, "repo": repo, "base": base, "model": body.get("model"),
                "allowed_tools": body.get("allowed_tools") or [], "pid": None,
                "exit_file": "exit_code.1",
                "instructions": instructions, "policy": policy, "policy_hash": policy_hash,
                "timeout_minutes": min(self.max_job_minutes,
                                       policy.get("limits", {}).get("task_minutes", self.max_job_minutes)),
            }
            job["run_started_at"] = job["started_at"]
            self.jobs[job_id] = job
            self._save(job)
            try:
                self._launch(job, body["prompt"], job.get("allowed_tools"), bool(body.get("resume_session")))
            except Exception as exc:
                job.update({"state": "exited", "exit_code": 127, "ended_at": now(), "result": trim(exc)})
                self._save(job)
                raise HttpError(500, "could not start agent")
            return self.public(job)

    def _command(self, job, prompt, allowed_tools, resume):
        agent = job["agent"]
        mode = job["mode"]
        policy = job.get("policy") or {}
        if agent == "claude":
            cmd = [self.agents[agent], "-p", "--output-format", "stream-json", "--verbose"]
            if resume:
                cmd += ["--resume", job["session_id"]]
            if mode == "read-only":
                cmd += ["--permission-mode", "plan"]
            elif mode == "auto-edit":
                cmd += ["--permission-mode", "acceptEdits"]
            elif mode == "full":
                cmd += ["--dangerously-skip-permissions"]
            policy_tools = policy.get("tools") or {}
            requested = [str(item) for item in (allowed_tools or [])]
            if "claude_allow" in policy_tools:
                permitted = policy_tools["claude_allow"]
                requested = ([item for item in requested if item in permitted] if requested else list(permitted))
            if requested:
                cmd += ["--allowedTools", ",".join(requested)]
            denied = list(policy_tools.get("claude_deny") or [])
            if (policy.get("network") or {}).get("agents") == "deny":
                denied += ["WebFetch", "WebSearch"]
            denied = list(dict.fromkeys(denied))
            if denied:
                cmd += ["--disallowedTools", ",".join(denied)]
            if job.get("model"):
                cmd += ["--model", job["model"]]
            return cmd  # prompt arrives on stdin
        if agent == "codex":
            sandbox = "read-only" if mode in ("read-only", "supervised") else "workspace-write"
            network_flag = (((policy.get("network") or {}).get("agents") == "allow")
                            and self.network.get("agents") == "allow")
            if resume:
                cmd = [self.agents[agent], "exec", "resume", "--json"]
                if mode == "full":
                    cmd += ["--dangerously-bypass-approvals-and-sandbox"]
                else:
                    cmd += ["-c", 'sandbox_mode="%s"' % sandbox]
                if job.get("model"):
                    cmd += ["--model", job["model"]]
                if network_flag:
                    cmd += ["-c", "sandbox_workspace_write.network_access=true"]
                cmd += [job["session_id"], "-"]  # "-": prompt from stdin
            else:
                cmd = [self.agents[agent], "exec", "--json"]
                if mode == "full":
                    cmd += ["--dangerously-bypass-approvals-and-sandbox"]
                else:
                    cmd += ["-s", sandbox]
                if job.get("model"):
                    cmd += ["--model", job["model"]]
                if network_flag:
                    cmd += ["-c", "sandbox_workspace_write.network_access=true"]
                cmd.append("-")  # "-": prompt from stdin
            return cmd
        request_path = self._job_dir(job) / ("ollama-request.%d.json" % job["runs"])
        atomic_json(request_path, {
            "url": self.ollama_url, "model": job["model"], "prompt": prompt,
            "instructions": job.get("instructions") or "",
        }, 0o600)
        return [sys.executable, str(Path(__file__).resolve()), "--ollama-worker", str(request_path)]

    def _launch(self, job, prompt, allowed_tools, resume):
        delivered = prompt
        if job["agent"] != "ollama" and job.get("instructions"):
            delivered = ("<fleet-instructions>\n" + job["instructions"]
                         + "\n</fleet-instructions>\n\n" + prompt)
        cmd = self._command(job, delivered, allowed_tools, resume)
        job_dir = self._job_dir(job)
        exit_path = job_dir / job["exit_file"]
        prompt_path = job_dir / ("prompt.%d.txt" % job["runs"])
        fd = os.open(prompt_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(delivered)
        prompt_in = open(prompt_path, "rb")
        raw = open(job_dir / "events.jsonl", "ab", buffering=0)
        env = os.environ.copy()
        env["PATH"] = agent_search_path()
        try:
            if os.name == "nt":
                # Keeping command arguments out of the batch file avoids quoting prompts twice.
                wrapper_path = job_dir / ("run.%d.cmd" % job["runs"])
                env["FLEET_EXIT_FILE"] = str(exit_path)
                wrapper_path.write_text(
                    ('@echo off\n%*\nset "fleet_code=%errorlevel%"\n'
                     '>"%FLEET_EXIT_FILE%" echo %fleet_code%\n'),
                    encoding="utf-8",
                )
                flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                process = subprocess.Popen(
                    ["cmd.exe", "/d", "/c", str(wrapper_path), *cmd],
                    cwd=job["cwd"], env=env, stdin=prompt_in, stdout=raw,
                    stderr=subprocess.STDOUT, creationflags=flags, close_fds=True,
                )
            else:
                wrapper = "%s; code=$?; printf '%%s\\n' \"$code\" > %s" % (
                    shlex.join(cmd), shlex.quote(str(exit_path)),
                )
                process = subprocess.Popen(
                    ["/bin/sh", "-c", wrapper], cwd=job["cwd"], env=env,
                    stdin=prompt_in, stdout=raw, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                )
        finally:
            raw.close()
            prompt_in.close()
        job["pid"] = process.pid
        self._save(job)
        self._start_monitor(job, process)

    def message(self, job, body):
        text = body.get("text")
        if not isinstance(text, str) or not text:
            raise HttpError(400, "text is required")
        with self.lock:
            self.refresh(job)
            if job["state"] == "running":
                raise HttpError(409, "job is running")
            if not job.get("session_id") or job["agent"] == "ollama":
                raise HttpError(409, "job has no resumable session")
            mode = body.get("mode") or job["mode"]
            if mode not in MODE_RANK:
                raise HttpError(400, "invalid mode")
            if MODE_RANK[mode] > MODE_RANK[self.max_mode]:
                self.audit("mode_denied", "requested resume mode exceeded max_mode")
                raise HttpError(403, "requested mode exceeds runner max_mode")
            job.update({
                "mode": mode, "state": "running", "exit_code": None,
                "ended_at": None, "runs": job["runs"] + 1,
                "allowed_tools": body.get("allowed_tools") or [],
                "exit_file": "exit_code.%d" % (job["runs"] + 1),
                "run_started_at": now(),
            })
            if job.get("gate"):
                job["gate"].update({"state": "pending", "exit_code": None, "output_tail": ""})
            self._save(job)
            try:
                self._launch(job, text, job["allowed_tools"], True)
            except Exception as exc:
                job.update({"state": "exited", "exit_code": 127, "ended_at": now(), "result": trim(exc)})
                self._save(job)
                raise HttpError(500, "could not resume agent")
            return self.public(job)

    def cancel(self, job):
        with self.lock:
            self.refresh(job)
            if job["state"] == "running":
                pid = job.get("pid")
                try:
                    if os.name == "nt":
                        self._kill_group(pid)
                    else:
                        self._kill_group(pid, force=False)
                        for _ in range(20):
                            if not process_alive(pid):
                                break
                            time.sleep(0.05)
                        if process_alive(pid):
                            self._kill_group(pid)
                except (OSError, subprocess.SubprocessError):
                    pass
                job.update({"state": "cancelled", "ended_at": now()})
                self._parse_events(job)
                self._save(job)
            return self.public(job)

    def cleanup(self, job):
        with self.lock:
            self.refresh(job)
            if job["state"] == "running":
                raise HttpError(409, "job is running")
            if job.get("worktree") and Path(job["cwd"]).exists():
                # Keep the agent's uncommitted work on its branch before the worktree goes away.
                # Any git failure aborts the cleanup: never force-remove a worktree we couldn't save.
                git = ["git", "-C", job["cwd"], "-c", "user.name=AI Fleet", "-c", "user.email=fleet@localhost"]

                def run_git(*args):
                    out = subprocess.run(git + list(args), capture_output=True, text=True, timeout=120)
                    if out.returncode:
                        raise HttpError(500, trim(out.stderr or "git %s failed; worktree kept" % args[0]))
                    return out.stdout

                if run_git("status", "--porcelain").strip():
                    run_git("add", "-A")
                    run_git("commit", "--no-verify", "-m", "fleet: work from task " + job["id"])
                    if run_git("status", "--porcelain").strip():
                        raise HttpError(500, "worktree still has unsaved changes; kept it")
                removed = subprocess.run(
                    ["git", "-C", job["repo"], "worktree", "remove", "--force", job["cwd"]],
                    capture_output=True, text=True, timeout=60,
                )
                if removed.returncode:
                    raise HttpError(500, trim(removed.stderr or "could not remove worktree"))
            return self.public(job)

    def log(self, job, offset):
        self.refresh(job)
        raw_path = self._job_dir(job) / "events.jsonl"
        lines = []
        try:
            raw_lines = raw_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            raw_lines = []
        for raw_line in raw_lines:
            try:
                event = json.loads(raw_line)
            except ValueError:
                if raw_line.strip():
                    lines.append("error: " + raw_line.strip())
                continue
            event_type = event.get("type", "event")
            if event_type == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if block.get("type") == "text":
                        lines.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        lines.append("tool %s: %s" % (block.get("name", ""), json.dumps(block.get("input"))))
            elif event_type == "result":
                if event.get("result"):
                    lines.append(str(event["result"]))
                if event.get("is_error") and event.get("error"):
                    lines.append("error: " + str(event["error"]))
            elif event_type in ("item.started", "item.completed", "item.updated"):
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    lines.append(str(item["text"]))
                elif item.get("type") in ("command_execution", "mcp_tool_call", "tool_call"):
                    # Codex reports each tool call when it starts and again when it completes:
                    # show the call once, then its output.
                    if event_type == "item.started":
                        label = item.get("command") or item.get("name") or item.get("server") or item.get("type")
                        lines.append("tool: " + str(label))
                    elif event_type == "item.completed" and item.get("aggregated_output"):
                        lines.append(str(item["aggregated_output"]).rstrip()[-4000:])
            elif event_type == "error":
                lines.append("error: " + str(event.get("message") or event))
            elif event_type == "ollama.result":
                lines.append(str(event.get("text") or ""))
        data = ("\n".join(line for line in lines if line) + ("\n" if lines else "")).encode("utf-8")
        offset = min(max(offset, 0), len(data))
        return {"offset": len(data), "text": data[offset:].decode("utf-8", errors="replace")}

    def diff(self, job):
        cwd = job["cwd"]
        inside = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True,
            text=True,
        )
        if inside.returncode:
            raise HttpError(400, "job cwd is not a git repository")
        base = job.get("base") or "HEAD"

        def git_output(args, allow_difference=False):
            result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, errors="replace")
            if result.returncode and not (allow_difference and result.returncode == 1):
                raise HttpError(500, trim(result.stderr or "git diff failed"))
            return result.stdout

        stat = git_output(["git", "diff", "--stat", base, "--"])
        patch = git_output(["git", "diff", "--no-ext-diff", base, "--"])
        untracked_text = git_output(["git", "ls-files", "--others", "--exclude-standard", "-z"])
        for relative in [item for item in untracked_text.split("\0") if item]:
            path = str(Path(cwd) / relative)
            stat += git_output(["git", "diff", "--no-index", "--stat", "--", os.devnull, path], True)
            patch += git_output(["git", "diff", "--no-index", "--", os.devnull, path], True)
        return {"stat": stat[-200000:], "diff": patch[-200000:]}


def free_disk_gb(path):
    """Free disk in GB; FLEET_FREE_DISK_GB overrides it (tests, like FLEET_RAM_GB)."""
    configured = os.environ.get("FLEET_FREE_DISK_GB")
    if configured:
        try:
            value = float(configured)
            if math.isfinite(value) and value >= 0:
                return value
        except ValueError:
            pass
    return shutil.disk_usage(path).free / 1e9


def system_ram_gb():
    configured = os.environ.get("FLEET_RAM_GB")
    if configured:
        try:
            value = float(configured)
            if math.isfinite(value) and value > 0:
                return value
        except ValueError:
            pass
    try:
        if os.name == "nt":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                            ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                            ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                            ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                            ("avail_extended", ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.total_phys / 1e9
        if sys.platform == "darwin":
            result = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3)
            if result.returncode == 0:
                return int(result.stdout.strip()) / 1e9
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        return pages * size / 1e9
    except (AttributeError, OSError, ValueError, subprocess.SubprocessError):
        return 0.0


class ModelManager:
    """Durable, node-local Ollama model operations. No controller policy can widen this config."""

    def __init__(self, root, ollama_url, config):
        self.root = Path(root)
        self.ollama_url = ollama_url.rstrip("/")
        self.registry = os.environ.get("OLLAMA_REGISTRY", "https://registry.ollama.ai/v2").rstrip("/")
        self.config = dict(config)
        self.operations_dir = self.root / "operations"
        self.operations_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.managed_path = self.root / "models.json"
        self.lock = threading.RLock()
        self.operations = {}
        self.cancelled = set()
        for path in self.operations_dir.glob("*.json"):
            operation = read_json(path)
            if isinstance(operation, dict) and isinstance(operation.get("id"), str):
                self.operations[operation["id"]] = operation
        for operation in list(self.operations.values()):
            if operation.get("state") not in FINAL_OPERATION_STATES:
                self._start(operation)

    def _managed(self):
        value = read_json(self.managed_path, [])
        if isinstance(value, dict):
            value = value.get("models", [])
        return {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()

    def _save_managed(self, names):
        atomic_json(self.managed_path, sorted(names), 0o600)

    def _save(self, operation):
        atomic_json(self.operations_dir / (operation["id"] + ".json"), operation, 0o600)

    def _update(self, operation, **fields):
        with self.lock:
            if operation.get("state") == "cancelled" and fields.get("state") != "cancelled":
                return False
            operation.update(fields)
            if operation.get("state") in FINAL_OPERATION_STATES and not operation.get("ended_at"):
                operation["ended_at"] = now()
            self._save(operation)
            return True

    def _request(self, method, path, body=None, timeout=30):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.ollama_url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error")
            except (ValueError, AttributeError):
                detail = None
            raise RuntimeError(detail or "Ollama returned HTTP %s" % exc.code)

    def _tags(self):
        body = self._request("GET", "/api/tags")
        return body.get("models", []) if isinstance(body.get("models"), list) else []

    def listing(self):
        version = self._request("GET", "/api/version").get("version")
        tags = self._tags()
        loaded = self._request("GET", "/api/ps").get("models", [])
        managed = self._managed()
        return {
            "engine": "ollama", "version": version, "free_disk_gb": free_disk_gb(self.root),
            "ram_gb": system_ram_gb(),
            "models": [{"name": item.get("name"), "size_gb": (item.get("size") or 0) / 1e9,
                        "digest": item.get("digest"), "managed": item.get("name") in managed}
                       for item in tags if isinstance(item, dict) and item.get("name")],
            "loaded": [{"name": item.get("name"), "size_gb": (item.get("size") or 0) / 1e9}
                       for item in (loaded if isinstance(loaded, list) else [])
                       if isinstance(item, dict) and item.get("name")],
        }

    @staticmethod
    def _params(value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise HttpError(400, "params must be an object")
        allowed = {"num_ctx", "temperature", "top_p", "seed", "think", "keep_alive"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise HttpError(400, "unknown model parameter: " + unknown[0])
        def number(name, low, high):
            v = value.get(name)
            if name in value and (not isinstance(v, (int, float)) or isinstance(v, bool)
                                  or not math.isfinite(v) or v < low or v > high):
                raise HttpError(400, "invalid " + name)
        if "num_ctx" in value and (not isinstance(value["num_ctx"], int) or isinstance(value["num_ctx"], bool)
                                   or not 512 <= value["num_ctx"] <= 131072):
            raise HttpError(400, "invalid num_ctx")
        number("temperature", 0, 2)
        number("top_p", 0, 1)
        if "seed" in value and (not isinstance(value["seed"], int) or isinstance(value["seed"], bool)):
            raise HttpError(400, "invalid seed")
        if "think" in value and not isinstance(value["think"], bool):
            raise HttpError(400, "invalid think")
        if "keep_alive" in value and (not isinstance(value["keep_alive"], str)
                                      or not KEEP_ALIVE.fullmatch(value["keep_alive"])):
            raise HttpError(400, "invalid keep_alive")
        return dict(value)

    @staticmethod
    def _validate_ref(reference):
        if not isinstance(reference, str) or not MODEL_REF.fullmatch(reference):
            raise HttpError(400, "invalid model reference")

    def _validate(self, kind, body):
        allowed = ({"ref", "params", "derived", "override_fit"} if kind == "ensure"
                   else {"ref", "derived", "confirm_preexisting"})
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise HttpError(400, "unknown model field: " + unknown[0])
        self._validate_ref(body.get("ref"))
        derived = body.get("derived")
        if derived is not None and (not isinstance(derived, str) or not DERIVED_MODEL.fullmatch(derived)):
            raise HttpError(400, "invalid derived model name")
        if kind == "ensure":
            params = self._params(body.get("params"))
            override = body.get("override_fit", False)
            if not isinstance(override, bool):
                raise HttpError(400, "override_fit must be boolean")
            return params, override
        confirm = body.get("confirm_preexisting", False)
        if not isinstance(confirm, bool):
            raise HttpError(400, "confirm_preexisting must be boolean")
        return {}, confirm

    def create(self, kind, body):
        if not self.config["remote_management"]:
            raise HttpError(403, "remote model management is disabled on this node")
        params, flag = self._validate(kind, body)
        with self.lock:
            running = next((op for op in self.operations.values()
                            if op.get("state") not in FINAL_OPERATION_STATES), None)
            if running and running.get("kind") == kind and running.get("ref") == body.get("ref"):
                return dict(running)  # the same request again: idempotent
            if running:
                raise HttpError(409, "operation %s is already running" % running["id"])
            if kind == "remove" and not flag:
                managed = self._managed()
                present = {item.get("name") for item in self._tags()}
                protected = [name for name in (body.get("derived"), body["ref"])
                             if name in present and name not in managed]
                if protected:
                    raise HttpError(409, "model is pre-existing; set confirm_preexisting to remove it")
            operation = {
                "id": "op-" + secrets.token_hex(8), "kind": kind, "ref": body["ref"],
                "derived": body.get("derived"), "state": "checking", "reason": None,
                "progress": {"completed": 0, "total": 0, "status": "checking"},
                "download_gb": None, "loaded_gb": None, "started_at": now(), "ended_at": None,
                "params": params, "override_fit": flag if kind == "ensure" else False,
                "confirm_preexisting": flag if kind == "remove" else False,
            }
            self.operations[operation["id"]] = operation
            self._save(operation)
            self._start(operation)
            return dict(operation)

    def _start(self, operation):
        target = self._ensure if operation.get("kind") == "ensure" else self._remove
        threading.Thread(target=target, args=(operation,), name=operation["id"], daemon=True).start()

    def get(self, operation_id):
        with self.lock:
            operation = self.operations.get(operation_id)
            if not operation:
                raise HttpError(404, "operation not found")
            return dict(operation)

    def all(self):
        with self.lock:
            return [dict(item) for item in sorted(self.operations.values(), key=lambda op: op.get("started_at", ""),
                                                  reverse=True)]

    def cancel(self, operation_id):
        with self.lock:
            operation = self.operations.get(operation_id)
            if not operation:
                raise HttpError(404, "operation not found")
            if operation.get("state") in FINAL_OPERATION_STATES:
                return dict(operation)
            self.cancelled.add(operation_id)
            operation.update(state="cancelled", reason="cancelled", ended_at=now())
            self._save(operation)
            return dict(operation)

    def _manifest_size(self, reference):
        repo, separator, tag = reference.rpartition(":")
        if not separator:
            repo, tag = reference, "latest"
        if "/" not in repo:
            repo = "library/" + repo
        request = urllib.request.Request("%s/%s/manifests/%s" % (self.registry, repo, tag),
                                         headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                manifest = json.load(response)
            size = (manifest.get("config") or {}).get("size", 0)
            size += sum(layer.get("size", 0) for layer in manifest.get("layers", []) if isinstance(layer, dict))
            return size / 1e9 if size > 0 else None
        except (OSError, ValueError, TypeError):
            return None

    def _pull(self, operation):
        request = urllib.request.Request(self.ollama_url + "/api/pull",
                                         data=json.dumps({"model": operation["ref"], "stream": True}).encode(),
                                         method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=3600) as response:
            for raw in response:
                if operation["id"] in self.cancelled or operation.get("state") == "cancelled":
                    return False
                try:
                    update = json.loads(raw)
                except ValueError:
                    continue
                if update.get("error"):
                    error = str(update["error"])
                    if "newer version" in error.lower():
                        try:
                            version = self._request("GET", "/api/version").get("version")
                        except Exception:
                            version = "unknown"
                        self._update(operation, state="blocked", reason="engine_upgrade: %s (current %s)" % (error, version))
                    else:
                        self._update(operation, state="failed", reason=error)
                    return False
                progress = {"completed": update.get("completed", 0), "total": update.get("total", 0),
                            "status": update.get("status", "pulling")}
                self._update(operation, progress=progress)
        return True

    def _ensure(self, operation):
        try:
            names = {item.get("name") for item in self._tags()}
            target = operation.get("derived") or operation["ref"]
            if operation["ref"] in names and target in names:
                self._update(operation, state="ready", progress={"completed": 0, "total": 0, "status": "already present"})
                return
            pulled = operation["ref"] not in names
            if pulled:
                size = self._manifest_size(operation["ref"])
                if size is None:
                    self._update(operation, state="blocked", reason="size unknown")
                    return
                self._update(operation, download_gb=size)
                free = free_disk_gb(self.root)
                if free < size + self.config["disk_reserve_gb"]:
                    self._update(operation, state="blocked", reason="no_space: needs %.1f GB plus %.1f GB reserve" %
                                 (size, self.config["disk_reserve_gb"]))
                    return
                need = size * 1.2 + operation.get("params", {}).get("num_ctx", 0) / 8192.0
                available = system_ram_gb() - self.config["ram_reserve_gb"]
                if need > available and not operation.get("override_fit"):
                    self._update(operation, state="blocked", reason="wont_fit: needs %.1f GB, %.1f GB available" %
                                 (need, available))
                    return
                self._update(operation, state="pulling", progress={"completed": 0, "total": 0,
                                                                    "status": "pulling manifest"})
                if not self._pull(operation):
                    return
                managed = self._managed()
                managed.add(operation["ref"])
                self._save_managed(managed)
            if operation.get("derived") and operation["derived"] not in names:
                self._update(operation, state="configuring")
                engine_params = {key: value for key, value in operation.get("params", {}).items()
                                 if key in ("num_ctx", "temperature", "top_p", "seed")}
                self._request("POST", "/api/create", {"model": operation["derived"], "from": operation["ref"],
                                                        "parameters": engine_params}, timeout=3600)
                managed = self._managed()
                managed.add(operation["derived"])
                self._save_managed(managed)
            self._update(operation, state="verifying")
            params = operation.get("params", {})
            chat = {"model": target, "messages": [{"role": "user", "content": "Reply with the word ready."}],
                    "stream": False, "options": {"num_predict": 16}}
            if "think" in params:
                chat["think"] = params["think"]
            reply = self._request("POST", "/api/chat", chat, timeout=900)
            if not str((reply.get("message") or {}).get("content") or "").strip():
                raise RuntimeError("smoke test returned an empty reply")
            loaded = self._request("GET", "/api/ps").get("models", [])
            loaded_size = next(((item.get("size") or 0) / 1e9 for item in loaded
                                if isinstance(item, dict) and item.get("name") == target), None)
            self._request("POST", "/api/generate", {"model": target, "keep_alive": 0})
            self._update(operation, state="ready", loaded_gb=loaded_size,
                         progress={"completed": 1, "total": 1, "status": "ready"})
        except Exception as exc:
            self._update(operation, state="failed", reason=str(exc)[:1000])

    def _remove(self, operation):
        try:
            self._update(operation, state="checking")
            present = {item.get("name") for item in self._tags()}
            targets = [name for name in (operation.get("derived"), operation["ref"]) if name and name in present]
            for name in targets:
                self._request("DELETE", "/api/delete", {"model": name})
            remaining = {item.get("name") for item in self._tags()}
            if any(name in remaining for name in targets):
                raise RuntimeError("model still present after delete")
            managed = self._managed()
            managed.difference_update(targets)
            self._save_managed(managed)
            self._update(operation, state="removed", progress={"completed": 1, "total": 1, "status": "removed"})
        except Exception as exc:
            self._update(operation, state="failed", reason=str(exc)[:1000])


class HttpError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Handler(BaseHTTPRequestHandler):
    server_version = "FleetRunner/2"

    def log_message(self, fmt, *args):
        # Deliberately avoid standard request logging: POST bodies contain prompts.
        return

    @property
    def manager(self):
        return self.server.manager

    def _send(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        token = self.server.runner_token
        supplied = self.headers.get("X-Runner-Token", "")
        return bool(token and hmac.compare_digest(token, supplied))

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise HttpError(400, "invalid content length")
        if length > 1024 * 1024:
            raise HttpError(413, "request body too large")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, "invalid JSON")
        if not isinstance(value, dict):
            raise HttpError(400, "JSON body must be an object")
        return value

    def _route(self, method):
        parsed = urlparse(self.path)
        parts = [item for item in parsed.path.split("/") if item]
        if method == "GET" and parts == ["health"]:
            agents = find_agents(self.manager.ollama_url)
            self.manager.agents = agents
            running = 0
            for job in self.manager.jobs.values():
                self.manager.refresh(job)
                running += job.get("state") == "running"
            return {"ok": True, "version": VERSION, "max_mode": self.manager.max_mode,
                    "agents": agents, "running": running,
                    "gate_profiles": sorted(self.manager.gate_profiles),
                    "allow_raw_gates": self.manager.allow_raw_gates,
                    "allowed_roots": self.manager.allowed_roots,
                    "max_jobs": self.manager.max_jobs,
                    "models": self.manager.models_config,
                    "policy": True, "network": self.manager.network}
        if method == "GET" and parts == ["models"]:
            return self.manager.model_manager.listing()
        if method == "GET" and parts == ["operations"]:
            return self.manager.model_manager.all()
        if method == "GET" and len(parts) == 2 and parts[0] == "operations":
            return self.manager.model_manager.get(parts[1])
        if method == "POST" and parts == ["models", "ensure"]:
            return self.manager.model_manager.create("ensure", self._body())
        if method == "POST" and parts == ["models", "remove"]:
            return self.manager.model_manager.create("remove", self._body())
        if method == "POST" and len(parts) == 3 and parts[0] == "operations" and parts[2] == "cancel":
            self._body()
            return self.manager.model_manager.cancel(parts[1])
        if method == "GET" and parts == ["jobs"]:
            return self.manager.list_public()
        if method == "POST" and parts == ["jobs"]:
            return self.manager.create(self._body())
        if len(parts) >= 2 and parts[0] == "jobs":
            job = self.manager.get(parts[1])
            if method == "GET" and len(parts) == 2:
                return self.manager.public(job)
            if method == "GET" and len(parts) == 3 and parts[2] == "log":
                try:
                    offset = int(parse_qs(parsed.query).get("offset", ["0"])[0])
                except ValueError:
                    raise HttpError(400, "invalid offset")
                return self.manager.log(job, offset)
            if method == "GET" and len(parts) == 3 and parts[2] == "diff":
                return self.manager.diff(job)
            if method == "POST" and len(parts) == 3 and parts[2] == "message":
                return self.manager.message(job, self._body())
            if method == "POST" and len(parts) == 3 and parts[2] == "cancel":
                return self.manager.cancel(job)
            if method == "POST" and len(parts) == 3 and parts[2] == "cleanup":
                return self.manager.cleanup(job)
        raise HttpError(404, "not found")

    def _handle(self, method):
        try:
            self.manager.request_context.remote = self.client_address[0]
        except (AttributeError, IndexError, TypeError):
            self.manager.request_context.remote = ""
        if not self._authorized():
            self.manager.audit("auth_failed", "invalid runner token")
            self._send(401, {"detail": "invalid runner token"})
            return
        try:
            self._send(200, self._route(method))
        except HttpError as exc:
            self._send(exc.status, {"detail": exc.detail})
        except Exception:
            self._send(500, {"detail": "internal server error"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


class RunnerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def ollama_worker(request_path):
    request_data = read_json(request_path, {})
    messages = []
    if request_data.get("instructions"):
        messages.append({"role": "system", "content": request_data["instructions"]})
    messages.append({"role": "user", "content": request_data.get("prompt", "")})
    payload = json.dumps({
        "model": request_data.get("model"),
        "messages": messages,
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        request_data.get("url", "").rstrip("/") + "/api/chat", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            body = json.load(response)
        text = (body.get("message") or {}).get("content", "")
        print(json.dumps({"type": "ollama.result", "text": text}), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"type": "error", "message": str(exc)}), flush=True)
        return 1


def parse_bind(value):
    host, separator, port = value.rpartition(":")
    if not separator:
        raise ValueError("RUNNER_BIND must be host:port")
    return host, int(port)


def runner_token(fleet_dir):
    configured = os.environ.get("RUNNER_TOKEN")
    if configured:
        return configured
    path = Path(fleet_dir).expanduser() / "runner.token"
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        token = secrets.token_urlsafe(32)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            # O_EXCL: if another runner created it first, use that token instead of overwriting it.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            for _ in range(50):
                token = path.read_text(encoding="utf-8").strip()
                if token:
                    break
                time.sleep(0.02)
            if not token:
                raise SystemExit("runner.token exists but is empty; delete it and restart the runner")
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(token + "\n")
            print("Runner token generated; register this node's runner token on the master.", file=sys.stderr)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--ollama-worker":
        return ollama_worker(sys.argv[2])
    fleet_dir = os.environ.get("FLEET_DIR", str(Path.home() / ".ai-fleet"))
    ollama_url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    address = parse_bind(os.environ.get("RUNNER_BIND", "127.0.0.1:11436"))
    server = RunnerServer(address, Handler)
    server.manager = JobManager(fleet_dir, ollama_url)
    server.runner_token = runner_token(fleet_dir)

    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
# <<< runner.py
RUNNER_PY
  chmod 700 "$FLEET_DIR/runner.py"
}

configure_runner_policy() {
  local cfg="$FLEET_DIR/runner.json" root has_roots=0 policy_roots_set="$ALLOW_ROOTS_SET"
  if [ -f "$cfg" ]; then
    has_roots="$(python3 - "$cfg" <<'PY'
import json, sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8"))
    print(1 if isinstance(value, dict) and "allowed_roots" in value else 0)
except (OSError, ValueError):
    print(0)
PY
)"
  fi
  if [ "$ALLOW_ROOTS_SET" = 0 ] && [ "$has_roots" = 0 ] && [ "$YES" = 0 ]; then
    echo "Folders agents may work in (enter one absolute path at a time; blank finishes; no default):"
    while true; do
      read -rp "Allow root: " root || root=""
      [ -n "$root" ] || break
      ALLOW_ROOTS+=("$root")
    done
    policy_roots_set=1
  fi
  [ "$has_roots" = 0 ] && policy_roots_set=1
  python3 - "$cfg" "$policy_roots_set" "${ALLOW_ROOTS[@]}" <<'PY'
import json
import os
import sys

path, roots_set, *roots = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as handle:
        config = json.load(handle)
except (OSError, ValueError):
    config = {}
if not isinstance(config, dict):
    config = {}
if roots_set == "1":
    config["allowed_roots"] = [os.path.realpath(os.path.expanduser(item)) for item in roots]
config.setdefault("max_mode", "auto-edit")
config.setdefault("denied_paths", [])
profiles = config.setdefault("gate_profiles", {})
if not isinstance(profiles, dict):
    profiles = config["gate_profiles"] = {}
profiles.setdefault("pytest", ["python3", "-m", "pytest", "-q"])
profiles.setdefault("unittest", ["python3", "-m", "unittest", "-q"])
config.setdefault("allow_raw_gates", False)
config.setdefault("max_jobs", 2)
config.setdefault("max_job_minutes", 120)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2, sort_keys=True)
    handle.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
}

install_runner() {
  local ip="$1" py; py="$(command -v python3)"
  write_runner
  configure_runner_policy
  say "Installing the agent runner on $ip:11436 (max mode: $(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["max_mode"])' "$FLEET_DIR/runner.json"))"
  local path_dirs="$HOME/.local/bin:$HOME/.claude/local:$HOME/.npm-global/bin:$HOME/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
  if [ "$OS" = Darwin ]; then
    local la="$HOME/Library/LaunchAgents/local.ai-fleet.runner.plist"
    write_launchd_plist "$la" "local.ai-fleet.runner" 1 1 "" "" "$FLEET_DIR/runner.log" 4 \
      RUNNER_BIND "$ip:11436" \
      FLEET_DIR "$FLEET_DIR" \
      OLLAMA_URL "http://$ip:11434" \
      PATH "$path_dirs" \
      "$py" "$FLEET_DIR/runner.py"
    launchctl bootout "gui/$(id -u)" "$la" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$la"
  else
    local user runner_exec runner_bind_env runner_fleet_env runner_ollama_env runner_path_env
    user="$(id -un)"
    runner_exec="$(systemd_quote exec "$py" "$FLEET_DIR/runner.py")"
    runner_bind_env="$(systemd_quote environment "RUNNER_BIND=$ip:11436")"
    runner_fleet_env="$(systemd_quote environment "FLEET_DIR=$FLEET_DIR")"
    runner_ollama_env="$(systemd_quote environment "OLLAMA_URL=http://$ip:11434")"
    runner_path_env="$(systemd_quote environment "PATH=$path_dirs")"
    $SUDO tee /etc/systemd/system/ai-fleet-runner.service >/dev/null <<EOF
[Unit]
Description=AI fleet agent runner (tailnet only)
After=tailscaled.service
Wants=tailscaled.service
[Service]
User=$user
Environment=$runner_bind_env
Environment=$runner_fleet_env
Environment=$runner_ollama_env
Environment=$runner_path_env
ExecStart=$runner_exec
Restart=always
RestartSec=5
KillMode=process
[Install]
WantedBy=multi-user.target
EOF
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now ai-fleet-runner.service
    $SUDO systemctl restart ai-fleet-runner.service
  fi
  echo "Agents run as $(id -un) with this user's logins. To change what they may do, edit $FLEET_DIR/runner.json"
  echo "(allowed_roots, gate_profiles, max_mode and limits) and restart the runner."
  local token_file="$FLEET_DIR/runner.token" token="" node_name
  node_name="$(hostname -s 2>/dev/null || hostname)"
  for _ in $(seq 1 30); do
    [ -s "$token_file" ] && { token="$(tr -d '\r\n' < "$token_file")"; break; }
    sleep 1
  done
  if [ -n "$token" ]; then
    echo "Runner token (shown once). On the master run: printf '%s\\n' '$token' | uv run --script master/controller/controller.py runner-token set '$node_name'"
  else
    echo "Runner token was not created yet; check $FLEET_DIR/runner.log and restart the runner."
  fi
}

# ---------- main ----------
if [ "$SCAN_ONLY" = 1 ]; then
  find_tailscale
  scan "$(tailnet_ip)"
  exit 0
fi

say "AI fleet onboarding: $(hostname -s 2>/dev/null || hostname), $OS, $(ram_gb)GB RAM"
ensure_basics
ensure_tailscale
IP="$(tailnet_ip)"
[ -n "$IP" ] || { echo "No tailnet IP: is Tailscale signed in?"; exit 1; }
configure_ollama "$IP"
say "Scanning for LLMs, engines and agents"
scan "$IP"
install_services "$IP"
install_runner "$IP"

say "Done"
echo "This node will show as 'pending' in the master dashboard within ~30s. Approve it there, then assign roles."
[ "$OS" = Darwin ] && [ "$(ram_gb)" -le 16 ] && echo "16GB tip (run yourself, resets on reboot): sudo sysctl iogpu.wired_limit_mb=12288"
[ "$OS" = Darwin ] && echo "Keep awake on power: System Settings > Battery > Options > Prevent automatic sleeping when the display is off."
exit 0

#!/usr/bin/env bash
# AI fleet onboarding for macOS and Linux. Copy this one file to a machine and run it:
#   ./onboard.sh                  interactive: join fleet, set up Ollama, scan, publish inventory
#   ./onboard.sh --yes            accept all defaults (installs + recommended models)
#   ./onboard.sh --models "qwen3:8b qwen3:4b"   pull these models
#   ./onboard.sh --scan-only      just rescan and republish (runs hourly after onboarding)
# Run as your normal user; it asks for sudo when it needs it.
set -euo pipefail

FLEET_DIR="${FLEET_DIR:-$HOME/.ai-fleet}"
INFO_PORT=11435
SCAN_ONLY=0; YES=0; MODELS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --scan-only) SCAN_ONLY=1 ;;
    --yes|-y) YES=1 ;;
    --models) MODELS="$2"; shift ;;
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
import json
import os
import re
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


VERSION = "1"
MODES = ("read-only", "supervised", "auto-edit", "full")
MODE_RANK = {name: rank for rank, name in enumerate(MODES)}
JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PUBLIC_JOB_KEYS = (
    "id", "agent", "mode", "state", "exit_code", "session_id", "cwd",
    "worktree", "branch", "started_at", "ended_at", "result", "denials",
    "gate", "runs",
)


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
        return datetime.datetime.fromisoformat(job["started_at"]).timestamp()
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
        self.max_mode = config.get("max_mode", "auto-edit")
        if self.max_mode not in MODE_RANK:
            self.max_mode = "auto-edit"
        self.agents = find_agents(self.ollama_url)
        self._load_jobs()

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
                job["gate"]["state"] = "pending"
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
            if process is not None:
                try:
                    process.wait()
                except OSError:
                    pass
            else:
                while job.get("state") == "running" and process_alive(job.get("pid")):
                    time.sleep(0.2)
            # The wrapper writes its code immediately before exiting; allow that write to land.
            for _ in range(20):
                self.refresh(job)
                if job.get("state") != "running":
                    break
                time.sleep(0.05)
        threading.Thread(target=monitor, name="job-" + job["id"], daemon=True).start()

    def _gate_worker(self, job):
        gate = job["gate"]
        try:
            completed = subprocess.run(
                gate["cmd"], cwd=job["cwd"], shell=True, capture_output=True,
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
        if gate.get("state") == "running" or job["id"] in self.gate_threads:
            return
        gate.update({"state": "running", "exit_code": None, "output_tail": ""})
        self.gate_threads.add(job["id"])
        self._save(job)
        threading.Thread(target=self._gate_worker, args=(job,), name="gate-" + job["id"], daemon=True).start()

    def _make_worktree(self, job_id, repo):
        repo = str(Path(repo).expanduser().resolve())
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
            raise HttpError(403, "requested mode exceeds runner max_mode")
        if not isinstance(body.get("prompt"), str):
            raise HttpError(400, "prompt is required")
        if agent == "ollama" and not body.get("model"):
            raise HttpError(400, "model is required for ollama")
        if body.get("gate") and MODE_RANK[mode] < MODE_RANK["auto-edit"]:
            # The gate is a shell command outside the agent's sandbox; read-only modes must not run one.
            raise HttpError(400, "a gate needs auto-edit or full mode")
        if not self.agents.get(agent):
            raise HttpError(400, "%s agent is unavailable" % agent)
        with self.lock:
            if job_id in self.jobs or (self.jobs_dir / job_id).exists():
                raise HttpError(409, "duplicate job id")
            job_dir = self.jobs_dir / job_id
            job_dir.mkdir(mode=0o700, parents=False)
            os.chmod(job_dir, 0o700)
            repo = body.get("repo")
            use_worktree = bool(repo and body.get("worktree"))
            branch = None
            base = None
            if use_worktree:
                try:
                    repo, cwd, branch, base = self._make_worktree(job_id, repo)
                except Exception:
                    shutil.rmtree(job_dir, ignore_errors=True)
                    raise
            else:
                cwd = body.get("cwd") or repo
                if cwd:
                    cwd = str(Path(cwd).expanduser().resolve())
                    if not Path(cwd).is_dir():
                        shutil.rmtree(job_dir, ignore_errors=True)
                        raise HttpError(400, "cwd does not exist")
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
            gate_cmd = body.get("gate")
            gate = ({"cmd": gate_cmd, "state": "pending", "exit_code": None, "output_tail": ""}
                    if isinstance(gate_cmd, str) and gate_cmd else None)
            job = {
                "id": job_id, "agent": agent, "mode": mode, "state": "running",
                "exit_code": None, "session_id": body.get("resume_session"), "cwd": cwd,
                "worktree": use_worktree, "branch": branch, "started_at": now(),
                "ended_at": None, "result": "", "denials": [], "gate": gate,
                "runs": 1, "repo": repo, "base": base, "model": body.get("model"),
                "allowed_tools": body.get("allowed_tools") or [], "pid": None,
                "exit_file": "exit_code.1",
            }
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
            if allowed_tools:
                cmd += ["--allowedTools", ",".join(str(item) for item in allowed_tools)]
            if job.get("model"):
                cmd += ["--model", job["model"]]
            return cmd  # prompt arrives on stdin
        if agent == "codex":
            sandbox = "read-only" if mode in ("read-only", "supervised") else "workspace-write"
            if resume:
                cmd = [self.agents[agent], "exec", "resume", "--json"]
                if mode == "full":
                    cmd += ["--dangerously-bypass-approvals-and-sandbox"]
                else:
                    cmd += ["-c", 'sandbox_mode="%s"' % sandbox]
                if job.get("model"):
                    cmd += ["--model", job["model"]]
                cmd += [job["session_id"], "-"]  # "-": prompt from stdin
            else:
                cmd = [self.agents[agent], "exec", "--json"]
                if mode == "full":
                    cmd += ["--dangerously-bypass-approvals-and-sandbox"]
                else:
                    cmd += ["-s", sandbox]
                if job.get("model"):
                    cmd += ["--model", job["model"]]
                cmd.append("-")  # "-": prompt from stdin
            return cmd
        request_path = self._job_dir(job) / ("ollama-request.%d.json" % job["runs"])
        atomic_json(request_path, {
            "url": self.ollama_url, "model": job["model"], "prompt": prompt,
        }, 0o600)
        return [sys.executable, str(Path(__file__).resolve()), "--ollama-worker", str(request_path)]

    def _launch(self, job, prompt, allowed_tools, resume):
        cmd = self._command(job, prompt, allowed_tools, resume)
        job_dir = self._job_dir(job)
        exit_path = job_dir / job["exit_file"]
        prompt_path = job_dir / ("prompt.%d.txt" % job["runs"])
        fd = os.open(prompt_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(prompt)
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
                raise HttpError(403, "requested mode exceeds runner max_mode")
            job.update({
                "mode": mode, "state": "running", "exit_code": None,
                "ended_at": None, "runs": job["runs"] + 1,
                "allowed_tools": body.get("allowed_tools") or [],
                "exit_file": "exit_code.%d" % (job["runs"] + 1),
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
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
                    else:
                        os.killpg(pid, signal.SIGTERM)
                        for _ in range(20):
                            if not process_alive(pid):
                                break
                            time.sleep(0.05)
                        if process_alive(pid):
                            os.killpg(pid, signal.SIGKILL)
                except OSError:
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
                git = ["git", "-C", job["cwd"], "-c", "user.name=AI Fleet", "-c", "user.email=fleet@localhost"]
                if subprocess.run(git + ["status", "--porcelain"], capture_output=True, text=True).stdout.strip():
                    subprocess.run(git + ["add", "-A"], capture_output=True)
                    snap = subprocess.run(git + ["commit", "--no-verify", "-m", "fleet: work from task " + job["id"]],
                                          capture_output=True, text=True)
                    if snap.returncode:
                        raise HttpError(500, trim(snap.stderr or "could not save the task's changes"))
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


class HttpError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Handler(BaseHTTPRequestHandler):
    server_version = "FleetRunner/1"

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
        return not token or self.headers.get("X-Runner-Token") == token

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
                    "agents": agents, "running": running}
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
        if not self._authorized():
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
    payload = json.dumps({
        "model": request_data.get("model"),
        "messages": [{"role": "user", "content": request_data.get("prompt", "")}],
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


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--ollama-worker":
        return ollama_worker(sys.argv[2])
    fleet_dir = os.environ.get("FLEET_DIR", str(Path.home() / ".ai-fleet"))
    ollama_url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    address = parse_bind(os.environ.get("RUNNER_BIND", "127.0.0.1:11436"))
    server = RunnerServer(address, Handler)
    server.manager = JobManager(fleet_dir, ollama_url)
    server.runner_token = os.environ.get("RUNNER_TOKEN", "")

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
  [ -f "$FLEET_DIR/runner.json" ] || echo '{"max_mode": "auto-edit"}' > "$FLEET_DIR/runner.json"
}

install_runner() {
  local ip="$1" py; py="$(command -v python3)"
  write_runner
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
  echo "(max_mode: read-only | supervised | auto-edit | full) and restart the runner."
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

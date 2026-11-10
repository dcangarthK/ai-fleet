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
  local ip="$1" bin la="$HOME/Library/LaunchAgents/local.ai-fleet.ollama.plist" extra=""
  bin="$(command -v ollama || echo /Applications/Ollama.app/Contents/Resources/ollama)"
  brew services stop ollama >/dev/null 2>&1 || true
  osascript -e 'quit app "Ollama"' >/dev/null 2>&1 || true
  if [ "$(ram_gb)" -le 16 ]; then
    extra="<key>OLLAMA_MAX_LOADED_MODELS</key><string>1</string><key>OLLAMA_NUM_PARALLEL</key><string>1</string><key>OLLAMA_CONTEXT_LENGTH</key><string>16384</string>"
  fi
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$la" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.ai-fleet.ollama</string>
  <key>ProgramArguments</key><array><string>$bin</string><string>serve</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>OLLAMA_HOST</key><string>$ip:11434</string>
    <key>OLLAMA_KEEP_ALIVE</key><string>10m</string>
    <key>OLLAMA_FLASH_ATTENTION</key><string>1</string>
    <key>OLLAMA_KV_CACHE_TYPE</key><string>q8_0</string>
    $extra
  </dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$FLEET_DIR/ollama.log</string>
</dict></plist>
EOF
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
    $SUDO mkdir -p /etc/systemd/system/ollama.service.d
    $SUDO tee /etc/systemd/system/ollama.service.d/fleet.conf >/dev/null <<EOF
[Unit]
After=tailscaled.service
Wants=tailscaled.service
[Service]
Environment=OLLAMA_HOST=$ip:11434
Environment=OLLAMA_KEEP_ALIVE=10m
Environment=OLLAMA_FLASH_ATTENTION=1
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
    cat > "$la/local.ai-fleet.info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.ai-fleet.info</string>
  <key>ProgramArguments</key><array>
    <string>$py</string><string>-m</string><string>http.server</string><string>$INFO_PORT</string>
    <string>--bind</string><string>$ip</string><string>--directory</string><string>$FLEET_DIR/public</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/dev/null</string>
</dict></plist>
EOF
    cat > "$la/local.ai-fleet.scan.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.ai-fleet.scan</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$self</string><string>--scan-only</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>FLEET_DIR</key><string>$FLEET_DIR</string>
  </dict>
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key><string>$FLEET_DIR/scan.log</string>
  <key>StandardErrorPath</key><string>$FLEET_DIR/scan.log</string>
</dict></plist>
EOF
    for job in info scan; do
      launchctl bootout "gui/$(id -u)" "$la/local.ai-fleet.$job.plist" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$la/local.ai-fleet.$job.plist"
    done
  else
    local user; user="$(id -un)"
    $SUDO tee /etc/systemd/system/ai-fleet-info.service >/dev/null <<EOF
[Unit]
Description=AI fleet node inventory (tailnet only)
After=tailscaled.service
Wants=tailscaled.service
[Service]
User=$user
ExecStart=$py -m http.server $INFO_PORT --bind $ip --directory $FLEET_DIR/public
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
Environment=FLEET_DIR=$FLEET_DIR
ExecStart=/bin/bash $self --scan-only
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

say "Done"
echo "This node will show as 'pending' in the master dashboard within ~30s. Approve it there, then assign roles."
[ "$OS" = Darwin ] && [ "$(ram_gb)" -le 16 ] && echo "16GB tip (run yourself, resets on reboot): sudo sysctl iogpu.wired_limit_mb=12288"
[ "$OS" = Darwin ] && echo "Keep awake on power: System Settings > Battery > Options > Prevent automatic sleeping when the display is off."
exit 0

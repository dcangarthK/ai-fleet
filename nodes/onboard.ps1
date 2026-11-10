# AI fleet onboarding for Windows. Copy this one file to the machine and run in an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\onboard.ps1
#   .\onboard.ps1 -Yes                         accept all defaults (installs + recommended models)
#   .\onboard.ps1 -Models "qwen3:14b","qwen3:4b"   pull these models
#   .\onboard.ps1 -ScanOnly                    just rescan and republish (runs hourly after onboarding)
param([switch]$ScanOnly, [switch]$Yes, [string[]]$Models = @(), [string]$FleetDir = "")
$ErrorActionPreference = "Stop"
if (-not $FleetDir) { $FleetDir = if ($env:FLEET_DIR) { $env:FLEET_DIR } else { Join-Path $env:ProgramData "ai-fleet" } }
$InfoPort = 11435
New-Item -ItemType Directory -Force -Path (Join-Path $FleetDir "public") | Out-Null

function Say($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ask($q) { if ($Yes) { return $true }; $a = Read-Host "$q [Y/n]"; return ($a -eq "" -or $a -match "^[Yy]") }
function Have($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }
function Write-Utf8File($Path, $Content) {
  $encoding = New-Object System.Text.UTF8Encoding($false)
  [IO.File]::WriteAllText($Path, $Content, $encoding)
}
function Quote-WindowsPath($Path) { return '"' + $Path + '"' }
function Escape-CmdValue($Value) { return ([string]$Value).Replace("%", "%%") }
function RamGB { [int]((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB) }
function GpuGB {
  if (Have nvidia-smi) { $v = nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | Sort-Object { [int]$_ } | Select-Object -Last 1; return [int]([int]$v / 1024) }
  return 0
}
function GetJson($url) { try { Invoke-RestMethod $url -TimeoutSec 2 } catch { $null } }

$TS = "$env:ProgramFiles\Tailscale\tailscale.exe"
$OllamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
function TailnetIp { if (Test-Path $TS) { ((& $TS ip -4) | Select-Object -First 1).Trim() } else { "" } }

function Ensure-Tailscale {
  if (-not (Test-Path $TS)) {
    Say "Installing Tailscale"
    winget install --id Tailscale.Tailscale -e --accept-package-agreements --accept-source-agreements
  }
  $tags = try { ((& $TS status --json) | ConvertFrom-Json).Self.Tags } catch { @() }
  if (-not ($tags -contains "tag:llm-node")) {
    Say "Joining the tailnet as tag:llm-node (log in if a URL appears)"
    # No --reset: keep the machine's existing Tailscale settings (routes, DNS, exit node).
    & $TS up --advertise-tags=tag:llm-node
    if ($LASTEXITCODE -ne 0) {
      Write-Host "Tailscale wants existing non-default flags repeated. Run the command it suggests above, keeping --advertise-tags=tag:llm-node, then re-run."
      exit 1
    }
  }
}

function Recommended {
  $ram = RamGB; $gpu = GpuGB
  if ($gpu -ge 24 -or $ram -ge 48) { return @("qwen3-coder:30b", "gpt-oss:20b", "qwen3:8b") }
  if ($gpu -ge 12 -or $ram -ge 32) { return @("qwen3:14b", "qwen3:8b", "qwen3:4b") }
  if ($ram -ge 16) { return @("qwen3:8b", "qwen3:4b") }
  return @("qwen3:4b")
}

function Configure-Ollama($ip) {
  if (-not (Test-Path $OllamaExe)) {
    if (-not (Ask "Ollama is not installed. Install it to serve local models?")) { return }
    winget install --id Ollama.Ollama -e --accept-package-agreements --accept-source-agreements
  }
  Say "Configuring Ollama to listen on the tailnet only (${ip}:11434)"
  foreach ($kv in @(@("OLLAMA_HOST", "${ip}:11434"), @("OLLAMA_KEEP_ALIVE", "10m"), @("OLLAMA_FLASH_ATTENTION", "1"))) {
    [Environment]::SetEnvironmentVariable($kv[0], $kv[1], "User")
  }
  $env:OLLAMA_HOST = "${ip}:11434"
  Get-Process "ollama*" -ErrorAction SilentlyContinue | Stop-Process -Force
  # Launch through Explorer so Ollama runs as the normal user, not with this elevated token.
  $ollamaApp = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama app.exe"
  Start-Process explorer.exe -ArgumentList (Quote-WindowsPath $ollamaApp)
  for ($i = 0; $i -lt 30; $i++) { if (GetJson "http://${ip}:11434/api/tags") { break }; Start-Sleep 1 }

  $count = @((GetJson "http://${ip}:11434/api/tags").models).Count
  $want = $Models
  if ($want.Count -eq 0 -and $count -eq 0) {
    $want = Recommended
    if (-not (Ask "No models yet. Pull recommended models for $(RamGB)GB RAM / $(GpuGB)GB GPU ($($want -join ' '))?")) { $want = @() }
  }
  foreach ($m in $want) { & $OllamaExe pull $m }
}

function Scan($ip) {
  $u = $env:USERPROFILE
  $cs = Get-CimInstance Win32_ComputerSystem
  $hw = [ordered]@{
    os = "Windows"; os_version = (Get-CimInstance Win32_OperatingSystem).Version; arch = $env:PROCESSOR_ARCHITECTURE
    hostname = $env:COMPUTERNAME.ToLower(); cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name
    ram_gb = [int]($cs.TotalPhysicalMemory / 1GB)
    gpu = ((Get-CimInstance Win32_VideoController | ForEach-Object Name) -join "; ")
  }
  $serverDefs = [ordered]@{ 11434 = @("ollama", "/api/tags"); 1234 = @("lmstudio", "/v1/models"); 8000 = @("vllm/openai", "/v1/models");
    8080 = @("llama.cpp/openai", "/v1/models"); 1337 = @("jan", "/v1/models"); 4891 = @("gpt4all", "/v1/models"); 5000 = @("openai-compatible", "/v1/models") }
  $servers = @()
  foreach ($port in $serverDefs.Keys) {
    $engine, $path = $serverDefs[$port]
    foreach ($h in @($ip, "127.0.0.1") | Where-Object { $_ }) {
      $body = GetJson "http://${h}:${port}${path}"
      if ($null -eq $body) { continue }
      $models = if ($engine -eq "ollama") { @($body.models | ForEach-Object { [ordered]@{ name = $_.name; size_gb = [math]::Round($_.size / 1e9, 1); params = $_.details.parameter_size; quant = $_.details.quantization_level } }) }
                else { @($body.data | ForEach-Object { [ordered]@{ name = $_.id } }) }
      $routed = @(11434, 8000, 1234, 8080) -contains $port
      $servers += [ordered]@{ engine = $engine; port = $port; models = $models; reachable_from_master = [bool]($ip -and $h -eq $ip -and $routed)
        hint = $(if ($h -eq $ip -and $routed) { "" } elseif ($h -eq $ip) { "port $port isn't routed by the fleet; serve it on 8000, 1234 or 8080" } else { "bound to localhost only; bind it to the tailnet IP to use it in the fleet" }) }
      break
    }
  }
  $engines = [ordered]@{}
  if (Test-Path $OllamaExe) { $engines.ollama = $OllamaExe }
  foreach ($p in @("$env:LOCALAPPDATA\Programs\LM Studio", "$u\.lmstudio")) { if (Test-Path $p) { $engines.lmstudio = $p; break } }
  foreach ($c in @("llama-server", "vllm", "local-ai", "docker")) { $cmd = Get-Command $c -ErrorAction SilentlyContinue; if ($cmd) { $engines[$c] = $cmd.Source } }
  if (Test-Path "$env:LOCALAPPDATA\Programs\jan") { $engines.jan = "$env:LOCALAPPDATA\Programs\jan" }

  $files = @()
  Get-ChildItem "$u\.cache\huggingface\hub" -Directory -Filter "models--*" -ErrorAction SilentlyContinue | Select-Object -First 200 |
    ForEach-Object { $files += [ordered]@{ source = "huggingface"; name = $_.Name.Substring(8).Replace("--", "/") } }
  foreach ($base in @("$u\.lmstudio\models", "$u\models", "$u\Downloads")) {
    Get-ChildItem $base -Recurse -Filter *.gguf -ErrorAction SilentlyContinue | Select-Object -First 100 |
      ForEach-Object { $files += [ordered]@{ source = "gguf"; name = $_.Name; size_gb = [math]::Round($_.Length / 1e9, 1) } }
  }

  $docker = @()
  if (Have docker) {
    $keys = "ollama|vllm|llama|localai|text-generation|tgi|lmdeploy|sglang|litellm|kimi"
    docker ps --format "{{.Image}}`t{{.Ports}}`t{{.Names}}" 2>$null | Where-Object { $_ -match $keys } |
      ForEach-Object { $p = $_ -split "`t"; $docker += [ordered]@{ image = $p[0]; ports = $p[1]; name = $p[2] } }
  }

  $cliNames = [ordered]@{ claude = "Claude Code"; codex = "OpenAI Codex"; gemini = "Gemini CLI"; qwen = "Qwen Code"; kimi = "Kimi CLI";
    aider = "Aider"; opencode = "OpenCode"; goose = "Goose"; crush = "Crush"; "cursor-agent" = "Cursor Agent"; amp = "Amp"; copilot = "GitHub Copilot CLI" }
  $extra = @("$u\.local\bin", "$env:APPDATA\npm", "$u\.bun\bin", "$u\.cargo\bin")
  $agents = @()
  foreach ($c in $cliNames.Keys) {
    $path = (Get-Command $c -ErrorAction SilentlyContinue).Source
    if (-not $path) { $path = $extra | ForEach-Object { Get-ChildItem "$_\$c*" -Include "$c.exe", "$c.cmd" -ErrorAction SilentlyContinue } | Select-Object -First 1 -ExpandProperty FullName }
    if ($path) {
      $ver = try { (& $path --version 2>&1 | Select-Object -First 1).ToString() } catch { "" }
      $agents += [ordered]@{ cli = $c; name = $cliNames[$c]; version = $ver }
    }
  }
  $apps = @()
  foreach ($a in @("Claude", "Codex", "ChatGPT", "Cursor", "Windsurf", "LM Studio", "Ollama", "Jan", "Msty", "AnythingLLM")) {
    if ((Test-Path "$env:LOCALAPPDATA\Programs\$a") -or (Test-Path "$env:ProgramFiles\$a")) { $apps += $a }
  }

  $manifest = [ordered]@{ schema = 1; scanned_at = [int][double]::Parse((Get-Date -UFormat %s)); tailnet_ip = $ip; hardware = $hw
    servers = $servers; engines = $engines; model_files = $files; docker = $docker; agents = $agents; apps = $apps }
  $json = $manifest | ConvertTo-Json -Depth 8
  Set-Content -Path (Join-Path $FleetDir "manifest.json") -Value $json -Encoding UTF8
  $manifest.engines = @($engines.Keys)  # published copy: no install paths
  Set-Content -Path (Join-Path $FleetDir "public\fleet.json") -Value ($manifest | ConvertTo-Json -Depth 8) -Encoding UTF8

  Write-Host "`n$($hw.hostname): Windows, $($hw.ram_gb)GB RAM, $($hw.gpu)"
  Write-Host "Tailnet IP: $(if ($ip) { $ip } else { 'not connected' })"
  Write-Host "`nModel servers:"
  foreach ($s in $servers) {
    Write-Host ("  {0,-18} :{1,-6} {2} models  [{3}]" -f $s.engine, $s.port, $s.models.Count, $(if ($s.reachable_from_master) { "fleet-ready" } else { "LOCAL ONLY" }))
    $s.models | Select-Object -First 12 | ForEach-Object { Write-Host "      - $($_.name)" }
    if ($s.hint) { Write-Host "      hint: $($s.hint)" }
  }
  if ($servers.Count -eq 0) { Write-Host "  none running" }
  Write-Host "`nInstalled engines: $(if ($engines.Count) { $engines.Keys -join ', ' } else { 'none' })"
  Write-Host "Model files on disk: $($files.Count)"
  if ($docker.Count) { Write-Host "LLM containers: $(($docker | ForEach-Object image) -join ', ')" }
  Write-Host "Agent CLIs: $(if ($agents.Count) { ($agents | ForEach-Object { "$($_.name) $($_.version)" }) -join ', ' } else { 'none' })"
  Write-Host "Desktop AI apps: $(if ($apps.Count) { $apps -join ', ' } else { 'none' })"
}

function Install-Services($ip) {
  Say "Publishing inventory on ${ip}:$InfoPort (tailnet only) and rescanning hourly"
  $self = Join-Path $FleetDir "onboard.ps1"
  if ($PSCommandPath -ne $self) { Copy-Item -LiteralPath $PSCommandPath -Destination $self -Force }
  $server = Join-Path $FleetDir "serve-info.ps1"
  $serverSource = @"
`$l = New-Object System.Net.HttpListener
`$l.Prefixes.Add("http://${ip}:$InfoPort/")
`$fleetJson = Join-Path `$PSScriptRoot "public\fleet.json"
`$l.Start()
while (`$l.IsListening) {
  `$c = `$l.GetContext()
  if (`$c.Request.Url.AbsolutePath -eq "/fleet.json") {
    `$b = [IO.File]::ReadAllBytes(`$fleetJson); `$c.Response.ContentType = "application/json"
  } else { `$b = [byte[]]@(); `$c.Response.StatusCode = 404 }
  `$c.Response.OutputStream.Write(`$b, 0, `$b.Length); `$c.Response.Close()
}
"@
  Write-Utf8File $server $serverSource

  $ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
  $infoArgs = "-NoProfile -ExecutionPolicy Bypass -File " + (Quote-WindowsPath $server)
  $info = New-ScheduledTaskAction -Execute $ps -Argument $infoArgs
  $boot = New-ScheduledTaskTrigger -AtStartup
  $settings = New-ScheduledTaskSettingsSet -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit 0 `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
  Register-ScheduledTask -TaskName "AI Fleet Info" -Action $info -Trigger $boot -Settings $settings -User "SYSTEM" -RunLevel Highest -Force | Out-Null
  Start-ScheduledTask -TaskName "AI Fleet Info"

  $scanArgs = "-NoProfile -ExecutionPolicy Bypass -File " + (Quote-WindowsPath $self) +
    " -ScanOnly -FleetDir " + (Quote-WindowsPath $FleetDir)
  $scan = New-ScheduledTaskAction -Execute $ps -Argument $scanArgs
  $hourly = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours 1)
  $scanSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
  Register-ScheduledTask -TaskName "AI Fleet Scan" -Action $scan -Trigger $hourly -Settings $scanSettings -User $env:USERNAME -Force | Out-Null

  # Allow only tailnet addresses (100.64.0.0/10) to reach the model and inventory ports.
  Get-NetFirewallRule -DisplayName "AI Fleet" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  New-NetFirewallRule -DisplayName "AI Fleet" -Direction Inbound -Protocol TCP -LocalPort 11434, $InfoPort, 11436, 1234, 8000, 8080 `
    -RemoteAddress 100.64.0.0/10 -Action Allow | Out-Null
}

# ---------- agent runner ----------
# Lets the master start Claude Code / Codex / Ollama jobs here (tailnet only, port 11436).
# Source embedded by nodes/build.py from nodes/runner.py; edit runner.py, then rebuild.
$RunnerSource = @'
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
'@

function Install-Runner($ip) {
  $py = (Get-Command python -ErrorAction SilentlyContinue).Source
  if (-not $py) {
    if (-not (Ask "Python is needed for the agent runner. Install it?")) { return }
    winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements
    $py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
  }
  $runnerPath = Join-Path $FleetDir "runner.py"
  Write-Utf8File $runnerPath $RunnerSource
  $cfg = Join-Path $FleetDir "runner.json"
  if (-not (Test-Path -LiteralPath $cfg)) { Write-Utf8File $cfg '{"max_mode": "auto-edit"}' }
  $cmd = Join-Path $FleetDir "runner.cmd"
  $cmdPython = Escape-CmdValue $py
  $cmdFleetDir = Escape-CmdValue $FleetDir
  $cmdSource = @"
@echo off
chcp 65001 >nul
set "RUNNER_BIND=${ip}:11436"
set "FLEET_DIR=$cmdFleetDir"
set "OLLAMA_URL=http://${ip}:11434"
"$cmdPython" "%~dp0runner.py"
"@
  Write-Utf8File $cmd $cmdSource
  Say "Installing the agent runner on ${ip}:11436 (runs as $env:USERNAME at logon)"
  $action = New-ScheduledTaskAction -Execute $cmd
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $settings = New-ScheduledTaskSettingsSet -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit 0 `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
  Register-ScheduledTask -TaskName "AI Fleet Runner" -Action $action -Trigger $trigger -Settings $settings -User $env:USERNAME -Force | Out-Null
  Start-ScheduledTask -TaskName "AI Fleet Runner"
  Write-Host "To change what agents may do, edit $cfg (max_mode: read-only | supervised | auto-edit | full)."
}

if ($ScanOnly) { Scan (TailnetIp); exit 0 }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host "Run this in an elevated PowerShell (Run as administrator)."; exit 1 }
if (-not (Have winget)) { Write-Host "winget is required (App Installer from the Microsoft Store)."; exit 1 }

Say "AI fleet onboarding: $env:COMPUTERNAME, Windows, $(RamGB)GB RAM, $(GpuGB)GB GPU"
Ensure-Tailscale
$ip = TailnetIp
if (-not $ip) { Write-Host "No tailnet IP: is Tailscale signed in?"; exit 1 }
Configure-Ollama $ip
Say "Scanning for LLMs, engines and agents"
Scan $ip
Install-Services $ip
Install-Runner $ip
Say "Done"
Write-Host "This node will show as 'pending' in the master dashboard within ~30s. Approve it there, then assign roles."

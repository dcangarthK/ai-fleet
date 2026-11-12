# AI fleet onboarding for Windows. Copy this one file to the machine and run in an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\onboard.ps1
#   .\onboard.ps1 -Yes                         accept all defaults (installs + recommended models)
#   .\onboard.ps1 -Models "qwen3:14b","qwen3:4b"   pull these models
#   .\onboard.ps1 -AllowRoot C:\code -AllowRoot D:\repos
#   .\onboard.ps1 -ScanOnly                    just rescan and republish (runs hourly after onboarding)
param([switch]$ScanOnly, [switch]$Yes, [string[]]$Models = @(), [string[]]$AllowRoot = @(),
      [string]$FleetDir = "", [string]$Enroll = "", [string]$EnrollFile = "", [switch]$EnrollPrompt,
      [string]$Master = "")
$ErrorActionPreference = "Stop"
# Prefer -EnrollPrompt (asks, input hidden) or -EnrollFile PATH: a code given as -Enroll VALUE stays in the
# process command line and PowerShell history.
$EnrollRequested = [bool]($Enroll -or $EnrollFile -or $EnrollPrompt)
$EnrollCode = ""
$AllowRootsSet = $PSBoundParameters.ContainsKey("AllowRoot")
if (-not $FleetDir) { $FleetDir = if ($env:FLEET_DIR) { $env:FLEET_DIR } else { Join-Path $env:ProgramData "ai-fleet" } }
$InfoPort = 11435
$RunnerTokenValue = ""
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

function Ensure-TailscaleConnection {
  if (-not (Test-Path $TS)) {
    Say "Installing Tailscale"
    winget install --id Tailscale.Tailscale -e --accept-package-agreements --accept-source-agreements
  }
  $status = try { (& $TS status --json) | ConvertFrom-Json } catch { $null }
  if ($EnrollRequested -and $null -ne $status -and @($status.Self.Tags) -contains "tag:llm-node") {
    throw "This machine is already tagged tag:llm-node; remove the tag before enrollment so it can reach the master."
  }
  if ($null -eq $status -or $status.BackendState -ne "Running") {
    Say "Joining the tailnet (log in if a URL appears)"
    & $TS up
    if ($LASTEXITCODE -ne 0) { throw "Tailscale sign-in failed" }
  }
}

function Ensure-WorkerTag {
  $tags = try { @(((& $TS status --json) | ConvertFrom-Json).Self.Tags) } catch { @() }
  if (-not ($tags -contains "tag:llm-node")) {
    Say "Advertising tag:llm-node after enrollment"
    & $TS set --advertise-tags=tag:llm-node
    if ($LASTEXITCODE -ne 0) { throw "Could not advertise tag:llm-node" }
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
import fnmatch
import glob
import hashlib
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
RUNNER_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
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
    "id", "agent", "engine", "prompt", "mode", "repo", "worktree", "cwd", "model", "gate", "gate_args",
    "allowed_tools", "resume_session", "instructions", "policy", "policy_hash",
    "effort", "tools", "max_turns",
    # account pools and limit handoff (docs/SPEC-limit-handoff.md 3.1, 4.3)
    "profile", "base_branch", "source_job",
}
PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
PROFILE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HANDOFF_BRANCH = re.compile(r"^fleet/t-[0-9a-f]{8}$")
# Never committed by a checkpoint (matched against each path's file name).
CHECKPOINT_SECRET_GLOBS = (".env*", "*.pem", "*.key", "id_*", "*credentials*")
EFFORTS = ("low", "medium", "high")
BUILTIN_DENIED = (
    ".ssh", ".aws", ".gnupg", ".config/gcloud", ".kube", ".docker", "Library/Keychains",
)
PUBLIC_JOB_KEYS = (
    "id", "agent", "engine", "mode", "state", "exit_code", "session_id", "cwd",
    "worktree", "branch", "started_at", "ended_at", "result", "denials",
    "gate", "runs", "instructions", "policy_hash", "usage", "num_turns", "limits_applied", "error",
    "profile", "account_label",
)

DMR_DEFAULT_URL = "http://127.0.0.1:12434/engines/v1"


def loopback_base_url(value):
    """Return a normalized local HTTP base URL, refusing any address an external caller could reach."""
    if not isinstance(value, str):
        raise ValueError("engine base_url must be a loopback URL")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("engine base_url must use loopback (127.0.0.1, localhost or ::1)")
    if parsed.username or parsed.password or not parsed.port:
        raise ValueError("engine base_url must be a loopback URL with an explicit port")
    return value.rstrip("/")


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError(f"redirect refused ({code}); engine requests must stay on loopback")


# No proxies (HTTP(S)_PROXY must not carry a prompt off-host) and no redirects.
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirect)


def loopback_urlopen(request, timeout=None):
    return _LOOPBACK_OPENER.open(request, timeout=timeout)


def parse_docker_model_ls(output):
    """Parse Docker's JSON-lines model list; malformed rows are contained and unknown fields stay null."""
    models = []
    for line in (output or "").splitlines():
        if line.strip() and not line.lstrip().startswith("{"):
            # Real `docker model ls` prints a table: "MODEL NAME  PARAMETERS ... MODEL ID  CREATED  SIZE".
            fields = line.split()
            if len(fields) < 3 or fields[0] == "MODEL" or not re.match(r"^[\w.\-]+(/[\w.\-]+)*(:[\w.\-]+)?$", fields[0]):
                continue
            digest = next((f for f in fields[1:] if re.fullmatch(r"[0-9a-f]{12}", f)), None)
            models.append({"engine": "dmr", "reference": fields[0], "digest": digest,
                           "size": None, "warm": "unknown", "warm_source": None})
            continue
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        reference = row.get("Model") or row.get("model") or row.get("Name") or row.get("name")
        if not isinstance(reference, str) or not reference:
            continue
        digest = row.get("ID") or row.get("Digest") or row.get("digest")
        models.append({"engine": "dmr", "reference": reference,
                       "digest": digest if isinstance(digest, str) else None,
                       "size": None, "warm": "unknown", "warm_source": None})
    return models


class OpenAIEngine:
    """Small dependency-free adapter for DMR's OpenAI-compatible local HTTP API."""
    def __init__(self, base_url, timeout=2, docker_run=None):
        self.base_url = loopback_base_url(base_url)
        self.timeout = timeout
        self.docker_run = docker_run or run_capped
        self._catalog = {"engine": "dmr", "reachable": None, "models": [], "error": None, "checked_at": None}
        self.lock = threading.Lock()
        self.refreshed_at = None

    def request(self, method, path, body=None, timeout=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data,
                                         headers={"Content-Type": "application/json"}, method=method)
        with loopback_urlopen(request, timeout=timeout or self.timeout) as response:
            return json.load(response)

    def refresh(self):
        try:
            body = self.request("GET", "/models")
            rows = body.get("data") if isinstance(body, dict) else None
            if not isinstance(rows, list):
                raise ValueError("models response has no data list")
            models = []
            for row in rows:
                reference = row.get("id") if isinstance(row, dict) else None
                if isinstance(reference, str) and reference:
                    models.append({"engine": "dmr", "reference": reference, "digest": None,
                                   "size": None, "warm": "unknown", "warm_source": None})
            try:  # the CLI is optional: its failure must not discard a good /models catalog
                cli_models = parse_docker_model_ls(self.docker_run(["docker", "model", "ls"], timeout=3))
            except Exception:
                cli_models = []
            known = {item["reference"] for item in models}
            models.extend(item for item in cli_models if item["reference"] not in known)
            value = {"engine": "dmr", "reachable": True, "models": models,
                     "error": None, "checked_at": now()}
        except Exception as exc:
            value = {"engine": "dmr", "reachable": False, "models": [],
                     "error": trim(str(exc), 300), "checked_at": now()}
        with self.lock:
            self._catalog = value
            self.refreshed_at = time.monotonic()
        return value

    def age(self):
        """Seconds since the last refresh, or None if it never ran."""
        return None if self.refreshed_at is None else time.monotonic() - self.refreshed_at

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self._catalog))


class EngineCollector:
    def __init__(self, manager, interval=10):
        self.manager, self.interval = manager, interval

    def run_forever(self):
        while True:
            for adapter in self.manager.engine_adapters.values():
                adapter.refresh()
            time.sleep(self.interval)

    def start(self):
        threading.Thread(target=self.run_forever, name="engine-metrics", daemon=True).start()
        return self


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


ERROR_LINE = re.compile(r"^\s*error\b\s*[:\-]", re.IGNORECASE)
SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|xox[a-z])[-_][A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization)(\s*[=:]\s*)\S+"),
    re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}"),
)


def error_line(value, limit=500):
    """One agent error message, redacted of anything key-like and trimmed to its first `limit` characters."""
    text = " ".join(str(value or "").split())
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + m.group(2) + "[redacted]") if m.lastindex == 2 else "[redacted]", text)
    return text[:limit]


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


def find_agents(ollama_url, ollama_up=None):
    """`ollama_up` from the metrics thread's cache skips the inline probe (docs/SPEC-live-monitor.md 4.1)."""
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
    if ollama_up is not None:
        agents["ollama"] = ollama_url.rstrip("/") if ollama_up else None
        return agents
    try:
        with urllib.request.urlopen(ollama_url.rstrip("/") + "/api/tags", timeout=1) as response:
            if 200 <= response.status < 300:
                agents["ollama"] = ollama_url.rstrip("/")
    except (OSError, urllib.error.URLError):
        pass
    return agents


# ---------- machine metrics and heartbeats (docs/SPEC-live-monitor.md 4.1, 12.1) ----------

METRICS_SECONDS = 15
METRICS_COMMAND_TIMEOUT = 3
NODE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def run_capped(argv, timeout=METRICS_COMMAND_TIMEOUT):
    """A command's stdout, or None when it fails, isn't there or runs over (the child is killed)."""
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                text=True, errors="replace")
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        return None
    return out if proc.returncode == 0 else None


def parse_vm_stat(text):
    """macOS memory used in GB: active + wired + compressed pages, with the page size from vm_stat's header."""
    if not text:
        return None
    size = re.search(r"page size of (\d+) bytes", text)
    if not size:
        return None
    pages = {}
    for name, key in (("Pages active", "active"), ("Pages wired down", "wired"),
                      ("Pages occupied by compressor", "compressed")):
        found = re.search(re.escape(name) + r":\s+(\d+)", text)
        if not found:
            return None
        pages[key] = int(found.group(1))
    return sum(pages.values()) * int(size.group(1)) / 1e9


def parse_swapusage(text):
    """`sysctl vm.swapusage`: the used swap in GB."""
    found = re.search(r"used\s*=\s*([\d.]+)([KMGT])", text or "")
    if not found:
        return None
    scale = {"K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}[found.group(2)]
    return float(found.group(1)) * scale * (1.024 ** {"K": 1, "M": 2, "G": 3, "T": 4}[found.group(2)]) / 1e9


def parse_meminfo(text):
    """Linux /proc/meminfo: (total GB, used GB, swap used GB)."""
    values = {}
    for line in (text or "").splitlines():
        name, _, rest = line.partition(":")
        number = rest.strip().split(" ")[0]
        if number.isdigit():
            values[name] = int(number) * 1024 / 1e9
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    swap = (values["SwapTotal"] - values["SwapFree"]) if "SwapTotal" in values and "SwapFree" in values else None
    return total, (total - available) if total is not None and available is not None else None, swap


def ollama_status(ollama_url, timeout=METRICS_COMMAND_TIMEOUT):
    """{"up", "loaded"}: Ollama answering /api/tags (the same probe as before), and the models it has in memory
    (/api/ps; an Ollama without it is still up, with nothing listed)."""
    base = ollama_url.rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=timeout) as response:
            if not 200 <= response.status < 300:
                return {"up": False, "loaded": []}
    except (OSError, ValueError, urllib.error.URLError):
        return {"up": False, "loaded": []}
    try:
        with urllib.request.urlopen(base + "/api/ps", timeout=timeout) as response:
            body = json.loads(response.read() or b"{}")
        loaded = [m.get("name") or m.get("model") for m in body.get("models") or [] if isinstance(m, dict)]
        return {"up": True, "loaded": sorted(m for m in loaded if isinstance(m, str))}
    except (OSError, ValueError, urllib.error.URLError, AttributeError):
        return {"up": True, "loaded": []}


def collect_metrics(root, ollama_url, run=run_capped, read=None, ollama=ollama_status, platform=None):
    """One collection: numbers or None (never a guess). Windows gets disk, cores and Ollama only."""
    platform = platform or sys.platform
    out = {"mem_total_gb": None, "mem_used_gb": None, "swap_used_gb": None, "disk_free_gb": None, "load5": None,
           "cores": os.cpu_count()}
    try:
        out["disk_free_gb"] = round(free_disk_gb(root), 1)
    except OSError:
        pass
    if platform == "darwin":
        total = run(["sysctl", "-n", "hw.memsize"])
        try:
            out["mem_total_gb"] = round(int(total.strip()) / 1e9, 1) if total else None
        except ValueError:
            pass
        used = parse_vm_stat(run(["vm_stat"]))
        out["mem_used_gb"] = round(used, 1) if used is not None else None
        swap = parse_swapusage(run(["sysctl", "vm.swapusage"]))
        out["swap_used_gb"] = round(swap, 2) if swap is not None else None
    elif platform.startswith("linux"):
        try:
            text = read("/proc/meminfo") if read else Path("/proc/meminfo").read_text()
        except OSError:
            text = ""
        total, used, swap = parse_meminfo(text)
        out.update({k: round(v, 2) if v is not None else None
                    for k, v in (("mem_total_gb", total), ("mem_used_gb", used), ("swap_used_gb", swap))})
    if hasattr(os, "getloadavg"):
        try:
            out["load5"] = round(os.getloadavg()[1], 2)
        except OSError:
            pass
    out["ollama"] = ollama(ollama_url)
    return out


class MetricsCollector:
    """Collects in a daemon thread every METRICS_SECONDS; /health serves the cache with its age, so a slow or
    swapping machine never slows /health down."""
    def __init__(self, root, ollama_url, collect=None, interval=METRICS_SECONDS):
        self.root, self.ollama_url, self.interval = root, ollama_url, interval
        self.collect = collect or (lambda: collect_metrics(self.root, self.ollama_url))
        self.lock = threading.Lock()
        self.values, self.at = None, None

    def once(self):
        values = self.collect()
        with self.lock:
            self.values, self.at = values, time.monotonic()

    def run_forever(self):
        while True:
            try:
                self.once()
            except Exception:  # keep collecting whatever happens
                pass
            time.sleep(self.interval)

    def start(self):
        threading.Thread(target=self.run_forever, name="metrics", daemon=True).start()
        return self

    def snapshot(self):
        with self.lock:
            if self.values is None:
                return None
            return dict(self.values, age_s=round(time.monotonic() - self.at, 1))


def allowed_controller(url):
    """https anywhere; plain http only to a loopback or tailnet address (12.1)."""
    import ipaddress
    try:
        parsed = urlparse(url)
    except (TypeError, ValueError):
        return False
    host = (parsed.hostname or "").lower()
    if not host or parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        return False
    if parsed.scheme == "https":
        return True
    if host == "localhost" or host.endswith(".ts.net"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")


def sign_heartbeat(token, node_id, ts, body):
    message = "%s\n%d\n%s" % (node_id, int(ts), hashlib.sha256(body).hexdigest())
    return hmac.new(token.encode(), message.encode(), hashlib.sha256).hexdigest()


class Heartbeat:
    """Pushes the cached /health to the controller about every 10 s, signed with the runner token (the token itself
    never goes on the wire). Needs `controller` in runner.json and the node id the controller told us."""
    def __init__(self, manager, token, report, interval=10.0, jitter=2.0, post=None):
        self.manager, self.token, self.report = manager, token, report
        self.interval, self.jitter = interval, jitter
        self.post = post or self._post

    def ready(self):
        url = getattr(self.manager, "controller_url", None)
        return bool(url and self.token and getattr(self.manager, "node_id", None))

    @staticmethod
    def _post(url, body, headers):
        request = urllib.request.Request(url, data=body, method="POST", headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status

    def beat(self, now=None):
        """One push; False when it isn't configured yet or the controller didn't take it."""
        if not self.ready():
            return False
        body = json.dumps(self.report()).encode()
        ts = int(time.time() if now is None else now)
        node_id = self.manager.node_id
        headers = {"Content-Type": "application/json", "X-Fleet-Node": node_id, "X-Fleet-Ts": str(ts),
                   "X-Fleet-Sig": sign_heartbeat(self.token, node_id, ts, body)}
        try:
            return 200 <= self.post(self.manager.controller_url.rstrip("/") + "/api/nodes/heartbeat", body, headers) < 300
        except Exception:  # an unreachable or refusing controller: the poll is the fallback
            return False

    def next_delay(self):
        return max(1.0, self.interval + (secrets.randbelow(2001) / 1000.0 - 1.0) * self.jitter)

    def run_forever(self):
        while True:
            time.sleep(self.next_delay())
            try:
                self.beat()
            except Exception:  # a failing report must not end the thread: the next beat tries again
                pass

    def start(self):
        threading.Thread(target=self.run_forever, name="heartbeat", daemon=True).start()
        return self


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
        power = config.get("power") if isinstance(config.get("power"), dict) else {}
        minimum = power.get("min_battery_pct", 30)
        self.min_battery_pct = (minimum if isinstance(minimum, int) and not isinstance(minimum, bool)
                                and 0 <= minimum <= 100 else 30)
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
        engines = config.get("engines") if isinstance(config.get("engines"), dict) else {}
        dmr = engines.get("dmr") if isinstance(engines.get("dmr"), dict) else {}
        self.engine_adapters = {}
        self.engine_config = {"ollama": {"enabled": True, "base_url": self.ollama_url}}
        if dmr.get("enabled") is True:
            base_url = loopback_base_url(dmr.get("base_url", DMR_DEFAULT_URL))
            self.engine_config["dmr"] = {"enabled": True, "base_url": base_url}
            self.engine_adapters["dmr"] = OpenAIEngine(base_url)
        self.profile_config = self._profiles_from(config.get("profiles"))
        self.request_context = threading.local()
        controller = config.get("controller")
        self.controller_url = controller if isinstance(controller, str) and allowed_controller(controller) else None
        if isinstance(controller, str) and not self.controller_url:
            print("runner.json controller %r ignored: use https, or http to a loopback or tailnet address"
                  % controller, file=sys.stderr)
        self.node_id = None  # learned from the controller's token-checked polls (X-Fleet-Node-Id)
        self.metrics = None  # a MetricsCollector, started by main()
        self.agents = find_agents(self.ollama_url)
        self.model_manager = ModelManager(self.root, self.ollama_url, self.models_config)
        self._load_jobs()

    def engine_catalog(self, refresh=True):
        out = {}
        for name, adapter in self.engine_adapters.items():
            out[name] = adapter.refresh() if refresh else adapter.snapshot()
        return out

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

    def _review_worktree(self, value, mode):
        """`value` when it is exactly the worktree of one of this runner's jobs and the new job is read-only."""
        if mode != "read-only" or not isinstance(value, str):
            return None
        expanded = os.path.expanduser(value)
        if not os.path.isabs(expanded) or not os.path.isdir(expanded):
            return None
        resolved = os.path.realpath(expanded)
        for job in list(self.jobs.values()):
            if job.get("worktree") and job.get("cwd") and self._comparable(job["cwd"]) == self._comparable(resolved):
                self.audit("review_worktree", "read-only review of job " + job["id"])
                return resolved
        return None

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

    # ---- paid-model budget (docs/SPEC-paid-budget.md): usage, effort, review limits, profiles ----

    def cli_help(self, agent):
        """The agent CLI's help text (cached per binary), so only flags the installed CLI has are used."""
        path = self.agents.get(agent)
        if not path:
            return ""
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            stamp = None
        cache = self.__dict__.setdefault("_help_cache", {})
        key = (path, stamp)
        if key not in cache:
            argv = [path, "exec", "--help"] if agent == "codex" else [path, "--help"]
            try:
                done = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=15,
                                      stdin=subprocess.DEVNULL, env=dict(os.environ, PATH=agent_search_path()))
                cache[key] = (done.stdout or "") + (done.stderr or "")
            except (OSError, subprocess.SubprocessError):
                cache[key] = ""
        return cache[key]

    def _has_flag(self, agent, flag):
        return bool(re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", self.cli_help(agent) or ""))

    @staticmethod
    def _review_tools(job):
        """The review tool list, narrowed by the policy's claude_allow when it has one."""
        tools = [str(item) for item in (job.get("tools") or [])]
        permitted = ((job.get("policy") or {}).get("tools") or {}).get("claude_allow")
        return [item for item in tools if item in permitted] if permitted is not None else tools

    def limits_applied(self, job):
        """What this run really passes to the CLI: null where the CLI has no such option (nothing is faked)."""
        agent = job.get("agent")
        effort = job.get("effort")
        if agent == "codex":
            return {"effort": effort, "max_turns": None, "tools": None}
        if agent != "claude":
            return {"effort": None, "max_turns": None, "tools": None}
        return {"effort": effort if effort and self._has_flag("claude", "--effort") else None,
                "max_turns": (job.get("max_turns") if job.get("max_turns") and self._has_flag("claude", "--max-turns")
                              else None),
                "tools": (self._review_tools(job) if job.get("tools") is not None and self._has_flag("claude", "--tools")
                          else None)}

    @staticmethod
    def codex_profile_dir():
        return Path(os.path.expanduser(os.environ.get("FLEET_CODEX_PROFILE") or "~/.codex-fleet"))

    def codex_profile_ready(self):
        # Ready once the owner signed it in (CODEX_HOME=~/.codex-fleet codex login). The file is never read here.
        home = self.codex_profile_dir()
        return home.is_dir() and (home / "auth.json").exists()

    @classmethod
    def _profiles_from(cls, value):
        """The `profiles` block of runner.json: {agent: [{id, dir, label}]}, or None when there is none.
        Entries with a bad id or label, or a directory outside the user's home, are left out."""
        if not isinstance(value, dict):
            return None
        home = str(Path.home())
        out = {}
        for agent in ("claude", "codex"):
            entries = value.get(agent)
            if not isinstance(entries, list):
                continue
            kept = []
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                    continue
                if not PROFILE_ID.fullmatch(entry["id"]) or any(e["id"] == entry["id"] for e in kept):
                    continue
                label = entry.get("label")
                if label is not None and (not isinstance(label, str) or not PROFILE_LABEL.fullmatch(label)):
                    continue
                folder = entry.get("dir")
                if folder is not None:
                    if not isinstance(folder, str) or not (folder.startswith("~/") or os.path.isabs(folder)):
                        continue
                    folder = os.path.expanduser(folder)
                    if not cls._within(folder, home) or cls._comparable(folder) == cls._comparable(home):
                        continue
                elif entry["id"] != "fleet":
                    continue  # only the inherited profile may name no directory
                kept.append({"id": entry["id"], "dir": folder, "label": label})
            out[agent] = kept
        return out

    def profile_entries(self, agent):
        """This agent's profiles. With no `profiles` block: one, `fleet` (the inherited environment)."""
        if self.profile_config is None or agent not in self.profile_config:
            return [{"id": "fleet", "dir": None, "label": None}]
        return self.profile_config[agent]

    def profile_entry(self, agent, profile_id):
        for entry in self.profile_entries(agent):
            if entry["id"] == profile_id:
                return entry
        return None

    @staticmethod
    def profile_ready(entry):
        # Ready only means the directory exists; nothing in it is read. "fleet" is the inherited environment,
        # which doesn't use the listed directory, so it is always ready.
        return entry["id"] == "fleet" or entry["dir"] is None or os.path.isdir(entry["dir"])

    def profile_report(self):
        """For /health: per agent [{id, label, ready}]. No directories, no account identity."""
        return {agent: [{"id": e["id"], "label": e["label"], "ready": self.profile_ready(e)}
                        for e in self.profile_entries(agent)] for agent in ("claude", "codex")}

    def agent_env(self, agent, base, profile=None):
        env = dict(base)
        entry = self.profile_entry(agent, profile) if profile and profile != "fleet" else None
        if entry and entry["dir"]:
            # A named profile: its directory from this runner's own config (the request only carries the id).
            env["CLAUDE_CONFIG_DIR" if agent == "claude" else "CODEX_HOME"] = entry["dir"]
            return env
        # "fleet" (or no profile): the inherited environment, exactly as before.
        if agent == "codex" and self.codex_profile_ready():
            env["CODEX_HOME"] = str(self.codex_profile_dir())
        return env

    def profiles(self, env=None):
        env = os.environ if env is None else env
        claude_dir = env.get("CLAUDE_CONFIG_DIR")
        return {"codex_profile": "fleet" if self.codex_profile_ready() else "shared",
                "claude_profile": "fleet" if claude_dir and os.path.isdir(os.path.expanduser(claude_dir)) else "shared"}

    def profile_of(self, agent):
        """The profile a job of this agent runs under (docs/SPEC-limit-handoff.md 3.1): fleet or shared."""
        return self.profiles().get(agent + "_profile") if agent in ("claude", "codex") else None

    @staticmethod
    def budget_support():
        return True

    def default_models(self):
        """The model each agent CLI uses when a task names none (from its settings), when one is configured."""
        found = {}
        claude_dir = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
        settings = read_json(Path(os.path.expanduser(claude_dir)) / "settings.json", {})
        if isinstance(settings, dict) and isinstance(settings.get("model"), str) and settings["model"]:
            found["claude"] = settings["model"]
        codex_home = (str(self.codex_profile_dir()) if self.codex_profile_ready()
                      else os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
        try:
            text = (Path(os.path.expanduser(codex_home)) / "config.toml").read_text(encoding="utf-8")
            match = re.search(r'^model\s*=\s*"([^"\n]+)"', text, re.MULTILINE)
            if match:
                found["codex"] = match.group(1)
        except (OSError, UnicodeDecodeError):
            pass
        return found

    @staticmethod
    def _add(total, value):
        if isinstance(value, int) and not isinstance(value, bool):
            return (total or 0) + value
        return total

    def _usage(self, job, lines):
        """Tokens for the current run only. Claude: the stream-json result event; Codex: turn.completed events,
        summed. `input` counts uncached input for both (Codex reports cached tokens inside input_tokens)."""
        usage = {"input": None, "output": None, "cached": None, "cost_usd": None, "model": None}
        turns = None
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "result":
                if isinstance(event.get("num_turns"), int) and not isinstance(event.get("num_turns"), bool):
                    turns = event["num_turns"]
                found = event.get("usage")
                if isinstance(found, dict):
                    usage["input"] = self._add(self._add(None, found.get("input_tokens")),
                                               found.get("cache_creation_input_tokens"))
                    usage["output"] = self._add(None, found.get("output_tokens"))
                    usage["cached"] = self._add(None, found.get("cache_read_input_tokens"))
                    cost = event.get("total_cost_usd")
                    usage["cost_usd"] = (float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                                         else None)
                    models = event.get("modelUsage")
                    if isinstance(models, dict) and models:
                        def output_tokens(name):
                            value = models[name].get("outputTokens") if isinstance(models[name], dict) else None
                            return value if isinstance(value, (int, float)) else 0
                        usage["model"] = max(models, key=output_tokens)
            elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
                found = event["usage"]
                given, cached = found.get("input_tokens"), found.get("cached_input_tokens")
                if isinstance(given, int) and isinstance(cached, int):
                    given = given - cached
                usage["input"] = self._add(usage["input"], given)
                usage["cached"] = self._add(usage["cached"], cached)
                usage["output"] = self._add(usage["output"], found.get("output_tokens"))
                usage["model"] = job.get("model")
        return usage, turns

    @staticmethod
    def _codex_error(job, lines):
        """The last error Codex reported in this run: an `error:` line on its stderr (events.jsonl holds both
        streams) or the message of an `error` or `turn.failed` event. Only for a failed run; redacted and trimmed.
        A usage-limit message ("... try again at 1:00 PM") reaches the controller this way."""
        if job.get("agent") != "codex" or job.get("exit_code") in (0, None):
            return None
        found = None
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                if ERROR_LINE.match(line):
                    found = re.sub(r"^\s*error\b\s*[:\-]\s*", "error: ", line, count=1, flags=re.IGNORECASE)
                continue
            if not isinstance(event, dict):
                continue
            message = None
            if event.get("type") == "error":
                message = event.get("message")
            elif event.get("type") == "turn.failed":
                failure = event.get("error")
                message = failure.get("message") if isinstance(failure, dict) else failure
            if isinstance(message, str) and message.strip():
                found = message
        return error_line(found) if found else None

    def _parse_events(self, job):
        raw_path = self._job_dir(job) / "events.jsonl"
        session_id = None
        result = ""
        denials = []
        try:
            with open(raw_path, "rb") as handle:
                data = handle.read()
        except OSError:
            return
        offset = job.get("events_offset") or 0
        if not isinstance(offset, int) or not 0 <= offset <= len(data):
            offset = 0
        lines = data.decode("utf-8", errors="replace").split("\n")
        current = data[offset:].decode("utf-8", errors="replace").split("\n")
        job["usage"], job["num_turns"] = self._usage(job, current)
        job["error"] = self._codex_error(job, current)
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

    def _handoff_source(self, body, repo):
        """(base_branch, source job) for a handoff job, or (None, None). The new worktree starts from the source's
        checkpoint branch; refused unless that branch is a fleet task branch of a job this runner ran in the
        same repo, and it exists."""
        base_branch, source_id = body.get("base_branch"), body.get("source_job")
        if base_branch is None and source_id is None:
            return None, None
        if not isinstance(base_branch, str) or not HANDOFF_BRANCH.fullmatch(base_branch):
            self.audit("handoff_denied", "base_branch is not a fleet task branch")
            raise HttpError(400, "base_branch must be a fleet task branch (fleet/t-xxxxxxxx)")
        if not isinstance(source_id, str) or not JOB_ID.fullmatch(source_id):
            raise HttpError(400, "base_branch needs source_job")
        source = self.jobs.get(source_id)
        if source is None:
            raise HttpError(400, "source job not found")
        if not repo or not body.get("worktree"):
            raise HttpError(400, "a handoff job needs a repo and its own worktree")
        if not source.get("repo") or self._comparable(source["repo"]) != self._comparable(repo):
            self.audit("handoff_denied", "source job is in another repo")
            raise HttpError(400, "source job is in another repo")
        if source.get("branch") != base_branch:
            raise HttpError(400, "base_branch is not the source job's branch")
        found = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + base_branch],
                               capture_output=True, text=True, timeout=15)
        if found.returncode:
            raise HttpError(400, "base_branch does not exist")
        return base_branch, source

    def _make_worktree(self, job_id, repo, policy=None, start=None):
        repo = self.check_path(repo, policy)
        self._check_git_common_dir(repo, policy)
        base = subprocess.run(
            ["git", "-C", repo, "rev-parse", start or "HEAD"], capture_output=True,
            text=True, timeout=15,
        )
        if base.returncode:
            raise HttpError(400, "repo is not a git repository")
        target = self.worktrees_dir / job_id
        branch = "fleet/" + job_id
        made = subprocess.run(
            ["git", "-C", repo, "worktree", "add", "-b", branch, str(target)] + ([start] if start else []),
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
        engine = body.get("engine", "ollama")
        if agent == "ollama" and engine not in ("ollama", "dmr"):
            raise HttpError(400, "unknown engine")
        if agent != "ollama" and body.get("engine") is not None:
            raise HttpError(400, "engine is only valid for local-model jobs")
        if agent == "ollama" and engine == "dmr" and engine not in self.engine_adapters:
            raise HttpError(400, "dmr engine is unavailable")
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
        if body.get("effort") is not None and body["effort"] not in EFFORTS:
            raise HttpError(400, "effort must be low, medium or high")
        tools = body.get("tools")
        if tools is not None and (not isinstance(tools, list) or any(
                not isinstance(tool, str) or not TOOL_NAME.fullmatch(tool) for tool in tools)):
            raise HttpError(400, "tools must be a list of tool names")
        if (tools is not None and agent == "claude" and not self._review_tools({"tools": tools, "policy": policy})
                and not self._has_flag("claude", "--tools")):
            # The effective list (after the policy's claude_allow narrows it) is empty. Only `--tools ""` can say
            # "no tools"; without it the restriction would be lost and the CLI's default tools would return.
            raise HttpError(400, "this Claude CLI can't run with no tools (no --tools option)")
        max_turns = body.get("max_turns")
        if max_turns is not None and (not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1):
            raise HttpError(400, "max_turns must be a positive integer")
        if agent == "ollama" and not body.get("model"):
            raise HttpError(400, "model is required for ollama")
        if agent == "ollama" and engine == "dmr":
            adapter = self.engine_adapters["dmr"]
            catalog = adapter.snapshot()
            if adapter.age() is None or adapter.age() >= 10:  # reuse a fresh snapshot instead of a probe per submit
                catalog = adapter.refresh()
            if not catalog["reachable"]:
                raise HttpError(503, "dmr engine is unreachable")
            if body.get("model") not in {item["reference"] for item in catalog["models"]}:
                raise HttpError(400, "model is not in the dmr catalog")
        if body.get("gate") and MODE_RANK[mode] < MODE_RANK["auto-edit"]:
            self.audit("gate_denied", "gate requested below auto-edit mode")
            raise HttpError(400, "a gate needs auto-edit or full mode")
        profile = body.get("profile")
        profile_entry = None
        if profile is not None:
            # Only an id from this runner's own config; never a path from the request.
            profile_entry = (self.profile_entry(agent, profile)
                             if isinstance(profile, str) and PROFILE_ID.fullmatch(profile) and agent != "ollama"
                             else None)
            if profile_entry is None:
                self.audit("profile_denied", "unknown profile id")
                raise HttpError(400, "unknown profile for %s on this runner" % agent)
        if not self.agents.get(agent) and not (agent == "ollama" and engine == "dmr"):
            raise HttpError(400, "%s agent is unavailable" % agent)
        gate = self._gate(body.get("gate"), body.get("gate_args"))
        repo = self.check_path(body["repo"], policy) if body.get("repo") else None
        if repo:
            self._check_git_common_dir(repo, policy)
        base_branch, source = self._handoff_source(body, repo)
        requested_cwd = None
        if body.get("cwd"):
            # A read-only review may run inside a build job's worktree (it lives under this runner's own
            # directory, which is otherwise always refused); nothing else may.
            requested_cwd = self._review_worktree(body["cwd"], mode) or self.check_path(body["cwd"], policy)
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
                    repo, cwd, branch, base = self._make_worktree(job_id, repo, policy, base_branch)
                except Exception:
                    shutil.rmtree(job_dir, ignore_errors=True)
                    raise
                if source is not None:
                    # Diff from where the source started, so its checkpointed work shows as part of this job's.
                    base = source.get("base") or base
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
                "id": job_id, "agent": agent, "engine": engine if agent == "ollama" else None,
                "mode": mode, "state": "running",
                "exit_code": None, "session_id": body.get("resume_session"), "cwd": cwd,
                "worktree": use_worktree, "branch": branch, "started_at": now(),
                "ended_at": None, "result": "", "denials": [], "gate": gate,
                "runs": 1, "repo": repo, "base": base, "model": body.get("model"),
                "allowed_tools": body.get("allowed_tools") or [], "pid": None,
                "exit_file": "exit_code.1",
                "instructions": instructions, "policy": policy, "policy_hash": policy_hash,
                "effort": body.get("effort"), "tools": tools, "max_turns": max_turns,
                "profile": profile_entry["id"] if profile_entry else self.profile_of(agent),
                "account_label": profile_entry["label"] if profile_entry else None,
                # the requested profile id: what every run of this job (resumes too) uses for its environment
                "profile_id": profile_entry["id"] if profile_entry else None,
                "base_branch": base_branch, "source_job": source["id"] if source else None,
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
            review_tools = self._review_tools(job) if job.get("tools") is not None else None  # [] means no tools
            if review_tools is not None:
                requested = review_tools  # a review gets only its tool list (already narrowed by the policy)
            else:
                requested = [str(item) for item in (allowed_tools or [])]
                if "claude_allow" in policy_tools:
                    permitted = policy_tools["claude_allow"]
                    requested = ([item for item in requested if item in permitted] if requested else list(permitted))
            if requested:
                cmd += ["--allowedTools", ",".join(requested)]
            if review_tools is not None and self._has_flag("claude", "--tools"):
                cmd += ["--tools", ",".join(review_tools)]
            if job.get("effort") and self._has_flag("claude", "--effort"):
                cmd += ["--effort", job["effort"]]
            if job.get("max_turns") and self._has_flag("claude", "--max-turns"):
                cmd += ["--max-turns", str(job["max_turns"])]
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
                if job.get("effort"):
                    cmd += ["-c", 'model_reasoning_effort="%s"' % job["effort"]]  # per run, never in config.toml
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
                if job.get("effort"):
                    cmd += ["-c", 'model_reasoning_effort="%s"' % job["effort"]]  # per run, never in config.toml
                cmd.append("-")  # "-": prompt from stdin
            return cmd
        engine = job.get("engine") or "ollama"
        request_path = self._job_dir(job) / ("%s-request.%d.json" % (engine, job["runs"]))
        if engine == "dmr":
            atomic_json(request_path, {
                "url": self.engine_adapters["dmr"].base_url, "model": job["model"], "prompt": prompt,
                "instructions": job.get("instructions") or "",
            }, 0o600)
            return [sys.executable, str(Path(__file__).resolve()), "--openai-worker", str(request_path)]
        atomic_json(request_path, {"url": self.ollama_url, "model": job["model"], "prompt": prompt,
                                   "instructions": job.get("instructions") or ""}, 0o600)
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
        events_path = job_dir / "events.jsonl"
        try:
            job["events_offset"] = events_path.stat().st_size  # usage counts this run only
        except OSError:
            job["events_offset"] = 0
        job.update({"usage": None, "num_turns": None, "error": None, "limits_applied": self.limits_applied(job)})
        raw = open(events_path, "ab", buffering=0)
        env = self.agent_env(job["agent"], os.environ.copy(), job.get("profile_id"))
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
        if "profile" in body:
            raise HttpError(400, "a resumed job keeps the profile it started with")
        if job.get("profile_id") and self.profile_entry(job["agent"], job["profile_id"]) is None:
            # Its session lives in that profile; under another one it would not be found.
            raise HttpError(409, "this job's profile is no longer configured on this runner")
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
            effort = body.get("effort")
            if effort is not None and effort not in EFFORTS:
                raise HttpError(400, "effort must be low, medium or high")
            if effort:
                job["effort"] = effort
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

    @staticmethod
    def _secret_path(relative, denied):
        name = relative.rsplit("/", 1)[-1]
        return (any(fnmatch.fnmatch(name, pattern) for pattern in CHECKPOINT_SECRET_GLOBS)
                or any(fnmatch.fnmatch(relative, pattern) for pattern in denied))

    def checkpoint(self, job, body=None):
        """Commit a stopped job's uncommitted work on its own branch, for a handoff (docs/SPEC-limit-handoff.md
        4.3 step 2). Secret-looking and policy-denied paths stay out and are reported. No hooks, no signing."""
        body = body or {}
        label = body.get("label") if isinstance(body.get("label"), str) else ""
        label = re.sub(r"[^A-Za-z0-9._:/ -]", "", label)[:80] or job.get("agent") or "agent"
        with self.lock:
            self.refresh(job)
            if job["state"] == "running":
                raise HttpError(409, "job is running")
            cwd = job.get("cwd")
            if not job.get("worktree") or not cwd or not Path(cwd).is_dir():
                raise HttpError(409, "job has no worktree")
            branch = "fleet/" + job["id"]

            def git(*args, check=True):
                out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, errors="replace",
                                     timeout=120)
                if check and out.returncode:
                    raise HttpError(500, trim(out.stderr or "git %s failed" % args[0], 500))
                return out

            head = git("symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
            if head != "refs/heads/" + branch or job.get("branch") != branch:
                raise HttpError(409, "worktree is not on its own branch %s" % branch)
            git_dir = Path(git("rev-parse", "--absolute-git-dir").stdout.strip())
            for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "index.lock"):
                if (git_dir / name).exists():
                    raise HttpError(409, "worktree is mid-merge, mid-rebase or locked")
            denied = []
            for path in ((job.get("policy") or {}).get("filesystem") or {}).get("denied") or []:
                full = os.path.realpath(os.path.expanduser(path))
                if self._within(full, cwd):
                    denied.append(os.path.relpath(full, os.path.realpath(cwd)).replace(os.sep, "/") + "*")
            git("add", "-A")
            staged = [p for p in git("diff", "--cached", "--name-only", "-z").stdout.split("\0") if p]
            excluded = sorted(p for p in staged if self._secret_path(p, denied))
            if excluded:
                git("reset", "-q", "--", *excluded)
            if not git("diff", "--cached", "--name-only").stdout.strip():
                tip = git("rev-parse", "HEAD").stdout.strip()
                return {"sha": tip, "committed": False, "excluded": excluded, "branch": branch, "stat": ""}
            committed = subprocess.run(
                ["git", "-C", cwd, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                 "-c", "user.name=AI Fleet", "-c", "user.email=fleet@localhost",
                 "commit", "-q", "--no-verify", "--no-gpg-sign", "-m",
                 "checkpoint: stopped at usage limit (%s)" % label],
                capture_output=True, text=True, errors="replace", timeout=120)
            if committed.returncode:
                git("reset", "-q", check=False)
                raise HttpError(500, trim(committed.stderr or "checkpoint commit failed", 500))
            sha = git("rev-parse", "HEAD").stdout.strip()
            stat = git("show", "--stat", "--format=", sha).stdout
            self.audit("checkpoint", "job %s checkpointed (%d paths left out)" % (job["id"], len(excluded)))
            return {"sha": sha, "committed": True, "excluded": excluded, "branch": branch, "stat": trim(stat, 4000)}

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

    def _tree(self, job):
        """Hash of the job's working tree as it is now (untracked files included, ignored ones not). Uses a
        temporary GIT_INDEX_FILE, so the job's own index is never touched."""
        index = self._job_dir(job) / ("tree-index.%d" % time.time_ns())
        env = dict(os.environ, GIT_INDEX_FILE=str(index))
        try:
            done = None
            # Start from HEAD's tree so tracked files that match .gitignore stay in; then take the worktree as is.
            for args in (["git", "read-tree", "HEAD"], ["git", "add", "-A"], ["git", "write-tree"]):
                done = subprocess.run(args, cwd=job["cwd"], env=env, capture_output=True, text=True,
                                      errors="replace", timeout=120)
                if done.returncode:
                    raise HttpError(500, trim(done.stderr or "git %s failed" % args[1]))
            return done.stdout.strip()
        finally:
            for path in (index, Path(str(index) + ".lock")):
                try:
                    path.unlink()
                except OSError:
                    pass

    def tree(self, job):
        inside = subprocess.run(["git", "-C", job["cwd"], "rev-parse", "--show-toplevel"], capture_output=True,
                                text=True)
        if inside.returncode:
            raise HttpError(400, "job cwd is not a git repository")
        return {"tree": self._tree(job)}

    def diff(self, job, since=None):
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

        if since is not None:
            # The change since an earlier tree hash (a re-review sees only this delta).
            if not isinstance(since, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", since):
                raise HttpError(400, "since must be a tree hash")
            current = self._tree(job)
            stat = git_output(["git", "diff", "--stat", since, current, "--"])
            patch = git_output(["git", "diff", "--no-ext-diff", since, current, "--"])
            return {"stat": stat[-200000:], "diff": patch[-200000:], "tree": current}

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
            if (running and running.get("kind") == kind and running.get("ref") == body.get("ref")
                    and running.get("derived") == body.get("derived") and (running.get("params") or {}) == (params or {})
                    and bool(running.get("override_fit" if kind == "ensure" else "confirm_preexisting")) == bool(flag)):
                return dict(running)  # exactly the same request again: idempotent
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
                                                        "parameters": engine_params, "stream": False}, timeout=3600)
                managed = self._managed()
                managed.add(operation["derived"])
                self._save_managed(managed)
            self._update(operation, state="verifying")
            params = operation.get("params", {})
            chat = {"model": target, "messages": [{"role": "user", "content": "Reply with the word ready."}],
                    "stream": False, "options": {"num_predict": 256}}
            if "think" in params:
                chat["think"] = params["think"]
            reply = self._request("POST", "/api/chat", chat, timeout=900)
            message = reply.get("message") or {}
            # Reasoning models may spend the whole budget thinking; any output proves the model loads and runs.
            if not str(message.get("content") or message.get("thinking") or "").strip():
                raise RuntimeError("smoke test returned an empty reply")
            loaded = self._request("GET", "/api/ps").get("models", [])
            loaded_size = next(((item.get("size") or 0) / 1e9 for item in loaded
                                if isinstance(item, dict) and item.get("name") == target), None)
            self._request("POST", "/api/generate", {"model": target, "keep_alive": 0, "stream": False})
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


def system_power():
    """Return portable best-effort battery state; failures deliberately become unknown."""
    fake = os.environ.get("FLEET_FAKE_POWER")
    if fake is not None:
        try:
            value = json.loads(fake)
            if isinstance(value, dict):
                return {"on_battery": value.get("on_battery"), "battery_pct": value.get("battery_pct")}
        except ValueError:
            pass
    try:
        if sys.platform == "darwin":
            output = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=3).stdout
            match = re.search(r"(\d+)%", output)
            source = output.splitlines()[0].lower() if output else ""
            return {"on_battery": (True if "battery power" in source else
                                    False if "ac power" in source else None),
                    "battery_pct": min(100, max(0, int(match.group(1)))) if match else None}
        if sys.platform.startswith("linux"):
            batteries = []
            for directory in glob.glob("/sys/class/power_supply/*"):
                base = Path(directory)
                if (base / "type").read_text(errors="ignore").strip().lower() != "battery":
                    continue
                capacity = int((base / "capacity").read_text().strip())
                status = (base / "status").read_text(errors="ignore").strip().lower()
                batteries.append((capacity, status))
            if batteries:
                pct = round(sum(item[0] for item in batteries) / len(batteries))
                return {"on_battery": any(item[1] == "discharging" for item in batteries),
                        "battery_pct": min(100, max(0, pct))}
        if sys.platform == "win32":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                            ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                            ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
            state = Status()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(state)):
                return {"on_battery": None if state.ACLineStatus == 255 else state.ACLineStatus == 0,
                        "battery_pct": None if state.BatteryLifePercent == 255 else int(state.BatteryLifePercent)}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {"on_battery": None, "battery_pct": None}


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
            return health_report(self.manager)
        if method == "GET" and parts == ["engines"]:
            return self.manager.engine_catalog(refresh=True)
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
                return self.manager.diff(job, since=parse_qs(parsed.query).get("since", [None])[0])
            if method == "POST" and len(parts) == 3 and parts[2] == "tree":
                return self.manager.tree(job)
            if method == "POST" and len(parts) == 3 and parts[2] == "message":
                return self.manager.message(job, self._body())
            if method == "POST" and len(parts) == 3 and parts[2] == "cancel":
                return self.manager.cancel(job)
            if method == "POST" and len(parts) == 3 and parts[2] == "cleanup":
                return self.manager.cleanup(job)
            if method == "POST" and len(parts) == 3 and parts[2] == "checkpoint":
                return self.manager.checkpoint(job, self._body())
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
        # The controller tells us our node id, for signed heartbeats. Only from a token-checked request (12.1).
        node_id = self.headers.get("X-Fleet-Node-Id")
        if node_id and NODE_ID.fullmatch(node_id):
            self.manager.node_id = node_id
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


def health_report(manager):
    """/health, also the body of a heartbeat. Metrics and Ollama come from the metrics thread's cache when it runs,
    so no slow command or probe sits on this path (docs/SPEC-live-monitor.md 4.1)."""
    metrics = manager.metrics.snapshot() if getattr(manager, "metrics", None) else None
    ollama_up = (metrics.get("ollama") or {}).get("up") if metrics else None
    agents = find_agents(manager.ollama_url, ollama_up=ollama_up)
    engine_health = (manager.engine_catalog(refresh=False)
                     if callable(getattr(manager, "engine_catalog", None)) else {})
    manager.agents = agents
    running = 0
    active_dmr = set()
    for job in list(manager.jobs.values()):  # a snapshot: jobs are added from request threads
        manager.refresh(job)
        running += job.get("state") == "running"
        if job.get("state") == "running" and job.get("engine") == "dmr" and isinstance(job.get("model"), str):
            active_dmr.add(job["model"])
    for item in engine_health.get("dmr", {}).get("models") or []:
        if item.get("reference") in active_dmr:
            item.update(warm=True, warm_source="active_runner_job")
    power = system_power()
    power["min_battery_pct"] = manager.min_battery_pct
    power["ok"] = not (power["on_battery"] is True and power["battery_pct"] is not None
                       and power["battery_pct"] < manager.min_battery_pct)
    report = {"ok": True, "version": VERSION, "digest": RUNNER_DIGEST,
              "power": power, "max_mode": manager.max_mode,
              "agents": agents, "running": running,
              "gate_profiles": sorted(manager.gate_profiles),
              "allow_raw_gates": manager.allow_raw_gates,
              "allowed_roots": manager.allowed_roots,
              "max_jobs": manager.max_jobs,
              "models": manager.models_config,
              "policy": True, "network": manager.network,
              "budget": manager.budget_support(), **manager.profiles(),
              "profiles": manager.profile_report(), "checkpoint": True,
              "default_models": manager.default_models(),
              "engines": engine_health,
              # pushes heartbeats (informational: the controller trusts only heartbeats it accepted)
              "heartbeat": bool(getattr(manager, "controller_url", None))}
    if metrics is not None:
        report["metrics"] = metrics
        report["age_s"] = metrics.get("age_s")
    return report


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


def openai_worker(request_path):
    request_data = read_json(request_path, {})
    messages = []
    if request_data.get("instructions"):
        messages.append({"role": "system", "content": request_data["instructions"]})
    messages.append({"role": "user", "content": request_data.get("prompt", "")})
    payload = json.dumps({"model": request_data.get("model"), "messages": messages,
                          "stream": False}).encode("utf-8")
    try:
        base = loopback_base_url(request_data.get("url", ""))
        request = urllib.request.Request(base + "/chat/completions",
                                         data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with loopback_urlopen(request, timeout=900) as response:
            body = json.load(response)
        choices = body.get("choices") if isinstance(body, dict) else None
        text = ((choices[0].get("message") or {}).get("content")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None)
        if not isinstance(text, str):
            raise ValueError("OpenAI-compatible response has no message content")
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
    if len(sys.argv) == 3 and sys.argv[1] == "--openai-worker":
        return openai_worker(sys.argv[2])
    fleet_dir = os.environ.get("FLEET_DIR", str(Path.home() / ".ai-fleet"))
    ollama_url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    address = parse_bind(os.environ.get("RUNNER_BIND", "127.0.0.1:11436"))
    server = RunnerServer(address, Handler)
    server.manager = JobManager(fleet_dir, ollama_url)
    server.runner_token = runner_token(fleet_dir)
    server.manager.metrics = MetricsCollector(server.manager.root, ollama_url).start()
    EngineCollector(server.manager).start()
    Heartbeat(server.manager, server.runner_token, lambda: health_report(server.manager)).start()

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

function Set-DefaultProperty($Object, $Name, $Value) {
  if (-not ($Object.PSObject.Properties.Name -contains $Name)) {
    $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
  }
}

function Configure-RunnerPolicy($cfg) {
  $config = $null
  if (Test-Path -LiteralPath $cfg) {
    try { $config = Get-Content -LiteralPath $cfg -Raw | ConvertFrom-Json } catch { $config = $null }
  }
  if ($null -eq $config) { $config = [PSCustomObject]@{} }
  $hasRoots = $config.PSObject.Properties.Name -contains "allowed_roots"
  $roots = @($AllowRoot)
  $setRoots = $AllowRootsSet
  if (-not $setRoots -and -not $hasRoots -and -not $Yes) {
    Write-Host "Folders agents may work in (enter one absolute path at a time; blank finishes; no default):"
    while ($true) {
      $root = Read-Host "Allow root"
      if (-not $root) { break }
      $roots += $root
    }
    $setRoots = $true
  }
  if (-not $hasRoots) { $setRoots = $true }
  if ($setRoots) {
    $resolved = @($roots | ForEach-Object { [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($_)) })
    if ($hasRoots) { $config.allowed_roots = $resolved }
    else { $config | Add-Member -NotePropertyName allowed_roots -NotePropertyValue $resolved }
  }
  Set-DefaultProperty $config "max_mode" "auto-edit"
  # Where this runner pushes its signed heartbeats (docs/SPEC-live-monitor.md 12.1); kept if already set.
  if ($Master) { Set-DefaultProperty $config "controller" $Master }
  Set-DefaultProperty $config "denied_paths" @()
  Set-DefaultProperty $config "gate_profiles" ([PSCustomObject]@{})
  if ($null -eq $config.gate_profiles -or -not ($config.gate_profiles -is [PSCustomObject])) {
    $config.gate_profiles = [PSCustomObject]@{}
  }
  Set-DefaultProperty $config.gate_profiles "pytest" @("python", "-m", "pytest", "-q")
  Set-DefaultProperty $config.gate_profiles "unittest" @("python", "-m", "unittest", "-q")
  Set-DefaultProperty $config "allow_raw_gates" $false
  Set-DefaultProperty $config "max_jobs" $(if ((RamGB) -le 16) { 1 } else { 2 })
  Set-DefaultProperty $config "max_job_minutes" 120
  Set-DefaultProperty $config "power" ([PSCustomObject]@{ min_battery_pct = 30 })
  # Written out so the owner can see the switch; the console may install models only when it is true.
  Set-DefaultProperty $config "models" ([PSCustomObject]@{ remote_management = $false; max_loaded = 1; disk_reserve_gb = 8; ram_reserve_gb = 4 })
  Write-Utf8File $cfg ($config | ConvertTo-Json -Depth 8)
}

function Install-Runner($ip) {
  $py = (Get-Command python -ErrorAction SilentlyContinue).Source
  if (-not $py) {
    if (-not (Ask "Python is needed for the agent runner. Install it?")) { return }
    winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements
    $py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
  }
  $runnerPath = Join-Path $FleetDir "runner.py"
  # Drop build.py's marker lines so the installed file is byte-identical to the master's nodes/runner.py
  # (the controller compares digests to show "update available").
  $runnerText = ($RunnerSource -replace '^# >>> runner\.py[^\n]*\r?\n', '') -replace '\r?\n# <<< runner\.py\s*$', ''
  Write-Utf8File $runnerPath ($runnerText + "`n")
  $cfg = Join-Path $FleetDir "runner.json"
  Configure-RunnerPolicy $cfg
  $cmd = Join-Path $FleetDir "runner.cmd"
  $cmdPython = Escape-CmdValue $py
  $cmdFleetDir = Escape-CmdValue $FleetDir
  $cmdSource = @"
@echo off
chcp 65001 >nul
set "RUNNER_BIND=${ip}:11436"
set "FLEET_DIR=$cmdFleetDir"
set "OLLAMA_URL=http://${ip}:11434"
set "CLAUDE_CONFIG_DIR=%USERPROFILE%\.claude-fleet"
set "CODEX_HOME=%USERPROFILE%\.codex-fleet"
if not exist "%CLAUDE_CONFIG_DIR%" mkdir "%CLAUDE_CONFIG_DIR%"
if not exist "%CODEX_HOME%" mkdir "%CODEX_HOME%"
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
  Write-Host "To change what agents may do, edit $cfg (allowed_roots, gate_profiles, max_mode and limits)."
  $tokenPath = Join-Path $FleetDir "runner.token"
  $token = ""
  for ($i = 0; $i -lt 30; $i++) {
    if (Test-Path -LiteralPath $tokenPath) { $token = (Get-Content -LiteralPath $tokenPath -Raw).Trim(); if ($token) { break } }
    Start-Sleep -Seconds 1
  }
  if ($token) {
    $script:RunnerTokenValue = $token
    $nodeName = $env:COMPUTERNAME.ToLower()
    if (-not $EnrollRequested) {
      Write-Host "Runner token (shown once). On the master run: printf '%s\n' '$token' | uv run --script master/controller/controller.py runner-token set '$nodeName'"
    }
  } else {
    Write-Host "Runner token was not created yet; inspect the AI Fleet Runner scheduled task and restart it."
  }
}

if ($ScanOnly) { Scan (TailnetIp); exit 0 }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host "Run this in an elevated PowerShell (Run as administrator)."; exit 1 }
if (-not (Have winget)) { Write-Host "winget is required (App Installer from the Microsoft Store)."; exit 1 }

if ($EnrollRequested) {  # resolve the code first: a bad file or no terminal stops before anything changes
  if (-not $Master) { throw "-Master is required with -Enroll, -EnrollFile or -EnrollPrompt" }
  if ($EnrollFile) {
    if (-not (Test-Path -LiteralPath $EnrollFile)) { throw "-EnrollFile $EnrollFile: no such file" }
    $EnrollCode = (Get-Content -LiteralPath $EnrollFile -Raw).Trim()
  } elseif ($EnrollPrompt) {
    $secure = Read-Host -AsSecureString "Console enrollment code"
    $EnrollCode = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
  } else {
    Write-Warning "The enrollment code was passed on the command line, so it is visible in the process list and PowerShell history. Next time use -EnrollPrompt or -EnrollFile PATH."
    $EnrollCode = $Enroll
  }
  if (-not $EnrollCode) { throw "The enrollment code is empty" }
}
Say "AI fleet onboarding: $env:COMPUTERNAME, Windows, $(RamGB)GB RAM, $(GpuGB)GB GPU"
Ensure-TailscaleConnection
$ip = TailnetIp
if (-not $ip) { Write-Host "No tailnet IP: is Tailscale signed in?"; exit 1 }
Configure-Ollama $ip
Say "Scanning for LLMs, engines and agents"
Scan $ip
Install-Services $ip
Install-Runner $ip
if ($EnrollRequested) {
  if (-not $RunnerTokenValue) { throw "Runner token was not created; enrollment cannot complete" }
  $body = @{ code = $EnrollCode; hostname = $env:COMPUTERNAME.ToLower(); runner_token = $RunnerTokenValue } | ConvertTo-Json -Compress
  Invoke-RestMethod -Method Post -Uri ($Master.TrimEnd('/') + "/api/enroll/complete") -ContentType "application/json" -Body $body | Out-Null
  $RunnerTokenValue = ""
  $EnrollCode = ""
  Write-Host "Enrollment completed; approve this pending node in the console."
}
Ensure-WorkerTag
Say "Done"
Write-Host "This node will show as 'pending' in the master dashboard within ~30s. Approve it there, then assign roles."

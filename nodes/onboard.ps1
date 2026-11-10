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
  Start-Process explorer.exe -ArgumentList "`"$env:LOCALAPPDATA\Programs\Ollama\ollama app.exe`""
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
  if ($PSCommandPath -ne $self) { Copy-Item $PSCommandPath $self -Force }
  $server = Join-Path $FleetDir "serve-info.ps1"
  @"
`$l = New-Object System.Net.HttpListener
`$l.Prefixes.Add("http://${ip}:$InfoPort/")
`$l.Start()
while (`$l.IsListening) {
  `$c = `$l.GetContext()
  if (`$c.Request.Url.AbsolutePath -eq "/fleet.json") {
    `$b = [IO.File]::ReadAllBytes("$FleetDir\public\fleet.json"); `$c.Response.ContentType = "application/json"
  } else { `$b = [byte[]]@(); `$c.Response.StatusCode = 404 }
  `$c.Response.OutputStream.Write(`$b, 0, `$b.Length); `$c.Response.Close()
}
"@ | Set-Content $server -Encoding UTF8

  $ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
  $info = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$server`""
  $boot = New-ScheduledTaskTrigger -AtStartup
  $settings = New-ScheduledTaskSettingsSet -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit 0 `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
  Register-ScheduledTask -TaskName "AI Fleet Info" -Action $info -Trigger $boot -Settings $settings -User "SYSTEM" -RunLevel Highest -Force | Out-Null
  Start-ScheduledTask -TaskName "AI Fleet Info"

  $scan = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$self`" -ScanOnly -FleetDir `"$FleetDir`""
  $hourly = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours 1)
  $scanSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
  Register-ScheduledTask -TaskName "AI Fleet Scan" -Action $scan -Trigger $hourly -Settings $scanSettings -User $env:USERNAME -Force | Out-Null

  # Allow only tailnet addresses (100.64.0.0/10) to reach the model and inventory ports.
  Get-NetFirewallRule -DisplayName "AI Fleet" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  New-NetFirewallRule -DisplayName "AI Fleet" -Direction Inbound -Protocol TCP -LocalPort 11434, $InfoPort, 1234, 8000, 8080 `
    -RemoteAddress 100.64.0.0/10 -Action Allow | Out-Null
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
Say "Done"
Write-Host "This node will show as 'pending' in the master dashboard within ~30s. Approve it there, then assign roles."

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
  [pscustomobject]@{ engine = "dmr"; enabled = $false; status = "unknown"; detail = "docker CLI not found; Docker Desktop/DMR is owner-installed and optional"; models = @() } | ConvertTo-Json -Depth 4
  exit 0
}
$rows = @(& $docker.Source model ls 2>$null)
if ($LASTEXITCODE -ne 0) {
  [pscustomobject]@{ engine = "dmr"; enabled = $false; status = "unknown"; detail = "docker model ls failed; DMR availability is UNVERIFIED"; models = @() } | ConvertTo-Json -Depth 4
  exit 0
}
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
  [pscustomobject]@{ engine = "dmr"; enabled = $false; status = "unknown"; detail = "Python helper unavailable; DMR catalog is UNVERIFIED"; models = @() } | ConvertTo-Json -Depth 4
  exit 0
}
$models = (($rows -join [Environment]::NewLine) | & $python.Source (Join-Path $PSScriptRoot "networking.py") dmr | ConvertFrom-Json)
[pscustomobject]@{ engine = "dmr"; enabled = $true; status = "discovered"; detail = "catalog is read-only; verify Docker Desktop binds host TCP to loopback before enabling the runner"; models = @($models) } | ConvertTo-Json -Depth 5

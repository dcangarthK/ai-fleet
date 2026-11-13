[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
  [pscustomobject]@{ engine = "dmr"; enabled = $false; status = "unknown"; detail = "docker CLI not found; Docker Desktop/DMR is owner-installed and optional"; models = @() } | ConvertTo-Json -Depth 4
  exit 0
}
$rows = @(& $docker.Source model ls 2>$null)
[pscustomobject]@{ engine = "dmr"; enabled = $true; status = "discovered"; detail = "catalog is read-only; verify Docker Desktop binds host TCP to loopback before enabling the runner"; models = $rows } | ConvertTo-Json -Depth 4

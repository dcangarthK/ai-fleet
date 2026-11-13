[CmdletBinding()]
param([string]$TaskName = "AI Fleet Runner")

$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$service = Get-Service -Name "AIFleetRunner" -ErrorAction SilentlyContinue
if ($service -and $service.Status -eq "Running" -and $service.StartType -eq "Automatic") {
  [pscustomobject]@{ availability = "available-before-owner-logon"; source = "windows-service"; detail = "automatic Windows service is running" } | ConvertTo-Json -Compress
  exit 0
}
if ($task) {
  $hasLogon = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -match "Logon" }).Count -gt 0
  if ($hasLogon) {
    [pscustomobject]@{ availability = "available-after-owner-logon"; source = "scheduled-task"; detail = "per-user logon task; service compatibility remains UNVERIFIED" } | ConvertTo-Json -Compress
    exit 0
  }
}
[pscustomobject]@{ availability = "unknown"; source = "read-only-check"; detail = "no proven automatic runner service or logon task" } | ConvertTo-Json -Compress

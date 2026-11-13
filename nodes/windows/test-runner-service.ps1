[CmdletBinding()]
param([string]$TaskName = "AI Fleet Runner")

$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$service = Get-Service -Name "AIFleetRunner" -ErrorAction SilentlyContinue
$taskObservation = if ($task) { [ordered]@{ State = [string]$task.State; Triggers = @($task.Triggers | ForEach-Object { $_.CimClass.CimClassName }) } } else { $null }
$serviceObservation = if ($service) { [ordered]@{ Status = [string]$service.Status; StartType = [string]$service.StartType } } else { $null }
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
  [pscustomobject]@{ availability = "unknown"; source = "read-only-check"; detail = "Python helper unavailable; runner service availability is UNVERIFIED" } | ConvertTo-Json -Compress
  exit 0
}
([ordered]@{ task = $taskObservation; service = $serviceObservation } | ConvertTo-Json -Compress) |
  & $python.Source (Join-Path $PSScriptRoot "networking.py") availability

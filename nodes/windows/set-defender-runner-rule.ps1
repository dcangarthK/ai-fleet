[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)] [string[]]$MasterTailnetIp,
  [Parameter(Mandatory = $true)] [string]$TailscaleInterfaceAlias,
  [switch]$Approve,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$runnerRuleName = "AI Fleet Runner 11436"
$compatibilityRuleName = "AI Fleet Controller Compatibility"
$legacyRuleName = "AI Fleet"
$python = Get-Command python -ErrorAction Stop
$helper = Join-Path $PSScriptRoot "networking.py"
$rule = (& $python.Source $helper rule @MasterTailnetIp | ConvertFrom-Json)
$addresses = @($rule.remote_addresses)
$networkCategory = (Get-NetConnectionProfile -InterfaceAlias $TailscaleInterfaceAlias -ErrorAction Stop).NetworkCategory
$profile = switch ($networkCategory) {
  "DomainAuthenticated" { "Domain" }
  "Private" { "Private" }
  "Public" { "Public" }
  default { throw "Tailscale interface has no usable firewall profile: $TailscaleInterfaceAlias" }
}
$legacy = @(Get-NetFirewallRule -DisplayName $legacyRuleName -ErrorAction SilentlyContinue)

Write-Host "Proposed runner rule: inbound Allow TCP local port 11436 from $($addresses -join ', ') on $TailscaleInterfaceAlias ($profile) only."
Write-Host "Proposed compatibility rule: inbound Allow TCP local ports 11434,11435 from the same master address(es) only, until controller migration retires them."
if ($legacy.Count) { Write-Host "Legacy broad '$legacyRuleName' rule detected and will be removed after approval." }
else { Write-Host "No legacy broad '$legacyRuleName' rule detected." }
if (-not $Approve -or $DryRun) { Write-Host "Read-only proposal: no Defender Firewall command was run."; exit 0 }

# This is a remove-then-create migration, not an atomic replacement. A failed creation fails closed.
Get-NetFirewallRule -DisplayName $runnerRuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Get-NetFirewallRule -DisplayName $compatibilityRuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
$legacy | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $runnerRuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort 11436 -RemoteAddress $addresses -InterfaceAlias $TailscaleInterfaceAlias -Profile $profile | Out-Null
New-NetFirewallRule -DisplayName $compatibilityRuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort 11434,11435 -RemoteAddress $addresses -InterfaceAlias $TailscaleInterfaceAlias -Profile $profile | Out-Null
Get-NetFirewallRule -DisplayName $runnerRuleName,$compatibilityRuleName | Get-NetFirewallPortFilter | Format-Table -AutoSize
Get-NetFirewallRule -DisplayName $runnerRuleName,$compatibilityRuleName | Get-NetFirewallAddressFilter | Format-Table -AutoSize

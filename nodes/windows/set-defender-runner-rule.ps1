[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)] [string[]]$MasterTailnetIp,
  [switch]$Approve,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ruleName = "AI Fleet Runner 11436"
$tailnet = [ipaddress]::Parse("100.64.0.0")
$tailnetMask = [ipaddress]::Parse("255.192.0.0")
$addresses = @()
foreach ($raw in $MasterTailnetIp) {
  $address = $null
  if (-not [ipaddress]::TryParse($raw, [ref]$address) -or $address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
    throw "MasterTailnetIp must contain concrete IPv4 addresses only: $raw"
  }
  $bytes = $address.GetAddressBytes(); $networkBytes = $tailnet.GetAddressBytes(); $maskBytes = $tailnetMask.GetAddressBytes()
  for ($i = 0; $i -lt 4; $i++) { if (($bytes[$i] -band $maskBytes[$i]) -ne ($networkBytes[$i] -band $maskBytes[$i])) { throw "MasterTailnetIp is outside the Tailnet IPv4 range: $raw" } }
  if ($addresses -notcontains $address.IPAddressToString) { $addresses += $address.IPAddressToString }
}
if ($addresses.Count -eq 0) { throw "At least one master Tailnet IPv4 address is required." }

Write-Host "Proposed Defender rule: inbound Allow TCP local port 11436 from $($addresses -join ', ') only."
Write-Host "No model port and no Tailnet CIDR will be included."
if (-not $Approve) { throw "Refusing to change Defender Firewall without -Approve. Re-run with -Approve -DryRun to inspect only, then remove -DryRun after owner review." }
if ($DryRun) { Write-Host "Dry run: no Defender Firewall command was run."; exit 0 }

# The named rule is replaced atomically in scope: it is the only rule this script may change.
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort 11436 -RemoteAddress $addresses | Out-Null
Get-NetFirewallRule -DisplayName $ruleName | Get-NetFirewallPortFilter | Format-Table -AutoSize
Get-NetFirewallRule -DisplayName $ruleName | Get-NetFirewallAddressFilter | Format-Table -AutoSize

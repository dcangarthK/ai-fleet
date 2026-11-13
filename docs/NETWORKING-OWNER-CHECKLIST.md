# Networking owner checklist

## Phase N3 — Windows parity

1. Enrol and approve the Windows node before it advertises `tag:llm-node`.
2. Compatibility hold: leave Ollama on the node Tailnet IP at TCP 11434 and the inventory service at TCP 11435. The controller still reads those paths directly; retire them only after the controller migrates to runner inventory and evaluation endpoints.
3. If Docker Desktop is already owner-installed, run `nodes/windows/discover-dmr.ps1`. Verify DMR's actual host bind is loopback-only before opting the runner into DMR.
4. Find the actual Tailscale adapter alias with `Get-NetAdapter`, then inspect the proposed Defender migration with `nodes/windows/set-defender-runner-rule.ps1 -MasterTailnetIp <master-tailnet-ip> -TailscaleInterfaceAlias <alias> -DryRun`. After review, rerun without `-DryRun` and with `-Approve`. It removes the legacy broad `AI Fleet` rule, installs the narrow TCP 11436 runner rule, and retains TCP 11434/11435 for the same concrete master address(es) only until the compatibility hold is retired.
5. Run `nodes/windows/test-runner-service.ps1` and record whether the existing per-user runner is only available after owner logon. Do not install a Windows service until compatibility with local subscription profiles is verified.

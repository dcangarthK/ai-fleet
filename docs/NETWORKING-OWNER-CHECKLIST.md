# Networking owner checklist

## Phase N3 — Windows parity

1. Enrol and approve the Windows node before it advertises `tag:llm-node`.
2. Confirm Ollama is listening only on `127.0.0.1:11434`; do not expose an engine port to the tailnet or LAN.
3. If Docker Desktop is already owner-installed, run `nodes/windows/discover-dmr.ps1`. Verify DMR's actual host bind is loopback-only before opting the runner into DMR.
4. Review the exact Defender proposal, then (only if approved) run `nodes/windows/set-defender-runner-rule.ps1 -MasterTailnetIp <master-tailnet-ip> -Approve`. Run it first with `-Approve -DryRun`; it permits only TCP 11436 from the supplied concrete master Tailnet IPv4 address(es).
5. Run `nodes/windows/test-runner-service.ps1` and record whether the existing per-user runner is only available after owner logon. Do not install a Windows service until compatibility with local subscription profiles is verified.

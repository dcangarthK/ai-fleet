# Networking owner checklist

Steps only the owner can do (docs/SPEC-networking.md §11). No controller action applies or approves any of these.

## Phase N4: optional gateway adapter

Code: `master/controller/gateway/`. It ships a driver interface, read-only discovery, a namespaced plan, dry-run
diff, apply with canary and rollback, and an in-memory `FakeRouter`. The OpenWrt and OPNsense drivers are stubs that
raise `NotImplementedError`; nothing in the repo contacts a router. Skip this whole section if you have no suitable
router: enrolment never depends on it.

- [ ] Confirm the router hardware and OS (OpenWrt or OPNsense) and that you own it. Until you do, N4 stays unused.
- [ ] Decide whether you want a worker VLAN/SSID. This is a router change you make by hand.
- [ ] Measure the egress destinations workers need (DNS, NTP, Tailscale, subscription CLIs, updates, model pulls)
      before any WAN allowlist exists. Do not enable a default WAN block until that list is complete.
- [ ] Back up the router configuration before any change.
- [ ] Review the dry-run diff once a real driver exists. Every fleet rule id starts with `aifleet-`; any other rule
      is yours and is never edited. Do not name any of your own rules with that prefix: they would be replaced.
      Rule order (first match wins): fleet lateral deny, your rules, fleet allows, fleet default WAN deny. So your
      denies win over fleet allows, the lateral deny wins over your allows, but your catch-all allow placed
      earlier overrides the default WAN deny.
- [ ] Approve the apply yourself (the `owner_approved` flag); keep a second path into the router open during the
      confirm window.
- [ ] Choose the canaries yourself: the controller only requires at least one that expects "reachable", so include
      worker to master TCP 8443 and the console path, plus one that expects a blocked flow.
- [ ] After apply, check the canary result and that master, workers and the console are still reachable. If not,
      let the confirm window expire or roll back from the router's own UI.

Steps only the owner can apply (SPEC-networking.md section 11). Nothing in the repo applies them.

## Phase N0: tailnet policy and 11435 audit

1. Review `netpolicy/tailnet-policy.hujson` against SPEC-networking.md sections 4 and 14 item 5. It keeps the
   master -> worker port list unchanged (the controller still uses 11434/11435/8000/1234) and adds the
   worker -> master 8443 grant with worker -> worker denied. The narrowed end state is
   `netpolicy/tailnet-policy-target.hujson`: do NOT apply it until probes and evals move to the runner.
2. Member/admin console tests: in `netpolicy/tailnet-policy.hujson`, the commented-out `OWNER_EMAIL` test row is a
   placeholder. In the admin console copy only, replace `OWNER_EMAIL` with your own tailnet login and uncomment it
   so the console checks the member and admin rows. Never commit your real identity to the repo.
3. In the Tailscale admin console, Access controls: paste the file, run the console's tests, then save.
4. Enrolment: the current onboarding advertises `tag:llm-node` itself (SPEC-networking.md section 2). Enrolling
   untagged and tagging after approval is the later enrol-before-tag flow (Windows N3 / `onboard.ps1`), so follow
   the existing join checklist for now. The repo tests for this are an ACL lookup only, not a simulated
   enrolment.
5. The `tcp:22` grant covers plain sshd over the tailnet only. Tailscale SSH needs a separate `ssh` block, which
   this policy does not contain.
6. Run `sudo python3 scripts/audit_11435.py` on each macOS/Linux node (read-only `lsof` listing; without root it
   only sees your own sockets and reports UNVERIFIED). Exit 0 = verified clean, 1 = wildcard or LAN listener on
   11435, 2 = UNVERIFIED. Windows has no `lsof`: the script exits 2 there, so check with
   `Get-NetTCPConnection -State Listen -LocalPort 11435` by hand. Retire 11435 only after telemetry shows no
   clients (SPEC section 5.1).

# Networking: owner checklist

Steps only the owner can apply. Nothing here is run by the fleet. See `docs/SPEC-networking.md` section 11.

## Phase N2: loopback engines and firewall posture

**Warning: migration order.** LiteLLM reaches a worker's Ollama at `http://<node ip>:11434`. Once step 1 binds that
engine to loopback, those remote Ollama roles stop working through LiteLLM (and step 4 blocks the path in the ACL
too). The controller now skips such a node's deployments and prints a warning (event `deployment.skipped`; also when the engine bind cannot be observed), so
LiteLLM fails over to other deployments in the same role. Before step 1, move the roles you still need onto the
master or another node, or accept the outage for that worker. Evals are not affected: they go through the runner.

Do these in order, one worker at a time.

1. **Update each node.** Re-run `nodes/onboard.sh` (macOS/Linux) on the worker. It now binds Ollama to
   `127.0.0.1`, points the runner's `OLLAMA_URL` at loopback, and removes the old 11435 info service.
2. **Check the node on Live.** The engine should be healthy and the firewall badge pass (Windows) or, on macOS, warn or unknown (reading pf needs root, so a normal-user runner shows "pf unreadable"; that is expected). Press "Check now"
   (read-only) to refresh the posture.
   - warn/fail: read the badge details (pf, application firewall, Defender, listener table) and fix on the node.
   - unknown: the runner could not read the setting (often a permission error). It is not a pass.
3. **Confirm nothing listens on the raw ports.** On the node, check that 11434 and 11435 are not bound to the
   Tailscale address (for example `lsof -nP -iTCP:11434 -sTCP:LISTEN` shows only `127.0.0.1`).
4. **Apply the narrowed ACL** only after every node passes steps 1-3. Compare `tailscale/policy.n2-narrowed.hujson`
   with `tailscale/policy.hujson`, paste it into the Tailscale admin console (Access controls) and confirm the
   built-in tests pass. The file is proposed, not applied. Effect: master reaches workers on TCP 11436 only; raw
   engine ports are denied to everyone.
5. **Roll back** if a node goes red on Live: paste the previous `tailscale/policy.hujson` back.
6. Node approval in the console stays manual.

### What N2 drops or changes (known gaps)

- The raw engine ports 8000 (vLLM), 1234 (LM Studio) and 8080 (llama.cpp) are no longer probed or routed. Only
  Ollama (and Docker Model Runner through the runner) is detected. This follows spec section 12.
- The 11435 inventory service is retired, so `inventory.agents` and `hardware.cpu` are no longer refreshed.
  Existing nodes keep their last values; new nodes show nothing for them. Hardware memory still arrives in `/health`.
- Posture limits: macOS `pf` is only readable as root, so pf-enabled nodes show warn ("rules not inspected"), never
  pass. Windows parsing matches English `netsh`/`netstat` output; on a localized Windows the status stays unknown.

## Phase N3 — Windows parity

1. Enrol and approve the Windows node before it advertises `tag:llm-node`.
2. Onboarding binds Ollama to `127.0.0.1:11434`, points the runner's `OLLAMA_URL` at loopback, and retires the old `AI Fleet Info` task and the tailnet-bound 11435 inventory listener.
3. If Docker Desktop is already owner-installed, run `nodes/windows/discover-dmr.ps1`. Verify DMR's actual host bind is loopback-only before opting the runner into DMR.
4. Find the actual Tailscale adapter alias with `Get-NetAdapter`, then inspect the proposed Defender migration with `nodes/windows/set-defender-runner-rule.ps1 -MasterTailnetIp <master-tailnet-ip> -TailscaleInterfaceAlias <alias> -DryRun`. After review, rerun without `-DryRun` and with `-Approve`. It removes the legacy broad `AI Fleet` rule, installs the narrow TCP 11436 runner rule, and also removes the retired `AI Fleet Controller Compatibility` rule. TCP 11436 is the only port allowed, from the master address(es) only.
5. Run `nodes/windows/test-runner-service.ps1` and record whether the existing per-user runner is only available after owner logon. Do not install a Windows service until compatibility with local subscription profiles is verified.

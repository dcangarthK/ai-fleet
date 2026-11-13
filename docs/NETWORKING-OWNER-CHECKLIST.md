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

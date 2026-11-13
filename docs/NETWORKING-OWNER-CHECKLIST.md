# Networking owner checklist

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

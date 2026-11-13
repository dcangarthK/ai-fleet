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
      is yours and is never edited.
- [ ] Approve the apply yourself (the `owner_approved` flag); keep a second path into the router open during the
      confirm window.
- [ ] After apply, check the canary result and that master, workers and the console are still reachable. If not,
      let the confirm window expire or roll back from the router's own UI.

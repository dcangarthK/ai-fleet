# Networking owner checklist

Steps only the owner can apply (SPEC-networking.md section 11). Nothing in the repo applies them.

## Phase N0: tailnet policy and 11435 audit

1. Review `netpolicy/tailnet-policy.hujson` against SPEC-networking.md section 4.
2. In the Tailscale admin console, Access controls: paste the file (it replaces `tailscale/policy.hujson`) and
   check the console's own "tests" pass. It drops master -> worker 11434/11435/8000/1234/8080 and keeps 11436.
3. Enrol new nodes while untagged (they reach the master on 8443 as owner-member devices), approve the node in
   the console, then apply `tag:llm-node` in the admin console.
4. Run `python3 scripts/audit_11435.py` on each node (read-only `lsof` listing). Exit 1 means a wildcard or LAN
   listener on 11435. Retire 11435 only after telemetry shows no clients (SPEC section 5.1).

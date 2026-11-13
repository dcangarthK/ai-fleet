# Networking: owner checklist

Steps only the owner can apply. Nothing here is run by the fleet. See `docs/SPEC-networking.md` section 11.

## Phase N2: loopback engines and firewall posture

Do these in order, one worker at a time.

1. **Update each node.** Re-run `nodes/onboard.sh` (macOS/Linux) on the worker. It now binds Ollama to
   `127.0.0.1`, points the runner's `OLLAMA_URL` at loopback, and removes the old 11435 info service.
2. **Check the node on Live.** The firewall badge should show pass and the engine healthy. Press "Check now"
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

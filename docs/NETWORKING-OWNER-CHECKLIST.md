# Networking: owner checklist

Steps only the owner can apply. Nothing here is run by the fleet. See `docs/SPEC-networking.md` section 11.

## Phase N2: loopback engines and firewall posture

**Warning: migration order.** LiteLLM reaches a worker's Ollama at `http://<node ip>:11434`. Once step 1 binds that
engine to loopback, those remote Ollama roles stop working through LiteLLM (and step 4 blocks the path in the ACL
too). The controller now skips such a node's deployments and prints a warning (event `deployment.skipped`), so
LiteLLM fails over to other deployments in the same role. Before step 1, move the roles you still need onto the
master or another node, or accept the outage for that worker. Evals are not affected: they go through the runner.

Do these in order, one worker at a time.

1. **Update each node.** Re-run `nodes/onboard.sh` (macOS/Linux) on the worker. It now binds Ollama to
   `127.0.0.1`, points the runner's `OLLAMA_URL` at loopback, and removes the old 11435 info service.
2. **Check the node on Live.** The engine should be healthy and the firewall badge pass (Windows) or warn with "pf rules not inspected" (macOS, expected). Press "Check now"
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

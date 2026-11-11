# SPEC: fleet networking

Status: draft for owner approval. `UNVERIFIED` means the repository does not establish the claim and it must be
checked on the target machine before implementation depends on it.

## 1. Goal and decisions

Make a new Mac or Windows worker easy to enrol, safe to operate, and able to serve local models without publishing a
model engine to the LAN or tailnet.

Decisions:

1. **Tailscale remains the network layer.** Keep `tag:master`, `tag:llm-node`, owner-controlled tag assignment,
   MagicDNS and enrolment. The current policy already defines those tags and is deny-by-default
   ([`tailscale/policy.hujson:1`](../tailscale/policy.hujson#L1),
   [`tailscale/policy.hujson:3`](../tailscale/policy.hujson#L3)). Replace its direct model-port grants with the
   narrower policy in §4.
2. **The runner is the only worker service reachable by the master:** HTTP/TCP 11436 over Tailscale, protected by a
   unique node token. Every runner route, including `/health`, is currently behind a constant-time token check
   ([`nodes/runner.py:2125`](../nodes/runner.py#L2125), [`nodes/runner.py:2191`](../nodes/runner.py#L2191)). TLS is
   supplied by the encrypted Tailscale transport; the application credential remains mandatory.
3. **Model engines are node-local.** Ollama and Docker Model Runner (DMR) listen on loopback only. DMR's host TCP
   API is normally port 12434 according to the owner notes, but that default and its exact API paths are
   **UNVERIFIED** until tested against the installed Docker Desktop
   ([`docs/research/owner-notes-laptop2-docker.md:13`](research/owner-notes-laptop2-docker.md#L13)). The controller
   never connects to 11434 or 12434.
4. **The runner adapts engines; it does not hold provider/API keys.** Claude and Codex continue as signed-in CLI
   subscription profiles. Profiles are node-local directories selected by ID, and a request never supplies their
   paths ([`docs/SPEC-limit-handoff.md:29`](SPEC-limit-handoff.md#L29),
   [`docs/SPEC-limit-handoff.md:46`](SPEC-limit-handoff.md#L46)).
5. **Host-firewall posture is observed, not silently changed.** The controller may specify and check expectations.
   It may not edit macOS `pf`, the macOS application firewall, or Windows Defender Firewall without an explicit,
   contemporaneous owner approval.
6. OpenWrt/OPNsense WAN rules, reservations and local DNS are optional defence-in-depth. A router replacement is not
   a prerequisite; gateway enforcement cannot cover same-host traffic anyway
   ([`docs/research/ai-fleet-networking-investigation.md:155`](research/ai-fleet-networking-investigation.md#L155)).

## 2. Verified baseline and gaps

- The master is tagged `tag:master`; workers advertise `tag:llm-node`. Setup avoids resetting unrelated Tailscale
  settings unless the owner explicitly passes its reset option ([`master/setup.sh:79`](../master/setup.sh#L79),
  [`nodes/onboard.sh:264`](../nodes/onboard.sh#L264)). Enrolment happens before tagging because tagged workers cannot
  reach the enrolment endpoint under the present policy ([`docs/LAPTOP2-JOIN.md:79`](LAPTOP2-JOIN.md#L79)).
- The current ACL lets the master reach raw Ollama, inventory and other engine ports as well as the runner
  ([`tailscale/policy.hujson:8`](../tailscale/policy.hujson#L8)). This conflicts with the selected boundary and must be
  migrated, not preserved.
- Workers currently bind Ollama and the runner to their tailnet IP
  ([`nodes/onboard.sh:331`](../nodes/onboard.sh#L331), [`nodes/onboard.sh:3087`](../nodes/onboard.sh#L3087)). The master
  runner is already loopback-only ([`master/setup.sh:197`](../master/setup.sh#L197)). Worker engines must move to
  loopback; the worker runner remains on the tailnet IP.
- The runner has token-checked `/health`, model-operation and jobs routes
  ([`nodes/runner.py:2145`](../nodes/runner.py#L2145)). Its health report currently exposes cached Ollama metrics,
  job/policy state and profiles ([`nodes/runner.py:2218`](../nodes/runner.py#L2218)); it has no DMR adapter or firewall
  posture.
- The existing engine schema anticipates several engine instances per node
  ([`docs/SPEC-control-plane.md:287`](SPEC-control-plane.md#L287)), but model install/remove is currently Ollama-specific
  ([`docs/SPEC-control-plane.md:85`](SPEC-control-plane.md#L85)). DMR is discover-and-run first; remote DMR model
  mutation is out of scope until its CLI behaviour is verified.
- Windows onboarding installs Tailscale, tags the node, configures Ollama, inventories engines, installs a runner,
  and enrols it ([`nodes/onboard.ps1:36`](../nodes/onboard.ps1#L36),
  [`nodes/onboard.ps1:2678`](../nodes/onboard.ps1#L2678)). Gaps are §7: automatic firewall mutation, tailnet-bound
  Ollama, no DMR inventory, and a runner task that starts only at user logon.

## 3. Threat model and trust zones

Protect against an untrusted LAN peer or internet host reaching fleet services; a compromised worker moving
laterally; a stolen runner token being reused against another node; a prompt-injected agent calling an unapproved
model or changing network policy; and accidental engine exposure. Also contain, rather than deny, a compromised
master: node-local runner policy remains the hard ceiling. The existing target guarantee is that a compromised
master cannot widen a node's folders, tools, network or model-management permissions
([`docs/SPEC-control-plane.md:182`](SPEC-control-plane.md#L182)).

Trust zones:

- **Master:** trusted controller and scheduler, not an unrestricted shell authority on workers.
- **Workers:** individually enrolled but mutually untrusted. Compromise of one must not grant access to another.
- **Owner devices:** authenticated tailnet members; only tailnet admins receive maintenance access.
- **Internet and ordinary LAN:** untrusted. No inbound fleet service is published to either.

Inventory data, a hostname, DHCP lease, LAN address or Tailscale reachability is not authorization. The runner token,
node approval, role assignment and node-local policy are all required at their respective layers.

## 4. Deny-by-default reachability

Everything not explicitly allowed below is denied. “Stateful reply” means only response traffic for a permitted
outbound connection.

| Source | Destination | Allowed ports/protocols | Enforcement and purpose |
|---|---|---|---|
| Master | Worker | TCP 11436, HTTP inside Tailscale | Tailscale grant `tag:master → tag:llm-node`; host firewall; token-checked runner API. |
| Worker | Master | TCP 8443, HTTPS inside Tailscale | Signed `POST /api/nodes/heartbeat` only at the app layer; ACL cannot distinguish paths. Heartbeats use an HMAC and never send the token ([`docs/SPEC-live-monitor.md:344`](SPEC-live-monitor.md#L344)). |
| Owner tailnet member | Master | TCP 8443, HTTPS | Token-protected console and pre-tag enrolment via Tailscale Serve; the master maps it to loopback, not port 8700 ([`master/setup.sh:116`](../master/setup.sh#L116)). |
| Tailnet admin | Master, worker | TCP 22, SSH | Owner maintenance only. If SSH is disabled on a host, no listener is required. |
| Worker | Worker | None | No lateral runner, engine, inventory or SSH access. |
| Owner device | Worker | None by default | Admin SSH is the sole exception above. No model or runner access. |
| LAN peer / internet | Master, worker | None inbound | No port forwarding; engines bind loopback; runner binds only the tailnet address. |
| Master or worker | Internet | DNS UDP/TCP 53 to its configured resolver; NTP UDP 123; HTTPS TCP 443; Tailscale transport UDP 41641 **UNVERIFIED** plus stateful replies | Required for name/time sync, Tailscale/DERP, subscription CLIs, installs and model pulls. Exact Tailscale egress requirements must be verified before an optional WAN allowlist is enabled. |
| Worker runner | Local engines | TCP 11434 Ollama; TCP 12434 DMR, loopback only | Local probes, catalog and inference through engine adapters. Never granted in Tailscale. |

Required policy changes:

- Reduce master → worker to TCP 11436.
- Add worker → master TCP 8443 so configured heartbeat pushes can work. The current test explicitly denies this
  path ([`tailscale/policy.hujson:18`](../tailscale/policy.hujson#L18)).
- Retain member → master 8443 and admin → fleet SSH.
- Add ACL tests that deny 11434, 11435, 12434, 1234, 8000 and 8080 from master, workers and ordinary members; deny
  worker → worker 11436; and accept only the rows above.

Tailscale policy is necessary but not sufficient: it limits network reach, while the runner token limits callers and
the runner's path/model policy limits actions. A shared tailnet or source IP alone never authorizes work.

## 5. Node service and firewall contract

### 5.1 Bindings

| Service | Bind | Exposure |
|---|---|---|
| Runner | the node's Tailscale IPv4 address, TCP 11436 | Master only through ACL + host firewall + token. |
| Ollama | `127.0.0.1:11434` | Runner only. |
| Docker Model Runner | `127.0.0.1:12434` | Runner only; actual Docker Desktop bind control is **UNVERIFIED**. If Docker Desktop cannot restrict host TCP to loopback, do not enable host TCP until a host-firewall deny is owner-approved and verified. |
| Legacy inventory server | none after migration | Fold inventory into `/health`; retire TCP 11435 after compatibility telemetry shows no clients. |

IPv6 must be checked explicitly; a loopback-only IPv4 bind must not leave an IPv6 wildcard listener. The research
identifies IPv6 bypass as a required threat test
([`docs/research/ai-fleet-networking-investigation.md:464`](research/ai-fleet-networking-investigation.md#L464)).

### 5.2 Posture object and read-only verification

The runner adds this redacted, factual object to cached `/health` and heartbeats:

```json
{
  "firewall": {
    "checked_at": 0,
    "age_s": 0,
    "status": "pass|warn|fail|unknown",
    "backend": "macos-pf|macos-application-firewall|windows-defender|unknown",
    "runner_tailnet_only": true,
    "engine_loopback_only": true,
    "ipv6_wildcard": false,
    "details": []
  }
}
```

Unknown or permission-denied observations stay `unknown`; they are never inferred. The collector runs off the request
path with a short timeout, like existing cached metrics, whose design prevents a slow command from delaying
`/health` ([`nodes/runner.py:326`](../nodes/runner.py#L326)). Live shows a per-node **Firewall: pass / warning /
fail / unknown** badge, observation age, and non-secret details.

Expected posture:

- **macOS:** inspect listening sockets and the enabled/status state of `pf` and the application firewall. Pass when
  engines are loopback-only, the runner is tailnet-only, and the effective firewall does not broadly admit its
  process/port. Whether the application firewall alone can express the required source restriction is
  **UNVERIFIED**; use a reviewed `pf` rule only with owner approval.
- **Windows:** inspect listening sockets plus enabled Defender Firewall profiles and matching inbound rules. Pass
  when TCP 11436 is allowed only from the master's current tailnet address(es), and engine ports have no non-loopback
  listener or inbound allow. Source addresses are observations, not durable identity; Tailscale ACL remains primary.
- A controller “check now” requests a read-only refresh. Any proposed remediation is rendered as a diff/command for
  the owner; no endpoint applies it.

## 6. Docker Model Runner beside Ollama

### 6.1 Engine abstraction and role selection

Model identity is `(engine, reference, digest/version when available)`. Add `docker-model-runner` to
`engine_instances`; do not overload an Ollama model name. A role assignment selects a catalog model, and that catalog
entry selects its engine. The controller sends the resolved `engine_instance_id` and model reference in the
token-checked job request. The runner rejects an instance/reference not present in its latest local catalog or not
allowed by node-local policy.

Keep existing `agent: "ollama"` jobs compatible. New local-model jobs carry an explicit engine; an omitted engine on
the legacy route means Ollama. Engine selection is deterministic and audited; there is no automatic “try any local
API” fallback. Failover re-runs normal placement against another assignment, not another undeclared engine.

### 6.2 DMR adapter

- Configuration: local base URL (default candidate `http://127.0.0.1:12434`) and an explicit enabled flag in
  node-local `runner.json`. The controller may narrow/disable it, never change the base URL remotely.
- Catalog: run `docker model ls` locally with a timeout and parse its machine-readable output if the installed CLI
  provides one. The supported output flag, schema, digest and size fields are **UNVERIFIED**. Until verified, a
  fixture-backed text parser may report only exact model references and must preserve unknown fields as null.
- Health: cached CLI exit status, local TCP-connect result and listener binding. No prompt, completion, paid call or
  model load is permitted for health. A catalog failure does not make the runner itself unhealthy.
- Inference: the adapter uses Python's standard-library HTTP client to the installed DMR OpenAI-compatible local API;
  its exact base path and response fields are **UNVERIFIED**. No OpenAI SDK and no API key are introduced.
- Warm models: report `warm: true|false|unknown`, source and observation age per engine/model. Use a verified,
  read-only DMR runtime status if one exists; otherwise report `unknown` rather than probing with inference or
  claiming that an installed model is warm. Runner-observed active DMR jobs may report that exact model warm while
  active. §12 routing may prefer only fresh `true`; `unknown` gets no warm preference. Existing Ollama warmth comes
  from cached `/api/ps` and is already age-limited
  ([`docs/SPEC-live-monitor.md:424`](SPEC-live-monitor.md#L424), [`nodes/runner.py:307`](../nodes/runner.py#L307)).
- Operations: phase 1 is catalog + inference only. DMR pulls/removals and Docker Desktop upgrades remain owner-only
  until commands, progress, cancellation and ownership semantics are verified.

The owner-noted Laptop 2 models are candidates, not assignments; benchmark results choose roles
([`docs/research/owner-notes-laptop2-docker.md:17`](research/owner-notes-laptop2-docker.md#L17)).

## 7. Windows node

Use **Ollama by default** for the first Windows node because onboarding already installs/configures it and inventories
its API ([`nodes/onboard.ps1:61`](../nodes/onboard.ps1#L61), [`nodes/onboard.ps1:95`](../nodes/onboard.ps1#L95)). DMR
is opt-in when the owner has Docker Desktop and enables its local host API; it uses the same loopback-only adapter as
Laptop 2. Do not install Docker Desktop automatically.

Required Windows changes:

1. Enrol before applying the restrictive tag, matching the Mac flow. Today PowerShell tags before completing
   enrolment ([`nodes/onboard.ps1:2684`](../nodes/onboard.ps1#L2684)); this can make `-Enroll` unable to reach 8443
   under deny-by-default policy.
2. Bind Ollama to loopback, not the tailnet address. Bind only the runner to tailnet TCP 11436.
3. Replace the current unconditional remove/create of an `AI Fleet` firewall rule
   ([`nodes/onboard.ps1:208`](../nodes/onboard.ps1#L208)) with: read-only audit by default; show the exact proposed
   rule; apply only after an interactive owner confirmation (or a separate explicit approval flag); verify and
   report the effective rule. The rule allows TCP 11436 from the master tailnet address(es), not all of
   `100.64.0.0/10`, and never includes model ports.
4. Add DMR catalog discovery through `docker model ls`; the existing scan only identifies the Docker executable and
   selected containers ([`nodes/onboard.ps1:111`](../nodes/onboard.ps1#L111),
   [`nodes/onboard.ps1:125`](../nodes/onboard.ps1#L125)).
5. Install the runner as a restartable service available before user logon, running under a dedicated least-
   privilege account **UNVERIFIED** for compatibility with subscription CLI profiles. Until that is proven, retain
   the current per-user Scheduled Task, label it “available after owner logon,” and surface that limitation in Live.
   The present task is triggered at logon and runs as the owner
   ([`nodes/onboard.ps1:2653`](../nodes/onboard.ps1#L2653)).

## 8. Sleep, failure and recovery: gaps only

Do not create a second liveness or failover system. The runner already pushes signed cached health about every ten
seconds, polling remains a fallback, and one helper defines offline state
([`docs/SPEC-live-monitor.md:344`](SPEC-live-monitor.md#L344),
[`docs/SPEC-live-monitor.md:396`](SPEC-live-monitor.md#L396)). Queued unpinned work is re-placed; pinned or started
work waits rather than creating two writers ([`docs/SPEC-live-monitor.md:447`](SPEC-live-monitor.md#L447)).

Only these networking/engine gaps remain:

- permit worker → master TCP 8443 in the Tailscale policy;
- make engine and firewall observations cached heartbeat fields;
- generalize warm reporting from an Ollama string list to per-engine tri-state observations without weakening the
  existing freshness cutoff;
- show `sleeping/offline`, `heartbeat lost, polling`, engine unavailable and firewall failure as distinct facts;
- on wake or network change, re-resolve the node's tailnet address, re-check binds/firewall posture, refresh engine
  catalogs, and let the existing heartbeat/poll transition restore eligibility;
- DMR request timeout/cancellation must map into the existing job state machine; no engine-specific retry may start a
  duplicate job.

## 9. Optional router hardening

These are optional because router capability and ownership are **UNVERIFIED**:

- a static DHCP lease and local DNS name for human convenience only; neither is fleet identity or authorization;
- an OpenWrt/OPNsense rule denying worker WAN egress except required DNS, time, Tailscale, subscription, update and
  model-registry destinations, after those destinations are measured;
- a dedicated VLAN/SSID for workers and owner-approved logging of denied lateral flows;
- an OpenWrt/OPNsense adapter later, with staged apply/confirm/rollback. The research recommends integration first
  and a router image only after the controller is proven
  ([`docs/research/ai-fleet-networking-investigation.md:511`](research/ai-fleet-networking-investigation.md#L511)).

Do not block all WAN access on Laptop 2 now: Tailscale control/relay, subscription CLIs, Docker/Ollama model pulls and
updates need egress, and the exact allowlist is not established. Router policy never replaces the overlay, runner
token or host checks.

## 10. Delivery phases

All automated tests use fake sockets, subprocess output, clocks, Tailscale status and firewall command output. They
must not change a real network, firewall, Tailscale policy, Docker Desktop setting or model installation.

### Phase N0 — contract and tailnet-policy patch

Deliver: the matrix in §4 encoded in `policy.hujson`; inventory/client compatibility audit for TCP 11435; red tests
for loopback engines and ACL grants.

Acceptance:

- policy tests accept master → runner, worker → master 8443, member → master 8443 and admin SSH;
- they deny every raw engine/inventory port and all worker-to-worker service traffic;
- fake enrolment proves a node can complete enrolment before receiving `tag:llm-node`;
- no listener or firewall is changed by tests.

### Phase N1 — DMR on Laptop 2 behind the runner

Deliver: engine adapter interface, DMR config, cached health, `docker model ls` catalog, token-checked inference and
explicit engine selection in assignments/jobs.

Acceptance:

- fixture `docker model ls` output produces exact references and null for unknown fields; malformed/timeout output is
  contained;
- fake TCP/listener checks distinguish loopback-only, wildcard and down without an inference call;
- fake OpenAI-compatible responses exercise success, stream/error/cancel and model mismatch; no SDK/key exists;
- a master request directly to fake port 12434 is denied by policy, while the same model succeeds through fake
  runner 11436 with the right node token;
- two same-named models on different engines remain distinct, and a role invokes only its assigned engine;
- DMR warmth is `unknown` unless directly observed, and stale warmth never affects ordering.

### Phase N2 — binding migration and firewall posture

Deliver: loopback engine configuration, retirement plan for 11435, read-only macOS/Windows posture collectors,
`/health` schema and Live badge.

Acceptance:

- fake socket tables cover IPv4/IPv6 loopback, tailnet bind and wildcard exposure;
- fake `pf`, application-firewall and Defender outputs yield pass/warn/fail/unknown with age;
- permission errors remain unknown and never crash/delay `/health`;
- controller “check now” performs reads only; tests assert no mutating command was invoked;
- Live distinguishes engine failure from runner/network/firewall failure.

### Phase N3 — Windows parity

Deliver: enrol-before-tag, loopback Ollama, optional DMR discovery, owner-approved narrow Defender rule, and runner
service feasibility result.

Acceptance:

- PowerShell tests prove no firewall command runs without the explicit approval interaction/flag;
- the proposed rule contains only TCP 11436 and resolved master tailnet addresses, never a raw model port or the
  whole tailnet CIDR;
- fake logon/boot/restart cases accurately report runner availability;
- both Ollama-default and owner-enabled DMR nodes publish engine-specific catalog/health through the runner.

### Phase N4 — optional gateway adapter

Only after the owner confirms router hardware. Deliver read-only capability discovery first, then an owner-approved,
namespaced WAN/VLAN policy with dry-run and rollback.

Acceptance: unrelated router rules survive; bad canary policy rolls back; controller loss leaves last-known-good
forwarding; absent/unsupported router features do not block fleet enrolment.

## 11. Owner-only steps

The owner must:

- install and sign in to Tailscale, paste/review the tailnet policy, enable MagicDNS/HTTPS certificates, approve each
  device/tag and approve nodes in the console (the existing checklist already keeps node approval manual at
  [`docs/LAPTOP2-JOIN.md:149`](LAPTOP2-JOIN.md#L149));
- install Docker Desktop on Laptop 2, enable DMR, choose whether host TCP is enabled, verify its actual loopback bind
  and approve any required host-firewall rule;
- choose/install engines and models, sign CLI subscription profiles in locally, and approve remote model management;
- review and approve any macOS `pf`/application-firewall or Windows Defender Firewall change;
- opt into sleep prevention on mains power if desired; sleeping is otherwise a normal offline event;
- supply router access and approve gateway changes if optional N4 is wanted.

No controller action may impersonate these approvals.

## 12. Explicit rejections

- **API SDK clients or provider keys:** rejected. They conflict with subscription CLI profiles and add secrets. DMR
  integration is local HTTP through the runner; Claude/Codex remain CLIs. The owner notes record this prior decision
  ([`docs/research/owner-notes-laptop2-docker.md:31`](research/owner-notes-laptop2-docker.md#L31)).
- **Raw LAN or tailnet exposure of 11434/12434/other model ports:** rejected. It bypasses runner token, node policy,
  role/engine selection and audit. The outside suggestion to call 12434 from Laptop 1 conflicts with the established
  boundary ([`docs/research/owner-notes-laptop2-docker.md:26`](research/owner-notes-laptop2-docker.md#L26)).
- **Docker-mounted profile/auth directories:** rejected. Profiles stay host-local and are selected by opaque ID;
  containers would broaden credential exposure and conflict with CLI execution as the node user. The runner reports
  readiness but no directories or account identity ([`docs/SPEC-limit-handoff.md:38`](SPEC-limit-handoff.md#L38)).
- **A new retry/failover loop:** rejected. It would race the existing leases and duplicate work; §12 already defines
  placement, offline state and recovery.
- **Automatic firewall edits during onboarding or controller reconciliation:** rejected. A bad rule can lock out the
  management path; research requires staged, probed, rollback-capable changes
  ([`docs/research/ai-fleet-networking-investigation.md:438`](research/ai-fleet-networking-investigation.md#L438)).
- **Mandatory OpenWrt/OPNsense, static leases or local DNS:** rejected as requirements. They are useful optional
  hardening, not prerequisites for a roaming cross-platform fleet.
- **Internet-wide WAN block on Laptop 2:** rejected until required endpoints are measured and an owner-approved
  gateway exists. It would break required control, install or subscription traffic.
- **LLM/completion health probes:** rejected. Health is local process/socket/catalog evidence; paid-model probes are
  already prohibited ([`docs/SPEC-live-monitor.md:432`](SPEC-live-monitor.md#L432)).

## 13. Out of scope

Public model serving; inbound port forwarding; a new VPN or packet filter; replacing Tailscale; automatic router
replacement; TLS interception; IDS/IPS; multi-controller leader election; migration of a running session; DMR model
pull/remove until its CLI is verified; strong multi-tenant isolation; and automatic firewall remediation.

## 14. Tonight's scope (2026-11-12), approved after Sonnet's review

**Status:** Codex's design (§1–13) is approved as direction. Sonnet's review said "approve with changes", and
tonight builds only the following.

1. **The OpenAI-compatible engine adapter in the runner** (trimmed N1).
   - **Engines:** `ollama` (existing, the default when omitted) and `dmr` (Docker Model Runner).
   - **Engine config** in `runner.json`, under `engines.dmr`:
     - `base_url`, which must be loopback (`127.0.0.1`, `localhost` or `::1`; anything else is refused);
     - the DMR default `http://127.0.0.1:12434/engines/v1`.
   - **Catalog:** `GET {base_url}/models`, plus an optional `docker model ls` parse when the docker CLI has the
     `model` subcommand.
   - **Inference:** `POST {base_url}/chat/completions`, reached only through the runner's token-checked API.
   - **Health:** reachable or unreachable, with no LLM call.
   - **Warm state:** best-effort.
   - **Model names** are namespaced per engine, so `ai/qwen3:latest` (dmr) is kept apart from `qwen3` (ollama).
   - **The controller:**
     - assignments, the catalog and Live carry `engine`;
     - a job's `engine` is passed to the runner;
     - unknown engines are refused.
2. **A real integration test,** opt-in and skipped when the engine is absent.
   - The same adapter points at Ollama's OpenAI-compatible endpoint (`http://127.0.0.1:11434/v1`) on the master,
     and runs a real prompt end to end through the runner and controller with a small local model.
   - It is run tonight as proof-of-concept evidence, and the result is recorded in the overnight report.
3. **A Laptop 2 smoke check for the owner,** `nodes/check_dmr.sh`.
   - It captures real DMR facts as fixtures: `docker model ls`, `curl 127.0.0.1:12434/engines/v1/models`, and
     `lsof -iTCP:12434` showing the bind address.
   - It flags any bind that isn't loopback as a FAIL.
   - It is documented in `docs/LAPTOP2-JOIN.md`.
4. **Windows enrol-before-tag** in `nodes/onboard.ps1`, matching `onboard.sh`. The firewall rules aren't touched
   tonight. Test it with fakes or by parsing the script.
5. **Minimal N0:** policy tests only.
   - Assert that worker → worker is denied and worker → master 8443 is allowed.
   - Don't narrow the 11434/11435 grants yet, because the controller probes and evals still use them
     (`controller.py:63,70,208`, `evaluations.py:241`).

**Deferred:**
- the binding migration and ACL narrowing (after probes and evals move to the runner);
- the firewall posture collectors and Live badge (N2);
- the Windows service and Defender automation;
- N4 and the router material.

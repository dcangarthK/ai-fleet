# SPEC: route LiteLLM to loopback-only workers through the runner relay

Revision 3. It folds in the Fable reviews t-47dbe134 (H1–H3, M1–M6, L1–L5) and t-3f892237 (N1–N7).

## Problem
A worker whose Ollama listens only on loopback (SPEC-networking 4) is skipped by `desired_deployments`
(`master/controller/controller.py:284`), so its role assignments are never routed (`route_reason` "engine loopback-only").
The runner port (11436) is already reachable from the master over the tailnet and is token-checked.

## Design

### 1. Relay credential (H1, M3)
- The relay token is `relay_token = hex(HMAC-SHA256(node_runner_token, "relay-chat-v1"))`.
  - The controller computes it from the per-node column `nodes.runner_token` only.
  - The global `RUNNER_TOKEN` fallback (`tasks.py:117`) is never used or derived from. A node without its own token
    is not relay-routed; its reason is "no node token".
- The runner derives the same value from its own token.
  - It accepts the relay token **only** on `POST /v1/chat/completions`, sent as `Authorization: Bearer <relay_token>`.
  - Every other route rejects the relay token with 401.
  - The full runner token is **not** accepted on the relay route (N5).
  - The relay token is compared with `hmac.compare_digest`.
- LiteLLM gets the relay token as `api_key`. There is no `extra_headers`, and the full runner token never reaches LiteLLM.
- `model_info.relay_fp = sha256(relay_token)[:12]` is non-secret. `reconcile` compares it, and when the token is rotated
  it deletes the deployment and creates a new one (H3).

### 2. Runner route `POST /v1/chat/completions` (`nodes/runner.py`)
- **Advertising:** top-level `relay_chat: true` in `health_report` (`runner.py:4076`) (M6).
  - Add `relay_chat` to `HEALTH_FIELDS` (`master/controller/tasks.py:161`) so `clean_health` keeps it (N1).
  - In `merge_health` (`tasks.py:247`), a body without `relay_chat` sets it to false, so a downgraded runner loses the
    flag on the next heartbeat (N6).
- **Body:**
  - The cap for this route is 8 MiB. Above it returns 413 (M5).
  - `model` must match `EVAL_MODEL` and be in the installed-model list (N4).
    - The list comes from Ollama `/api/tags`, cached for 30 s.
    - A miss triggers one immediate refresh before the request is rejected, so a freshly ensured `fleet-*` model
      passes.
    - Derived `fleet-<catalog_id>` names pass.
  - `messages` must be a list.
- **Keys** (M2): unknown keys are **stripped**, not rejected, and the stripped key names (never their values) are
  written to the audit line. Forwarded keys: model, messages, temperature, top_p, max_tokens, max_completion_tokens,
  stop, seed, stream, stream_options, response_format, tools, tool_choice, parallel_tool_calls, frequency_penalty,
  presence_penalty, n, user.
- **Forwarding:** to `loopback_base_url(ollama_url) + "/v1/chat/completions"`, loopback only.
- **Concurrency:** at most 2 relay requests in flight per runner (M1). Above that the runner returns 429 with
  `Retry-After: 2` and an OpenAI-style body: `{"error": {"message": "relay busy", "type": "rate_limit_error",
  "code": "rate_limit_exceeded"}}` (N7).
- **Audit** (M4): `manager.audit("relay_chat", {model, remote_ip, stream, stripped_keys})` on every request.
  Prompts and outputs are never logged.
- **`stream: false`:** JSON response. An upstream HTTP error gives 502 "engine returned HTTP <code>".
- **`stream: true`** (M1):
  - Handled outside `_send` with `Content-Type: text/event-stream`, a flush per upstream chunk and no full-body
    buffering.
  - A client disconnect closes the upstream connection so Ollama stops.
  - An upstream error before headers gives 502. A mid-stream error closes the connection with nothing appended.
- **Timeout** (L3): 600 s per socket read, not a total.
- **Embedding:** re-embed with `python3 nodes/build.py` (onboard.sh / onboard.ps1).

### 3. Controller
- In `desired_deployments`, a node is relay-routed when all of these hold:
  - it is not the master;
  - `n["engine"] == "ollama"`;
  - `engine_not_reachable_remotely(n)` is true;
  - `nodes.runner_token` is set;
  - `json(nodes.runner).relay_chat is True`.
- A relay-routed node gets `{"model": "openai/<routed_model>", "api_base": "http://<ip>:<runner_port>/v1",
  "api_key": <relay_token>, "timeout": 600}` and `model_info: {id, relay_fp}`.
- **Assignment params on relay routes** (H2, N2):
  - Relay deployments only ever carry `temperature`, `top_p` and `seed` from the assignment params. Every other
    param, `num_ctx` included, is left out of `litellm_params`.
  - `num_ctx` takes effect only through the derived `fleet-<catalog_id>` model.
  - The controller leaves out the other params as well: `think`, `keep_alive`, and `num_ctx` when there is no
    `catalog_id`.
  - Any of those omissions sets an advisory note (below), and the assignment stays relay-routed.
- **Advisory notes** (N3):
  - `routed` keeps its meaning, "in the desired deployments with no ineligibility". It is computed from
    `_route_ineligible`, which returns None for relay-eligible assignments.
  - The ignored-params note is written to `route_reason` only when `routed=1`, as "relay: <param names> ignored".
  - `reconcile` (`controller.py:453`) sets `routed = int(mid in desired and ineligible is None)` and then writes the
    advisory note. `route_reason` is therefore "why not routed" when `routed=0`, and "advisory" when `routed=1`.
  - Check the console's roles view (`/api/fleet` consumers) for anything that treats a non-null `route_reason` as
    unrouted, and fix it to read `routed`.
- DMR stays unrouted, as `controller.py:278` already does.
- The `_loopback_warned` warning and the `deployment.skipped` event fire only when the relay route isn't possible
  (L2). Update the comment at `controller.py:286`.
- **`_route_ineligible`:**
  - "engine loopback-only" applies only to an old runner (no `relay_chat`); "no node token" applies when the token is
    missing.
  - A relay-eligible assignment returns None.
- **Secrets:** the token is never logged and never returned by a controller API.

### 4. SPEC-networking amendment (M4)
- Add to §4/§14: "LiteLLM inference to loopback-only workers goes through the runner relay on 11436
  (`/v1/chat/completions`, relay-scoped token, streaming allowed). Raw 11434/12434 stay closed."
- Update the "no streaming" docstring of `ollama_relay` so it says that applies to the eval relay only.

## Acceptance
- A1 (owner-verified on Laptop 2): coder and fast become `routed=1` after the runner update and refresh, with no ACL
  or bind change.
- A2 (owner-verified): a LiteLLM `coder` request, streamed and non-streamed, is answered by Laptop 2's
  qwen2.5-coder:14b through 11436.
- A3: raw 11434 stays closed, and the ACL tests are unchanged.

## Tests (write first)
- **Runner auth:**
  - no token → 401;
  - relay token on `/v1/chat/completions` → OK;
  - relay token on `/jobs` and `/services/restart` → 401;
  - full runner token on the relay route → 401.
- **Runner validation:**
  - bad model → 400;
  - unknown key stripped and named in the audit, value absent from the audit;
  - body over 8 MiB → 413;
  - non-loopback ollama_url → 502;
  - third concurrent request → 429 with the OpenAI error body and Retry-After;
  - unknown model → one tags refresh, then 400;
  - model added after the cache was filled → accepted.
- **Runner streaming:**
  - stream false returns JSON (fake urlopen);
  - stream true relays chunks with an SSE content type;
  - client disconnect closes upstream;
  - upstream 500 before headers → 502;
  - mid-stream error closes the connection;
  - the audit line never contains message content.
- **Health:**
  - `health_report` has `relay_chat: true`.
  - `relay_chat` survives `clean_health` on both the poll and the heartbeat path.
  - A heartbeat without `relay_chat` clears a stored true.
- **Controller:**
  - relay-eligible node → openai/ deployment with runner api_base, relay token as api_key, relay_fp, no extra_headers;
  - no `relay_chat` → skipped with "engine loopback-only";
  - global-token-only node → skipped with "no node token";
  - reachable node → unchanged ollama_chat route;
  - master with a loopback engine → unchanged;
  - DMR assignment → unchanged;
  - assignment with think and num_ctx (catalog_id set) → deployment has neither key, routed=1, reason
    "relay: think ignored";
  - num_ctx without catalog_id → left out, routed=1, reason names num_ctx;
  - token rotation → reconcile deletes and re-creates;
  - `_route_ineligible` returns None for a relay node;
  - neither token appears in `/api/fleet` or in controller logs.

## Build
- Kind `build`, Sonnet, tests first (L5), on the fleet-gate.
- Review: one Codex `security_review` (gpt-5.6-terra, effort high).
- Merge, then the owner pushes, then Laptop 2's runner updates.

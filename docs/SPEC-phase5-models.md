# Phase 5b: model catalog, installs, roles that wait

Part of [SPEC-control-plane.md](SPEC-control-plane.md) §3 and §9 (Phase 5). The console shell (5a) is already
built; this adds the backend and the Models/Roles pages on top of it. Builds on Phase 4's runner boundary.

Tests: `tests/test_runner_models.py` (runner, fake Ollama + fake registry) and `tests/test_models_api.py`
(controller, fake runner).

## Runner (contract additions; still standard library only)

### Node-local config (`runner.json`)

```json
"models": {"remote_management": false, "disk_reserve_gb": 8, "ram_reserve_gb": 4, "max_loaded": 1}
```

`remote_management` defaults to **false**: a node only installs or removes models when its owner said so
(onboarding asks). Invalid values → safe defaults. `/health` reports this block.

### Endpoints

| Route | Does |
|---|---|
| `GET /models` | `{engine: "ollama", version, free_disk_gb, ram_gb, models: [{name, size_gb, digest, managed}], loaded: [{name, size_gb}]}` from Ollama `/api/version`, `/api/tags`, `/api/ps` + `shutil.disk_usage(home)`. `managed` = pulled by this runner (recorded in `$FLEET_DIR/models.json`) |
| `POST /models/ensure` | Body `{ref, params?, derived?, override_fit?}` → an operation (below). 403 if `remote_management` is false |
| `POST /models/remove` | Body `{ref, derived?, confirm_preexisting?}` → an operation. A model the runner didn't pull is refused (409) unless `confirm_preexisting: true` |
| `GET /operations/<id>` | Operation state |
| `GET /operations` | All operations, newest first |
| `POST /operations/<id>/cancel` | Stop a pull (partial blobs are Ollama's to clean; the op ends `cancelled`) |

`ref` must match `^[a-z0-9][a-z0-9._-]{0,63}(/[a-z0-9][a-z0-9._-]{0,63})?(:[A-Za-z0-9][A-Za-z0-9._-]{0,63})?$`.
`params` keys allowed: `num_ctx` (int 512–131072), `temperature` (0–2), `top_p` (0–1), `seed` (int),
`think` (bool), `keep_alive` (string like `10m`). Others → 400. `derived` (optional) must match
`^fleet-[a-z0-9][a-z0-9.-]{0,60}$`.

### Operation

```json
{"id": "op-…", "kind": "ensure|remove", "ref": "qwen3:8b", "derived": "fleet-qwen3-8b",
 "state": "checking|pulling|configuring|verifying|ready|removed|failed|blocked|cancelled",
 "reason": null, "progress": {"completed": 0, "total": 0, "status": "pulling manifest"},
 "download_gb": 5.2, "loaded_gb": null, "started_at": "…", "ended_at": null}
```

Saved to `$FLEET_DIR/operations/<id>.json` (0600). On runner start, ops left in `checking`/`pulling`/
`configuring`/`verifying` are resumed (Ollama pulls resume from what's on disk). **One** non-final op at a time
per node: a second `ensure`/`remove` while one runs → 409 with the running op id. An `ensure` for a ref that's
already present (and its `derived` model too) returns a `ready` op without pulling (idempotent).

### Ensure flow

1. **checking:** read the manifest from the registry (`$OLLAMA_REGISTRY`, default
   `https://registry.ollama.ai/v2/library/<repo>/manifests/<tag>`) for `download_gb`. Unknown size → `blocked`,
   reason `size unknown`. Free disk < `download_gb + disk_reserve_gb` → `blocked`, reason starts `no_space`. RAM:
   `download_gb * 1.2 + kv_gb(num_ctx)` where `kv_gb = num_ctx / 8192 * 1.0` (conservative 1 GB per 8k), must be
   ≤ `ram_gb - ram_reserve_gb`, else `blocked` reason starts `wont_fit` unless `override_fit`.
2. **pulling:** `POST /api/pull {model: ref, stream: true}`; each JSON line updates `progress`. A line with
   `error` → `failed` with that error; if it contains `newer version` the state is `blocked` and reason starts
   `engine_upgrade:` followed by the error text. Record `ref` in `models.json` as managed (only if it wasn't
   already present before this op).
3. **configuring** (only if `derived`): `POST /api/create {model: derived, from: ref, parameters: {num_ctx,
   temperature, top_p, seed}}` (only the given ones). Derived names are always managed.
4. **verifying:** `POST /api/chat {model: derived or ref, messages: [{role: user, content: "Reply with the word
   ready."}], stream: false, think: <param if given>, options: {num_predict: 16}}`; non-empty reply required.
   Then `/api/ps` for `loaded_gb`, then unload with `POST /api/generate {model, keep_alive: 0}`.
5. **ready.**

### Remove flow
`POST /api/delete {model}` for the derived model (if any) and the ref; verify both absent from `/api/tags`;
`removed`. Remove the entries from `models.json`. A failed delete → `failed`, never `removed`.

## Controller

### Tables
- `model_catalog(id TEXT PK, reference, engine DEFAULT 'ollama', display_name, download_gb, digest,
  min_engine, defaults JSON, suggested_roles JSON, evals JSON, source, created_at)`.
- `node_models(node, catalog_id, desired 'present'|'absent', state, reason, op_id, progress JSON,
  ownership 'fleet-managed'|'pre-existing', params JSON, override_fit INT, updated_at, PK(node, catalog_id))`.
- `assignments` gains `state TEXT DEFAULT 'active'` (`pending_install|active|blocked|failed`), `catalog_id`,
  `params JSON`.

### Catalog
- `GET /api/models` [view] → catalog entries, each with `nodes: {<node_id>: {installed, state, reason,
  progress, fit}}` where `fit` ∈ `fits|tight|wont_fit|unknown` from node RAM (inventory) with the same formula
  (`tight` = within 2 GB of the limit).
- `POST /api/models` [admin] `{reference, display_name?, defaults?, suggested_roles?}`: validates the ref
  pattern, fetches the registry manifest (`FLEET_OLLAMA_REGISTRY`, default the public registry) for size and
  digest; 404 → 400 `"<ref> isn't in the registry"`. `id` = ref with `:`/`/` → `-`. Evals copied from
  `evals/results/results.json` when present.
- `DELETE /api/models/{id}` [admin]: refused while any node has it desired present.
- Node models: for runners whose `/health` has a `models` block, `refresh_nodes()` also reads `GET /models` and the
  node's model list is the union of that and the probe. A model present there that the fleet didn't install →
  ownership `pre-existing`.
- On first start the catalog is seeded from `evals/results/results.json` (models that were evaluated), without
  network.

### Installs
- `PUT /api/nodes/{node}/models/{id}` [operate] `{desired: "present"|"absent", params?, override_fit?,
  confirm_preexisting?}`. Upserts `node_models`; the reconcile loop does the rest. Returns the row.
- Reconcile (in the existing poll loop, ≤ every `FLEET_POLL_SECONDS`): for each row whose desired state
  isn't met and has no running op → call the runner (`/models/ensure` with `derived = "fleet-" + id` when params
  are set, or `/models/remove`); for rows with an op → poll `/operations/<id>` and copy state/progress/reason.
  When an ensure op is `ready`, trigger `refresh_nodes()` so the node's model list includes it.
- Model already on the node at first sight → ownership `pre-existing`.

### Roles that wait
- `POST /api/roles` gains `ensure_model: bool` (default false for API compatibility; the UI sends true) and
  `catalog_id?`. If the model isn't on the node and `ensure_model` → create/upsert the install (desired present)
  and the assignment with `state = pending_install`. Without `ensure_model` the old 400 stays.
- `desired_deployments()` only routes `active` assignments. A pending assignment becomes `active` when its install
  is `ready` **and** the model is in the node's list; `blocked`/`failed` mirrors the install.
- Assignment params (`think`, `temperature`, …) go into the LiteLLM deployment's `litellm_params` (UNVERIFIED key
  names for `think`; `temperature` is standard).

### Events
`model.install` / `model.remove` / `model.state` events with `{node, model, state, reason}` so the console updates
live.

## Console

- **Models page:** catalog table (model, size, eval scores per role, per-node cell showing Installed / progress
  bar / Blocked with reason / fit badge / Install button), "Add model" dialog (reference, display name, defaults:
  context, temperature, thinking), install dialog (node, params, override fit with an explicit warning), remove
  with dependency check (roles using it).
- **Operation drawer:** a small persistent strip listing running installs with progress, visible on every page.
- **Roles page:** "Assign" lists catalog models; ones not on the node show "will install (x GB)"; pending
  assignments show their blocker inline.

## Not in this phase
Engine upgrades, non-Ollama engines, canary evals before routing (Phase 7), fallbacks (Phase 8).

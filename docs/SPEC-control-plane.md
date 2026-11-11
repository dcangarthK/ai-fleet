# SPEC: Fleet control plane (phases 4–8)

Status: **draft for approval**. Claude and Codex each drafted a scope independently from the same brief; this is
the merged result. Where we disagreed, the "Review notes" at the end say who proposed what and why it landed
where it did. `UNVERIFIED` marks a model name, API field or CLI flag that must be tested before building on it.

## Goal

Turn ai-fleet from "a dashboard over what nodes already have" into a control plane: you say *what you want* in
the UI and the fleet makes it so, safely.

- **Models.** Add a model to a catalog, choose roles and nodes; nodes download, configure and smoke-test it in the
  background, and routing switches on only when it's ready.
- **Instructions and policy.** Fleet-wide, per-node, per-role, per-agent and per-task instructions and limits
  (folders, tools, network, budgets), **enforced on the node**, not just written down.
- **Skills.** Versioned, reviewable instruction bundles that reach Claude, Codex and local models.
- **A proper app UI** for all of it, also usable from a phone over Tailscale.

Non-goals: multi-tenant isolation, cloud nodes, training or fine-tuning, a model or skill marketplace.

## 1. Where we are (verified in the code)

| Area | Today | Gap |
|---|---|---|
| Models | `probe()` and `fetch_inventory()` read what a node already serves | No install/remove/update, no fit check, no per-model params. `probe()` returns only the **first** engine on a node |
| Roles | `assign()` refuses a model the node doesn't already have | Can't assign-then-install |
| Routing | `desired_deployments()` + `reconcile()` push healthy assignments to LiteLLM | No fallbacks, no params per role |
| Folder access | Mode ceiling = min(dashboard, runner `max_mode`) | **Runner accepts any `repo`/`cwd`** (`JobManager.create`, `_make_worktree`) |
| Gates | Controller sends a gate string | **Runner runs it with `shell=True`** (`_gate_worker`, runner.py:311), outside any agent sandbox: a compromised master gets a shell on every node without asking for `full` mode |
| Runner auth | One `RUNNER_TOKEN` for all nodes, **empty by default** (`tasks.py:28`) | Tailnet ACL is the only barrier; one leaked token = whole fleet |
| Instructions | Only the task prompt | No layered instructions, no skills |
| Audit | `events` table is task-oriented | No record of config changes, installs, token use |
| UI | 382-line vanilla `dashboard.html` | Fine for status; too thin for catalog, policy editing, effective-policy previews |

The three bold rows are security gaps in what's already built. They come first (Phase 4).

## 2. Principles

1. The controller declares desired state; **each node decides** whether that's allowed locally.
2. The master may **narrow** a node's access, never **widen** it. Widening is a local action on the node.
3. Instructions say how an agent should behave; policy decides what it *can* do. They're separate objects.
4. Every task records the exact instructions, policy, skills, model params and versions it ran with (snapshot +
   hash), so any result can be explained and reproduced.
5. Model names are validated references, never free-form strings passed to a shell.
6. Role assignment is declarative: it can sit "pending install" until its model is ready.
7. Ollama is the only engine the fleet installs into at first; other engines stay discover-and-route only.

## 3. Model catalog and installs

### 3.1 Catalog entry

```yaml
id: qwen3-8b
display_name: Qwen3 8B
engine: ollama
reference: qwen3:8b          # identity = engine + reference + digest
digest: sha256:…             # filled when resolved
download_gb: 5.2             # from the registry manifest (same code as evals/try_models.download_gb)
loaded_gb: 6.6               # measured from /api/ps after the smoke test
min_engine: null             # set when the registry answers 412 "requires a newer version"
capabilities: [chat, json, reasoning]
defaults: {num_ctx: 8192, temperature: 0.2, keep_alive: 10m, think: false}
suggested_roles: [coder, reviewer, fast]
evals: {coder: 75.6, reviewer: 92.9, fast: 97.2}
license_url: …
source: curated | custom
```

- **Add by name.** The controller asks a runner to resolve the reference against the registry and returns digest,
  size and compatibility before saving. Unknown fields show as unknown, never guessed. Of the models in the request,
  `qwen3:8b` is confirmed (our eval run); `gpt-oss:20b`, "Qwen 3.5 9B", "Gemma 4 12B" and "Gemma E4B" are
  **UNVERIFIED** until that lookup succeeds.
- **Seed catalog** from `evals/results/results.json`, so the eval scores show next to each model.

### 3.2 Per-node model state

Each `(node, model)` pair has a **desired** state (`present | absent`), an **observed** state (`missing |
pulling | verifying | ready | draining | removing | failed | blocked`), progress (bytes, %, layer), the last fit
result and error, and an **ownership** flag:

- `fleet-managed`: installed by the fleet; the fleet may remove it.
- `pre-existing`: was on the node before; **never removed automatically**. You can adopt it (making it
  fleet-managed) or remove it with an explicit confirmation.

### 3.3 Install flow

1. In the UI: pick model → pick nodes (each shows ✔ fits / ⚠ tight / ✖ won't fit / ? unknown) → optionally pick
   roles → Install.
2. Controller stores `desired=present` and an **operation** record.
3. Runner preflight: engine reachable; engine version OK; reference resolves; download size + node disk reserve
   fit; loaded size + KV cache for `num_ctx` + OS reserve fit in RAM; node-local policy allows remote model
   management.
4. Runner pulls via Ollama's `/api/pull`, streaming progress into a durable operation file (same detached
   wrapper + exit-file pattern as jobs, so it survives a runner restart).
5. Smoke test: one chat with the configured params; record actual loaded size from `/api/ps`; unload.
6. `ready` → controller refreshes inventory → waiting role assignments become active → `reconcile()` routes them.

Failures get a specific state with the fix shown in the UI: `blocked_engine_version` (with current vs needed
version and the upgrade command — gemma3:12b on Ollama 0.17.7 is exactly this case), `no_space`, `wont_fit`,
`registry_unknown`.

**Fit rules (defaults, editable per node):** keep ≥ 8 GB disk free; keep 3–4 GB RAM for macOS; include the KV cache
for the chosen context; one loaded model at a time on the 16 GB M1 (matches onboarding's
`OLLAMA_MAX_LOADED_MODELS=1`). "Won't fit" blocks activation; an admin can override, and the override is audited.
"Unknown" asks for confirmation.

### 3.4 Removal

Drain first: stop routing new requests, wait for in-flight tasks, refuse if another active assignment still needs
it, require confirmation for `pre-existing` models, delete via the Ollama API, verify it's gone. Catalog entry,
eval history and audit stay. A failed removal stays visible and retryable; partial failure never counts as removed.

### 3.5 Model parameters

Catalog defaults plus per-assignment overrides: context length, temperature/top-p/seed, max output tokens,
keep-alive, JSON mode, thinking on/off, request and load timeouts.

- Engine-level params (`num_ctx`, `temperature`) go into a **derived Ollama model** (a Modelfile `FROM qwen3:8b` +
  `PARAMETER` lines, named e.g. `fleet-qwen3-8b`), so every caller gets the same behaviour.
- Request-level params (`think`, timeouts, JSON mode) are sent per request: by `desired_deployments()` as LiteLLM
  deployment params, and by `ollama_worker()` for direct Ollama tasks. Ollama's `think` request field is
  **UNVERIFIED** on 0.17.7; per-model testing decides, never a prompt-only `/no_think` trick.
- The thinking setting is how qwen3:8b's two eval timeouts get fixed; the eval suite compares thinking on/off as
  separate configurations.

### 3.6 Roles that wait for installs

Assignment lifecycle: `pending_install → verifying → active | blocked | failed → draining → removed`.

`POST /api/assignments` takes `ensure_model: true` (the UI default). If the model is missing it creates the
install, shows the assignment as pending with its blocker inline, and activates it only after the smoke test.
This replaces the current refusal in `controller.assign()`. A failed verification never reaches LiteLLM.

## 4. Instructions and policy

### 4.1 Layers

1. Built-in safety baseline (shipped with the controller and runner)
2. Fleet
3. Node
4. Role (coder, reviewer, fast, …)
5. Agent (claude, codex, ollama)
6. Task template / workflow step
7. The task itself
8. **Node-local hard policy** (on the node; applied last; can't be changed remotely)

**Instructions** concatenate in that order. **Policy** only ever gets stricter: allowed folders/tools/hosts
intersect, denied ones union, limits take the minimum, and `false` wins for on/off switches.

```yaml
instructions: |
  Follow the repo's CLAUDE.md. Never touch production config.
filesystem:
  allowed_roots: ["~/code", "~/ai-fleet"]
  denied: ["**/.env*", "~/.ssh/**", "~/Library/Keychains/**", "~/.ai-fleet/**"]
network: {agents: deny}              # or {agents: allow, hosts: [github.com, pypi.org]}
tools: {claude_allow: [], claude_deny: ["WebFetch"]}
mode_ceiling: auto-edit
gate_profiles: [python-tests, npm-test]
limits: {max_concurrent: 2, task_minutes: 60, agent_minutes_per_day: 300, output_mb: 20, quiet_hours: "23:00-07:00"}
models: {remote_management: true, disk_reserve_gb: 8}
```

### 4.2 Where each rule is enforced

| Rule | Enforced by (authoritative) | Also checked by |
|---|---|---|
| Folder roots, denied paths | **Runner**: canonical paths (realpath of the path and every parent), symlink, junction and case-folding escapes rejected; git metadata paths checked too | Controller, UI |
| Mode ceiling | Runner local policy (exists today as `max_mode`) | Controller `ceiling()` |
| Gates | **Runner gate profiles**: named, defined on the node, run as argv with `shell=False`; raw gate strings off by default | Controller validation |
| Tools | Runner builds the agent command line (Claude `--allowedTools` / `--disallowedTools`, Codex sandbox mode) | Effective-policy preview |
| Network | Node OS sandbox/firewall (strong); agent flags (best effort: Codex sandbox has no network by default, Claude denies WebFetch/WebSearch) | — |
| Time, process, output limits | Runner wrapper + process group | Controller monitoring |
| Concurrency, RAM admission | Runner resource manager | Scheduler (`eligible`, `pick_node`) |
| Budgets, quiet hours | Controller scheduler | Prompt instruction |
| Model install/remove | Runner local `remote_management` switch | Controller admin scope |
| Instructions | Runner writes the controller's rendered snapshot into the agent: Claude `--append-system-prompt` (UNVERIFIED), Codex `AGENTS.md` in the worktree, Ollama `system` message | — |

The Claude flags the runner already uses (`--permission-mode`, `--allowedTools`) haven't been live-tested yet
(the Claude CLI isn't installed). No security promise rests on CLI flags alone.

### 4.3 Trust model

Target guarantee:

> A compromised master can ask a node to do anything the node already allows, but can't widen the node's
> folders, tools, network, mode ceiling, disk reserve, model management or update permissions.

What that takes:

- **A unique runner credential per node**, made at enrollment, rotatable, and always required (no empty default).
- Node policy in `~/.ai-fleet/runner.json` (extends today's `max_mode`), written by `onboard.sh` and editable only
  on the node; task worktrees can't reach it, the credential or operation state.
- Gate profiles instead of raw shell strings.
- Resource limits so the master can't fill a node's disk or RAM.
- A local **"disconnect from fleet"** command that revokes the credential and stops remote work.
- Node-local log of refused widening attempts.

**Two isolation levels, labelled in the UI:**
- *Convenience* (default): agents run as your user. The runner rules above hold, but an agent with `full` mode
  could still reach anything your user can.
- *Hardened*: agents run as a dedicated OS user (e.g. `_aifleet-agent`) that only has the allowed roots. Needed
  for a strong guarantee; it conflicts with reusing your logged-in Claude/Codex credentials, so it's opt-in per
  node (risk noted in §9).

### 4.4 UI for policy
Form view and YAML view, version history with diff and rollback, and an **effective-policy simulator**: pick
(node, role, agent, repo) and see every value, whether it's allowed, and which layer decided it.

## 5. Skills

A skill is a folder with `SKILL.md` (front-matter: id, version, description, supported agents, risk) plus
optional text resources, stored in the master repo under `skills/` so they're versioned in git, and pinned by
digest when a task uses them.

**Types:** `prompt-only` (default), `tool-aware` (describes tools the policy already allows), `executable` (has
scripts; off by default; needs approval on the node). A skill never grants permissions: policy still decides.

**Bind** skills to fleet / node / role / template / task, like policies.

**Delivery:**
- **Baseline for all three agents:** the skill text is compiled into the task's instruction snapshot. Works
  everywhere, today.
- **Native adapters**, added after feature detection: Claude → write into the worktree's `.claude/skills/<id>/`
  (UNVERIFIED that a headless `claude -p` run loads project skills); Codex → section in the worktree `AGENTS.md`
  (native Codex skills UNVERIFIED; never touch the user's global Codex config while tasks run concurrently).
- **Ollama:** system message only; `prompt-only` skills only; no files, shell or tools.
- Skill files live in the worktree only and are excluded from the task diff. A used skill version is immutable;
  editing a skill affects new tasks only.

## 6. Other features

**P0 (inside phases 4–6):** admin audit trail (actor/token, action, target, before/after, result); per-node
credentials + rotation; runner/controller version and capability reporting; operation retry/cancel/recovery;
model digest/source/licence visibility; node disconnect + emergency stop ("stop all tasks" button); config
backup/export as one YAML.

**P1 (phases 7–8):**
- **Onboarding from the UI:** "Add node" makes a one-time enrollment token and shows the copy-paste command (which
  also issues the node's own credential); the node appears pending; approve it in the UI.
- **Evals in the UI:** run the suite on any (node, model, params); leaderboard; "recommend assignments"; promotion
  thresholds (an incomplete or timed-out eval never auto-promotes).
- **Pipeline template:** the CLAUDE.md pipeline (spec → tests → build → gate → cross-review → resolve) as a
  fixed template with options (who builds, who reviews, max rounds), run as linked tasks via `parent_id`. Not a
  general workflow builder.
- **Routing fallbacks and circuit breakers** per role (primary → other node → Codex), via LiteLLM fallbacks.
- **Notifications rules:** disk pressure, node offline, failed pull, blocked task, approval waiting (extends the
  plugins in `master/plugins`).
- **Laptop awareness:** on battery / thermal pressure → stop scheduling heavy work there.
- **Retention:** cleanup of old task logs and worktrees; suggests removing fleet-managed models unused for 14
  days (suggests only, never silent).
- **Observability:** per role/model latency, tokens/s, error rate (from LiteLLM), node load; extends `/api/usage`.
- **Update notices** for runner/controller; updates applied with local confirmation.

**Cut from these phases:** automatic engine upgrades; installs into engines other than Ollama; arbitrary remote
shell; skill or model marketplace; general DAG/workflow builder; billing-grade token accounting; multi-user RBAC
beyond view/operate/admin; Kubernetes/containers. (Later candidates: typed sandboxed tools for local models, a
secrets broker with task-scoped leases, signed skill distribution, scheduled regression evals.)

## 7. UI

**Stack:** React + TypeScript + Vite, built to static files in `master/controller/ui/dist` and served by FastAPI —
production stays Python-only, with no Node server. TanStack Query (API state), React Router (real URLs), React Hook
Form + Zod (validated forms), Radix primitives + design tokens in CSS variables (accessible components, light and
dark), the existing SSE stream for live updates. Auth unchanged (`auth.py` cookie/token, `X-Fleet` header).
Controls are hidden or disabled by token scope.

**Layout:** left nav, ⌘K command palette, operation drawer (live progress of pulls, removals, evals), responsive
cards and tables down to phone width. Destructive actions show the exact impact (which roles lose routing, which
tasks are affected).

**Pages:**
- **Overview:** health, "needs you" inbox, running tasks, failed operations, disk/RAM pressure per node.
- **Tasks:** list + detail (live log, diff, gate, messages, and the snapshot of instructions, policy, skills and
  model it ran with).
- **Models:** catalog, add model, node fit matrix, installs with progress, eval scores, loaded state.
- **Roles:** each role's models across nodes, pending installs with their blocker, params, fallbacks; drag a model
  onto a role.
- **Nodes:** list + detail (hardware, engines + versions, agents, models + disk, isolation level, policy in
  effect, health history); Add node.
- **Policies & instructions:** layers, editor, history, effective-policy simulator.
- **Skills:** library, versions, risk, bindings, preview of what each agent receives.
- **Evals:** runs, leaderboard, recommendations.
- **Activity:** audit + events + refused requests.
- **Settings:** tokens, plugins/notifications, retention, backup/export, versions.

## 8. Data model and API

**Tables:** `engine_instances` (several engines per node; fixes `probe()`'s first-engine-only), `model_catalog`,
`node_models` (desired/observed/ownership/fit/params/error), `operations`, `roles`, `assignments` (+ state,
priority, params, blocking operation), `instruction_sets`, `policies`, `skills` / `skill_versions`, `task_skills`,
`audit_events`, `eval_runs`, `node_credentials`. `tasks` gains snapshot hashes for instructions/policy/skills,
resolved model params and limits. `nodes.inventory` stays as raw diagnostics, not the source of truth.

**Controller API** (all mutations audited; scope in brackets):
`GET/POST/PATCH /api/models` [admin] · `GET /api/models/{id}/compatibility` [view] ·
`PUT /api/nodes/{node}/models/{model}` (desired state) [operate] · `GET /api/operations[/{id}]` [view],
`POST …/cancel|retry` [operate] · `GET/POST/PATCH /api/roles` [admin] · `POST/DELETE /api/assignments` [admin] ·
`GET/POST/PATCH /api/instructions`, `/api/policies` [admin] · `POST /api/policies/preview` [view] ·
`GET/POST/PATCH /api/skills` [admin] · `POST /api/tasks` gains `role`, `skills`, `template`, `limits`,
`gate_profile` · `GET /api/audit` [view] · `GET /api/evals`, `POST /api/evals/run` [operate] ·
`POST /api/enroll` [admin] · `GET /api/export` [admin].

**MCP (`master/mcp/server.py`):** read catalog, operations and effective policy; request installs or assignments
only when the user asks; `assign_role` returns a pending assignment instead of failing. Approvals, engine upgrades,
policy widening and destructive removals stay UI-only.

**Runner contract v2** (keep v1 routes working; `VERSION = "2"`):
- `GET /health` adds a feature list, engines + versions, disk/RAM/GPU and pressure, local policy summary,
  isolation level, active operations.
- `GET /models` · `PUT /models/{ref}` (ensure present) · `DELETE /models/{ref}` (ensure absent) ·
  `GET /operations/{id}` · `POST /operations/{id}/cancel` · `GET /policy` (redacted effective local limits).
- `POST /jobs` adds the policy snapshot + hash, rendered instructions, skill bundle, limits, model params,
  `gate_profile` + args, and the requesting identity for the audit.
- The runner **refuses** unknown security fields rather than ignoring them.

## 9. Phases

Each phase runs the CLAUDE.md pipeline: tests first, gate, cross-review by whoever didn't build it.

**Phase 4 — Secure runner boundary** (small, closes today's gaps)
Folder allow-list with canonical-path checks; node-local `runner.json` ceiling; gate profiles (argv, no shell);
per-node credentials, always required; protected runner state; resource limits; node-local audit log.
Acceptance tests:
1. Allowed repo task runs as before; restart/resume/diff unchanged.
2. `repo` or `cwd` outside the roots → 403, including through a symlink, `..`, a macOS case variant, a Windows
   junction or drive path.
3. A gate argument containing `; rm -rf ~` can't run a second command.
4. A pushed policy can't widen local roots or mode.
5. A task can't modify `runner.json`, the credential or operation files.
6. Node A's credential is rejected by node B; an empty credential is rejected.

**Phase 5 — Models, roles and the new UI shell**
React app with parity for every current dashboard action, plus Models, Roles and the operation drawer; catalog;
Ollama desired-state installs/removals with progress; fit check; ownership; derived models + params; pending
assignments.
Acceptance tests:
1. Fake Ollama pull reports layered progress and reaches `ready`; the UI updates live.
2. A runner restart mid-pull recovers without a duplicate operation; concurrent "ensure present" requests are
   idempotent.
3. Not enough disk → refused before downloading; won't-fit → blocked, admin override is audited.
4. A registry 412 → `blocked_engine_version` with the versions and command shown.
5. An assignment to a missing model stays pending, activates only after the smoke test; a failed smoke test never
   reaches LiteLLM.
6. Removal drains routing first; `pre-existing` models are never auto-removed.
7. On the 16 GB node two large models are never loaded at once.
8. Live check: install `qwen3:8b` with thinking off on Laptop 2 from the UI, assign it to `reviewer`, see it routed.

**Phase 6 — Instructions and policy**
Layers, editor, history, simulator, task snapshots, budgets, network switches, instructions delivered to all
three agents.
Acceptance tests:
1. Instruction order is deterministic; policy never loosens down the layers; controller vs runner disagreement →
   the stricter value.
2. A task record reproduces exactly the instructions and policy it ran with; a resume uses the original snapshot.
3. Ollama gets the instructions as a system message; Claude and Codex receive the same content through their
   adapters (verified by fake agents capturing their input).
4. A simulated compromised master can't widen node-local policy.

**Phase 7 — Skills, evals, pipeline template**
Prompt-only skills with bindings and snapshots; native Claude/Codex adapters behind feature detection; evals in
the UI with promotion thresholds; the CLAUDE.md pipeline template.
Acceptance tests: the same skill yields equivalent content for all three agents; a used skill version is
immutable; executable skills are refused without node approval; Ollama never receives scripts; eval runs record
model digest, engine version, hardware and params; thinking on/off compare as separate configs; incomplete evals
never promote; a pipeline run creates linked build → gate → review tasks.

**Phase 8 — Operations polish**
Onboarding from the UI, fallbacks, notification rules, laptop awareness, retention, observability, export/import,
update notices, full accessibility and mobile pass, retire `dashboard.html`.
Acceptance tests: an enrollment token works once and expires; SSE reconnects without duplicates; view/operate/admin
tokens see only their controls; keyboard, focus, contrast and phone layouts pass; a controller restart keeps all
visible operation state.

## 10. Risks

- Strong isolation (dedicated OS user) conflicts with reusing your logged-in Claude/Codex credentials.
- Ollama's features vary by version; engine upgrades can change model behaviour and invalidate evals.
- Context length can turn a model that "fits" into one that doesn't.
- Registry sizes differ from real disk use when layers are shared.
- A model pull is supply-chain content: digest and source must be visible.
- Claude/Codex native skill and flag behaviour may change; the compiled-instruction baseline is the fallback.
- Windows paths and sandboxing need their own tests.
- The controller's audit isn't tamper-proof if the master itself is compromised; node-local logs cover refusals.
- One SQLite controller is fine, but migrations and backups must be deliberate.

## 11. Decisions for you

1. **Order:** security boundary first (Phase 4), then models + new UI (5)? *Recommended.* Or models + UI first.
2. **Node sovereignty:** the master may only narrow node access; widening is done on the node. *Recommended.*
3. **Isolation:** "convenience" by default with "hardened" opt-in per node (*recommended*), or hardened for every
   remote node from the start.
4. **UI stack:** React/TypeScript/Vite served by FastAPI. *Recommended.* Or keep a dependency-free page.
5. **Engine upgrades:** manual, confirmed on the node. *Recommended.* Never automatic.
6. **Thinking defaults:** off for `coder` and `fast`, configurable for `reviewer`, confirmed by evals.
7. **Model names:** what you meant by "Qwen 3.5 9B", "Gemma 4 12B" and "Gemma E4B"; they get checked against the
   registry when added.
8. **Windows:** keep the runner contract compatible now, harden Windows in parallel before calling it
   production-ready. *Recommended.*

## Review notes

- **Codex found three gaps Claude's draft missed**, all checked in the code and now Phase 4: gates run with
  `shell=True` (runner.py:311); `RUNNER_TOKEN` is shared and empty by default (tasks.py:28); `probe()` returns
  only the first engine on a node. Codex also contributed model ownership (pre-existing vs fleet-managed),
  per-task snapshots, gate profiles, the restrictive merge rules and the isolation levels.
- **Claude's draft contributed** the derived-Ollama-model approach for engine params, the effective-policy
  simulator, onboarding from the UI, the per-node fit matrix and the pipeline template.
- **Disagreement: UI timing.** Codex put the React app last; Claude put it first. Merged: the shell arrives in
  Phase 5 with Models and Roles, because those pages need it and the request centres on the UI; full polish waits
  for Phase 8.
- **Disagreement: workflows.** Codex cut workflows to P2; Claude had them at P1. Merged: one fixed template for
  the existing CLAUDE.md pipeline in Phase 7, no general workflow builder.
- **Disagreement: native skills.** Claude planned to write into `.claude/skills`; Codex said this was unverified.
  Merged: compiled instructions are the baseline for every agent, and native adapters come only after feature
  detection.

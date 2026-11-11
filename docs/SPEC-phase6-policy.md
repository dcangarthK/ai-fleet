# Phase 6: instructions and policy

Part of [SPEC-control-plane.md](SPEC-control-plane.md) §4 and §9. Builds on Phase 4 (node-local ceiling) and 5b.

Tests: `tests/test_policy.py` (pure merge rules), `tests/test_runner_policy.py` (runner), Phase 6 section of
`tests/test_controller.py`.

## Objects

**Instruction set:** `{scope, scope_id, version, text, author, created_at}`. Text ≤ 16 KB.
**Policy:** `{scope, scope_id, version, body, author, created_at}`.
`scope` ∈ `fleet` (scope_id `*`), `node` (node id), `role` (role name), `agent` (`claude|codex|ollama`).
Saving creates a new version; the newest version per (scope, scope_id) is in force. Older versions stay for
history and rollback (rollback = save an old version's content as a new version).

### Policy body (every field optional)

```json
{
  "filesystem": {"allowed_roots": ["~/code"], "denied": ["~/code/secret-project"]},
  "tools": {"claude_allow": ["Read", "Edit", "Bash(npm test:*)"], "claude_deny": ["WebFetch"]},
  "network": {"agents": "deny"},
  "mode_ceiling": "auto-edit",
  "limits": {"task_minutes": 60, "max_concurrent": 2, "agent_minutes_per_day": 300,
             "quiet_hours": ["23:00-07:00"]}
}
```

Unknown keys → 400. Types are validated (roots/denied absolute or `~/…`; tool names `^[A-Za-z][A-Za-z0-9_]*(\(.{0,200}\))?$`;
`quiet_hours` items `HH:MM-HH:MM`, may wrap midnight).

## Merge (pure function, `master/controller/policy.py`)

Layers in order: `fleet`, `node`, `role` (when the task has a role), `agent`, `task` (the task's own optional
`policy` field). Only layers that exist take part.

| Field | Rule |
|---|---|
| instructions | concatenated in layer order, each as `## <layer label>\n<text>` |
| `filesystem.allowed_roots` | intersection of the layers that set it (a root survives if it is inside a root of every other layer that set one; the narrower path is kept) |
| `filesystem.denied` | union |
| `tools.claude_allow` | intersection of the layers that set it |
| `tools.claude_deny` | union |
| `network.agents` | `deny` if any layer says `deny` |
| `mode_ceiling` | minimum |
| `limits.*` numbers | minimum |
| `limits.quiet_hours` | union of windows |

Result: `{instructions, policy, sources}` where `sources` maps every field path (e.g. `limits.task_minutes`) to
the layer that decided it (for intersections/unions: the list of contributing layers). `hash` = sha256 of the
canonical JSON of `{instructions, policy}`.

## Task snapshot

At task creation the controller computes the effective result for `(node picked, role, agent, task policy)` and
stores `tasks.instructions`, `tasks.policy` (JSON) and `tasks.policy_hash`. These never change for the task's
life: approvals, follow-up messages and resumes use the same snapshot. `GET /api/tasks/{id}` returns them.

Scheduling uses the effective policy:
- `mode_ceiling` below the task's mode → the task is refused (409) like today's node ceiling.
- `max_concurrent` (per node): a node at its limit isn't eligible; if no node is, the task waits `queued` and
  starts when a slot frees.
- `agent_minutes_per_day` (per node, from `agent_seconds` of tasks created in the last 24 h): over budget → not
  eligible; reason `daily agent budget used`.
- `quiet_hours` (master's local time): tasks created in a window stay `queued` until it ends; running tasks are
  not stopped.

## Runner (POST /jobs additions)

`instructions` (string ≤ 32 KB) and `policy` (object, the effective policy's `filesystem`, `tools`, `network`,
`limits.task_minutes`) and `policy_hash`. The runner applies them **only narrowing** its own `runner.json`:
- paths: a request path must pass the local check **and** be inside a policy `allowed_roots` (when given) and
  outside policy `denied`; a policy root outside the local roots grants nothing.
- `task_minutes`: `min(local max_job_minutes, policy)`.
- Claude: `claude_deny` → `--disallowedTools` (comma-joined); `claude_allow` → intersect with the request's
  `allowed_tools` (when both given) and with itself otherwise; `network.agents == "deny"` adds `WebFetch` and
  `WebSearch` to the disallowed list.
- Codex: network allowed only when the policy says `allow` **and** local `runner.json` has `"network":
  {"agents": "allow"}`; then `-c sandbox_workspace_write.network_access=true`. Otherwise no flag (Codex's sandbox
  has no network by default).
- Instructions delivery: Claude and Codex get them at the top of the stdin prompt inside
  `<fleet-instructions>…</fleet-instructions>` followed by a blank line and the task prompt. Ollama gets them as a
  `system` message. Nothing is written into the repository.
- The job state stores `policy_hash` and the instructions; `POST /jobs/<id>/message` reuses them.
- `/health` reports `"policy": true` (feature flag) and local `network` setting.

## Controller API

| Route | Scope |
|---|---|
| `GET /api/instructions` (all scopes, newest version each) / `GET /api/instructions/{scope}/{id}/history` | view |
| `PUT /api/instructions/{scope}/{id}` `{text}` | admin |
| `GET /api/policies` / `GET /api/policies/{scope}/{id}/history` | view |
| `PUT /api/policies/{scope}/{id}` `{body}` | admin |
| `POST /api/policies/preview` `{node, role?, agent, policy?}` → `{instructions, policy, sources, hash}` | view |

All writes emit `policy.changed` / `instructions.changed` events with `{scope, scope_id, version, author}`
(`author` = token label, or `local` on the local port). `POST /api/tasks` accepts `role?` and `policy?` (task
layer). MCP `spawn_task` gains `role` and passes nothing else new.

## Console

**Policies page**: left list of layers (Fleet, then each node, role and agent, marked when they have content);
right side: instructions editor (textarea) and policy form (folders, denied paths, tools, network switch,
ceiling, limits, quiet hours) with a JSON view, Save, version history with diff and "restore this version".
**Simulator** panel: pick node, role, agent → effective instructions and policy, each field showing the layer
that decided it. Task detail sheet shows the snapshot (instructions and policy) the task ran with.

# Phase 8: operations polish

Part of [SPEC-control-plane.md](SPEC-control-plane.md) §6 and §9. Everything here is small on its own; together
it's what makes the fleet comfortable to run day to day.

Tests: Phase 8 section of `tests/test_controller.py`, `tests/test_runner_power.py`, console unit tests.

## 1. Who am I, and controls by scope
`GET /api/me` → `{scope: "view"|"operate"|"admin", label, local: bool}` (local port → `admin`, label `local`).
The console hides controls the scope can't use (from the Codex review of 5a): view sees no mutation buttons;
operate sees task create/message/cancel/complete/cleanup and installs; admin sees approve/reject, nodes, roles,
policies, skills, catalog edits.

## 2. Routing truth
`reconcile()` records, per assignment, whether a LiteLLM deployment exists after the last successful sync:
`assignments.routed INT`, `assignments.route_reason TEXT` (`litellm not configured`, `node unhealthy`,
`master model host not set`, `sync failed: …`, or null). `/api/fleet` returns them; the console's "Live" means
`routed`, never just node health (also from the review).

## 3. Enrolling a node from the console
- `POST /api/enroll` [admin] `{name?}` → `{code, expires_at, command}`. Code: 10 random base32 characters, stored
  hashed, single use, 30-minute expiry.
- `command` is the line to paste on the new machine:
  `curl -fsS https://<master>/enroll/onboard.sh | bash -s -- --enroll <code> --master https://<master>`
  (`/enroll/onboard.sh` is served without a token on the public port; it is the same script as
  `nodes/onboard.sh`.)
- The script runs normally, then `POST /api/enroll/complete` `{code, hostname, runner_token}` (this route accepts
  the code instead of a fleet token). The controller stores the token against the hostname; when discovery finds
  a node with that hostname it gets the token, still `pending`. **Approval stays a separate admin action.**
- Wrong/expired/used code → 403 and an audit event; 5 failures from one source in 10 minutes → 429.

## 4. Laptop awareness
Runner `/health` adds `power: {on_battery: bool|null, battery_pct: int|null}` (macOS `pmset -g batt`, Linux
`/sys/class/power_supply/*/`, Windows `GetSystemPowerStatus`; unknown → nulls). `runner.json` gains
`"power": {"min_battery_pct": 30}`. A node on battery below its minimum is not eligible for `claude`/`codex`
tasks (reason `on battery (23%)`); Ollama one-shots are still allowed.

## 5. Role fallbacks
`roles(name PK, fallback TEXT NULL, description)`; `PUT /api/roles/{name}` [admin] `{fallback, description}`.
No cycles (400). `/api/fleet` returns `roles: [{name, fallback, description}]` for every role that has a row or an
assignment. MCP `ask_model`/`review_diff`: on an error or timeout from a role, try its fallback once and say
in the reply which role answered. The console shows the chain on the Roles page.

## 6. Model-call stats
The MCP bridge reports each model call: `POST /api/usage/calls` `{role, model, ms, ok, tokens_in?, tokens_out?,
fallback_from?}` (operate). `GET /api/usage/calls?days=7` → per role and model: count, error rate, p50/p95 ms.
Overview shows a compact table; no external metrics stack.

## 7. Notifications
`notify_rules(id, event_glob, plugin, enabled)`; defaults: `task.awaiting_approval`, `task.blocked`,
`pipeline.escalated`, `model.state` with state `blocked|failed`, `node.offline`. Settings page lists rules with
on/off switches and a "send a test" button. API: `GET /api/notify-rules` [view], `PUT /api/notify-rules/{id}`
`{enabled}` [admin], `POST /api/notify-rules/{id}/test` [admin] (sends a `notify.test` event through that plugin). Plugins keep their existing contract.

## 8. Retention
Daily: remove worktrees (runner `/cleanup`, which snapshots first) of tasks `done`/`cancelled` for more than
`FLEET_KEEP_WORKTREE_DAYS` (default 7; branches are kept); delete events older than 90 days; mark
fleet-managed models unused by any role or task for 14 days as `idle` (the console suggests removal; nothing is
removed automatically).

## 9. Export and import
`GET /api/export` [admin] → one JSON document: catalog, desired installs, roles (+fallbacks), assignments,
instructions, policies, skills (current versions), bindings, notify rules. No secrets (no tokens, no runner
tokens). `POST /api/import?dry_run=1` [admin] → the diff it would apply; without `dry_run` it applies it as new
versions/rows (never deletes unless `"prune": true`).

## 10. Updates
The master knows the runner it would install (`nodes/runner.py` digest). `/api/fleet` marks nodes whose runner
`/health` digest differs (`runner.digest` added to health) as `update_available`, and the node page shows the
one-line update command. Nothing updates itself.

## 11. Retire the classic dashboard
`/` redirects to `/app/`; the old page stays at `/classic` for one release. `/login` redirects to `/app/`.

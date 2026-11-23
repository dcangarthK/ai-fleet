# SPEC: limit policy (plan 1.0, 1.1 shadow, 1.2 `same_node`, 1.3 decisions)

Source: `docs/research/FLEET-IMPROVEMENT-PLAN-2026-11-19.md` lines 158-161; `AUDIT-A-control-plane-2026-11-19.md`
(F3, five limit writers). Status: P1 builds the shadow only. **No caller changes behaviour.** Regression tests that pin
section 2: `tests/test_limit_policy.py` (`PinnedBehaviourTests`).

## 1. Scope

A paid task stopped on a provider usage limit (`failure_reason == "limit"`, `resets_at` set). Five writers can park it
or move it. This spec pins what each does today (UNVERIFIED for the two not covered by a pinning test, marked below).

## 2. Current behaviour per entry point (pinned)

| Entry point | Park | Handoff | Fail |
|---|---|---|---|
| `handoff.handle_limit` (called by `after_settled`, and by the pipeline path) | Writes a `resume_schedule` row at `resets_at + RESUME_DELAY` (120 s); the task row stays `failed`. Reasons, in order: kind's `on_task_limit != "handoff"`; profile pinned; `max_handoffs` used; node runner can't hand off; no worktree/repo; no `candidate_slots`; handoff refused by admission | Child `retry` task from a checkpoint on `candidate_slots(...)[0]`; source resolved `handed_off` | Never fails a task. Not a limit failure -> `{"action": "none"}` |
| `capacity.requeue_on_limit` (runs first, `after_settled`) | Live: task -> `queued`, `not_before = resets_at` (no delay), `budget_wait="limit"`. Shadow: only emits `task.requeue_shadow`, task stays `failed` | Defers (returns False) when the kind hands off and `candidate_slots` is non-empty | None |
| `tasks.requeue_limit_wait` (backstop, `poll_task`) | `queued`, `not_before = earliest reset over usable accounts` (UNVERIFIED, no pinning test) | None | None |
| auto-approve hold (tasks.py ~1289) | `queued`, `not_before = verdict.until` (UNVERIFIED, no pinning test) | None | None |
| `capacity.migrate_tick` move / timed wait | Moves a *queued* task to a free slot, or rewrites `not_before`; one raw UPDATE | n/a | None |

Known disagreements (AUDIT-A 1.2, unchanged): wait time is `resets_at + 120` vs `resets_at` vs earliest account reset;
`candidate_slots` ignores pins; the same limit can be a handoff for one path and a park for the next.
Observed in the shadow run: `requeue_on_limit` may see no eligible slot where `handle_limit` then finds one
(`tests/test_limit_policy.py`, handoff case, rule `requeue_on_limit`). Not investigated further here.

## 3. Shadow policy (1.1)

`limit_policy.plan_limit(task, rules, now) -> Park(until) | Handoff(slot) | Fail(reason)`. Pure: reads, never writes.

1. Not a limit failure -> `Fail("not a limit failure")`; no `resets_at` -> `Fail("no reset time known")`.
2. Kind not `on_task_limit == "handoff"` -> `Park(resets_at)`.
3. Pinned profile -> Park. `max_handoffs` used -> Park. No worktree/repo -> Park. No `candidate_slots` -> Park.
4. Otherwise `Handoff(candidate_slots[0])`.

One wait time (`resets_at`, no delay). The node-runner capability check is left out of the shadow (`candidate_slots`
already returns [] for a node without profile support).

Shadow wiring: `handle_limit` (non-manual) computes the shadow **before** acting, calls the unchanged
`_handle_limit`, then `shadow_compare` writes one decision row and, if actual and shadow differ, a
`limit.shadow_mismatch` event. `requeue_on_limit` does the same beside its `task.requeue_shadow` event (shadow mode,
once per run). A mismatch is: different action; both park with `until` more than 1 s apart; both handoff to different
slots. Any exception in the shadow is logged and swallowed. Cut-over is out of scope.

## 4. `same_node` (1.2 part)

`handoff.candidate_slots(task, rules, now, *, same_node=True)`. `True` (default) is today's behaviour: the task's own
node only. `False` returns the task's node first, then every other node's eligible slots. No caller passes `False`.
Warning: `_insert` writes the source task's `node_id`, not the slot's, so a cross-node result must not be handed off
until that is fixed.

## 5. Decisions (1.3)

Table `decisions(id, task_id, kind, inputs, candidates, rule, chosen, shadow_choice, ts)`; JSON text in `inputs`,
`candidates`, `chosen`, `shadow_choice`. Rows written (observation only; a failed write is swallowed):

| kind | writer | rule | notes |
|---|---|---|---|
| `placement` | `tasks.pick_node` | `least_busy` | `task_id` is NULL (the id doesn't exist yet); `inputs` has agent/model/mode/role; `candidates` the eligible node ids; `chosen` the node id. Explicit `wanted` node is not recorded |
| `limit.park` / `limit.handoff` | `handle_limit` | `handle_limit` | `chosen` the action taken, `shadow_choice` the shadow's; `inputs.mismatch` |
| `limit.park` | `requeue_on_limit` (shadow mode) | `requeue_on_limit` | as above |

`GET /api/decisions?task_id=&limit=` (view scope, newest first, limit max 1000). Mounted through `handoff.router`.
Not recorded yet: `requeue_limit_wait`, the auto-approve hold, capacity moves, the manual handoff, the stall reroute.
`candidates` is empty for limit rows; the shadow's choice is the only candidate evidence so far.

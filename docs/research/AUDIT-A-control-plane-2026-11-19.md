# AUDIT A: control-plane overlap and automatable decisions (2026-11-19)

Scope: the limit parkers, the slot rankers, and the orchestrator decisions in the limit, handoff, placement and budget
code. Read-only. Only the functions named in the brief and their direct callers were read. Seed findings are taken
as given; every new claim cites file:line as read in this run. Items marked UNVERIFIED were not confirmed.

## Summary

- Five places write a wait or placement state, not three: the three named parkers, plus the auto-approve hold
  (tasks.py:1289-1297) and the capacity move/timed-wait writer (capacity.py:173-198, reached from migrate_tick
  capacity.py:201-230). There is also a stall wait store (handoff.py:665-677) that is not a limit path.
- Three slot selectors exist, not two: `candidate_slots` (handoff.py:269), `rank_slots` (handoff.py:359), and
  `placement`/`pick_node` (tasks.py:486, 502) for normal spawns. They use different data sources and different rules.
- The reviewer-independence check runs only at spawn (tasks.py:1849 -> check_independence, tasks.py:1494-1514).
  Handoffs never run it (`_claim_to_insert`, handoff.py:875-898). A limit-handed review can land on its builder's model.
- Which path wins: for a task that hits a limit, `capacity.requeue_on_limit` runs first in live mode
  (handoff.py:626-629). If it returns False, `handle_limit` runs and writes a `resume_schedule` row. The
  `requeue_limit_wait` backstop runs last and only when no row exists (tasks.py:985, 1030).

## Section 1. Overlap and drift

### 1.1 The limit parkers

Call order for a task that fails on a limit (live mode):

1. `handoff.after_settled` (handoff.py:614-629): limit failures go to `capacity.requeue_on_limit` (handoff.py:627)
   first, then to `handle_limit` (handoff.py:629).
2. `tasks.poll_task` (tasks.py:970-985): after `after_settled`, if the task is still a limit failure with no
   resolution and not pipeline-owned, it calls `requeue_limit_wait` (tasks.py:982-985).
3. The pipeline path has its own call to `handle_limit` (controller.py:2164-2166).

| | `handoff.handle_limit` (handoff.py:791) | `capacity.requeue_on_limit` (capacity.py:285) | `tasks.requeue_limit_wait` (tasks.py:1018) |
|---|---|---|---|
| Decides | hand off to a slot, or schedule a resume | queued with `not_before`, or defer to handoff | queued with `not_before` (backstop) |
| Wait computation | `resets_at + RESUME_DELAY` (2 min) (handoff.py:506-508). Uses the task's own `resets_at` only | `not_before = task["resets_at"]` (capacity.py:293, 308). No delay | `_earliest_limit_reset` = min over usable accounts (tasks.py:996-1015), falling back to `resets_at`. No delay |
| Writes | `resume_schedule` row: at, reschedules, state, reason, run (handoff.py:511-515). On handoff: claim, new task, old task resolution (handoff.py:875-898) | `tasks.transition`: queued, not_before, ended_at=None, error, budget_wait='limit', resolution=None (capacity.py:308-309) | `transition`: same fields (tasks.py:1037-1038). Emits `task.rerouted` (tasks.py:1040), which is misleading |
| Eligibility | on_task_limit == "handoff" (handoff.py:821); not pinned (823); max_handoffs (825); profile_support (828); worktree and repo (830); candidate_slots non-empty (832) | PAID_AGENTS, limit_failure, usage mode not off, resets > now (capacity.py:291-294). Defers when kind is "handoff" and worktree and candidate_slots non-empty (297-299). Does not check pins, max_handoffs or profile_support | state failed, failure_reason limit, no resolution, no `resume_schedule` row (tasks.py:1027-1031) |
| Mode | live and shadow. Shadow mode writes a schedule row | live writes; shadow only emits `task.requeue_shadow` (capacity.py:300-304) and returns False | live only in effect |

Other writers, same family:

- Auto-approve hold (tasks.py:1288-1297): sets state queued, `not_before = verdict.until`, budget_wait='limit', and
  keeps approved_tools. It uses the admission verdict's own `until`, a third source of reset time.
- Capacity move and timed wait (capacity.py:173-198): one raw UPDATE rewrites node_id, profile, account,
  account_label, model, budget_key, budget_identity, not_before and error. It is the only writer that moves a task
  to another account while it waits. Driven by `migrate_tick` (capacity.py:201-230) via `tick` (capacity.py:364-372).
- Stall wait (handoff.py:665-677): `wait_for_slot` writes a stall record with `retry_at` and exponential back-off, a
  separate store that the sweep reads. Not used by limit failures (handoff.py:697-703 sends "limit" to handle_limit).

### 1.2 Where the parkers disagree

1. **Wait time.** The same limit gives three times. `handle_limit` gives resets+2 min. `requeue_on_limit` gives resets.
   `requeue_limit_wait` gives the earliest reset over accounts, with no delay. In live mode, `requeue_on_limit` wins for
   every kind it accepts, so `handle_limit` never schedules for them. In shadow mode `handle_limit` schedules at +2 min
   and the chosen time is never applied.
2. **Eligibility.** `requeue_on_limit` defers to a handoff on a candidate that `handle_limit` may then refuse. The
   deferral test is `candidate_slots` alone (capacity.py:297-299). `handle_limit` re-runs admission on the chosen slot
   inside `_claim_to_insert` (handoff.py:893-898). If that refuses, `handle_limit` falls through to
   `schedule_wait` at resets+2 min (handoff.py:838-839). Result: a task that was deferred on a free slot waits 2 min
   longer than if it had been requeued directly.
3. **Pins.** `candidate_slots` ignores pins (handoff.py:269-325 has no `_profile_is_pinned` call). A pinned task with a
   free slot elsewhere is deferred by `requeue_on_limit`, then parked by `handle_limit` on its own account
   (handoff.py:823-824), at +2 min. The pin is respected only at the end of the chain.
4. **Fields written.** `handle_limit` writes a separate table. The other two write `tasks` columns, and
   `capacity.timed_wait` also rewrites placement (capacity.py:186-193). The two `tasks`-column writers set the same
   columns with different values for the same condition.
5. **Backstop.** `requeue_limit_wait` fires only when no schedule row exists (tasks.py:1030). In shadow mode
   `handle_limit` writes one, so the backstop never runs there. In live mode it runs only when `handle_limit` returned
   without scheduling (for example a raised exception, or a handoff that moved the task on, which sets resolution).
   I could not name a common case beyond these. UNVERIFIED how often it fires in production.

### 1.3 The slot rankers

| | `candidate_slots` (handoff.py:269-325) | `rank_slots` (handoff.py:359-417) | `placement` / `pick_node` (tasks.py:486-537) |
|---|---|---|---|
| Used by | limit handoff (handoff.py:832, 1001, 1041), requeue_on_limit (capacity.py:298) | stall reroute (handoff.py:749), agent="auto" via `allocate` (tasks.py:1537), capacity move and timed wait (capacity.py:91, 143) | normal spawn (tasks.py:1830), live.py:877 |
| Source | `tasks.node_slots` and `tasks.usable` (handoff.py:309-310); `limit_snapshot` and `blocked_by` (handoff.py:316-317); `running_count` (handoff.py:320) | `resources()` (handoff.py:212-), which also reads node_slots and limit_snapshot | `node_rows`, `eligible` (tasks.py:299) |
| Scope | same node only (handoff.py:275-279) | same node only when task has node_id (handoff.py:391-393) | named node, or all approved nodes (tasks.py:489-492) |
| Fallbacks | uses `limits.fallbacks` (handoff.py:286) | ignores fallbacks (docstring, handoff.py:368) | none |
| Cross-vendor | only if `limits.allow_cross_vendor` (handoff.py:293) | always allowed | not applicable |
| Order | config order: same model first, then fallbacks | tier_rank (handoff.py:414), then idle, then catch-all model | temporary_reason, warm Ollama model, running_tasks, RAM (tasks.py:533-536). No tier |
| Builder exclusion | **none** | yes: build_attempts (handoff.py:381-383, 407-410); whole agent banned when builder model unknown (handoff.py:386) | none |
| Rule 0 and model guard | yes (handoff.py:302-307) | yes (handoff.py:401-404) | not in this path (UNVERIFIED beyond eligible at tasks.py:299-328) |

Which ranker wins depends on the entry point, not the task. The same review task can be placed three ways.
Independence is also checked two ways: spawn compares (agent, resolved model) with build_attempts
(tasks.py:1512), while rank_slots bans the whole agent when the builder's model is unknown (handoff.py:386). Spawn
refuses an unnamed test model outright (tasks.py:1510-1511).

### 1.4 Parallel model paths

`model="auto"` goes to `auto_pick` and `intel.rank` (tasks.py:1636-1637, 1566-1594), with `intel_routes.exclusion`.
`agent="auto"` goes to `allocate` and `rank_slots` (tasks.py:1638-1641, 1518-1541). Both are chosen per request and
neither is used by handoffs or stall reroutes. The seed finding holds: intel data is consulted only for model="auto"
and for refusal hints.

## Section 2. Decisions the orchestrator makes by hand that the controller can make

I can evidence ten. Items 7 to 10 are already done server-side: the orchestrator instructions still list them, so the
owner should remove them from the orchestrator's duties.

1. **Reviewer identity for a review round.** Decision (CLAUDE.md "Paid-model budget", "Review of a round"): the
   orchestrator names agent and model for each review. Data: `build_attempts` (handoff.py:138), the builder exclusion in
   `rank_slots` (handoff.py:381-383, 407-410), `allocate` (tasks.py:1518-1541). Smallest change: when a review-kind
   task has no agent, call `allocate` instead of requiring agent="auto". Today `allocate` runs only when agent is
   "auto" (tasks.py:1638-1640).

2. **Model for an ad-hoc test or review that names no model.** Decision: the orchestrator must name a model, or the
   spawn is refused with "can't tell which model" (tasks.py:1510-1511). Data: the same `rank_slots` builder exclusion.
   Smallest change: for kinds in `INDEPENDENT_KINDS`, run `allocate` before admission (tasks.py:1638-1640) instead of
   refusing at check_independence (tasks.py:1494-1514).

3. **Final-review routing to Codex.** Decision (CLAUDE.md Paid rule 1: check `fleet_status` before routing a final
   review). Data: per-slot `state` (free, busy, limited, at_budget) from `resources()` (handoff.py:212-255), tier floors
   for final and security reviews (handoff.py:330-331), codex kinds (budget.py:32-33). Smallest change: a per-kind
   preferred-agent entry in the budget rules, applied as a tier-order tiebreak inside `rank_slots`. UNVERIFIED that
   "scarce" (budget.py:32) sorts ahead of "premium" in `budget.TIERS`; check before relying on it.

4. **Gate-failure fix bounce (max 3 rounds, then escalate).** Decision (CLAUDE.md pipeline step 4). Data:
   `gate_failures` (tasks.py:1445), `loopheal.gate_event` and `gate_text` (loopheal.py:138-152), `emit_bounce`
   (loopheal.py:155-158), `max_fix_rounds` (batch.py:34). The UI already builds the same bounce message client-side
   (ui/src/lib/insights.ts:80-83). Smallest change: a controller-side message on gate failure for ad-hoc build tasks,
   reusing `gate_text`, with the count taken from `gate_failures`. UNVERIFIED whether ad-hoc gate failures already
   auto-bounce; I read only the pipeline path.

5. **Merge, then complete, then cleanup.** Decision (CLAUDE.md "Team" table, "After merging a branch"). Data:
   `_branch_merged` (tasks.py:2387-2401) answers "is this branch in main"; `cleanup_run` already removes merged
   branches and records the decision (intel_routes.py:209-232). Smallest change: after `complete` (tasks.py:2187),
   call `_branch_merged` and run the cleanup on success, instead of a second operator call.

6. **Spend report per summary (CLAUDE.md rule 10).** Decision: the orchestrator counts paid tasks and limit hits by
   hand. Data: `budget.status()` per model with `today_tokens`, soft and hard flags (budget.py:1405), and the task
   rows (agent, model, kind) in `tasks`. Smallest change: a `fleet_status` field giving task counts and limit hits
   per paid model over the batch, from the existing tables.

7. **Waiting for a reset and routing elsewhere after a usage limit (CLAUDE.md rule 7).** Already server-side:
   `after_settled` (handoff.py:614-629), `requeue_on_limit` (capacity.py:285), `migrate_tick` (capacity.py:201-230).
   Action: remove from the orchestrator's duties. The drift in 1.2 is the real remaining work.

8. **Queueing when a paid model is at its concurrency limit (CLAUDE.md rule 8).** Already server-side: `start_wait`
   (budget.py:1261-), the `max_concurrent` checks (budget.py:1169, 1278), and `try_start` (tasks.py:880) starts the
   queued task. Action: remove from the orchestrator's duties.

9. **Stall reroute, including the choice of the next slot.** Already server-side: `after_settled` (handoff.py:620-623),
   `handle_handoff` (handoff.py:697-), `rank_slots` with `slot_identity` exclusion (handoff.py:749-750).

10. **Review re-send with the delta only (CLAUDE.md rule 4) and bounce after a review.** Already server-side for
    pipelines: the delta prompt (controller.py:1932) and the bounce path (controller.py:2150-2169). Ad-hoc re-reviews
    are UNVERIFIED: I did not find a controller path for them.

Count: 10 items evidenced. Items 1 to 6 are candidate automations. Items 7 to 10 are already server-side and should
be dropped from the orchestrator's instructions rather than automated again.

## Notes for the owner

- The biggest correctness gap is the missing independence check on handoffs (section 1.3, `_claim_to_insert`
  handoff.py:875-898 does not call `check_independence`). A limit-handed review can run on its builder's model.
  This is a bug, not a duplicate path.
- The second is the deferral in `requeue_on_limit` (capacity.py:297-299). It defers on a snapshot of
  `candidate_slots` that `_claim_to_insert` may later refuse, and the result is an extra 2 minutes (1.2, item 2).
- Event naming: `requeue_limit_wait` emits `task.rerouted` (tasks.py:1040) for a plain requeue. Readers of that event
  cannot tell a handoff from a wait.
- No gate was run. Read-only audit; no tests were executed.

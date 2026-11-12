# Spec: capacity-driven placement (Part G)

Status: draft v1, 2026-11-12. The build starts only after Part E and Part F (`docs/SPEC-dashboard-truth.md`) have
merged into main, together with the open fixes. Depends on Part E's `rank_slots` and stall handling, and on Part F
requirements 6 (one limit helper) and 11 (Codex limits from Codex).

## 1. Problem
The controller places a task once, when it is created, and only moves it when Part E sees it stall. On 2026-11-12
the orchestrator had to move work by hand six times:
- Codex showed as limited for 22 hours while it had 96% of its window left (a parsed reset time rolled to the next day).
- Queued tasks stayed pinned to a limited profile while the spare account was free.
- A task that failed on a limit was never retried after the reset.
- A review looped three times on a refused tool.
- Gate failures were diagnosed and sent back to the builder by hand.
- The claude-mem worker hung with nothing restarting it.

## 2. Principle
**The heartbeat carries capacity, and the controller places work to match it, continuously.** Capacity is per
(account, model key): free slots, live usage left in each window, reset times, and node health. Placement is a
loop, not a one-time choice. It runs on every heartbeat and every task state change, using the budget rules, the
tier order and the independence rules that already exist. Fable never builds, fixes or tests.

## 3. Requirements
1. **Live capacity in the heartbeat.**
   - Each runner reports, per account it serves:
     - `slots_free`;
     - `usage` = `{window: "5h"|"weekly", used_percent, resets_at, source, observed_at}`.
   - Sources:
     - Codex: the newest `rate_limits` record in its session logs (Part F requirement 11).
     - Claude: the usage the CLI reports (statusline or `/usage` data). Where none is available, `source: "inferred"` comes from limit failures.
   - Stale usage (older than 30 minutes) is labelled stale and is never used to clear a limit.
2. **Limits follow live usage.** A model is limited only while live usage says so, or while a limit failure is
   newer than the last fresh usage record. Fresh data showing headroom clears the limit automatically, so no owner
   reset is needed. A reset time parsed from error text is a hint, never rolled past the next fresh record. Admission,
   placement and every page use the same helper (Part F requirement 6).
3. **Queued work migrates.** On every tick, each queued task that is waiting on a limit or a busy slot is re-ranked with
   `rank_slots`. If an eligible slot is free on another account or model, the task moves there:
   - It keeps the same task id and gets an event `placement.moved {from, to, reason}`.
   - Moves honour the task's pinned fields. If the owner pinned `profile` or `model` explicitly, the task never moves.
     Orchestrator-chosen defaults are not pins.
   - It respects the tier order (cheapest capable first), per-account concurrency, rule 0 (Fable never builds, fixes
     or tests) and reviewer independence.
4. **Limit failures re-queue.** A task that fails on a limit goes back to `queued` with `not_before = resets_at`, unless
   requirement 3 can place it elsewhere sooner. There is no orchestrator action and no paid availability check
   (budget rule 7).
5. **Loop detection.** A session that hits the same refused tool twice, or makes N turns with no output change (default
   3), is stopped. The task then settles as `failed(loop)` with the last result kept, or escalates. Its last result
   stays readable. A review that already produced a verdict settles as done, with a note that the loop was cut off.
6. **Gate bounce.** A failed gate sends the gate's failure summary back to the same builder session, via `/message`,
   automatically, up to 3 rounds (pipeline step 4). The failure summary is the failing test names plus the first error
   of each, not the raw tail. After round 3 the task escalates to the owner. Known trap signatures (stale runner embed,
   a merge that kept old onboarding scripts) add the matching builder checklist line to the message.
7. **Service self-heal.** If a node service (runner, controller worker, claude-mem worker, Ollama) misses 3 heartbeats
   or fails health checks for 2 minutes, the controller restarts it via its service manager. It then verifies health
   and records `service.restarted`. Two failed restarts in 15 minutes raise an alert instead of looping.
8. **Visible decisions.** Every placement, move, re-queue, loop cut, gate bounce and restart is an event shown on Live
   with its reason. Each one counts in the spend report.
9. **Owner override.** The Budget page can freeze placement per account (`hold`). Nothing moves onto or off a held
   account. `budget_override` stays owner-only.

## 4. Out of scope
- Buying or switching plans.
- Cross-machine model pulls (the owner does those).
- Changing the budget rules themselves.

## 5. Acceptance and tests
- **Usage:**
  - A stale Codex `rate_limits` record does not clear a limit.
  - A fresh record with headroom clears it.
  - "try again at 1:47 PM" seen at 15:00 never limits Codex until tomorrow.
- **Migration:**
  - A task queued on a limited fleet profile moves to a free spare slot within one tick.
  - A pinned task does not move.
  - A build never lands on Fable.
  - A review never lands on its builder's model.
- **Re-queue:** a limit failure re-queues with `not_before`, then runs after the reset, with a fake clock.
- **Loop:** two identical refusals stop the session, and a review with a verdict settles as done.
- **Gate bounce:**
  - The 1st and 2nd failures resume the builder with a summary.
  - The 4th failure escalates.
  - A trap signature adds its checklist line.
- **Self-heal:**
  - A fake service restarts and verifies once.
  - A second failure within 15 minutes alerts and stops.
- **Gate and watcher:** the gate passes, and the Haiku watcher confirms on Live that moves and restarts show with reasons.

## 6. Delivery
| Step | Who |
|---|---|
| Spec review | Sonnet, low effort |
| Build | fleet Sonnet; requirements 1–2 first, then 3–4, then 5–9 |
| Review | Codex `gpt-5.6-terra`, medium effort (placement touches admission, so it is design-sensitive) |
| Final | Fable, delta green light |
| Check | Haiku watcher pass, plus a real migration test: queue on a limited account and watch it move |
| Summaries and commit messages | local qwen3:8b |

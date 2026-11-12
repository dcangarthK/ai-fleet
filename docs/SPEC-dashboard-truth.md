# Spec: dashboard truth (Part F)

Status: draft v1, 2026-11-12. The owner asked for it to be queued for fleet Claude; the build starts after Part E
merges. Evidence: the Haiku watcher's notes (`docs/watch/dashboard-notes.md`, passes 1–2, task t-620955b7).

## 1. Problem
The console tells the owner things that aren't true:
- Finished work shows as failed.
- The same task reads differently on different pages.
- Alerts stay open after the work is resolved.
- The budget page disagrees with the resources page about which accounts are limited.

The owner reads the dashboard to decide what needs them, so every false "Failed" or "Blocked" costs their attention.

## 2. Principle
**One derived display state per task and per work item, computed in one place (`tasks.live_display`) and used by
every API and page.** Raw `state` stays in the data for the engine. Pages never re-derive labels.

## 3. Requirements
1. **Settled outcomes win over raw failure.** A task whose resolution is `done_elsewhere`, `handed_off` or
   `dismissed`, or whose work item has outcome `merged`, displays as settled. Labels: "Done elsewhere · <commit>",
   "Handed off → <task>", "Dismissed", "Merged · <commit>". It is never "Failed", and it is never red on any page
   (Live chains included). Covers F6, N1 and N3.
2. **Superseded attempts.** An attempt followed by a later successful attempt in the same work item shows
   "Superseded by <task>", in grey. Its denied first run (`Blocked: needs approval` that the policy auto-approved
   and resumed) shows "Auto-approved and resumed", not "Blocked". Only a task that is blocked **now** shows
   "Blocked". Covers F4.
3. **One vocabulary.** Live chain stages use the same labels as `/api/work` and `/api/tasks`: `done` becomes
   "Ready for review", "Merged" and so on. A cancelled task reads "Cancelled", never "failed". A limited task reads
   "Paused: limit, resets T" everywhere. Covers F1, F2 and F3.
4. **Alerts close.** Each alert gets `state: open | resolved` and `resolved_by`. An alert resolves automatically when
   its task becomes settled (requirement 1), is superseded, its limit resets, or its node recovers. Live shows open
   alerts only, with resolved ones behind a toggle. Covers F5 and P2-4.
5. **Re-queues link.** Every task the orchestrator, a handoff or a reroute creates in place of another carries
   `parent_id` (and `link` kind `retry|handoff|reroute|requeue`). `POST /api/tasks` gains `replaces: <task id>`: it
   sets the link and resolves the old task as `handed_off`. Covers F1 and N4.
6. **One source for account limits.** `/api/budget/status`, `/api/fleet`, `/api/resources` and `/api/live` all
   compute limit state from one helper, per **(account, model key)**, honouring `limit_scope` (`account` limits every
   key on that account, `model` only that key). This is a data correctness bug, not only display: admission must use
   the same helper. The test is that a limit on the fleet account shows identically on all four and admission
   refuses accordingly. Covers P2-1.
7. **Events feed is newest-first and current.** `/api/events/list` returns the newest N, newest first, with paging
   (`before=<id>`). If recording stopped, find out why and fix it. The test is that an event emitted now is first in
   the list. Covers P2-3.
8. **Orchestrator presence survives restarts.** Presence is derived from requests carrying `X-Fleet-Session`.
   After a controller restart, Live shows the last known presence as "stale since T" instead of empty, until a new
   request refreshes it. Covers P2-2.
9. **Usage counts are defined.** `/api/usage` states its window and counts `failed` as unsettled failures only
   (requirement 1). Covers P2-6.
10. **Chain counts.** A chain stage `count` equals the number of runs shown for that stage, or is renamed to what it
    really counts. Covers N2.

## 4. Out of scope
- Visual redesign.
- New pages.
- Changing the engine's raw states.

## 5. Acceptance and tests
- **Fixture work items:**
  - done-elsewhere;
  - handed off;
  - merged with failed attempts;
  - auto-approved first run;
  - cancelled chain;
  - limited and requeued.

  Each one gets identical labels from `/api/live`, `/api/work` and `/api/tasks`, and none of them reads "Failed"
  or "Blocked" unless it truly is.
- **Alerts** resolve on each trigger in requirement 4.
- **Limits:** one limit row gives the same state on all four endpoints, and admission agrees.
- **Events:** an event emitted last is listed first.
- **UI:** the vitest suite for the label and colour mapping passes. The existing suites pass, and the gate passes.
- **Live check:** the Haiku watcher runs a pass after the merge, and every finding from passes 1–2 reads FIXED.

## 6. Delivery
| Step | Who |
|---|---|
| Spec review | Sonnet (low effort) |
| Build | fleet Sonnet; requirement 6 first |
| Review | Opus, one round |
| Security review | not needed (display and accounting; requirement 6 is reviewed by Opus) |
| Final | Fable, delta green light |
| Check | Haiku watcher pass |
| Summaries and commit messages | local qwen3:8b |

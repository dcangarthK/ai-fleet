# Spec: dashboard truth (Part F)

Status: draft v1, 2026-11-12. The owner asked for it to be queued for fleet Claude; the build starts after Part E
merges. Evidence: the Haiku watcher's passes 1–2 (task t-620955b7), inlined in section 8 so this spec stands alone.

## 1. Problem
The console tells the owner things that aren't true:
- Finished work shows as failed.
- The same task reads differently on different pages.
- Alerts stay open after the work is resolved.
- The budget page disagrees with the resources page about which accounts are limited.

The owner reads the dashboard to decide what needs them, so every false "Failed" or "Blocked" costs their attention.

## 2. Principle
**One derived display state per task and per work item, computed in one place and used by every API and page.**
That place is the existing `tasks.live_display` (`master/controller/tasks.py:472`), which wraps
`lineage.display_status` (`master/controller/lineage.py:145`) and adds retry-child and limit-state handling. Part F
extends `tasks.live_display`; it does not add a second deriver. Raw `state` stays in the data for the engine. Pages
never re-derive labels or colours from `state`.

### 2.1 State → label → tone, with precedence
Tones are the ones the code already emits (`lineage.py`, `tasks.py:487-496`): `running`, `ok`, `paused`, `attention`
(the only red), `resolved` (shown with a tick), `muted` (grey). The first matching row wins:

| # | Condition | Label | Tone |
|---|---|---|---|
| 1 | Work item outcome `merged` | "Merged · <commit>" | ok |
| 2 | Resolution `done_elsewhere` | "Done elsewhere · <commit>" | resolved |
| 3 | Resolution `handed_off` | "Handed off → <task>" | resolved |
| 4 | Resolution `dismissed` | "Dismissed" | muted |
| 5 | Later successful attempt in the same work item | "Superseded by <task>" | muted |
| 6 | Failed attempt with a retry child | "Retried as <task>" | resolved |
| 7 | Denied first run later auto-approved and resumed | "Auto-approved and resumed" | muted |
| 8 | `blocked` or `awaiting_approval` now | "Blocked: needs approval" | attention |
| 9 | Limit on record and reset in the future | "Paused: limit, resets T" | paused |
| 10 | Limit reset passed, no resume yet | "Limit cleared: resume or hand off" | attention |
| 11 | `interrupted` | "Interrupted" | attention |
| 12 | `failed`, none of the above | "Failed: <reason>" | attention |
| 13 | `cancelled` | "Cancelled" | muted |
| 14 | `review` | "Ready for review" | ok |
| 15 | `done` | "Done" | ok |
| 16 | `queued`, `starting`, `working` | "Queued", "Starting", "Working" | running |

Precedence rule: settled outcomes (rows 1–7) beat the raw state, so a task with `state=failed` never shows red once it
is settled. Rows 8–12 apply only to unsettled tasks. Live chain stages and alerts take their label and tone from this
table via the attempt's `display`, never from `state`. The UI maps `tone` to a colour in one place
(`ui/src/lib/data.ts`, `Jw` map in the built bundle) and falls back to `display`, not `state`, where it has one.

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
4. **Alerts close.** Decision: alerts are **computed, not persisted**. `live.alerts()` (`master/controller/live.py:498`)
   already rebuilds the list on every call from `events` rows, checking each task alert against the task's current
   state (`_task_alert_open`, `live.py:483`) and each `budget.limit` alert against the account's current limit state.
   No alerts table exists and none is added. Each alert gets a computed `state: open | resolved` and `resolved_by`
   (`settled`, `superseded`, `limit_reset`, `node_recovered`), derived in that function; today `state` is copied from
   event data and is null for task alerts (F5). `_task_alert_open` uses the settled test from the table in 2.1.
   Live shows open alerts by default and resolved ones behind a toggle (`include_resolved=1`). Covers F5 and P2-4.
5. **Re-queues link.** Every task the orchestrator, a handoff or a reroute creates in place of another carries
   `parent_id` and a `link`. Today `LINKS` is `retry, resume, review, fix, merge, check` (`lineage.py:12`) and
   `POST /api/tasks` rejects anything else (`tasks.py:1108`). Reconcile: a handoff or reroute is a `retry` of the same
   work on another account or node, so it reuses `retry`, with the reason in a new `link_reason` field
   (`retry|handoff|reroute|requeue`). `handed_off` stays a resolution kind (`lineage.py:14`), not a link. No new
   `LINKS` values are added, so `CHAIN_LINKS` (`lineage.py:22`) and chain grouping are unchanged. A re-review (N4)
   uses `review`. `POST /api/tasks` gains `replaces: <task id>`: it
   sets the link and resolves the old task as `handed_off`. Covers F1 and N4.
6. **One source for account limits.** `/api/budget/status`, `/api/fleet`, `/api/resources` and `/api/live` all
   compute limit state from one helper, per **(account, model key)**, honouring `limit_scope` (`account` limits every
   key on that account, `model` only that key). This is a data correctness bug, not only display: admission must use
   the same helper. The test is that a limit on the fleet account shows identically on all four and admission
   refuses accordingly. Covers P2-1.
7. **Events feed is newest-first and current.** `/api/events/list` returns the newest N, newest first, with paging
   (`before=<id>`). If recording stopped, find out why and fix it. The test is that an event emitted now is first in
   the list. Covers P2-3. **Compatibility:** today the endpoint is `event_list(after=0, limit=200)`, returning events after
   an id in ascending order (`tasks.py:1788`), and the UI calls `after=0&limit=1000` (`ui/src/lib/data.ts:29`). Once
   events pass 1000 the UI sees only the oldest 1000, a likely cause of P2-3 (UNVERIFIED until reproduced). Keep
   `after=<id>` exactly as is (ascending, for incremental consumers). When `after` is omitted, or `before` is given,
   return newest first. Change the UI call to omit `after`. The response shape (a list of event dicts) is unchanged.
8. **Orchestrator presence survives restarts.** Presence is derived from requests carrying `X-Fleet-Session`.
   After a controller restart, Live shows the last known presence as "stale since T" instead of empty, until a new
   request refreshes it. Covers P2-2.
9. **Usage counts are defined.** `/api/usage` states its window and counts `failed` as unsettled failures only
   (requirement 1). Covers P2-6.
10. **Chain counts.** A chain stage `count` equals the number of runs shown for that stage, or is renamed to what it
    really counts. Covers N2.

11. **Codex limits come from Codex.** The runner reads the newest `rate_limits` record in the Codex session logs
    (`$CODEX_HOME/sessions/**/rollout-*.jsonl`: `primary` = 5-hour window, `secondary` = weekly, each with
    `used_percent` and `resets_at`) and reports it on `/health`. This is the same data `codex /status` shows.
    The budget page shows both windows, and admission uses them. A Codex error message only marks the model
    limited until the next fresh `rate_limits` record; a reset time parsed from error text that has already passed
    must never roll forward to the next day. Test: a parsed "try again at 1:47 PM" seen at 15:00 does not limit
    Codex until tomorrow. Covers the false 22-hour Codex limit on 2026-11-12 (t-54eebb94).

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
  - limited and requeued;
  - `limit_resets`: an owner reset (`limit_resets` row, `live.py:570`) clears the limit on all four endpoints and
    closes the `budget.limit` alert;
  - `interrupted`: shows "Interrupted" (attention) until settled, then follows the settled label;
  - `hidden`: a hidden work item or task keeps the same label where it is shown, and its alerts do not count as open;
  - batch performance: `live_display` over a batch (for example 300 tasks, `/api/tasks?limit=300`) does not run a
    query per row. Load retry children, usage-limit rows and the limit snapshot once per request. The test asserts a
    fixed query count independent of row count.

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
| Build | fleet Sonnet; order below |
| Review | Opus, one round |
| Security review | not needed (display and accounting; requirement 6 is reviewed by Opus) |
| Final | Fable, delta green light |
| Check | Haiku watcher pass |
| Summaries and commit messages | local qwen3:8b |

## 7. Build order
Each step lands with its tests before the next starts:
1. The state → label → tone matrix (section 2.1) as a table-driven test over fixture tasks, written first.
2. Requirement 6: one limit helper per (account, model key). It is a correctness bug that touches admission, so it goes first.
3. Requirements 1–3: settled outcomes, superseded attempts and one vocabulary, all through `tasks.live_display`.
4. Requirement 5: re-queue links and `replaces`.
5. Requirement 4: computed alert state, which depends on the settled test from steps 3–4.
6. Requirements 9–10: usage counts and chain counts.
7. Requirements 7–8: events feed and orchestrator presence.
8. Requirement 11 (Codex limits) is built alongside requirement 6, since both change how limit state is read.

## 8. Evidence from the watcher (passes 1–2, 2026-11-12)
Read-only GETs of `/api/live`, `/api/tasks`, `/api/work`, `/api/budget/status`, `/api/resources`, `/api/fleet`,
`/api/events/list` and `/api/usage`. Findings, by id:
- **F1 (medium):** t-2cb99fe4 reads "Paused: limit, resets 1:47 PM" on work and tasks, but `failed` on the Live chain,
  with `task.failed` (710) and `budget.limit` (711) alerts open. Its replacement t-54eebb94 had `parent_id=None` and
  `link=None`, in its own root.
- **F2 (medium):** overnight lead roots t-3a9b8dbd and t-b2886783 read `failed` on Live chains, "Cancelled" on work and tasks.
- **F3 (low):** Live chains say `done`; work says "Ready for review" for the same roots (for example t-c3a35126, t-203f0576).
- **F4 (low/medium):** about 8 build and fix items show run 1 as "Blocked: needs approval" (`failure_reason=denied`)
  followed by a successful rerun (for example t-62f37b18, t-d23c90ef, t-620955b7). It reads like an open approval request.
- **F5 (low):** alerts 699 (t-85afa645), 710 and 711 have `state=null` after the work was resolved; node disk alerts
  correctly end `cleared`.
- **F6 (low):** t-62f37b18 and t-85afa645 are `state=failed` with display "Done elsewhere ...". Badge colour follows
  `state`, so they may render red (UNVERIFIED; JSON only, not the rendered UI).
- **F7 (info):** ollama drafts t-c3ef4f30 and t-6743a975 are genuinely failed; whether superseded is UNVERIFIED.
- **N1:** t-d23c90ef (`review`) is "Ready for review" though its root is "Merged 278364f". **N2:** chain stage `count=2`
  for t-62f37b18, which has five runs. **N3:** review roots t-68e85d0d and t-d3308bf9 stay "Ready for review" with
  `outcome=None` after the merge. **N4:** t-1c80e795 has `parent_id` but `link=None`.
- **P2-1 (high):** `/api/budget/status` and the `/api/fleet` budget block say fleet Fable, Sonnet and Haiku are `ok`,
  while `/api/resources` and `/api/live` say fleet Claude is limited until 1:20 PM (`limited_until=1794482400`). Only
  Opus showed `limit_scope=model`.
- **P2-2:** `/api/live` `orchestrator` was `[]` after a controller restart. **P2-3:** `/api/events/list` held 200
  events, all about 25 h old, none from that evening. **P2-4:** old `task.failed` events have no resolution although
  work shows the items merged. **P2-6:** `/api/usage` per-node `failed` counts (claude 9, codex 1, ollama 2) did not
  reconcile with `/api/tasks` failed-state counts (14, 5, 2); window and definition were not stated.

# SPEC: task lineage, failure reasons and honest status

Status: **approved by the owner** (2026-11-11). Builder: Claude/Sonnet via the fleet. Reviewer: a different
agent or model (Codex or Fable).

## 1. Problem

The console shows every failed task in red, forever, including failures that were retried and succeeded:

- `Overview.tsx:79` renders `[...working, ...problems.slice(0, 5)]` under **"Working now"**, so failures sit next
  to running work.
- A task's `state` is `failed` whether the agent hit an account limit, the gate failed, the job errored or it was
  a throwaway availability check. On 2026-11-11 every red row on the Overview was an account limit.
- Nothing links an attempt to its retry, its review, its fix round or its merge. `tasks.parent_id` exists
  (`store.py:26`, `tasks.py:486`) but nothing sets it for retries, and work finished outside the fleet (for example in
  the orchestrator's own session) leaves the fleet task `failed` even though the change is merged.

Goal: the owner sees **what is happening now**, **what needs them**, and for each piece of work **how it ended
and how it got there**.

## 2. Concepts

| Term | Meaning |
|---|---|
| **Work item** | One piece of work the owner cares about (e.g. "G6: grounding views"). It is the root task of a lineage. |
| **Attempt** | Any task in a work item: the original build, a retry, a review, a fix round, a merge. |
| **Link** | `parent_id` (the attempt it follows) + `link` (why it exists): `retry`, `resume`, `review`, `fix`, `merge`, `check`. |
| **Failure reason** | Why a task ended badly: `limit`, `gate_failed`, `denied`, `error`, `interrupted`, `cancelled`. |
| **Resolution** | How a failed attempt was dealt with: `resolved_by` (a later attempt in the same work item that succeeded) or `dismissed` (owner/orchestrator said it no longer matters). |
| **Outcome** | The work item's final result: `merged` (with commit), `done`, `abandoned`, or open. |

## 3. Data model (controller, `store.py` migration, additive only)

New nullable columns on `tasks`:

- `link TEXT` in {`retry`,`resume`,`review`,`fix`,`merge`,`check`} or NULL (NULL = root / unknown).
- `failure_reason TEXT` in {`limit`,`gate_failed`,`denied`,`error`,`interrupted`,`cancelled`} or NULL.
- `resets_at REAL` (epoch seconds) when `failure_reason = 'limit'` and the reset time could be parsed, else NULL.
- `resolution TEXT` JSON or NULL: `{"kind": "resolved_by"|"dismissed"|"done_elsewhere", "task_id"?: str,
  "commit"?: str, "by"?: str, "note"?: str, "at": float}`.
- `outcome TEXT` JSON or NULL, root tasks only: `{"kind": "merged"|"done"|"abandoned", "commit"?: str, "at": float}`.
- `hidden INTEGER DEFAULT 0` (set for `link = 'check'`).

Existing rows migrate with all new columns NULL/0; nothing is rewritten. The existing `state` machine
(`tasks.py:43-45`) is unchanged: `failed` stays `failed`; the new fields describe it.

## 4. Behaviour

### 4.1 Failure reason (pure function, `tasks.py`)

`failure_reason(task, job) -> str | None`, applied whenever a task enters `failed`, `blocked`, `interrupted` or
`cancelled`:

| Condition (first match wins) | Reason |
|---|---|
| state `cancelled` | `cancelled` |
| state `interrupted` (runner lost the job) | `interrupted` |
| state `blocked` (denials) | `denied` |
| result/error text matches a usage-limit message (Claude: `You've hit your … limit`; Codex: its usage-limit message; match case-insensitively on "limit" + "reset"/"resets"/"try again") | `limit` |
| a gate was requested and its state is not `passed` | `gate_failed` |
| anything else with `exit_code != 0` or a start error | `error` |

For `limit`, parse a reset time when present ("resets 2:30pm (Africa/Johannesburg)", "try again at 1:00 PM") into
`resets_at` in the given (or local) timezone, choosing the next occurrence after `ended_at`. Unparseable → NULL.

### 4.2 Links

- `POST /api/tasks/{id}/message` that **resumes** a settled task keeps the same task (no new row); add an event
  `task.resumed` with the previous `failure_reason`. (The attempt history is the event log; see 4.6.)
- `POST /api/tasks` accepts optional `parent_id` and `link`. Validation: `parent_id` must exist; `link` must be in
  the allowed set; `link` without `parent_id` is 400 (except `check`).
- Pipelines set links themselves: the build task is the work item root (`link` NULL), each review task gets
  `parent_id = build_task`, `link = 'review'`; a rebuild round reuses the build task (already true).
- `link = 'check'` sets `hidden = 1`.

### 4.3 Automatic resolution

When a task ends successfully (`review` with gate passed or no gate, or `done`), every earlier attempt in the same
work item (walk `parent_id` to the root, then all descendants) that is `failed`/`interrupted`/`blocked` with no
resolution and the **same purpose** gets `resolution = {"kind": "resolved_by", "task_id": <this task>}`.
Same purpose = same `link` value, or the failed task is the parent of the successful one with `link` in
{`retry`,`resume`,`fix`}. A failed review is never resolved by a successful build, and vice versa.

### 4.4 Manual resolution and outcomes (orchestrator / owner)

- `POST /api/tasks/{id}/resolve` body `{"kind": "dismissed"|"done_elsewhere", "commit"?: str, "by"?: str,
  "note"?: str}`. Allowed only from `failed`, `interrupted`, `blocked`, `cancelled`, `review`. `done_elsewhere`
  requires `by` (e.g. "Sonnet 5.5 (orchestrator session)") and, if `commit` is given, it must be a 7–40 char hex
  string. Admin scope only. Emits `task.resolved`.
- `POST /api/tasks/{id}/outcome` body `{"kind": "merged"|"done"|"abandoned", "commit"?: str}` on a root task.
  `merged` requires `commit`. Emits `task.outcome`. Setting `merged`/`done` on the root also marks the root
  `done` if it is in `RESUMABLE` (reusing `complete`).
- Neither endpoint ever approves a `blocked`/`awaiting_approval` task or changes permissions; `dismissed` on a
  `blocked` task leaves it `blocked` (it only hides it from "Needs attention").

### 4.5 Display status (pure function, shared by API and UI)

`display_status(task) -> {"tone": "running"|"ok"|"paused"|"attention"|"resolved"|"muted", "label": str}`:

| Task | Tone | Label |
|---|---|---|
| `queued`/`starting`/`working` | running | Working / Queued |
| `awaiting_approval` | attention | Needs your approval |
| `blocked`, unresolved | attention | Blocked: needs approval |
| `failed` + `limit`, unresolved | paused | Paused: limit, resets 2:30 PM (or "Paused: usage limit") |
| `failed`/`interrupted` other reasons, unresolved | attention | Failed: gate / Failed: error / Interrupted |
| any with `resolution.kind = resolved_by` | resolved | Resolved by `<title of task>` |
| any with `resolution.kind = done_elsewhere` | resolved | Done elsewhere by `<by>` (`<commit>`) |
| any with `resolution.kind = dismissed` | muted | Dismissed |
| `review` | ok | Ready for review |
| `done` | ok | Done |
| `cancelled`, unresolved | muted | Cancelled |

`GET /api/tasks` and `GET /api/tasks/{id}` include `link`, `failure_reason`, `resets_at`, `resolution`, `outcome`,
`hidden`, `display` (the result above) and `root_id`.

### 4.6 Work items API

`GET /api/work?limit=50&include_hidden=0` returns one entry per root task, newest activity first:
`{root_id, title, outcome, display, updated_at, attempts: [{id, title, agent, model, link, state, display,
failure_reason, started_at, ended_at}]}`. Attempts are ordered by `created_at`, plus synthetic entries from
`task.resumed` events so a resumed task shows each run ("run 1: paused (limit)", "run 2: gate passed").
`display` of the work item = the outcome if set, else the latest attempt's display.

### 4.7 Console

- **Overview:**
  - "Working now" lists only running and queued tasks.
  - New "Needs attention" lists `attention`-tone tasks only (unresolved real failures, blocked, awaiting
    approval), with the reason.
  - A small "Paused" line lists limit-paused tasks with reset times.
  - Hidden (`check`) tasks are never shown.
- **Tasks page:**
  - The default view is **Work items** (one row per root with its outcome/status pill and agent/model of the
    latest attempt). Expanding a row shows the attempt timeline, e.g.:
    `Sonnet build · Paused: limit (2:30 PM) → resumed · gate passed → Fable 5.2 review · changes requested →
    fix · gate passed → Fable 5.2 review · approved → merged ec2ab20`.
  - A toggle "All attempts" shows the current flat list, with the new status pills.
  - "Show checks" reveals hidden tasks.
- Task detail: shows reason, reset time, resolution (with link to the resolving task), and buttons
  "Mark done elsewhere…" and "Dismiss" (admin only).
- Colours: running = existing working colour; ok = green; paused = amber; attention = red; resolved = grey with
  ✓; muted = grey. Phone layout: no horizontal page scroll.

### 4.8 Notifications

- `limit` failures do not use the failure notification; one "paused until …" notice per work item at most.
- Resolution and outcome events do not notify by default.

### 4.9 Out of scope (later)

Automatic resume at `resets_at`; merging work items across repos; editing lineage after the fact
(beyond resolve/outcome).

## 5. Acceptance criteria

1. Overview "Working now" never contains a task whose state is not queued/starting/working.
2. A Claude task ending with "You've hit your 5-hour limit · resets 2:30pm (Africa/Johannesburg)" gets
   `failure_reason = limit`, `resets_at` = next 14:30 SAST after `ended_at`, and shows amber "Paused".
3. A task whose requested gate fails gets `gate_failed`; a denied tool → `denied`; a lost job → `interrupted`.
4. A failed build resumed via `/message` that then passes its gate shows as one work item whose status is
   "Ready for review", with both runs in its timeline.
5. A failed task followed by a `retry` child that succeeds is marked "Resolved by <child>" automatically; a failed
   **review** is not resolved by a successful **build**.
6. `POST /resolve {"kind":"done_elsewhere","by":"…","commit":"ec2ab20"}` on a failed task shows "Done elsewhere"
   and removes it from Needs attention; invalid commit → 400; non-admin token → 403.
7. `POST /outcome {"kind":"merged","commit":"…"}` on a root shows the work item as "Merged <commit>" and marks the
   root `done`.
8. Neither new endpoint can move a task out of `blocked` or `awaiting_approval`.
9. `link = 'check'` tasks never appear on Overview or the default Tasks view.
10. Existing tasks (all new fields NULL) still list and render; existing tests are not weakened.
11. `scripts/gate.sh`, `npx tsc -b`, `npx vitest run` and `npx vite build` pass; rebuilt `dist` is committed.

## 6. Test cases

Python (`tests/test_tasks_lineage.py`, pure functions plus API through the existing test client):

- `failure_reason`: cancelled; interrupted; blocked/denied; Claude limit text; Codex limit text; gate failed;
  exit_code 1 without limit text; start error.
- `parse_reset`: "resets 2:30pm (Africa/Johannesburg)" before and after 14:30 local (next day rollover);
  "try again at 1:00 PM"; garbage → None.
- `display_status`: one case per table row in 4.5.
- Auto-resolution: retry child success resolves parent; fix child success resolves parent; review failure not
  resolved by build success; already-resolved task untouched; resolution walks to the root across two levels.
- API: create with `parent_id`+`link`; unknown parent → 400; bad link → 400; link without parent → 400 (except
  check); `/resolve` happy path, bad commit, wrong state, blocked stays blocked; `/outcome` merged requires commit,
  sets root done; `/api/work` groups attempts, orders them, includes resumed runs, hides checks unless asked.
- Migration: an existing DB without the new columns opens, migrates, and old rows read back with NULLs.
- Pipelines: review tasks are created with `parent_id = build_task`, `link = review`.

Vitest (`ui/src/lib/lineage.test.ts`):

- Status pill tone/label mapping for each `display.tone`.
- Timeline formatting from an attempts array (including resumed runs and a merged outcome).
- Overview filters: working vs needs-attention vs paused sets from a mixed task list.

## 7. Delivery

One build (Sonnet 5.5 or Claude), gate `fleet-gate`, reviewed by a different agent/model (Codex or Fable),
merged by a Claude task. After merge: restart the controller, then the orchestrator back-fills today's records
with `/resolve` and `/outcome` (G4 → merged `f789cfc`, fixes batch → merged `fcc66d2`, G6 → merged `ec2ab20`;
the availability checks → dismissed).

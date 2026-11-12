# Spec: stalled-task detection and rerouting (Part E)

Status: draft v1, 2026-11-12. Needs the owner's OK before building.

## 1. Problem
Codex task `t-253101a7` stayed "working" for about 7.5 hours while its process was dead.
- It sent no heartbeat (`heartbeat_at` was null), and the runner returned 500 for its log.
- Its worktree had no file changes for over 90 minutes.
- Sonnet, Opus, Fable and Haiku were all idle the whole time.

Part D's heartbeats cover **nodes**. Nothing covers a **task** that is "working" but dead or idle. So nothing moves
that work to free resources, and the orchestrator found out only when the owner asked.

The owner's requirement: "the leader sees all the resources (every account, every node, every model) and moves
work when a model is struggling."

## 2. Goals
1. Every running task has a measured **activity signal**: the process is alive, and output or files have changed
   recently.
2. The controller **detects a stall** within minutes, not hours.
3. It **saves the partial work** and **reroutes** it to the best free capable slot anywhere in the fleet: any
   account, node or model the rules allow. Reuse Part C's handoff machinery; don't build a second mechanism.
4. Live shows the stall, the reason and the move. The orchestrator gets an event and never has to poll.
5. All of it obeys the budget rules: admission, CLAUDE.md rule 0 (Fable never builds), independence and
   concurrency.

## 3. Definitions
- **Activity** for a job: the latest of the job's stdout/stderr growth, its worktree file mtimes (excluding
  `.git/`), and its session transcript growth when the runner knows the path. The runner measures it; the controller
  never reads node files.
- **Dead:** the runner reports the job's process as not alive, but the task is still `starting` or `working`.
- **Idle:** the process is alive, but the last activity is older than `idle_after` (default 20 min; a per-kind
  override in budget rules, e.g. `benchmark` 60 min).
- **Unreachable job:** the runner answers `/health` but errors on that job (as with t-253101a7) for 3 consecutive
  polls.
- **Overrun:** wall time exceeds `max_wall` for the kind (default: build 3 h, fix 90 min, review 45 min). An
  overrun alone only raises an alert; the task is not stalled while it is still active.

## 4. Runner changes (`nodes/runner.py`)
- Each job record gains `alive` (bool), `last_activity_at` (unix seconds) and `activity_src`
  (`"output"|"files"|"session"`).
  - Output growth is measured from the job's own log file size.
  - File activity comes from a bounded scan of the worktree at most every 60 s per job: at most 20,000 entries,
    skipping `.git/`, `node_modules/`, `.venv/` and `dist/`. Above the cap, `activity_src="output"` only.
- These fields go in the cached `/health` jobs list (Part D §4.1), so heartbeats carry them at no extra cost.
- `GET /jobs/{id}` must not 500 on a job whose process is gone. It returns the record with `alive:false`. This
  fixes the root cause seen on t-253101a7; the build investigates that failure first and adds a regression test.
- `python3 nodes/build.py` re-embeds the runner in the onboarding scripts.

## 5. Controller changes
### 5.1 Detection (`live.sweep_once`, existing 5 s loop)
- For each `starting|working` task on an online node, classify it as `ok | dead | idle | unreachable | overrun`
  from the latest runner data.
- A task's data is used only while it is fresh: under 60 s old, the same age rule as warm models (Part D §12.3).
  An offline node is Part D's case and is not a stall.
- Startup grace: no stall verdicts for 2 × `idle_after`'s check interval after a controller restart, and none
  before a task has been running for 5 min.
- Store the verdict in `tasks.stall` (JSON: `{kind, since, last_activity_at, detail}`). Emit `task.stalled` once
  per transition and `task.recovered` if activity resumes.

### 5.2 Response
| Verdict | Action |
|---|---|
| dead, unreachable | Checkpoint now and reroute (5.3) |
| idle | Send one nudge via `/message`, through admission: "You have made no progress for N min. Report status and continue, or stop and summarize." If still idle after a further `idle_after / 2`, stop the job, checkpoint and reroute. |
| overrun | Alert only (Live and SSE); the orchestrator decides |

- Stopping a job uses the existing runner cancel path: SIGTERM, then SIGKILL after 15 s.
- The task becomes `failed` with `failure_reason="stalled"` and its `stall` record. It is never silently
  `cancelled`.
- `awaiting_approval` and `blocked` tasks are **never** stalled; they wait for the owner.
- Pipeline-owned tasks (`handoff.pipeline_owned`) follow the same rule, but the pipeline lock order from Part C
  applies.

### 5.3 Reroute: reuse Part C
- A stalled failure is a new trigger for `handoff.handle_limit`'s path, renamed and generalized to
  `handle_handoff(task_id, reason)`.
  - **Checkpoint:** the existing checkpoint commits the worktree to its branch. The new attempt is a `retry` in a
    fresh worktree from that branch. Partial work is never lost.
  - **Slot choice:** `candidate_slots()` over `resources()`, i.e. every node, every account profile and every
    model, with these rules:
    - exclude the stalled `(agent, model, account)`;
    - prefer an idle slot, then the cheapest capable tier (budget table: build → Sonnet before Opus/Codex);
    - Fable is never chosen for build, fix or test kinds (rule 0);
    - respect independence: never a reviewer of the same lineage;
    - respect limits and concurrency from Part B admission.
  - **Prompt:** the existing `handoff_prompt`, with the reason "the previous attempt stalled (<kind>) after <N>
    min" and the diff stat of the checkpoint.
- **Limit:** at most 2 stall handoffs per work item (`handoffs_in_chain`). The third stall escalates: the task is
  marked `escalated`, Live raises a red alert, and nothing else runs.
- When no slot qualifies, the task waits with reason "stalled; no free slot" and the sweep retries each pass, the
  same level-triggered pattern as Part D §12.2.

### 5.4 Resource view for the orchestrator
- `GET /api/resources` (view scope) returns one row per slot: node, agent, model family, account label, state
  (`free|busy|limited until T|offline|signed out`), running/max, and today's tokens.
- This is the existing `handoff.resources()` exposed read-only, plus per-slot running counts. No new probes, and no
  paid availability checks (rule 7).
- `fleet_status` (the MCP tool) includes it, so an orchestrator in any session sees the whole fleet.

### 5.5 Allocation when spawning
- `spawn_task` may omit `model`. The controller then picks the cheapest capable free slot for the `kind`, using
  the same ranking as 5.3. An explicit `model` or `profile` still wins.
- Live shows "allocated by controller" on such tasks.

## 6. Live page
- Task rows show a stall badge (`dead`, `idle 23m`, `unreachable`, `overrun`) and, after a reroute, "moved to
  <model>@<account> on <node>: stalled".
- The alerts list gains `task.stalled`, `task.escalated` and `task.overrun`.
- A new Resources section shows the 5.4 table.

## 7. Security
- No new public endpoints. `/api/resources` is view scope.
- Runner activity fields are parsed by the existing `/health` parser; unknown fields are dropped, and timestamps
  are clamped to `[started_at, now + 5 s]`.
- The worktree scan never follows symlinks and never leaves the worktree root.
- The nudge and reroute pass full admission, so no path bypasses limits, kinds or approval. This closes the class
  of bug in the earlier Codex review (resume without admission) for these new paths.

## 8. Acceptance criteria
1. A job whose process dies is detected within 2 min and rerouted with its partial work on a new branch.
2. A job idle for `idle_after` is nudged once, then stopped and rerouted if still idle.
3. The new attempt runs on a different `(agent, model, account)`; Fable is never chosen for build/fix/test.
4. `awaiting_approval` / `blocked` tasks are never touched.
5. A third stall escalates and stops.
6. `GET /jobs/{id}` never 500s for a dead job.
7. `/api/resources` lists every slot in the fleet, including limited and offline ones.
8. `spawn_task` without `model` is allocated to the cheapest free capable slot.
9. Live shows badges, moves and the resources table. The existing UI tests still pass.
10. The gate passes. A real test kills a live job's process and watches the reroute happen end to end.

## 9. Test cases
- Runner: activity from output growth, from file changes, cap fallback, symlink not followed, dead job returns
  `alive:false`.
- Controller: each verdict from fixture data; stale data ignored; grace windows; nudge then reroute; slot ranking
  (excludes stalled slot, rule 0, independence, limits); chain limit escalation; approval states untouched; event
  emission once per transition; races (the task finishes while a reroute is claimed: the conditional UPDATE wins,
  no double run).
- API: `/api/resources` shape and scope; spawn without model.
- UI: badge, moved line, resources table (vitest).
- Real (opt-in `FLEET_REAL_STALL_TEST=1`): start an `ollama` job, kill its pid, and assert the reroute within 2 min.

## 10. Delivery, using the whole fleet
The controller allocates by `kind` (5.5 is built first, so the build itself uses it where it can). One writer per
worktree.

| Step | Work | Who |
|---|---|---|
| 0 | Spec review | Fable (review only) |
| 1a | Runner part (§4) + tests | Sonnet, fleet account |
| 1b | Controller detection, reroute, resources, allocation (§5) + tests | Sonnet, spare account (parallel; contract = §4 fields) |
| 1c | Live UI (§6) | Haiku or Sonnet, whichever is free, after 1b's API lands |
| — | Test drafts, commit messages, summaries | local `coder` / `fast` |
| 2 | Review of each diff | Opus (different from the Sonnet builders) |
| 3 | Fixes | the original builder; delta re-check by the same reviewer |
| 4 | Security review (it kills processes and moves work) | Codex, once |
| 5 | Final green light | Fable |
| 6 | Merge, restart, real stall test | Sonnet |

If a step's model is limited or stalls, the orchestrator moves it by hand by these same rules until Part E is live.

## 11. Out of scope
- Moving a live session between nodes.
- Automatic retries of reviews (a stalled review is rerouted, never duplicated).
- Speculative parallel builds of the same task.
- Paid availability probes.

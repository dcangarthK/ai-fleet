# Spec: stalled-task detection and rerouting (Part E)

Status: draft v2, 2026-11-12, after Fable's spec review (t-c3a35126, request changes). §12 lists what changed and
overrides earlier sections where they differ. Needs the owner's OK before building.

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

## 12. Changes in v2 (Fable review t-c3a35126); these override §3–§10 where they differ
**Step 0 of the build: root cause.** Find out why t-253101a7 outlived the runner's own `max_job_minutes` (120)
kill, and why `/jobs/{id}` returned 500 (suspect: the monitor thread was lost on a runner restart). Fix it, with a
regression test, before anything else.

1. **Ranking (fixes "doesn't allocate across the fleet").** A new `rank_slots(kind, exclude, task)` over
   `resources()`. It replaces `candidate_slots` for stalls and for `agent="auto"`, and is shared with
   `admit(reroute=…)` so there is one ranking. The filters are:
   - `kind_allowed` for the kind, plus a hard-coded rule 0 check (Fable is never chosen for build, fix or test),
     with its own test;
   - free concurrency, not limited, not signed out;
   - independence: for review and test kinds, exclude every model in `build_attempts()`, and carry `review_of`
     over.

   It orders by tier (cheap → standard → premium, per the budget table for the kind), then idle first. Cross-vendor
   is **allowed for stalls** (a stalled Codex build may go to Sonnet). The `limits.fallbacks` list is not used.
   - **Nodes:** v1 reroutes to slots **on the same node**, because the checkpoint branch lives there. All paid
     accounts are on the master today, so this covers the real case. Moving across nodes, which needs a way to
     transport the branch, is out of scope.
2. **Liveness.** `alive` = the agent process **or** the gate process is alive. Gate output counts as activity.
   Ollama jobs have no gate phase of their own.
3. **Dead is already handled.** A dead pid already becomes `lost → interrupted`. Both `interrupted` and
   `failed(stalled)` trigger a reroute. "Dead" in §3 then means only that the runner still says `running` while
   the pid is gone, which the root-cause fix should make impossible.
4. **Idle needs all three:** no output, no file changes **and** no CPU-time growth in the job's process group
   (`ps -o time` summed over the group; `psutil` is not required). Then `idle_after = clamp(0.25 × job timeout,
   15 min, 40 min)`.
5. **Nudge, made possible.**
   - For Claude and Codex: under the task lock, cancel the job, then transition `working → failed`
     (`failure_reason="stalled"`, a new value in `lineage.failure_reason`) with usage recorded. Then one
     `tasks.resume()` on the same session, which is admission-checked, with the nudge text. `stall.nudged_at` is
     persisted, so it nudges once even across restarts.
   - Ollama: no session, so it goes straight to reroute.
   - A second stall after the nudge leads to reroute.
6. **Stop without being cancelled.** All of this happens under the task lock:
   - cancel, then read the job the runner returns;
   - if the job already exited cleanly, settle it normally;
   - otherwise set `failed(stalled)`, not `cancelled`, so the poll mapping can't win.

   The work-item lock comes after, in Part C's lock order.
7. **A stall branch of its own.** `handle_handoff(task_id, reason)` has a stall branch that never calls
   `schedule_wait` and never says "usage limit". It gets:
   - a `reason` parameter for the checkpoint commit message and `handoff_prompt`;
   - its own wait state, "stalled; no free slot", retried each sweep pass;
   - a new budget rule `on_task_stall` (default `reroute` for every kind, reviews included).
8. **Runner `/health` gains `jobs`.**
   - Each entry is `{id, state, alive, gate_alive, idle_s, activity_src, cpu_s}`, at most 32 jobs and about 160 B
     each (well inside the 64 KB heartbeat cap).
   - `idle_s` is computed on the **runner's** clock, so clock skew doesn't matter.
   - The controller's health parser is extended explicitly for this field.
   - A runner that doesn't send `jobs` (an old node) gives the verdict `unknown`, never dead or idle.
9. **Safe reroute.** Reroute only after the runner confirms the process group is gone **and** the checkpoint
   commit succeeded.
   - If the checkpoint fails (for example 409 on a leftover `index.lock` or mid-rebase), or the runner can't
     confirm the process is gone: set `failed(stalled)` with `stall.escalated=true`, leave the worktree untouched,
     raise a red alert, and let the owner or orchestrator act.
   - It never deletes lock files.
10. **Escalation.** It is not a new state: `failed` + `stall.escalated=true` + the `task.escalated` event. The cap
    is 2 **stall** reroutes per work item, counted separately from limit handoffs (`stall_handoffs_in_chain`).
11. **Timeouts.** Overrun means 80% of the runner's `max_job_minutes` (or the task's timeout) has passed. It raises
    an alert only, and the runner's own kill stays the hard stop. The runner's cancel behaviour is kept as it is
    (SIGTERM, then SIGKILL after about 1 s).
12. **Auto allocation.** `agent="auto"` (and `model` omitted) asks the controller to pick with `rank_slots`. A plain
    omitted `model` keeps today's meaning: the node's default.
13. **Pipelines.** For pipeline-owned tasks, the pipeline poll calls `handle_handoff(…, "stalled")`. A stall is not
    a gate-failure round, and it doesn't use up one of the 3 fix rounds.
14. **Resources.** `GET /api/resources` already exists (`handoff.py:650`). It gains tier, `kind_allowed`
    summaries and idle state, and `fleet_status` includes it. No probes.
15. **Grace.** No verdicts for 120 s after a controller restart, nor during a task's first 5 min.
16. **Delivery changes.**
    - Split out step 1b0: `agent="auto"` and `rank_slots`, built first.
    - The real stall test (step 6) runs on **Haiku**, not on the builder's model, so no independence waiver is
      needed.
      - It uses two Ollama-capable slots, a small model and `qwen2.5:3b`, so a reroute target exists.
      - Alternatively, it kills a Claude Haiku job and expects the reroute to Sonnet.
    - Codex step 4 is the final **whole-branch** review. Fable step 5 is a **delta** green light on Codex's fixes
      only.
    - The summary reports spend: models, task counts and limit hits.

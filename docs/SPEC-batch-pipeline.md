# SPEC: batch pipeline (one controller call per batch)

Status: draft, for owner OK before build. This change is the spec only; no code.

## 1. Problem and goal

Today the orchestrator chat drives a batch one tool call at a time: spawn or run_pipeline, wait_tasks, task_status,
message_task, complete_task. Each call is a turn, and each turn re-reads the startup context (about 35-90k tokens).
Turns are the cost, not the tool results. Recommendation 2 of docs/MCP-TOKEN-RESEARCH-2026-11-17.md section 4 says
to run a whole batch inside the controller and return one summary. That research doc is in commit dfe524a; it is
not on main.

Goal: a batch of N work items costs the orchestrator about 5 turns (open, submit, wait, optional extra waits, close),
whatever N is. The controller runs the build, gate, review and fix loop for every item.

## 2. What exists now

- `run_pipeline` (master/mcp/server.py:643-664) posts one item to `POST /api/pipelines`. It takes one spec, one repo,
  one gate, a builder of `claude` or `codex`, and `max_rounds` (default 3). The MCP tool does not expose a model.
  The controller does accept `builder.model` and `reviewer.model` (master/controller/controller.py:1485-1497;
  master/controller/pipeline.py:23-58).
- Pipeline states are building, reviewing, resolving, review_waiting, ready, escalated, failed and cancelled
  (pipeline.py:9 for the terminal set). `ready` is terminal and the pipeline never merges (pipeline.py:399-402).
- Gate bounces: `GATE_BOUNCES = 3` (pipeline.py:12). A fourth gate failure escalates (pipeline.py:259-263).
- Review rounds: `_send_back` (pipeline.py:145-163) increments `round` and escalates when `round > max_rounds`.
  Trusted findings are kept in `budget.last_findings` for the delta re-review (pipeline.py:160-162).
- **Round arithmetic gap.** `round` starts at 1. With `max_rounds=3` the sequence is build(1), CHANGES, build(2),
  CHANGES, build(3), CHANGES, escalate. That is two fix rounds, not three. The policy in section 3 asks for three fix
  rounds, so the batch submits `max_rounds = max_fix_rounds + 1` (decided, section 10 item 1).
- `set_chain` (master/mcp/server.py:553-557; master/controller/live.py:201-238) is descriptive only. It never starts,
  admits or advances anything, so it cannot drive a batch. It stays as the Live label.
- `pipeline_status` (server.py:668-670) is a read. It does not wait.
- Reviewer independence: `pipeline.new` rejects a same-agent reviewer with the builder's model (pipeline.py:45-50).
  `tasks.check_independence` (master/controller/tasks.py:1493) covers tasks.
- **Blocked gap (verified).** `poll_pipeline` (master/controller/controller.py:2032) forwards a build to `step()` only
  when its state is one of review, failed, interrupted, cancelled or capped (controller.py:2082). `blocked` and
  `awaiting_approval` are not forwarded, and `step()` has no branch for them (pipeline.py:225-298). A blocked builder
  therefore leaves its pipeline in `building` with nothing to act on.
- **Existing approval path.** A task that a run leaves with denied tools becomes `blocked` (tasks.py:959). The
  transition to `blocked` calls `auto_approve_denials` (tasks.py:1092). That function (tasks.py:1265-1300) resumes the
  task with its denied tools when policy `auto_approve` allows the agent and tools (policy.py:71-79). It runs at most
  `AUTO_APPROVE_ROUNDS = 3` times (tasks.py:1262). It emits `task.auto_approved`, `task.auto_approve_held` or
  `task.auto_approve_exhausted`. `awaiting_approval` is set at creation for supervised and full modes
  (tasks.py:1861; handoff.py:948; tasks.py:52), and auto-approve does not act on it. The approve endpoint itself was
  not read in this pass: UNVERIFIED that only the owner can clear `awaiting_approval`; check at build time.
- Reviewers: `poll_pipeline` already treats a `blocked` reviewer as finished and uses its answer (controller.py:2113-2114).
- Model routing: `agent="auto"` exists for tasks (tasks.py:1518; handoff.py:359, via `rank_slots`). There is no
  `model="auto"` for pipelines. That is a gap this spec fills.
- Budget state: `budget.limit_state` (master/controller/budget.py:492) is what `fleet_status` reports. The controller
  uses the same function for routing.
- Waiting: `wait_tasks` (master/mcp/server.py:561-575) is the event-based long-poll pattern to copy (max 1800 s).

## 3. Input

`run_batch(items, policy)` (MCP) posts `POST /api/batches`. One call. It returns `batch_id` and one row per item.

Each item:
- `id`: unique within the batch.
- `title`.
- `spec` (text) or `spec_path`, with optional `spec_sections`. Review prompts carry section numbers, not spec text,
  as `run_pipeline` already does.
- `repo`: absolute path. `gate`: the test command.
- `files`: list of paths the item expects to touch. Used only for overlap checks.
- `builder`: `auto` (default), `claude` or `codex`. `builder_model`: optional.
- `security_sensitive`: bool, default false.
- `depends_on`: optional list of item ids in the same batch.

Policy (all optional; these are the defaults):
- `max_fix_rounds`: 3. Fix rounds after the first build. Submitted to the pipeline as `max_rounds = max_fix_rounds + 1`.
- `gate_bounces`: 3 (the existing `GATE_BOUNCES`).
- `reviewer_must_differ`: true. A reviewer must differ from the builder in agent or model. Codex-built items get a
  Claude reviewer. Claude-built items get a different Claude model, or Codex when `security_sensitive`.
- `model`: `auto`. Routing is in section 4.
- `final_review`: `{agent: "codex", model: "gpt-5.6-terra", effort: "high", once: true}`. One per batch.
- `on_limit`: `wait` (default) or `escalate`. `limit_wait_max`: 6 h. A longer wait escalates.
- `concurrency`: 1 Codex job and 2 Claude jobs per account (CLAUDE.md rule 8).
- `require_trusted`: true (existing pipeline default).

Validation fails the whole submit with 400 if any item is malformed, a `depends_on` cycle exists, a repo path is
relative, or a builder is not `auto|claude|codex`. The batch is not partly created.

## 4. Routing (`model: "auto"`)

Resolved at submit and re-checked at the start of each stage, using `budget.limit_state`:

- Build: Sonnet by default. Opus, or Codex (`gpt-5.6-terra`, medium effort), only when `security_sensitive` or
  the item asks for a hard build. Haiku does not build.
- Review (one reviewer per round, CLAUDE.md rule 2): Codex built, so Claude reviews (Fable or Sonnet; Fable for design
  or security-sensitive items). Claude built, so a different Claude model. Codex reviews only when
  `security_sensitive`, at high effort.
- Fix rounds: the same builder, same session where possible (the `message_task` path), so context carries.
- Re-review: the same reviewer, given only the fix delta and `budget.last_findings`.
- Fable never builds, fixes or tests (CLAUDE.md rule 0).
- A limited model is skipped for the next one in its row. If none is available, the item waits for the earliest
  `resets_at`. It never retries a limited model before its reset (CLAUDE.md rule 7).

## 5. Per-item steps

The controller runs these steps. Each step is a pure function over stored state (new `batch.py`), reusing
`pipeline.new` and `pipeline.step` for the review and fix part.

1. `queued`: waiting for `depends_on`, a concurrency slot, or a file overlap. Items with overlapping `files` run one at
   a time.
2. `building`: spawn the build task (`kind: build`, one writer per worktree). The runner then runs the gate.
3. Gate fails: send the gate text back to the builder (bounce). Up to `gate_bounces` bounces, then `escalated`.
4. `reviewing`: spawn one reviewer (`kind: review`, read-only).
5. Verdict. No actionable findings: `ready`. CHANGES: a fix round (round + 1), then gate, then re-review.
6. Fix rounds exhausted (CHANGES after `max_fix_rounds`): `escalated`.
7. `ready`: the branch waits for the batch final review. It is not merged.

Escalation to the owner. The item stops; other items keep running. The summary says why.
- Gate failed more than `gate_bounces` times.
- CHANGES after `max_fix_rounds` fix rounds.
- Builder `awaiting_approval`. Auto-approve does not cover it, so the item escalates at once with the reason
  `owner approval needed`.
- Builder `blocked` that stays blocked after the controller's auto-approve has finished (its `auto_approve_held` or
  `auto_approve_exhausted` event, or no auto-approve policy for its agent). The item escalates with the reason
  `owner approval needed`. Builder `blocked` that the controller auto-approves is not an escalation: the item waits
  (state `building`, note "waiting for controller auto-approve") and resumes when the task leaves `blocked`.
- Reviewer `blocked`: its answer is used, as today (controller.py:2113-2114).
- The batch never approves or unblocks a task. The owner's dashboard clears `awaiting_approval`.
- Budget limit: `on_limit = escalate`, or a wait longer than `limit_wait_max`.
- No trusted review (existing rule, pipeline.py:387-392).
- Builder `capped`, `failed`, `interrupted` or `cancelled` (existing handling, pipeline.py:225-298).

Batch final review. Runs once, after every item is settled (`ready`, `escalated`, `failed` or `cancelled`).
It is one Codex `final_review` (`gpt-5.6-terra`, high effort) over the `ready` branches. Each diff is sent by path or
as a bounded diff under about 60 KB; larger sets are split by area (CLAUDE.md rule 3). Findings are reported, not
auto-fixed: the owner decides. If Codex is limited, the final review waits until `resets_at`. It is not silently
routed to Claude, because the 2026-11-16 audit showed that outcome (docs/TOKEN-AUDIT-2026-11-16.md 2.3).

Never: approve or unblock a blocked task; merge to main; push; call `complete_task` or `cleanup_task`; use
`budget_override`. Ready branches are for the owner to merge.

## 6. Output

Per item, one short block:
- `id`, `state` (ready, escalated, failed or cancelled), `branch`, `rounds` (builds used). A blocked task shows as
  `escalated` with the reason `owner approval needed`, or as `building` while the controller auto-approves it.
- `verdict`: one line.
- `open_findings`: up to 5, each a severity and one line. Trimmed like `task_status` does.
- `models`: builder and reviewer models used, with counts per task.
- `spend`: tokens per model from the budget rows, not an estimate. The units are UNVERIFIED until the budget module is
  read at build time.
- `escalation`: the reason, when there is one.

Batch level: `state` (running, final_review, done), the final review block (verdict, model, findings), and the
per-model task counts (CLAUDE.md rule 10).

Chat wait: `wait_batch(batch_id, timeout_seconds=1800)` is one long-poll on `GET /api/batches/{id}/wait`. It returns
when every item and the final review are terminal, or at timeout with the current state. Nothing loops in the chat.
Expected chat shape: (1) get_plan and run_batch, (2) wait_batch, (3) wait_batch again only on timeout, (4) read
summary and put_plan. That is about 5 turns for any batch size.

`batch_status(batch_id, full=False)` returns the trimmed record. `full=True` returns the whole record, including the
gate output tail.

## 7. Acceptance criteria

- AC1. One `run_batch` call creates the batch and every item. No per-item call is needed from the chat.
- AC2. Every item ends in exactly one of ready, escalated, failed or cancelled. None is left in limbo.
- AC3. With the default policy, an item gets at most three fix builds after its first build. The fourth CHANGES escalates.
- AC4. A gate failure is bounced to the builder three times. The fourth escalates.
- AC5. Submit rejects a reviewer that matches the builder's agent and model (400). Codex-built items get Claude reviewers.
- AC6. Routing never assigns Fable to build, fix or test. The routing table is data and is tested.
- AC7. Exactly one Codex final review per batch, at high effort, model `gpt-5.6-terra`. It is not spawned while any
  item is unsettled, and not sent to Claude while Codex is limited.
- AC8. No code path in the pipeline or batch module calls an approve or unblock endpoint. The controller's existing
  auto-approve is the only thing that resumes a blocked task.
- AC9. No code path in the batch module merges, pushes, calls `complete_task` or `cleanup_task`, or uses
  `budget_override`.
- AC10. A limit wait does not retry before `resets_at`. A wait longer than `limit_wait_max` escalates.
- AC11. Never more than 1 Codex and 2 Claude jobs running per account at once.
- AC12. A controller restart mid-batch resumes from the database. It spawns no duplicate build or review.
- AC13. `wait_batch` makes one HTTP request and returns on settle or timeout. It does not poll.
- AC14. The default summary is trimmed. `full=True` returns the whole record.
- AC15. Overlapping `files` and `depends_on` run in order.
- AC16. `run_pipeline`, `set_chain`, `pipeline_status` and `wait_tasks` behave as before. Their tests pass unchanged.
- AC17. A build in `blocked` or `awaiting_approval` reaches `step()` as a build event. The pipeline is never left
  silently in `building` with a settled-blocked build.
- AC18. Auto-approve path: a blocked builder that the controller auto-approves stays `building` with the wait note,
  then continues through gate and review once the task leaves `blocked`. The item does not escalate.
- AC19. Stays-blocked path: a builder that stays `blocked` after auto-approve has finished, or is `awaiting_approval`,
  escalates with `owner approval needed`. Other items keep running. The batch does not wait on it.

## 8. Test cases

Run one test file per process (scripts/gate.sh rule).

- T1. `batch.step`: table-driven events mapped to expected states, with fake builds and reviews.
- T2. Round arithmetic: `max_fix_rounds=3` maps to `max_rounds=4`. Three CHANGES lead to build 4; the fourth CHANGES
  escalates. The test asserts the count of `message_build` actions.
- T3. Gate bounce: failures 1-3 bounce, failure 4 escalates.
- T4. Submit-time reviewer independence: same agent and model is rejected with 400.
- T5. Routing table: each (builder, security_sensitive, limit state) maps to the expected agent and model.
- T6. Final review gating: unsettled item means not spawned; all settled means spawned once; a repeated loop tick does
  not spawn it again.
- T7. Codex limited at final review: parked with `resets_at`; no Claude task spawned.
- T8a. Auto-approve path: build goes `blocked`; the fake controller emits `task.auto_approved` and moves the task to
  `working`. The item stays `building` with the wait note, then reaches gate and review. No escalation.
- T8b. Stays-blocked path: build goes `blocked` and the fake controller emits `task.auto_approve_held`; the task stays
  blocked. The item becomes `escalated` with `owner approval needed`, and the other items finish.
- T8c. `awaiting_approval` build: escalates on the first poll with `owner approval needed`.
- T8d. In all three, the mock HTTP client records no approve or unblock call (only the controller's own resume).
- T9. Restart: state persisted after each event; reload; no duplicate spawn.
- T10. `wait_batch`: returns on settle; returns with state at timeout; exactly one request.
- T11. Summary trimming: default trimmed; `full=True` whole.
- T12. Concurrency cap: never more than the per-account limits.
- T13. Static check: the batch module imports and calls none of merge, push, approve, unblock, complete_task,
  cleanup_task or budget_override.
- T14. Regression: existing pipeline, MCP and chain tests pass unchanged.
- T15. Turn budget: a scripted 10-item batch in the test harness uses at most 5 orchestrator tool calls.

## 9. What changes, and where

- `master/controller/batch.py` (new): validation, routing table, per-item state machine. Reuses `pipeline.new` and
  `pipeline.step`.
- `master/controller/controller.py`: `poll_pipeline` also forwards `blocked` and `awaiting_approval` builds (add to the
  tuple at controller.py:2082), so `step()` sees them.
- `master/controller/pipeline.py`: `step()` gets build-event branches. `blocked` sets `budget.waiting_approval` and
  keeps `building` (with a history note). A later poll that finds the build out of `blocked` clears the wait and
  continues. `awaiting_approval`, or `blocked` after auto-approve has finished, escalates with `owner approval
  needed`. No change to round rules or constants.
- `master/controller/controller.py`: batch routes next to the pipeline routes (around 2157-2230), a batch loop started
  like `pipeline_loop` (2139), and the batch table created with the pipelines table.
- `master/controller/live.py`: per-item chain labels for Live, via the existing chain writer. Descriptive only.
- `master/mcp/server.py`: `run_batch`, `batch_status`, `wait_batch`. Same trim conventions as `wait_tasks` and
  `task_status`.
- New test files for batch.py, the routes and the MCP tools. One file per process.
- Not changed: budget rules, `tasks.py` routing, `set_chain` semantics, `run_pipeline` arguments.

## 10. Decisions (all decided by the owner)

1. DECIDED: `max_fix_rounds = 3` means three real fix rounds after the first build, so it maps to `max_rounds = 4`.
   The mapping is stated in sections 2, 3 and 8 (T2); sections 5 and 7 use `max_fix_rounds` only. `max_rounds=3`
   is not used by the batch.
2. DECIDED: final-review findings are reported, not auto-fixed. This keeps spend bounded.
3. DECIDED: Codex limited at final review means wait, not fall back to Claude.
4. DECIDED: a limit wait over 6 h escalates.
5. DECIDED: a security-sensitive item gets Codex as its single reviewer, at high effort.
6. DECIDED: the batch stops at `ready`. Merging stays with the owner or a later merge task.
7. DECIDED: a blocked builder is handled by `pipeline.step`. The pipeline never approves or unblocks. Auto-approve
   (`task.auto_approved`) resumes it, and the item waits and then continues. If the task stays `blocked` or is
   `awaiting_approval`, the item is marked `escalated: owner approval needed` and the batch continues with the other
   items. Sections 2, 5, 6, 7 (AC8, AC17-AC19), 8 (T8a-d) and 9 reflect this.

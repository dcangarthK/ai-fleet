# Audit C: human intervention in the fleet (2026-11-19)

Produced by fleet task `t-3c3f70be` (Claude Sonnet, spare account) as a read-only review. The task had no Write or
Bash tool, so it could not commit; the orchestrator transcribed its report into this file unchanged apart from this
header.

Caveats from the author: the events table is only partly counted. There was no `sqlite3`, so only a grep of the raw
`fleet.db` was possible, which gives type names but no trustworthy counts. Event frequencies below come from the
written audits and are marked as documented; everything else is marked UNVERIFIED. `.claude/skills/fleet-batch/SKILL.md`
is not in the task branch's worktree, so it was read from the main checkout.

## 1. Catalogue of human touchpoints

**Event types seen in `fleet.db`:** `task.awaiting_approval`, `task.blocked`, `task.approved`, `task.auto_approved`,
`task.auto_approve_held`, `task.cancelled`, `task.requeued`, `task.rerouted` and `task.cleaned_up`. Counts are UNVERIFIED.

- **Node approval.** The owner approves in the dashboard. It protects against an unknown machine receiving code and
  credentials. Frequency in ten days is UNVERIFIED. `HANDOVER.md` shows Laptop 2 still unjoined on 11 Nov, with
  Tailscale and registering the spare account listed as owner actions.
- **Task approval and unblocking.** This is the largest stream.
  - On the night of 16 Nov, 30 of 37 first runs hit a Bash denial and went `blocked`. The controller auto-approved 28
    Bash and 1 Read denial (`TOKEN-DEEPDIVE-2026-11-17.md` §2.1). That is documented.
  - The remainder waited for a person. `t-a724a961` sat blocked overnight after `task.auto_approve_held`
    (`TOKEN-AUDIT-2026-11-17.md` §4).
  - `t-aa7312aa` was `awaiting_approval` at 02:10 on 17 Nov (`HANDOVER.md` §6).
  - It protects against a worker widening its own tool access.
- **Mode ceilings.** `max_mode auto-edit` is set in the runner config (`HANDOVER.md` §4). `full` is used only when the
  owner says so (`CLAUDE.md`). No change was recorded in ten days, so the frequency is close to zero. It protects
  against an unsandboxed worker.
- **Merges.** Merges are fleet tasks, not orchestrator acts, and they are frequent: the overnight run merged 43 commits
  (`HANDOVER.md`, 13 Nov). Owner sign-off per merge was relaxed after 11 Nov. The remaining human step is merging into
  the live checkout and restarting. It protects against bad code reaching the running controller.
- **Pushes.** Owner only. The orchestrator profile denies `Bash(git push *)` (`ORCHESTRATOR-PROFILE.md`), and workers
  have git-level no-push. Pushes are rare. It protects against irreversible publication.
- **Budget overrides.** `budget_override` is admin-only. The 13 Nov handover records none used. It protects the shared
  Codex and Claude limits.
- **Re-spawns after a limit.** Manual re-spawns were the norm on 11-12 Nov (`HANDOVER.md` §4b, copy-paste curls).
  Since then the controller hands off automatically. The last manual one was a harmful duplicate: `t-7e5a253d` ran 17
  seconds after a handoff and cost 2.08M cached tokens (`TOKEN-AUDIT-2026-11-16.md` §2.2).
- **Answering a blocked worker.** Done through `message_task`. The 16 Nov orchestrator chat made 9 such calls and 4
  `cancel_task` calls (§1.2). It protects against a worker guessing at scope.
- **Choosing a reviewer.** The orchestrator does this every round. The table in `CLAUDE.md` is only a default, and
  practice differs from it (see §4).
- **Deciding a deviation.** Examples: the orchestrator did a delta re-review itself instead of a second Codex run
  (`HANDOVER.md`, 16 Nov); whether `t-a724a961` is cancelled was left to the owner
  (`HANDOVER-2026-11-17-codex.md`); the owner clears Bash denials that the policy did not auto-approve.

## 2. Authority decisions versus gaps nothing else covered

**Keep with the owner:**
- **Pushes, node approval, mode ceilings and `budget_override`.** These expose the owner's machines, accounts or public
  repo. A bad call cannot be undone, and no check can judge it. The orchestrator is right never to hold these.
- **Cancelling work the owner commissioned.** The orchestrator should not discard the owner's scope.

**Only there because nothing else was watching:**
- **Task unblocking.** The policy already auto-approves most denials. The leftover blocks were about limits and
  restarts, not authority. Limit-caused admission failures should be held as `queued`; that fix was in flight on
  17 Nov (`t-f14bd3b`). For Bash denials, widen the pre-approved read-only commands (also in flight). The master rule
  "Never approve or unblock blocked tasks; that is the owner's job" is too broad. It should read "never approve denials
  the policy does not already cover".
- **Re-spawn after a limit.** Automatic handoff exists, so no person is needed. Both the rule and the human check can go.
- **Controller and MCP restarts after a merge.** These are mechanical. The skill makes the orchestrator remember them;
  a post-merge hook should do it. The 17 Nov handover shows the restart still pending hours later.
- **`complete_task` and `cleanup_task` after each merge.** The handover shows seven tasks waiting on these. A merge task
  should end with both calls.
- **Reviewer choice.** This is a rule lookup keyed on `kind` and builder. The controller already enforces independence.
  It should propose the reviewer, not ask the orchestrator to choose.
- **Deviation decisions.** Some are real judgment calls (skipping a second Codex run). Others only exist because the
  cost of Codex is not visible to the orchestrator, so it improvises.

## 3. The orchestrator's own cost per batch

The written rules force the session to do this, in order:

1. `get_plan`.
2. `fleet_status`.
3. One `spawn_task` per task, each with `kind`, `repo`, `gate`, `mode`, `parent_id`, `link`, `stage` and sometimes `profile`.
4. `wait_tasks`.
5. `task_diff` for each review.
6. Spawn the reviewer, resolve findings, spawn a re-review of the delta.
7. A Codex final review.
8. A merge task.
9. `complete_task` and `cleanup_task` for every task.
10. `put_plan`.
11. A spend report (rule 10) and a per-task record of models, commit, gate and verdicts.
12. A manual MCP restart.

**Re-derived each batch instead of stored:**
- **Briefs.** The skill stores the template, but the file lists and `file:line` context are rewritten each time. On
  16 Nov, 24 `spawn_task` calls each re-reasoned the same brief shape (`TOKEN-AUDIT-2026-11-16.md` rec #6).
- **Spend and per-task records.** Both are plain queries on `agent_usage`, but the rules ask the orchestrator to compose them.
- **Routing table.** The controller enforces kind-to-model rules, but the orchestrator still reads the table and
  decides. Drafts ran on Sonnet again the same day the rule was written (`TOKEN-DEEPDIVE-2026-11-17.md` §2.5).
- **Merge steps.** `HANDOVER.md` §5 spells them out as prose. The builder checklist adds the `onboard.sh` trap by hand.
- **Limit status and handoff state.** The orchestrator reads `fleet_status` and reasons about `limited_until` each time.

## 4. Contradictions and stale rules

**Contradictions:**
- **Who does the final review.** The pipeline says "Final. Claude reviews the whole diff" (`CLAUDE.md`, step 7). The
  budget table says "Final whole-branch review | Codex". Rule 1 repeats "send it to Codex". Step 7 should be rewritten.
- **Reviewer models.** The table says "Fable or Sonnet" for round reviews. Practice was Opus (`HANDOVER.md`, 13 Nov)
  and Haiku (`TOKEN-DEEPDIVE-2026-11-17.md` §2.6). The table lists neither.
- **Onboard scripts.** `CLAUDE.md` says to take main's copy of `nodes/onboard.sh` and `.ps1`. `HANDOVER.md` §6a calls
  that "wrong when the branch edits those scripts outside the embedded runner". `CLAUDE.md` was never corrected.
- **Who clears Bash blocks.** `HANDOVER.md` says three tasks were "blocked on Bash denials, which only the owner can
  clear". `TOKEN-DEEPDIVE-2026-11-17.md` says the controller auto-approved 28 of them. Both are from 16-17 Nov.
- **Running the gate.** `CLAUDE.md` says "Don't run the gate yourself". The fleet instructions in the session context
  say "Run the tests before claiming something works". The 11 Nov handover has a builder told to run
  `bash scripts/gate.sh` twice.
- **`fleet_status` cadence.** `CLAUDE.md` says call it "at the start of a task". The skill says once at the start of a batch.
- **Skill location.** `MASTER-INSTRUCTIONS.md` points at the skill "in ai-fleet". It is not in this branch's worktree,
  so fleet workers may not see it. Why is UNVERIFIED.

**Stale after the cause was fixed:**
- "Before you spawn a retry" (skill). The controller now refuses the duplicate itself.
- "Leave `model` unset on `draft`" (skill). The skill itself notes the controller now downgrades it.
- The final-review cap incident behind rule 1. `final_review` got its own cap (rec #1), but the paragraph still reads
  as a live problem.
- Owner standing instruction 4 in `HANDOVER.md`: use "Fable 5.1" when Codex is limited. The current Fable is 5.2, and
  rule 0 says Fable only reviews.
- Standing instruction 3, "Don't do anything by yourself", and the `curl` toolkit and "always `sleep` inside the loop"
  in `HANDOVER.md` §5. Both predate the MCP tools and the event-based `wait_tasks`.
- "Restart the MCP server after any `master/mcp/server.py` merge" (skill). It belongs in code.

## 5. Failure stories

1. **Orchestrator chat hoarding context.** One chat took 107M of 186M cache reads in a day, 57%. Rules added: one chat
   per batch, `get_plan` to open and `put_plan` to close (`MASTER-INSTRUCTIONS.md`).
2. **Duplicate retry after a handoff.** A hand-spawned retry 17 seconds after the controller's handoff wasted 3.5M
   cached tokens. Rules added: the controller now refuses it, and `handed_off_to` is shown.
3. **Final reviews capped with no verdict.** Three paid final reviews hit the context cap because `final_review` had no
   cap key. Rules added: a `final_review` cap, and final reviews go to Codex.
4. **Task stuck blocked overnight.** `t-a724a961` was held with a limit reason and never reached `failed`, so the limit
   machinery could not see it. Rule added: a limit at admission now holds the task as `queued`, never `blocked`.
5. **Bash denials doubled worker runs.** 30 of 37 first runs ended in a denial, adding 8.6M cache reads. Rules added:
   read-only commands are pre-approved, and workers are told their cwd is already the worktree.
6. **Drafts on Sonnet, twice.** The rule to leave drafts to Haiku failed the day it was written, costing $4.53. Rule
   added: enforced in code.

Spend: no paid models ran for this audit beyond the review task itself; no limit hits.

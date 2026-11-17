# Harness playbook adaptation for ai-fleet (2026-11-17)

Status: **proposal only.** Nothing in `CLAUDE.md`, `docs/MASTER-INSTRUCTIONS.md` or `.claude/skills/fleet-batch/SKILL.md`
has changed. The owner approves each item before anyone edits those files.

## Source and method

The source is a social-media summary of a Claude Code harness prompt written for Fable 5.2. It has five sections:
Direction, Crew, Harness, Gotchas and Start. Each idea is compared below with Anthropic's published guidance and
with ai-fleet as it stands: the repo `CLAUDE.md`, `docs/MASTER-INSTRUCTIONS.md` and the `fleet-batch` skill,
including its routing table and brief templates.

Anthropic sources:

- [A1] Building effective agents: https://www.anthropic.com/engineering/building-effective-agents
- [A2] Effective harnesses for long-running agents: https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
- [A3] Effective context engineering for AI agents: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- [A4] Writing effective tools for agents: https://www.anthropic.com/engineering/writing-tools-for-agents
- [A5] How we built our multi-agent research system: https://www.anthropic.com/engineering/built-multi-agent-research-system
- [A6] Claude Code best practices: https://www.anthropic.com/engineering/claude-code-best-practices
- [A7] Prompt engineering overview and Claude 4.x best practices: https://docs.claude.com/en/docs/build-with-claude/prompt-engineering/overview
  and https://docs.claude.com/en/docs/build-with-claude/prompt-engineering/claude-4-best-practices
- [A8] Context editing and memory tool: https://docs.claude.com/en/docs/build-with-claude/context-editing

Caveat: these URLs were cited from the published posts as known up to the model's training cutoff. They were not
fetched again in this session, so check the links before quoting them outside the team.

Each idea gets a verdict: **Adopt** (take it as written), **Adapt** (take the intent, change the form) or
**Reject**.

---

## 1. Direction

### 1.1 "The codebase is yours; ask for missing inputs, then keep going until it runs end to end" — **Adapt**

- **Fleet today.** The builder checklist says "stay inside the task's files; ask via the task (blocked) instead of
  guessing at scope." The pipeline needs the owner's OK on the spec before any build. Builders already run to the
  gate without check-ins.
- **Gap.** The "keep going" half is implicit. Builders sometimes stop at the first error or timeout and report it as
  a blocker, which uses a fix round. [A2] and [A7] both recommend saying plainly that the agent should finish the
  job and keep working through ordinary failures.
- **Conflict.** "The codebase is yours" conflicts with the owner's scope rule (agreed work only, the listed files
  only) and with the no-commits-in-/lombard rule. Ownership has to be bounded by the brief.
- **Proposal** (Build or fix brief template, after the Gate line):
  > Within the files listed above, this work is yours: keep going until the acceptance check passes. Errors,
  > timeouts and empty results are not blockers. Diagnose them, retry once, or try another tool. Stop and report
  > `blocked` only for a missing input, a file outside the list, or an action that needs the owner.

### 1.2 "Run effort high; rerun the evals at medium and xhigh before changing it" — **Adapt**

- **Fleet today.** Effort follows `kind`: reviews and fix rounds run at medium, re-reviews at low, and final and
  security reviews at high. Codex runs at medium by default. The `benchmark` kind and the eval leaderboard exist,
  but they compare models, not effort levels.
- **Gap.** Effort levels were set by judgement, not by eval. The playbook's real point is to measure before
  changing effort, and that is sound ([A1]: add complexity only when it measurably helps).
- **Conflict.** "Default high" conflicts with the paid-model budget. Reject the default and keep the rule.
- **Proposal** (`CLAUDE.md`, Paid-model budget, rule 5, extra sentence):
  > Change a kind's effort only after a `benchmark` run compares the current level with one level up and one
  > level down on the same eval set, with tokens logged per run.

## 2. Crew

### 2.1 "Orchestrator splits the goal into tasks, each with a done-check" — **Adopt** (already done)

- **Fleet today.** The pipeline has a spec with acceptance criteria and tests first. Each brief has `Acceptance:`
  and `Gate:` lines.
- **Gap.** None worth fixing. Optionally rename `Acceptance` to `Done when` so that every brief, including
  review and draft briefs, ends with a checkable condition.

### 2.2 "Builders at low effort, one per task, self-contained brief (goal, files, done-check)" — **Adapt**

- **Fleet today.** One writer per worktree. The brief template covers goal, files, acceptance and context.
  Builds default to Sonnet at the kind's default effort.
- **Gap.** Low effort for builders has not been measured. [A5] reports that vague briefs caused duplicated work and
  that effort should be scaled to task complexity, not set flat.
- **Proposal.** Keep the template. Run the 1.2 benchmark for `build` at low against medium before changing it. Do
  not adopt "low" untested: failed gate rounds cost more than the effort they save.

### 2.3 "Delegate only parallel work; each subagent adds cost" — **Adopt**

- **Fleet today.** The skill says to spawn independent tasks together, and the 17 Nov deep dive notes that
  re-scopes and cancellations cost $4.35 in one night. `CLAUDE.md` still reads "All work through the fleet …
  every build, fix, review, test …", which is close to an "always delegate" line.
- **Gap.** [A5] says multi-agent runs use about 15 times the tokens of a chat and pay off only on wide, parallel
  work. "All work through the fleet" pushes small sequential edits into paid spawns. The rule is there for
  observability on the Live page, not for parallelism.
- **Proposal** (`CLAUDE.md`, "All work through the fleet" bullet, replacement wording):
  > Builds, fixes, reviews, tests, merges and benchmarks are fleet tasks so they show on Live. Split work into
  > several tasks only when the pieces can run in parallel. A sequential chain of small edits is one task, not
  > several.

## 3. Harness

### 3.1 Tools do one job each, with plain descriptions and no "always delegate" lines — **Adopt**

- **Fleet today.** The `local-llm` MCP has about 30 tools. Some overlap: `wait_tasks` and `task_status`, and
  `review_diff` and a `review` kind spawn. Descriptions were not audited for imperatives.
- **Gap.** [A4] recommends fewer, well-named tools with return values trimmed for the model, and descriptions
  written like docs for a new hire. ai-fleet already trims return values. The descriptions have not had a pass.
- **Proposal.** A fleet `check` task on Haiku to list each `local-llm` tool description that contains "always",
  "must" or "never" and could be read as an instruction instead of a description, and each pair of tools with
  overlapping jobs. Output a list only, no edits.

### 3.2 Context is append-only; shorten it by compaction or context editing, never by editing old turns — **Adopt** (already done)

- **Fleet today.** Auto-compact at 200k, a fresh session per batch, and state in fleet.db and `put_plan`. This
  matches [A3] (compaction, structured note-taking, sub-agent isolation) and protects prompt caching.
- **Gap.** None. Optionally cite [A3] next to the "one chat per batch" rule so the reason survives.

### 3.3 Each memory note states its scope and reason; delete cautions whose reason is gone — **Adopt**

- **Fleet today.** Memory files use `**Why:**` and `**How to apply:**` lines. Some notes have outlived their
  reason. For example, `advisor-mode-dropped` ("Fable unavailable") sits next to the skill's "advisor mode is a
  trial" rule and `CLAUDE.md`'s Fable review role. That is three different statements about Fable.
- **Proposal** (MASTER-INSTRUCTIONS, Token saving):
  > Every memory note and every rule added after an incident names its scope and the reason, with a date or doc
  > reference. At the start of each batch, delete or update notes whose reason no longer holds. Contradictory notes
  > are a bug: fix them, do not add a third.
- **Immediate cleanup for the owner to decide:** settle the Fable status in one place. Today `CLAUDE.md` says
  Fable reviews, memory says advisor mode was dropped, and the skill says advisor mode is a trial.

### 3.4 Reads, tests and safe retries need no approval; risky actions are asked first — **Adopt** (mostly done)

- **Fleet today.** Modes are `auto-edit`, `supervised` and `full`. The `blocked` and `awaiting_approval` states
  go to the owner, and the orchestrator never approves. One Sonnet fix task blocked on a Bash permission
  (t-c7df6d7c) before it could build and test.
- **Gap.** Read-only and test commands still trigger permission blocks in `auto-edit`. [A6] recommends allow-lists
  for safe commands.
- **Proposal.** The owner extends the `auto-edit` allow-list to cover the gate command, `git status`, `git diff`,
  `git log` and the repo's test runners. Pushes, deletes and network calls stay ask-first.

### 3.5 The reviewer reads the diff, not the summary, and runs the tests; then fail, fix, rerun — **Adapt**

- **Fleet today.** Reviewers see the diff read-only. The runner, not the reviewer, runs the gate. The fix loop is
  capped at 3 rounds.
- **Gap.** The rule that reviewers read the diff and not the builder's summary is not written down. [A1]
  (evaluator-optimizer) and [A2] both stress checking against ground truth.
- **Conflict.** "The reviewer runs the tests" conflicts with the rule that agents should not re-run the gate.
  Keep the runner as the one gate. The reviewer reads the gate summary, which counts as ground truth.
- **Proposal** (Review brief template):
  > Judge the diff and the gate result below. Do not rely on the builder's summary of what it did.
  > Gate result: <Ran/OK/FAILED lines from the runner>

### 3.6 Evals score every change and log tokens per change — **Adapt**

- **Fleet today.** Tokens are logged per task (`fleet_usage`, token audits), and the gate runs on every change.
  Model evals exist in the benchmark leaderboard. Nothing scores prompt, brief or skill changes.
- **Gap.** Brief and `CLAUDE.md` edits ship with no before-and-after measure. [A4] and [A5] both describe small
  eval sets run on every prompt or tool change.
- **Proposal.** A `harness-eval` set of 5 to 10 fixed past tasks with known gates, run as a `benchmark` kind on
  Sonnet or Haiku after any change to `CLAUDE.md`, the skill or the brief templates. Record gate pass rate,
  rounds and tokens. Do not score every code change this way: the gate already covers code.

### 3.7 After several silent tool calls, a turn-scoped reminder says what the agent is doing — **Adapt**

- **Fleet today.** `report_activity` and the Live page show state. Workers do not post progress notes mid-task.
- **Gap.** A long silent run is indistinguishable from a hung one until the timeout.
- **Proposal.** Done by the runner, not in a prompt. After N tool calls with no text output, the runner posts a
  heartbeat to Live (last tool, files touched). This needs no extra model tokens. Do not add "report progress
  every N steps" to briefs, because it costs output tokens on every task.

## 4. Gotchas

### 4.1 Errors, timeouts and empty results are not blockers; do-not-touch files are — **Adopt**

Covered by the 1.1 wording. It matches the builder checklist's "ask via the task" rule for scope, and adds
the missing "keep going" half.

### 4.2 No token budgets in the prompt — **Adapt** (flags one of our rules)

- **Fleet today.** `CLAUDE.md` and the skill hold many numbers: the 60 KB prompt cap, the 25k startup target,
  "8k to 140k in four turns", and dollar figures. The controller also enforces token caps per kind.
- **Playbook argument.** Budgets written into a prompt make the model ration its work or stop early, and
  [A7] notes that Claude 4.x models follow instructions literally. Enforcement belongs in the harness.
- **Verdict.** Our caps are already enforced by the controller, which is the right layer. The numbers in
  `CLAUDE.md` and the skill are aimed at the orchestrator writing briefs, not at workers. Keep them there. Make
  sure no **worker** brief carries a token or turn budget. The brief templates currently carry none, which is
  correct.
- **Proposal** (skill, under "Brief templates"):
  > Do not put token, turn or cost budgets in a worker's brief. The controller enforces the caps. A number in
  > the brief makes the worker ration work instead of finishing it.

### 4.3 New instructions go in the newest turn, not in edits to old ones — **Adopt** (already done)

`message_task` steers in the same session by appending a turn. Add one line to the skill: "To change a running
task's instructions, use `message_task`. Do not cancel and re-spawn with an edited brief unless the scope changed."
This also follows from the deep dive's reroute costs.

### 4.4 No "answer without thinking" lines; lower the effort instead — **Adopt**

- **Fleet today.** Effort is set per kind through the controller. No briefs were seen with "don't think" lines,
  but the caveman SessionStart hook ("drop articles, fragments OK") affects output style, not reasoning, so it
  does not conflict.
- **Proposal** (skill, Brief templates):
  > To make a task cheaper, lower its effort through `kind` or `effort`. Do not tell the model to skip
  > thinking or to answer briefly in the brief.

## 5. Start: plan in 5 lines, build, recap what works, what failed and what's next — **Adopt**

- **Fleet today.** Results have a verdict line, findings and a gate summary. Builders do not post a plan up front,
  and the final summary format (what changed, who built, who reviewed, waivers, risks) applies to the
  orchestrator only.
- **Proposal** (Build or fix brief template, final lines):
  > Before editing, state your plan in at most 5 lines. When done, end with three short lists: Works, Failed,
  > Next.
  This gives the reviewer and the Live page a check on scope at the start, and gives the trimmed `wait_tasks`
  view something to keep.

---

## Conflicts with the owner's rules

| Playbook idea | Owner rule it conflicts with | Resolution |
|---|---|---|
| Effort high by default | Paid-model budget, rule 5 (match effort to the job) | Keep the per-kind effort. Use the eval idea only. |
| Builders at low effort | None directly, but it is unmeasured | Benchmark before changing. |
| "The codebase is yours" | Scope boundary, no commits in /lombard | Ownership bounded by the brief's file list. |
| Reviewer runs the tests | "Don't ask agents to re-run the gate" | The runner's gate result is the ground truth the reviewer reads. |
| Orchestrator and builders on one model (Fable) | Fable never builds, fixes or tests; Codex light model | The roles stay as the routing table sets them. The playbook's model choice does not carry over. |

## Our rules the playbook argues against

1. **"All work through the fleet"** reads like an "always delegate" line. Narrow it as in 2.3.
2. **Numbers in prompts.** They are fine in orchestrator docs and must stay out of worker briefs (4.2).
3. **Contradictory Fable rules** across `CLAUDE.md`, memory and the skill (3.3). Stale cautions should be
   deleted, not stacked.

## Top 5 recommended changes

1. Add the **"keep going; errors are not blockers; out-of-scope files are"** paragraph to the Build or fix brief
   (1.1, 4.1).
2. Narrow **"All work through the fleet"**: parallel pieces get their own tasks, and sequential small edits are one
   task (2.3).
3. Add the **reviewer judges the diff and the gate result, not the builder's summary** line to the Review brief
   (3.5).
4. Add **brief hygiene rules**: no token budgets, no "don't think" lines, and steer with `message_task` (4.2, 4.3,
   4.4). Also add the **plan in 5 lines, then Works/Failed/Next** close to the Build or fix brief (5).
5. Build a **harness-eval set** (5 to 10 past tasks) that runs after any change to `CLAUDE.md`, the skill or a
   template, and use it before changing any kind's effort (1.2, 3.6). Then clean up the contradictory Fable notes (3.3).

## Result of the 3.1 tool-description audit (2026-11-17)

The audit scanned the docstrings of the `local-llm` tools in `master/mcp/server.py` for the words always, must,
never and don't, and for descriptions over 120 words.

- **`spawn_task`** (277 words) is the only real finding. It contains "kind: always name it" and "Then use
  wait_tasks; don't poll". Proposed wording: "kind: the work type (build, fix, …); the budget rules use it to pick
  caps, effort and allowed models." and "wait_tasks blocks until the task settles." The parameter detail could
  move into the parameter descriptions, which would bring the docstring under 120 words.
- **No change needed:** `ask_model`, `report_activity` and `set_chain` use "never" to describe what the tool does,
  not to instruct the caller. `install_model` uses "must" for a user-consent guard and should keep it.

No edits were made. Changing `server.py` is a separate build, and the MCP server must be restarted afterwards.

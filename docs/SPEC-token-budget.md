# Spec: token budget per task (Part H)

Status: draft v1, 2026-11-12. Needs the owner's OK before any build. Runs after Part G batch 1 merges. It
touches the runner's launch arguments, and Part G batch 1 is changing the runner now.

## 1. Problem
Each 5-hour window burns out in under an hour. Measured from `~/.ai-fleet/jobs/*/events.jsonl` over the last
24 hours (66 jobs):

- **Total:** 356M cached input tokens read, 12M written to cache, 6k uncached input, 63k output. Almost all the
  cost is the context being re-sent on every turn. Output and the prompts we write are negligible.
- **One long session:** t-fabbd0aa, an Opus "fleet lead" session, ran 598 turns. Its context peaked at 467k and
  it read 157M tokens, which is 44% of the total.
- **Fixed overhead:** every worker starts with 45 tools, 67 skills, 6 plugins, the claude-mem MCP server and its
  hooks. That is 23–36k tokens on turn 1, and it is re-sent on every later turn.
- **Builds:** each build runs 100–230 turns with an average context of about 95k, for 10–23M tokens per build.
  Bash is the most-used tool, mostly repeated test runs whose output stays in context.
- **Reviews:** each Opus or Fable review runs 50–80 turns of Grep/Read, for 3–6M tokens.
- **The orchestrator chat:** also long-lived and large. It isn't in the job logs.

Cost per turn ≈ the context at that turn. A task's total ≈ turns × its average context. So the levers are:
**fewer turns, a smaller base context, slower context growth, and a cheaper model for each token.**

## 2. Principle
Every task runs on a token budget, just as it runs on a budget rule today. A worker starts lean: only the tools
its kind needs, the cheapest capable model, and medium effort. It sees only filtered tool output. No session
lives past its cap. Long work continues in a fresh session from a short stored summary.

## 3. Requirements
1. **Lean worker profile.** Runner-launched `claude -p` jobs start with:
   - `--strict-mcp-config`, with no MCP servers unless the task's kind needs one;
   - no skills, no plugins, and no SessionStart or UserPromptSubmit hooks. This means a per-profile worker
     settings file that the runner passes with `--settings`, separate from the owner's interactive profile;
   - an allowed-tools list per kind:
     - build/fix: Read, Edit, Write, Bash, Grep, Glob;
     - review: Read, Grep, Glob (git diff via the runner);
     - test: Bash.

   Target: turn-1 context ≤ 8k, down from 23–36k. Acceptance: measured at turn 1 on one build and one review.
2. **Per-task caps.** Each kind gets defaults for `max_turns` and `max_context`, editable on the Budget page
   (owner):
   - build/fix: 120 turns, 150k context;
   - review: 30 turns, 100k context;
   - re-review: 15 turns;
   - test: 20 turns.

   At a cap the runner sends one final instruction: "stop, commit, write a ≤ 300-word handoff summary". The task
   then settles as `capped`. It is not a failure.
3. **Fresh-session continuation.** A `capped` build that isn't done re-queues as a new task. It has
   `replaces = <old id>` (Part F requirement 5) and a prompt made of the spec reference, the handoff summary and
   `git log` of the branch, not the old transcript. The same worktree and branch are kept. This builds on the
   stored plan from Part G requirement 11. No session ever runs the way t-fabbd0aa did.
4. **No long-lived leads.** A "lead" or orchestrator task is refused unless it has caps. The leader from Part G
   requirement 11 runs as a chain of short sessions, one per step, each started from the stored plan. It is never
   one growing chat.
5. **Quiet tools (PreToolUse filter).** Worker profiles get a PreToolUse hook on Bash. It rewrites known noisy
   commands to show only what matters:
   - test runs (`python3 -m unittest`, `scripts/gate.sh`, `npx vitest`) print the pass/fail count plus each
     failing test's name and first error, capped at about 100 lines;
   - `git log` and `git diff` get default limits, and large file reads get a line cap.

   The full output is saved to a file in the job directory, so the worker can read a slice if it really needs to.
   The filter is ours and lives in the repo (no third-party code).
6. **One test run per round.** Builders run only the test files they touched, at most twice, before committing.
   The gate runs once afterwards (existing rule 6). The builder prompt says so, and the runner counts repeated
   identical test commands. The third run of the same command is refused, with the message: "the gate will run
   this".
7. **Model routing per sub-job.** Mechanical sub-work goes to the cheapest model:
   - Haiku: search/scout, log and test triage, and summaries (the handoff summary in requirement 3);
   - Sonnet: builds, fixes, round reviews;
   - Opus or Codex: hard builds, security reviews and final reviews only.

   Every spawned task and agent file pins `model` (and `effort`) explicitly, so nothing inherits the parent's
   model. Default effort is `medium`; `high` is used only for final and security reviews (existing rule 5). Haiku
   tasks that need Bash get it through the lean profile's allowed-tools list. Today Haiku test tasks fail with
   "requires approval".
8. **Bounded review input.** A review prompt carries:
   - the diff (or its file list plus the worktree path, not both);
   - the spec section numbers, not the spec text;
   - the previous findings for a re-review.

   It is told to read only the changed files and their direct callers. The 60 KB prompt cap stays.
9. **Idle burners off.** Worker and orchestrator profiles turn off anything that starts turns while no one is
   typing:
   - prompt suggestions;
   - goal check-ins (`CLAUDE_CODE_GOAL_CHECKIN_MINUTES=0`);
   - cross-session inbound messages (`hold`).

   The exact settings keys are checked against the current Claude Code settings reference before the build.
   This applies to the owner's interactive profile only with the owner's OK.
10. **Token telemetry on Live.** Per task, Live shows turns, peak context, cache-read and cache-write tokens, and
    turn-1 context, with a bar against the task's caps. An alert fires when a task passes 80% of a cap, or when
    turn-1 context goes over the lean target. The spend report (rule 10) adds tokens per kind and per model.
11. **Orchestrator hygiene.** The orchestrator:
    - keeps state in fleet.db and in the stored plan, not in chat;
    - waits on events, not polling loops that print output;
    - reads task results trimmed to the verdict and findings;
    - starts a fresh session per batch, using the requirement 3 summary.

## 4. Ideas reviewed from the field guide (owner's image, 2026-11-12)
The guide's numbers are illustrative, not measured. Section 1 above is our measured baseline.

| Guide point | Taken? | Where |
|---|---|---|
| Subagents inherit the parent model; pin `model:` per agent | Yes | Req 7 |
| Search and triage on Haiku | Yes | Req 7 |
| Sonnet as the default, Opus only when stuck | Already the rule; enforced | Req 7, budget rules |
| Medium effort by default | Yes | Req 7 |
| Hook that trims test output (PreToolUse, failures only) | Yes, our own filter | Req 5 |
| Turn off unused MCP servers | Yes, `--strict-mcp-config` | Req 1 |
| Prompt suggestions off, goal check-ins at 0, session messages on hold | Yes, for worker profiles; the owner's profile only with OK | Req 9 |
| Keep agent teams small | Partly: caps and the no-lead rule | Reqs 2, 4 |
| Not in the guide: the biggest burner here, one ever-growing session | Added | Reqs 2–4 |
| Not in the guide: the fixed skills, plugins and hooks overhead per turn | Added | Req 1 |

## 5. Safety constraints
- Caps never cut off a commit. The final instruction asks for a commit before the session stops. A `capped` task
  with uncommitted changes keeps its worktree for the next session.
- A filtered output never hides a failure. The filter always shows the exit code, and it says "full output: <path>"
  whenever it trimmed anything.
- Lean profiles must not weaken the safety rules. Credential redaction, read-only reviews and the
  no-approve/no-unblock rules stay as they are.
- Settings changes to the owner's own `~/.claude` profile are out of scope unless the owner asks.

## 6. Acceptance and tests
- **Lean profile:** turn-1 context of a build is ≤ 8k on a real job, and the job's init event lists only the
  allowed tools, with no MCP servers and no skills.
- **Caps:**
  - a fake job that reaches `max_turns` gets the stop instruction and settles `capped` with its summary saved;
  - continuation starts a new task with `replaces`, the same branch and a prompt under 4 KB.
- **No lead:** a lead task without caps is refused with a reason.
- **Filter:**
  - a failing unittest run shows the counts, the failing names and the first errors, in ≤ 100 lines, with the
    exit code kept;
  - a passing run shows one line;
  - the full log is on disk.
- **Repeat runs:** the third identical test command is refused with the gate message.
- **Routing:** a spawned task with no explicit `model` gets its kind's default, never the parent's. A Haiku test
  task can run Bash.
- **Telemetry:** Live shows turns, peak context and cache tokens per task, and the 80% alert fires on a fake
  task.
- **Measured win:**
  - one real Part G build and one real review are run before and after the change;
  - target: at least 50% fewer total input tokens for the same work;
  - the result is reported with the job ids.

## 7. Delivery
| Step | Who |
|---|---|
| Spec review | Sonnet, low effort |
| Build | Sonnet. Order: reqs 1 and 5 (biggest per-turn win, little risk), then 2–3, 6–7, 10, then 4, 8, 9, 11 |
| Review | Codex `gpt-5.6-terra`, medium effort: it touches the runner launch and admission |
| Final | Opus, or Codex at high effort (no Fable while the owner's Fable pause stands) |
| Measure | Haiku: the before/after token comparison from the job logs |

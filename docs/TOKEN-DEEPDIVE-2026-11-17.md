# Token deep dive: intelligent controller and UI revamp (16–17 Nov 2026)

Written 17 Nov 2026, about 04:30 SAST. Scope:

- The two orchestrator chats that ran the work:
  - controller-intel: Claude Code session 73007b3e
  - ui-revamp: Claude Code session a70555bf
- Every fleet task created after 16 Nov 20:00 SAST (`created_at >= 1794852000`).

Sources:

- The chat figures come from the session transcripts (`~/.claude/projects/*/<id>.jsonl`): usage per assistant message,
  de-duplicated by message id.
- The fleet figures come from `agent_usage`, `tasks` and `events` in `master/controller/fleet.db`.
- `cost_usd` is cumulative per task, so the dollar figures below take the per-task maximum.

## 1. Where the tokens went

| Pool | Turns / runs | Cache reads | Output | Cost |
|---|---:|---:|---:|---:|
| controller-intel chat (Opus 5.5) | 102 turns | 12.0M | 61k | subscription, unmetered |
| ui-revamp chat (Opus 5.5) | 150 turns | 18.7M | 88k | subscription, unmetered |
| Fleet workers (Sonnet/Haiku) | 61 runs, 37 tasks | 30.0M | 0.95M | $16.90 |
| Fleet workers (Codex terra) | 2 final reviews | 2.9M | 20k | not priced |

Both chats peaked near the compaction window, at 179k and 199k tokens of context, and compacted 3 and 4 times.
In the chats, cache reads outnumber new output by about 200 to 1. What drives the cost is the number of turns times
a large context.

## 2. What went wrong, ranked by tokens

### 2.1 Bash denials make most worker tasks run twice (largest worker-side waste)

30 of the 37 first runs ended in `error` and then `task.blocked`. The controller auto-approved them
(`task.auto_approved`: 28 for `Bash`, 1 for `Read`) and resumed each one as a new run. The resumed runs read
**8.6M** cache tokens, 29% of all worker cache reads, much of it re-reading the session before carrying on. The
block adds wall-clock time and wakes the orchestrator one extra time per task.

Cause: `BASH_RULES` (`nodes/worker/profile.py:27`) pre-approves only `python3`, `git`, `uv run`, the gate scripts and
`npx vitest`. Workers routinely ran three kinds of command that these rules deny:

- read-only shell commands: `ls`, `grep`, `wc`, `cat`, `head`, `sed -n`, `find`;
- `npm ci` and `npm run build` in `master/controller/ui`;
- commands prefixed with `cd <dir> &&`, which never match a prefix rule.

Each denial stopped the run.

### 2.2 The orchestrator's MCP tools return whole task rows

`spawn_task`, `complete_task`, `cleanup_task` and `cancel_task` each return the full task record: about 5 KB,
including the prompt echo, policy, placement and gate argv. Tool-result bytes across the two chats:

| Tool | controller-intel | ui-revamp |
|---|---:|---:|
| complete_task | 64 KB | 214 KB |
| spawn_task | 82 KB | 136 KB |
| cleanup_task | 56 KB | — |
| cancel_task | — | 37 KB |
| fleet_status | 10 KB | 32 KB |

That is about 600 KB, roughly 150k tokens, of which perhaps 1% was used. A result stays in context until compaction,
so it is re-read on every later turn; compaction caps how long, so the true cost is a few million cache reads per
chat, not the whole. Correction after review: `spawn_task` is already trimmed in code (`master/mcp/server.py`
`_trim_on`), but the MCP server running that night predated the change, so it was not live. The remaining gap is
`complete_task`, `cleanup_task` and `cancel_task`, which still return raw rows, plus restarting the MCP server.

### 2.3 Browser screenshots in the orchestrator

The ui-revamp chat's `browser_batch` returned 246 KB, the largest single result: screenshots for visual checks that
then stayed in context. Visual QA belongs in a fleet `check` task (Haiku) or a subagent, not in the long-lived chat.

### 2.4 Work that produced nothing

Failed, cancelled and capped tasks used **7.1M** cache reads and cost **$4.35**:

- t-596eae55: cancelled after 3.5M
- t-e2c61590: failed after 2.0M
- t-bbee3b06: failed after 0.8M
- t-a724a961: rejected after 0.4M
- t-7a72dbf3: hit its cap after 0.3M
- t-70fc7b86: cancelled after 0.1M

Most were cancelled for a reroute or a re-scope after the run had already started. Three Codex builds were cancelled
while still waiting for a Codex slot. Those cost no worker tokens but added orchestrator turns.

### 2.5 Drafts ran on Sonnet again

Seven `draft` tasks (Workbench and other mocks) ran on Sonnet because they were explicitly pinned to it: 5.4M cache
reads and **$4.53**. The rule that drafts go to Haiku (`fleet-batch` skill, audit-16 #5) only works if the
orchestrator leaves `model` unset, and it named Sonnet anyway. A rule that relies on discipline failed the same day
it was written, so it has to be enforced in code.

### 2.6 What went right

- Reviews ran on Haiku: t-3947f855 cost $0.03, and each re-review $0.01–0.02.
- Final reviews went to Codex.
- Prompt sizes stayed small: a median of 71 bytes on resumed runs, and under 2 KB on first runs.

## 3. Advisor mode (Fable), measured

`claude -p --model sonnet --advisor claude-fable-5-2` works headless. The executor calls an `advisor` server tool,
and `modelUsage` reports Fable separately. One trial on a trivial prompt:

| Model | Usage | Cost |
|---|---|---:|
| Sonnet (executor) | 70k cache reads + 27.6k cache writes | **$0.12** |
| Fable (advisor) | 50.3k **uncached** input + 293 output | **$0.41** |

The advisor receives the whole transcript at full input price on every call. On a build with 150k of context, one
call costs about $1.20 of Fable (extrapolated from one trial, not measured). Advisor mode therefore pays only when the context is still small and the advice
changes the outcome.

Use it for:

- scope, spec and plan tasks;
- review and approval verdicts;
- the first planning turns of a hard build.

Avoid it on long build and fix runs, where the executor may call the advisor late, with a large context.

Whether it beats a separate Fable review task depends on how often the executor calls the advisor, so it has to be
measured per task. Section 4 #4 adds that measurement.

## 4. Fixes (built in this batch)

**1. Widen `BASH_RULES`.**

- Pre-approve read-only shell commands: `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `rg`, `sed -n` (never `sed:*`),
  `diff`, `pwd`. Not `echo`, `test` or `find` (workers use Glob instead).
- Pre-approve only `npm ci --ignore-scripts` and `npm run build`, never `npm run:*` (arbitrary scripts).
- Tell workers in the worker instructions that their cwd is already the worktree, so no `cd … &&` prefix.
- Re-running the denied command inside the same resume is deferred.
- Re-embed the runner.
- Where: `nodes/worker/profile.py`, plus the `nodes/runner.py` re-embed.
- Expected saving: about 8.6M cache reads per batch, and 30 block-and-resume cycles.

**2. Trim the lifecycle MCP results.**

- `complete_task`, `cleanup_task` and `cancel_task` reuse `spawn_task`'s key set and `_trim_on`, unless called with
  `full=True`. Errors pass through untrimmed (tested). Then restart the MCP server so `spawn_task`'s trim goes live.
- Where: `master/mcp/server.py`.
- Expected saving: roughly 100k tokens of results per chat, a few million cache reads per long chat.

**3. Enforce draft routing.**

- An explicit Sonnet or Opus model on `kind=draft` is downgraded to Haiku (`summary` is not a budget kind), unless
  `budget_override` is set, which stays an admin-only field. Admission records `routed_from`.
- Where: `master/controller/budget.py` or `intel.py`.
- Expected saving: about $4.5 per UI batch.

**4. Support advisor mode in the runner and record its usage (last, behind a flag).**

- A job field `advisor` (a model id) adds `--advisor` to the run.
- The advisor's usage is recorded from `modelUsage` as a second `agent_usage` row with model `claude-fable-5-2`.
- `spawn_task(advisor=...)` passes the field through.
- Advisor mode is allowed only for kinds review, re_review, final_review, draft, check and plan, and for build when
  the prompt is under 8 KB.
- Where: `nodes/runner.py`, `master/controller/tasks.py`, `master/mcp/server.py`, `intel.py`.
- Expected saving: Fable's judgement without full Fable sessions. To be measured, not assumed.

**5. Already in flight.**

- t-bffd7542: the estimator excludes cache reads, so estimates come out right.
- t-f14bd3bb: a usage limit at admission holds the task in the queue rather than blocking it, so no task sits blocked
  overnight.

**6. Practice changes in the `fleet-batch` skill.**

- Visual QA goes to a Haiku `check` task, not the chat.
- Re-scope before spawning, not after.
- One chat per batch.
- Where: `.claude/skills/fleet-batch/SKILL.md`.
- Expected saving: fewer orchestrator turns.

## 5. Not measured

- The dollar cost of the orchestrator chats, which run on the subscription.
- The real advisor call rate on fleet work. The first runs with #4 will give it, and the Budget page will show it as
  its own line.

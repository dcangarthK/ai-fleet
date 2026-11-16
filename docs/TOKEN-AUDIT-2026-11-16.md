# Token audit — 2026-11-16

Status: findings, 2026-11-16. Recommendations 1-10 have since been applied in the working tree (see each row);
#11 was deliberately not done. The gate passes. Nothing is committed.

Originally a read-only investigation. Nothing in this report was committed; no fleet tasks were
spawned to produce it.

Scope: every Claude Code session and every fleet task that ran on 2026-11-16, across the owner profile
(`~/.claude`), the fleet profile (`~/.claude-fleet`) and the spare profile (`~/.claude-fleet-spare`), plus the
Codex task on `codex@master/fleet`.

Every figure below names the command or file it came from. Anything not measured is marked **unmeasured** rather
than estimated.

---

## 1. Headline numbers

### 1.1 All sessions, all profiles

Source: every assistant message in `~/.claude*/projects/*/*.jsonl` with `timestamp` starting `2026-11-16`,
summing the `message.usage` block. Aggregation script: `agg.py` in the session scratchpad (re-runnable; it reads
the transcripts only).

| Bucket | Assistant msgs | Fresh input | Output | Cache write | Cache read |
|---|---:|---:|---:|---:|---:|
| `~/.claude` — ai-fleet orchestrator chats | 903 | 1,802 | 697,599 | 4,505,888 | **113,410,071** |
| `~/.claude-fleet-spare` — worker sessions | 865 | 1,758 | 1,036,709 | 3,706,808 | 56,126,280 |
| `~/.claude-fleet` — worker sessions | 369 | 742 | 879,449 | 2,087,847 | 16,845,636 |
| `~/.claude` — claude-mem compaction jobs | 36 | 72 | 249,290 | 683,610 | 0 |
| **Total** | **2,173** | **4,374** | **2,863,047** | **10,984,153** | **186,381,987** |

Fresh (uncached) input is negligible — 4,374 tokens for the whole day. **Essentially the entire day's token cost
is context being re-read on every turn.** This matches the measurement already written up in
`docs/SPEC-token-budget.md` §1 ("Almost all the cost is the context being re-sent on every turn").

### 1.2 One session dominates

Source: same aggregation, per session file.

| Session | Profile | Model | Assistant msgs | Output | Cache write | Cache read |
|---|---|---|---:|---:|---:|---:|
| `7527aebd` (orchestrator chat) | `.claude` | claude-opus-5 | 838 | 663,797 | 4,044,042 | **106,926,426** |
| `e051a8f0` | `.claude-fleet-spare` | claude-fable-5-2 | 38 | 125,224 | 921,706 | 891,448 |
| `beb2874d` | `.claude-fleet-spare` | claude-sonnet-5-5 | 148 | 162,061 | 559,987 | 11,450,153 |
| `a28ecb40` | `.claude-fleet-spare` | claude-opus-5-5 | 152 | 138,501 | 320,875 | 12,355,705 |

**The single orchestrator chat `7527aebd` accounts for 57% of all tokens read today** (106.9M of 186.4M cache
read), on Opus. Source for its composition (`agg2.py`): 840 assistant messages, 497 user/tool-result messages,
context peaked at **169,547** tokens, total tool-result payload 1.31 MB.

Its tool calls: `Bash` ×281, `update_plan` ×48, `spawn_task` ×24, `wait_tasks` ×21, `wrap_up` ×16, `ToolSearch`
×14, `task_status` ×13, `message_task` ×9, `fleet_status` ×8, `cancel_task` ×4, `gateway_health` ×3, everything
else ≤2. 281 Bash calls in one growing context is the shape `SPEC-token-budget.md` §3 requirement 4 ("No
long-lived leads") was written to prevent — and that requirement has not been applied to the owner's interactive
orchestrator profile.

### 1.3 Fleet tasks (worker side, as the controller recorded it)

Source: `sqlite3 ~/ai-fleet/master/controller/fleet.db` (snapshot taken with `.backup`), table `agent_usage`,
`ts >= 2026-11-16 00:00`. 22 tasks created today (`tasks` table, same date filter), 37 runs recorded.

| Model | Runs | Fresh input | Output | Cached | Cost (USD) |
|---|---:|---:|---:|---:|---:|
| claude-sonnet-5-5 | 22 | 619,637 | 187,705 | 11,550,943 | 5.51 |
| claude-opus-5-5 | 6 | 142,214 | 53,710 | 6,299,264 | 10.57 |
| claude-haiku-5-5 | 5 | 163,687 | 105,795 | 2,852,032 | 0.24 |
| claude-fable-5-2 | 3 | 99,518 | 29,082 | 409,941 | 6.31 |
| gpt-5.6-terra (Codex) | 1 | 86,817 | 7,848 | 448,000 | *(not recorded)* |
| **Total** | **37** | **1,111,873** | **384,140** | **21,560,180** | **22.63** |

**Caveat on cost:** `agent_usage.cost_usd` is *cumulative per task*, not per run (e.g. `t-634b11a4` runs 1–7 read
0.80 → 0.84 → 1.06 → 1.38 → 1.44 → 1.77 → 2.44). The column above therefore uses `max(cost_usd)` per task, not
`sum`. Summing the column naively gives $35.88, which is wrong by 59%. Codex cost is **unmeasured** — the
controller records no `cost_usd` for `gpt-5.6-terra`.

`mcp__local-llm__fleet_status` → `budget.models[].today_tokens` (input+output only, no cache) agrees in shape:
sonnet 807,342 · haiku 269,482 · opus 195,924 · fable 128,600 · codex 94,665.

**The fleet-side and transcript-side numbers overlap** — a fleet task *is* a `~/.claude-fleet*` session, so
§1.1 and §1.3 must not be added together. §1.1 is the authoritative total; §1.3 attributes the worker share of
it by `kind` and model.

### 1.4 Account pressure

Source: `fleet_status` → `nodes[master].runner.capacity`.

| Account | 5-hour window | Weekly window |
|---|---|---|
| `claude@master/fleet` | **100% used**, rejecting until 19:30 | **93%** |
| `claude/spare` | 67% | 22% |
| `codex@master/fleet` | 5% | 16% |

The fleet account was burned out while **Codex sat at 5% of its 5-hour window and 16% of its week**. The scarcest
model by policy was the least-used model in practice.

---

## 2. Where the tokens went

### 2.1 By fleet task kind

Source: `agent_usage` joined to `tasks`, grouped by `kind`.

| Kind | Tasks | Fresh input | Output | Cached |
|---|---:|---:|---:|---:|
| build | 4 | 415,319 | 126,201 | 11,882,750 |
| fix | 4 | 223,359 | 80,451 | 4,207,088 |
| draft | 4 | 192,419 | 70,545 | 2,854,710 |
| test | 2 | 90,270 | 67,082 | 1,567,136 |
| final_review | 4 | 101,012 | 30,890 | 496,453 |
| security_review | 1 | 86,817 | 7,848 | 448,000 |
| review | 1 | 2,677 | 1,123 | 104,043 |

`build` + `fix` = 16.1M cached, 75% of all fleet-task tokens. Two tasks carry most of it:

- **`t-634b11a4`** (fast-generator SharePoint build, Sonnet, spare): **7 runs**, 87 turns, 277,276 fresh input,
  75,422 output, **5,774,041 cached**. Final state `failed` (`failure_reason: gate_failed`) even though its
  round-4 output was the work that got delivered.
- **`t-8a007477`** (SharePoint verification harness, Opus, spare): 2 runs, 90 turns, 135,247 fresh input,
  **5,041,451 cached**. Run 1 alone was 88 turns / 4,763,658 cached. This is the third task in a chain —
  `t-0b52151f` (capped, 300,283 cached) → `t-f0bcb12a` (capped, 766,975 cached) → `t-8a007477` — so the
  verification harness cost **6.1M cached tokens on Opus across three task records**.

### 2.2 Tokens that returned nothing

Source: `agent_usage` joined to `tasks` where `state in ('cancelled','capped','failed')`:
**379,815 fresh input · 123,612 output · 9,185,896 cached.** That is **43% of all fleet-task cached tokens**.

That figure is an upper bound — `t-634b11a4` is `failed` by gate but its work was delivered. Excluding it, the
genuinely-wasted portion is **3.4M cached tokens**, broken down as:

| Task | Model | What happened | Cached |
|---|---|---|---:|
| `t-47138efa` | sonnet | Docs fix round 1 on the fleet account; **hit the 5-hour limit** mid-run (`result`: "You've hit your 5-hour limit · resets 7:30pm"). 71 turns of work lost. | 1,414,687 |
| `t-7e5a253d` | sonnet | A **duplicate** respawn of the above, spawned by hand 17 seconds after the controller had already handed the work off to `t-ba303078`. Redid the same 52 turns from scratch. | 2,084,979 |
| `t-f0bcb12a` | opus | Capped build continuation, superseded. | 766,975 |
| `t-19cd4721` | haiku | Adversarial test round, capped. | 465,877 |
| `t-0b52151f` | opus | Capped build, superseded. | 300,283 |
| `t-3b52e06a` | **fable** | Final re-review, **context cap**, no verdict. | 142,012 |
| `t-05b68a86` | **fable** | Final review, **context cap**, no verdict. | 131,466 |
| `t-713ce723` | opus | Cancelled (wrong toolset). | 104,043 |
| `t-22aceb3e` | opus | Final review, **turn cap**, partial verdict only. | 86,512 |
| `t-6ef218d5` | fable | Cancelled before any run. | 0 |
| `t-fb77887f` | sonnet | Probe task, failed with `error` before any run. | 0 |

The `t-47138efa` → `t-7e5a253d` pair is the single clearest waste: **3.50M cached tokens for one round of docs
fixes**. The automatic limit handoff worked as designed — `t-47138efa` was checkpointed and the controller started
`t-ba303078` on another slot (`routed_from: claude:claude-sonnet-5-5 (fleet)`), which finished the round in 18
turns and 296,661 cached tokens. The waste was a second, manual retry: `t-7e5a253d` was spawned against the same
parent 17 seconds later (`link=retry`, empty `routed_from`), redoing from scratch what was already running. The
orchestrator had no way to see the handoff, because the trimmed MCP task view did not surface `resolution`.

Two causes, both now closed: the first attempt was dispatched to an account already near its 5-hour limit, and
nothing refused a manual retry of a task the controller had already handed off.

### 2.3 Why the paid final reviews produced nothing

This is a concrete, fixable defect, not a judgement call. Source: `~/.ai-fleet/jobs/<task>/state.json`, `cap` key.

| Task | Model | Cap hit | Context at cap | Limit | Turns used |
|---|---|---|---:|---:|---:|
| `t-05b68a86` | claude-fable-5-2 | `context` | 131,468 | 100,000 | 6 |
| `t-3b52e06a` | claude-fable-5-2 | `context` | 142,014 | 100,000 | 4 |
| `t-22aceb3e` | claude-opus-5-5 | `turns` | — | 30 | 30 |

`nodes/runner.py:83` `DEFAULT_TOKEN_CAPS` has keys for `build`, `fix`, `review`, `re_review`, `test`. There is
**no `final_review` key**, so a final review falls into the `review` bucket: 30 turns / 100k context. A
whole-branch final review of a four-document docs batch cannot be done in 100k of context.

The lean worker profile itself is working as specced: `t-3b52e06a`'s turn 1 shows
`cache_creation=5,170 / cache_read=3,223` — about **8.4k of turn-1 context**, against the
`SPEC-token-budget.md` §3 requirement 1 target of ≤8k. The blow-up is entirely from the reviewer reading whole
documents: context went 8.4k → 142k in four turns (~33k per turn of `Read` output).

Net effect: three premium-model final reviews (two Fable, one Opus) spent 360,000 cached tokens and returned
"provisional verdict / review incomplete". `t-3b52e06a` was itself the second attempt at the review
`t-05b68a86` failed to finish — **the cap caused the duplicate paid review**.

### 2.4 Prompt bloat — not the problem today

Source: `tasks.prompt_bytes`. Largest prompt of the day: `t-713ce723` at 8,556 bytes. Median ≈ 3,600 bytes. The
`budget_rules` v3 cap is `max_kb: 40` / `hard_max_kb: 120`. **No task came within a fifth of the soft cap.**
`run_prompt_bytes` is 71 for every continuation run, i.e. continuations correctly carry a pointer, not a
transcript.

Briefs were not the problem. **Tool output inside the session was.** The orchestrator chat read 1.31 MB of
tool results, and the single `fleet_status` call made during this audit returned roughly 12k tokens of node
inventory, DMR model lists, firewall details and per-node metrics — for a question that needed one line about
account capacity.

### 2.5 Local models were barely used

Source: `call_usage`, today: `coder` 2 calls (2,289 in / 479 out), `fast` 2 calls (18 in / 2 out, 1 of 2 failed),
`reviewer` 1 call (failed, no tokens). Total local work for the day: **~2.8k tokens.**

Context (from the session work state): the LiteLLM gateway was down for part of the day — port 4000 had no
listener because the Docker daemon was not running — and all five role assignments showed unrouted. It was
restored at ~15:58. Before that, every piece of work that could have gone local went to a paid model instead.

### 2.6 Routing vs. the stated intent

The `CLAUDE.md` "Paid-model budget" table says: builds and fix rounds → Sonnet; summaries, triage, drafts →
local `fast`/`coder`, escalating to Haiku; final whole-branch review → Codex, once per batch.

| Work | Intended | What ran | Verdict |
|---|---|---|---|
| Fast-generator build, 4 rounds | Sonnet | Sonnet | matches |
| Docs drafting ×4 | local `coder`/`fast`, else Haiku | Sonnet ×3, Haiku ×1 | **3 of 4 over-routed**; 3.0M cached on Sonnet for doc drafting |
| SharePoint verification harness | Sonnet (not a hard build) | **Opus**, 3 task records, 6.1M cached | **over-routed** |
| Final whole-branch review | **Codex**, once per batch | Opus ×1 + Fable ×2, all capped, no verdict | **wrong model, and it failed** |
| Security review of `4f7f71f` | Codex | Codex `gpt-5.6-terra` | matches |

Codex ran **once** all day (448,000 cached, 5% of its 5-hour window) while the Claude fleet account hit 100%.

---

## 3. Recommendations, ranked by tokens saved per unit of effort

| # | Recommendation | Where it lands | Est. saving | Effort |
|---|---|---|---|---|
| 1 | **Add a `final_review` key to `DEFAULT_TOKEN_CAPS`** — `{"turns": 60, "context": 250_000}` — in `nodes/runner.py:83` and the matching `DEFAULT_CAPS` in `master/controller/tokens.py`. Re-run `python3 nodes/build.py` to re-embed the runner in `onboard.sh`/`onboard.ps1`. | `nodes/runner.py`, `master/controller/tokens.py` | ~360k cached/day on premium models, plus the duplicate review each cap causes | **Low** — one dict, one re-embed |
| 2 | **Refuse a manual retry of a task the controller already handed off, and show the orchestrator where the work went.** The automatic handoff already carries the dead run's checkpoint; what went wrong was a duplicate hand-spawned retry (see 2.2). A `link=retry` against a parent whose `resolution` is `handed_off` is now rejected with the replacement's id, and the trimmed MCP task view carries `handed_off_to`. | `master/controller/tasks.py`, `master/mcp/server.py` | 2.1M cached/day (the `t-7e5a253d` repeat alone) | **Low–Med** |
| 3 | **Pre-flight the account before dispatch.** The mechanism already exists: `near_limit_percent` in the budget rules, consumed by `headroom_until()`. Its default of 90 was too loose — the fleet account passed 90 with runs already in flight and those runs then died mid-task. Lowered to 85. | `master/controller/budget.py`; rule on `/app/budget` | 1.4M cached/day | **Low** |
| 4 | **Trim `fleet_status`.** Default to accounts, budget state, routed roles and node health only; put node inventory, DMR model lists, firewall detail and metrics behind `full=True`, as `wait_tasks`/`task_status` already do (`SPEC-token-budget.md` §3.11). Today's single call cost ~12k tokens; the orchestrator made 8. | `master/mcp/` server, `fleet_status` tool | ~90k/day in the orchestrator chat, more on heavy days | **Low** |
| 5 | **Route `kind: draft` away from Sonnet — by default, not by refusal.** Three of four doc drafts ran on Sonnet for 3.0M cached. `KIND_DEFAULT_MODEL` already sends an unnamed `draft`/`check` to Haiku, so the remaining gap is orchestrator discipline, now written into the `fleet-batch` skill. Refusing `draft` outright on Sonnet was tried and reverted: `draft` is also the fallback kind for a task that names none, so the rule refused every kind-less Claude task. Reason recorded in `budget.py`. | `fleet-batch` skill; `budget.py` comment | ~2.5M cached/day on a docs batch | **Low** |
| 6 | **A `fleet-batch` skill for the orchestrator**, holding the pipeline that got re-derived in prose all day: spawn N tasks with `kind`/`profile`/`stage`/`parent_id` set → `wait_tasks` (trimmed) → `task_diff` → merge → `complete_task` → `cleanup_task`. 24 `spawn_task` calls today each re-reasoned the same brief shape. Ship it as `.claude/skills/fleet-batch/SKILL.md` in `ai-fleet`. | new `~/ai-fleet/.claude/skills/fleet-batch/` | Cuts orchestrator turns, which is the 107M-token line item; **saving unmeasured** but it is the largest pool | **Med** |
| 7 | **Apply `SPEC-token-budget.md` §3 req 4 ("No long-lived leads") to the owner's interactive orchestrator too.** The runner already refuses an uncapped `lead`/`orchestrator` task (`nodes/runner.py:2642`), but the desktop chat is exempt — and it read 106.9M tokens in 838 turns at a 169k peak context. Practice change: one chat per batch, started from `get_plan`, ended at `put_plan`. | `docs/MASTER-INSTRUCTIONS.md`, "Token saving" section | Up to ~50M/day if a day's work splits into 3–4 chats instead of one | **Med** — habit + a written rule |
| 8 | **Review briefs must bound what gets read.** Add to the review brief template: the diff file list and the worktree path (never both the diff and the path), named line ranges, and "read only the changed files and their direct callers" — `SPEC-token-budget.md` §3 req 8, written but not enforced in the brief the orchestrator actually sends. Fable went 8.4k → 142k of context in 4 turns on `Read` alone. | brief template in the `fleet-batch` skill (#6) | ~130k per paid review | **Low** |
| 9 | **Send final whole-branch reviews to Codex, once per batch**, per the existing `CLAUDE.md` rule. Codex sat at 5% of its 5-hour window while three Claude final reviews capped out. This needs no code change, only the routing discipline — but #1 must land first, or Codex will hit the same cap. | practice; optionally a `budget_rules` preference for `final_review` | Moves ~360k/day off a saturated account onto an idle one | **Low** |
| 10 | **Fix the `cost_usd` semantics.** `agent_usage.cost_usd` is cumulative-per-task but reads as per-run; any `SUM()` over it — including on `/app/budget` — overstates by ~59%. Either store the per-run delta or rename the column `cost_usd_cumulative`. | `master/controller/` usage writer; `/app/budget` queries | No tokens; the budget page is currently wrong | **Low** |
| 11 | **Record Codex cost.** *Not done.* Codex reports no cost in `turn.completed`, so pricing it would mean a hardcoded price table — a guess presented as a measurement. `CLAUDE.md` already makes `rate_limits`, not dollars, the Codex budget signal, and the budget page shows "not reported" honestly. | — | None | — |

### Not recommended

- **Prompt-size limits.** Already comfortably under the cap (§2.4); tightening them would save nothing.
- **Cutting the auto-compact window.** `autoCompactWindow: 200000` is already at the configured floor, and the
  orchestrator's peak context was 169k — it never hit the window. The cost is turn *count* at a large context,
  not a single oversized context.

---

## 4. Unmeasured

- **Codex cost in USD** — not recorded by the controller for `gpt-5.6-terra`.
- **The dollar value of the orchestrator chat.** The transcripts carry token counts, not prices, and the owner's
  subscription is not metered per token; §1.4 (percent of window consumed) is the only usable proxy.
- **Savings from recommendation #6 and #7.** Both reduce orchestrator turn count, which is the largest token
  pool, but neither can be sized without running a batch under the new practice and re-measuring.
- **Tokens spent by sessions whose transcripts have rolled over or been cleared**, if any — the aggregation sees
  only `.jsonl` files present on disk at the time of the audit.

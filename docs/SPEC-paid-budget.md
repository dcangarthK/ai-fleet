# SPEC: paid-model budget, tiered reviews and the Budget page

Status: **approved by the owner** (2026-11-11). Builds on `docs/SPEC-task-lineage.md` (failure reasons,
`resets_at`). The rules this enforces are in `CLAUDE.md` → "Paid-model budget".

## 1. Problem

Paid models (Codex, Claude Opus/Fable/Sonnet) share account limits with the owner's own use, and the fleet burns
through them:
- Over the last 24 hours (fleet DB, 2026-11-11), Codex ran 23 tasks. 18 were reviews, they sent about 757 KB of
  prompt, and every one ran at reasoning effort `high`.
- Codex inherits `model_reasoning_effort = "high"`, plugins and memories from the owner's personal
  `~/.codex/config.toml`.
- Two prompts of 199 KB were mostly the minified console bundle, and one was sent while Codex was already limited.
- Reviews resend the whole diff every round. Several prompts carry the diff *and* tell the reviewer to read the
  worktree.
- The fleet records no token counts for agent tasks. The runner discards the usage that Claude and Codex report.
- Nothing stops a task going to a model that is over its limit, and nothing caps concurrent paid jobs.

Goal: measure spend, enforce the budget rules in the controller, make local models do the first review pass, and
give the owner one console page to watch and change all of it.

## 2. Measurement

### 2.1 Runner captures usage (`nodes/runner.py`)
- **Claude:** the stream-json `result` event carries `usage` (`input_tokens`, `output_tokens`,
  `cache_read_input_tokens`, `cache_creation_input_tokens`), `total_cost_usd` and `modelUsage`. Store them on the
  job as `usage`.
- **Codex:** `--json` emits `turn.completed` events with `usage` (`input_tokens`, `cached_input_tokens`,
  `output_tokens`). Sum them per run.
- Job field `usage`: `{"input": int, "output": int, "cached": int, "cost_usd": float|null, "model": str|null}`. It
  covers the current run and resets on resume. Missing fields stay null; the runner never estimates them.
- The builder confirms the exact event shapes against the installed CLIs (`claude --help`, a recorded
  `codex exec --json` run) and adds fixture-based parser tests. It must not guess.

### 2.2 Controller stores it
- New table `agent_usage(id, ts, task_id, run, agent, model, kind, input, output, cached, cost_usd, prompt_bytes,
  effort, outcome)`, written when a run settles. `outcome` is `ok`, `limit`, `error` or `cancelled`.
- `prompt_bytes` is the size of the prompt the controller sent, after the hygiene rules in 3.4.
- `GET /api/budget/usage?days=7&group=model|kind|day` returns aggregates. Local-model calls already in
  `call_usage` are folded in with `cost_usd = 0`.

## 3. Enforcement

### 3.1 Task kind and effort
- `POST /api/tasks` and `spawn_task` accept two new fields:
  - `kind`: one of `build`, `fix`, `review`, `re_review`, `final_review`, `security_review`, `merge`, `benchmark`,
    `check` or `draft`. Default `build` when a gate is set, otherwise `draft`. Pipelines set it themselves.
  - `effort`: one of `low`, `medium` or `high`. Default comes from the budget rules for (model, kind).
- The runner applies `effort`:
  - **Codex:** `-c model_reasoning_effort="<effort>"`.
  - **Claude:** the CLI's effort flag or setting, if it has one. The builder verifies this. If the CLI has none, the
    task records `effort: null` and nothing is faked.

### 3.2 Budget rules (stored, versioned, editable)
- New table `budget_rules(version, body, author, created_at)`. It is versioned like policies, and the latest
  version applies.
- Body shape:

```json
{
  "models": {
    "codex":                    {"tier": "scarce",  "max_concurrent": 1, "kinds": ["final_review","security_review","build"],
                                 "default_effort": {"final_review":"high","security_review":"high","build":"high","*":"medium"},
                                 "daily_tokens_soft": 2000000, "daily_tokens_hard": null},
    "claude:claude-opus-*":     {"tier": "premium", "max_concurrent": 1, "kinds": ["build","final_review","security_review","review"]},
    "claude:claude-fable-*":    {"tier": "premium", "max_concurrent": 2, "kinds": ["review","re_review","final_review","security_review"]},
    "claude:claude-sonnet-*":   {"tier": "standard","max_concurrent": 2, "kinds": ["*"]},
    "claude:claude-haiku-*":    {"tier": "cheap",   "max_concurrent": 2, "kinds": ["*"]}
  },
  "unlisted": "allow",
  "prompt": {"max_kb": 40, "hard_max_kb": 120, "max_line_chars": 1000,
             "exclude_globs": ["master/controller/ui/dist/**", "**/*.min.js", "**/*.bundle.js", "**/package-lock.json", "evals/results/*.json"]},
  "limits": {"on_limit": "queue", "fallbacks": {"codex": ["claude:claude-fable-5-2"], "claude:claude-opus-*": ["claude:claude-sonnet-5-5"]}},
  "effort_ladder": {"build": ["medium", "medium", "high"], "fix": ["low", "medium", "high"], "re_review": ["low", "medium"]},
  "limit_scope": "model",
  "review_exec": {"tools": ["Read", "Grep", "Glob"], "max_turns": 8, "task_minutes": 15},
  "reviews": {"stages": ["local", "paid"], "max_paid_rounds": 2, "max_codex_rounds": 1, "paid_skip_when": null,
              "security_paths": ["master/controller/auth.py", "master/controller/policy.py", "nodes/runner.py", "master/mcp/**"]}
}
```

- **Matching:** keys are `agent` or `agent:model-glob`. The most specific match wins. A Claude task with no model
  matches `claude:<the node's default model>` when the runner reports one, otherwise `claude:*`.
- **Validation:** `PUT /api/budget/rules` validates everything and returns 400 on unknown keys, bad kinds, negative
  numbers or bad globs. Admin only.
- **Seeding:** the controller seeds the rules above on first start. It never overwrites the owner's edits.

### 3.3 Admission checks
These run in `create_task` before node selection, cheapest first. Each refusal gives a specific reason.

1. **Kind allowed:** if `kind` is not in the model's `kinds`, return 400:
   `"codex is reserved for [final_review, security_review, build]; use claude:claude-sonnet-* for merge"`. The
   alternative named is the cheapest model whose `kinds` allows this kind. The body field
   `budget_override: "<reason>"` (admin only) bypasses this check and is recorded on the task.
2. **No paid checks:** `kind: check` on a paid model returns 400. Local models may run checks.
3. **Limit breaker:** if the model's latest settled task failed with `failure_reason = limit` and its `resets_at` is
   in the future, the model is *limited*. The action depends on `limits.on_limit`:
   - `queue`: the task is created `queued` with reason "codex limited until 2:30 PM" and starts automatically after
     `resets_at`.
   - `fallback`: the task goes to the first fallback model that allows the kind and is not limited, and the task
     records `routed_from`.
   - `refuse`: return 409.

   A successful task on that model clears the limit.

   **Scope:** whether Claude's limits are per model or per account is not settled. On 2026-11-11 Sonnet kept
   working while Opus and Fable were limited, then was limited itself about 20 minutes later. With
   `limit_scope: "model"` (the default), the breaker keys on the model. If two different models on the same account
   (`claude` or `codex`) are limited with overlapping `resets_at`, the whole account counts as limited until the
   later reset. Fallbacks to another model on a limited account are then skipped, which prevents falling back from
   Opus to Sonnet on an exhausted account. `"account"` treats any limit as account-wide.
4. **Concurrency:** while `max_concurrent` tasks of the model are running, new ones are created `queued` (reason
   "waiting for a codex slot") and start in FIFO order.
5. **Daily tokens:**
   - Past `daily_tokens_soft`, tasks still start, and an event `budget.soft` is sent once per day per model as a
     notification.
   - Past `daily_tokens_hard` (when set), it behaves like a limit until local midnight.

6. **Effort ladder:** for kinds listed in `effort_ladder`, the effort for a build or fix depends on how many gate
   rounds it has failed in its work item. A first build runs at `medium` and the third attempt at `high`, so high
   effort is spent only after cheaper attempts failed. An explicit `effort` on the request still wins.
7. **Paid review rounds:** a pipeline runs at most `reviews.max_paid_rounds` paid review rounds, and Codex at most
   `reviews.max_codex_rounds`. Past that, the pipeline goes to `escalated` with the note "paid review rounds
   exhausted", and the owner decides.

None of these ever approves, unblocks or widens permissions. They can only queue, reroute or refuse.

### 3.4 Prompt hygiene
- Before sending a prompt, the controller removes diff sections whose file matches `prompt.exclude_globs` and adds
  one line per removed file, e.g. `[omitted generated file master/controller/ui/dist/assets/index-x.js]`.
- This applies to pipeline review prompts and to any `/api/tasks` prompt containing `diff --git` sections.
- After stripping, a prompt over `prompt.max_kb` is handled as follows:
  - if it contains diff sections (pipeline or `spawn_task`), it is split by file area, reusing
    `_review_diff_chunks`, into one task per chunk under a shared parent with `link: review`;
  - otherwise it starts with a `budget.large_prompt` warning event;
  - it is refused with 400 only above `prompt.hard_max_kb` (default 120).

  Measured: read-only review tasks averaged 39 KB (Claude) and 45 KB (Codex) over three days, so refusing at 40 KB
  would block about half of them.
- **Safety net for minified content:** in sections that are kept, any added line longer than
  `prompt.max_line_chars` is replaced with `[line omitted: N chars]`. The file is listed on the task, so the owner
  can add a glob for it. This catches minified files that no glob names yet.
- **Single source:** rewrite `review_prompt` (`controller.py:1305-1307`). Today it includes the full diff *and*
  says "Read surrounding files here for context". It also runs the reviewer in the original repo
  (`controller.py:1367-1372`), which still holds the pre-change code, so any file it reads is stale. The new prompt
  says: "Review only the diff below. Read a file only when a line you need is ambiguous or outside the diff; name
  each file you read." The reviewer's working directory becomes the build worktree, read-only, so any file it does
  read is current. A prompt must never both include the full diff and ask the reviewer to read the worktree.
- **Fix rounds:** `pipeline.py:105` tells the builder "…and rerun the gate". Change it to say the runner runs the
  gate. Include only the failing tail of the gate output, capped at 4 KB, not the whole log.
- The stripped bytes are recorded on the task as `prompt_stripped_bytes`.

### 3.5 Re-reviews send the delta
- In pipelines, round ≥ 2 reviewer prompts contain:
  - the previous round's findings;
  - the diff between the previous round's reviewed tree and the current one;
  - a list of changed files.
- The full diff is not resent. To capture the reviewed tree, the controller records the build worktree's tree hash
  (`git write-tree` via the runner, with the index refreshed from the worktree) when each review starts.
- The tree hash is taken with a temporary `GIT_INDEX_FILE`, so the builder worktree's own index is never touched.
- **Empty delta:** if the delta is empty, or whitespace-only, the round is recorded as "no change since last
  review" and no paid review is started.
- These reviews get `kind: re_review` and the rules' effort for it (default `low`).

### 3.7 Review execution limits
Input tokens grow with every turn, because each turn resends the context. Today, review tasks run as
`--permission-mode plan` with no tool restriction (`runner.py:708-709`) and no turn cap. For review kinds:
- **Tools:** only `review_exec.tools` (default Read, Grep, Glob), passed as the allowed-tools list at launch.
- **Turns:** at most `review_exec.max_turns`, using Claude's `--max-turns` if the installed CLI has it. The builder
  checks with `claude --help`; for Codex, it uses the closest supported setting or records that none exists.
- **Time:** a `task_minutes` limit from `review_exec`, using the existing `policy.limits.task_minutes`
  (`runner.py:94`).
- **Turn count:** `num_turns` is read from the result event and stored in `agent_usage`.

### 3.8 Pre-approved tools at launch
`tasks.py:434` always launches with `allowed_tools: []`. Tools a policy pre-approves are only granted after a
denial, through a resume, up to three times (`tasks.py:535-548`), and each resume is a full extra paid run. Pass the
policy's pre-approved tools at launch instead.

### 3.6 The fleet's own Codex profile
- The runner starts Codex with `CODEX_HOME=~/.codex-fleet` when that directory exists.
- `master/setup.sh --services-only` creates it with a minimal `config.toml`: model, `model_reasoning_effort = "medium"`,
  no plugins, no memories and no notify hook. It is created with mode 0700 and the setup never copies or reads
  `auth.json`.
- **Claude is already isolated:** `setup.sh:108-115` sets `CLAUDE_CONFIG_DIR=~/.claude-fleet`, and that profile has
  no plugins. Keep it lean, and report `claude_profile` on the same terms as Codex.
- **Owner action:** sign in once with `CODEX_HOME=~/.codex-fleet codex login`. Until then the runner falls back to
  the default home and reports `codex_profile: "shared"` in `/api/fleet`, and the Budget page shows a warning.

## 4. Tiered reviews: local first

### 4.1 Stages
- **Stage 0, deterministic (free):** the gate, plus the existing exact-quote and identical-fix checks on any review.
- **Stage 1, local (free):** the fleet `reviewer` role (local model, chunked per file) reviews every pipeline diff
  first.
  - Its findings pass the exact-quote check. Findings that survive go to stage 2 as **hints**, which the paid
    reviewer must confirm or reject. The paid reviewer's verdict decides.
  - If the local reviewer is *trusted* (G2: held-out precision ≥ 0.9 and recall ≥ 0.5) and requests changes with
    high/medium findings that survive the quote check, the pipeline sends them to the builder **before** any paid
    review. That saves a paid round on obvious defects.
  - If it is untrusted, its findings are **recorded only**, for the agreement measure in 4.3. They are not added
    to the paid prompt: phi4 had 0 of 11 findings confirmed, so injecting them would spend paid tokens confirming
    noise and could anchor the reviewer.
  - Only trusted local findings become hints.
- **Stage 2, one paid reviewer:** Sonnet or Fable by default, per the rules. It receives the stage-1 hints plus a
  diff stripped as in 3.4.
- **Stage 3, Codex:** only when the diff touches `reviews.security_paths`, or for the final review of a batch
  (`kind: final_review`, started by the orchestrator, not per pipeline).

### 4.2 Skipping the paid stage (opt-in, off by default)
- `reviews.paid_skip_when` may be set to `{"max_changed_lines": 40, "paths_only": ["docs/**", "tests/**"]}`.
- When a diff is within it, the gate passed, a *trusted* local reviewer approved, and no security path is touched,
  the pipeline goes to `ready` without a paid review. The history notes "paid review skipped by budget rule".
- It is off until the owner turns it on.

### 4.3 Measuring the local reviewer on real work
- When a paid stage-2 or stage-3 reviewer confirms or rejects a stage-1 hint, record it in
  `review_agreement(pipeline_id, round, local_model, hint_id, confirmed bool)`.
- A paid finding that stage 1 missed is recorded as a miss.
- The Budget page shows live precision and recall per local model next to its G2 benchmark numbers. Live numbers do
  **not** change trust automatically. They inform the owner, who changes role assignments.

## 5. Budget page (console `/budget`)

One page, in the nav after Overview. It uses the existing design system, has no horizontal page scroll at phone
width, and every section has an empty state that explains how data gets there.

0. **Fleet only:** every meter and total is labelled "fleet tasks only". The owner's own interactive use draws on
   the same limits, so real headroom is lower than shown.
1. **Status strip:**
   - one card per paid model: OK, *Limited until 2:30 PM*, or *At soft budget*;
   - running/max concurrent, e.g. "Codex 1/1";
   - today's tokens against the soft budget, as a meter;
   - a Codex profile warning if `codex_profile` is `shared`.
2. **Usage:**
   - tokens (input/output/cached) and cost, where reported, per model for today, 7 days and 30 days;
   - a stacked bar per day by model;
   - a table by task kind with tasks, tokens, average prompt KB and limit hits.
   - Charts use the console's chart conventions (follow the dataviz skill when building).
3. **Savings:**
   - tokens handled by local models (from `call_usage`) next to paid tokens;
   - local stage-1 reviews run;
   - paid rounds avoided (trusted local sent fixes back first);
   - paid reviews skipped by rule;
   - prompt bytes stripped;
   - re-reviews sent as delta, and bytes saved.
4. **Rules (admin edits, others read):**
   - a form for the budget rules: per-model tier, allowed kinds (chips), default effort per kind, max concurrent,
     daily soft/hard tokens;
   - prompt limits and exclude globs;
   - limit behaviour (queue/fallback/refuse) and fallbacks;
   - review stages, security paths and the paid-skip rule.
   - Saving creates a new version. The page shows the diff against the previous version and lets the owner restore
     it.
   - A plain-language summary is generated from the rules, e.g. "Codex: final and security reviews and hard builds
     only, 1 at a time, high effort."
5. **Local reviewers:** G2 benchmark precision/recall, live agreement (4.3), trusted badge, and the role each model
   holds.
6. **Recent refusals and reroutes:** the last 50 admission decisions (refused, queued for limit or slot,
   rerouted), each with the reason and a link to the task.

## 6. Orchestrator integration
- The MCP `spawn_task` gains `kind`, `effort` and `budget_override`, and `fleet_status` returns each paid model's
  budget status.
- The orchestrator's MCP token has `operate` scope, so it can never use `budget_override`. Overrides are the
  owner's (admin) decision.
- `CLAUDE.md` "Paid-model budget" points to the Budget page as the source of truth. The orchestrator names `kind` on
  every task.

## 7. Acceptance criteria
1. A Claude task and a Codex task each record input/output/cached tokens (and cost for Claude) from fixture output.
   `GET /api/budget/usage` aggregates them by model, kind and day.
2. `kind: merge` on Codex is refused with 400 naming a cheaper model. The same request with `budget_override` from
   an admin is allowed and recorded.
3. `kind: check` on any paid model is refused.
4. After a Codex task fails with a limit message resetting at 14:30:
   - with `on_limit: queue`, a new Codex task is created `queued` and starts only after 14:30;
   - with `fallback`, it is created on the fallback model with `routed_from: codex`;
   - with `refuse`, the request returns 409.
5. With Codex `max_concurrent: 1` and one Codex task running, a second is queued and starts when the first settles.
6. A review prompt containing a `ui/dist` diff section is sent without it, with the omission line;
   `prompt_stripped_bytes` > 0. A stripped prompt over `max_kb` is refused (tasks) or split (pipeline reviews).
7. A round-2 pipeline review prompt holds the previous findings and only the inter-round delta, and its kind is
   `re_review`.
8. A Codex task with `effort: medium` passes `-c model_reasoning_effort="medium"`. With `~/.codex-fleet` present the
   runner sets `CODEX_HOME` to it, and setup never reads or copies `auth.json`.
9. Pipeline with stage 1:
   - the local reviewer runs before the paid reviewer;
   - quote-checked hints reach the paid prompt;
   - a trusted local request_changes with a surviving medium finding sends the build back before any paid review;
   - an untrusted local reviewer never blocks or skips anything.
10. `paid_skip_when` unset → a paid review always runs. Set, with a small docs-only diff, gate passed and a trusted
    local approval → ready with the history note. A diff touching a security path always gets a paid review.
11. `PUT /api/budget/rules` is admin only, validates, versions, and can be restored. Bad input → 400.
12. None of the new code paths approve, unblock or widen permissions.
15. A review task launches with only `review_exec.tools`, a turn cap where the CLI supports one, and the review
    time limit. `num_turns` is recorded.
16. A task whose policy pre-approves tools receives them at launch: there is no denial-then-resume round.
17. A pipeline fix message no longer asks the builder to rerun the gate, and carries at most 4 KB of failing gate
    output.
18. A re-review round with an empty delta starts no paid task. Taking the tree hash leaves the builder worktree's
    index unchanged.
19. Untrusted stage-1 findings never appear in a paid prompt, but are recorded for agreement.
20. With `limit_scope: model`, Opus and Fable limited with overlapping resets make the claude account limited, and
    a fallback to Sonnet is skipped.
21. A diff-bearing `spawn_task` prompt over `max_kb` is split. A non-diff prompt over it starts with a warning. Only
    prompts over `hard_max_kb` are refused.
13. The Budget page shows sections 1–6 with real data and empty states, and phones have no horizontal scroll.
14. `scripts/gate.sh`, `npx tsc -b`, `npx vitest run` and `npx vite build` pass, the rebuilt dist is committed, and
    existing tests are not weakened.

## 8. Test cases
- **Runner parsers:** recorded Claude stream-json and Codex `--json` fixtures, including a run with several turns, a
  resumed run, and output with no usage.
- **Rules:**
  - validation (each bad field) and model-key matching (exact, glob, unlisted, Claude with no model);
  - default effort resolution;
  - seeding never overwrites edits.
- **Admission:** each of 3.3 1–5 on its own and in combination (limited plus at concurrency; override plus
  limited, where the override does not bypass the limit).
- **Hygiene:** stripping keeps non-generated sections byte-for-byte; the size check is applied after stripping;
  pipeline review chunking.
- **Delta re-review:** tree hash recorded per round; the round-2 prompt contents.
- **Stages:** local first; hints quote-checked; trusted send-back; untrusted hint-only; skip rule on/off; security
  path; agreement recorded on confirm, reject and miss.
- **Codex profile:** env set when the directory exists; fallback reported as `shared`.
- **API:** usage aggregation; rules GET/PUT/restore; admission decision log.
- **Vitest:**
  - rule summary text;
  - meter and status card states;
  - kind table formatting;
  - the rules form round-trip, so an unchanged form produces an identical body;
  - the phone layout class for the rules form.

## 9. Outside input considered (Gemini; independent Opus 5.5 review, 2026-11-11)

The Opus 5.5 review added sections 3.7 and 3.8, the account-wide limit scope, splitting instead of refusing
large prompts, hints from trusted local reviewers only, the fix-round gate text, the `re_review` effort ladder,
empty-delta skips, the temporary index, and the "fleet only" labels.

**Adopted:**
- the effort ladder (3.3 rule 6);
- caps on paid and Codex review rounds (3.3 rule 7);
- the long-line safety net and the single-source review instruction (3.4);
- a lower default prompt cap (40 KB);
- local-token totals on the Budget page.

**Rejected, and why:**
- **A separate router service calling the OpenAI and Anthropic APIs directly.** The fleet runs the `claude` and
  `codex` CLIs on subscription accounts through the runner. The API path would need API keys, would be billed
  differently, and would bypass the runner's sandbox, worktrees and approvals.
- **Gating on logprob or self-reported confidence (0.80 / 0.85).** G3 measured these signals on our local models,
  and every one was dropped: none cut held-out error enough at usable coverage. Gating on them would be
  uncalibrated.
- **Local reviewers approving rounds 1 and 2 on their own.** Measured local review quality is low (phi4: 0 of 11
  findings confirmed). Local models give hints and, once G2 trusts them, early send-backs. They never replace the
  paid verdict, unless the opt-in skip rule says so.
- **Rewriting `config.toml` on every call to set effort.** Concurrent jobs would race on the file. Effort is passed
  per invocation (`-c model_reasoning_effort=…`) instead, with a static fleet profile.
- **A fixed 30-minute cooldown after a limit error.** We parse the exact reset time from the error (`resets_at`).
- **A second SQLite database in `/tmp`, and a raw-HTML `/dashboard` with an unauthenticated policy POST and logs
  rendered through `innerHTML`.** That would lose data on reboot, let anyone on the network change the rules, and
  allow model output to inject script. The Budget page lives in the existing console, with admin-scoped,
  versioned rules in `fleet.db`.
- **Running model-written Python through an unsandboxed `subprocess`.** Model code only runs inside the existing
  eval sandbox.
- **The model names it suggests** (`gpt-oss:20b`, `qwen3:8b`, `gpt-5.3-codex`, `claude-sonnet-4-6`). None is
  installed or measured here. Any local candidate goes through the catalog and the G2 benchmark first.

## 10. Delivery
- Built in two parts so each review stays small:
  - **Part A:** measurement, enforcement and Codex profile (sections 2, 3 and 6).
  - **Part B:** tiered reviews and the Budget page (sections 4 and 5).
- Each part follows the owner's flow: Sonnet builds, then Claude (the orchestrator) reviews until satisfied, then
  one Codex review (delta-only on re-check), then a Sonnet merge.
- Part B starts after Part A merges, and after the G2 benchmark is committed, because trust in local reviewers
  depends on it.
- **Out of scope:**
  - per-account limits for the owner's own interactive sessions (only fleet tasks are measured);
  - dollar budgets for subscription accounts (cost is shown where the CLI reports it, not enforced);
  - automatic changes to trust from live agreement.

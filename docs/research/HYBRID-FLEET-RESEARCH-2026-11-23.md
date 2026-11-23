# Hybrid fleet research and gap analysis (2026-11-23)

Question from the owner: how do paid agent systems spin up agents for allocated work, how do other engineers run
multi-agent coding fleets and hybrid local + paid setups, and how can the local models on Laptop 2 and Laptop 3 carry
real work once they are back online?

This document builds on, and does not repeat, `docs/research/FLEET-IMPROVEMENT-PLAN-2026-11-19.md` (P0–P3, already
Codex-reviewed). Fleet facts come from a live `fleet_status()` on 2026-11-23, `evals/results/LEADERBOARD.md`,
`evals/results/GROUNDING.md` and the 2026-11-19 audits. External claims carry a source and a reliability label:
**[primary]** (official docs or paper), **[review]** (independent literature review), **[vendor]** (sells the thing),
**[blog]** (practitioner, unverified).

## 1. Where the fleet stands today (short)

- One working node (`master`). Laptop 2 (`njQNgvoQiz11CNTRL`, 2 Ollama models) and Laptop 3 (registered twice as
  `niCskTF7xM11CNTRL` and `n1fGTutur811CNTRL`, Ollama + DMR) are unhealthy. No local role is routed.
- **Local models are one-shot only.** `agent="ollama"` sends one chat completion with no tools and no file access
  (`nodes/runner.py` `ollama_worker`). `CLAUDE.md` assigns "boilerplate, test drafts" to the local `coder`, but there
  is no path for a local model's output to become a commit, so that assignment cannot be carried out even when nodes
  are up.
- Measured local quality (`evals/results/LEADERBOARD.md`, 2026-11-15): on synthetic role checks the 14B models score
  85–100%. On **real diff review** they are weak: best recall 0.70 (`phi4:14b`) with a 0.50 false-positive rate; best
  precision 1.00 (`glm-4.7-flash`, 19.6 GB, master only) at recall 0.30. Grounded answering is strong:
  `qwen2.5-coder:14b` 97%, 0% hallucination, 100% correct abstain. Logprob and self-confidence signals failed
  calibration for every model; only `qwen3:8b` three-sample agreement survived.
- None of the P0 items from the 2026-11-19 plan has merged yet (only commit 214dc43 since).
- 64 git worktrees are registered on master; many are old `fleet/t-*` branches (disk was 0.6 GB free on 11-19).

## 2. How paid systems spawn agents, compared with ours

| Dimension | Claude Code subagents / Agent SDK | Claude Code workflows | OpenAI Codex (cloud, `codex exec`) | Anthropic research system | ai-fleet |
|---|---|---|---|---|---|
| **Delegation** | Parent writes one prompt string; subagent has its own system prompt, tool list, model | Script calls `agent()` per step with a typed schema for the result | One task per prompt; cloud tasks are proposed as PRs | Lead gives each worker an objective, output format, tool guidance and task boundaries | Free-text `prompt` + `kind`; brief shape re-derived each batch (plan P2.1) |
| **Context passing** | Fresh context; only the prompt goes in, only the final message comes out; project CLAUDE.md still loaded unless `omitClaudeMd` [primary] | Same, plus structured outputs feed the next step | Repo preloaded in sandbox; `AGENTS.md` hierarchy guides behaviour [primary] | Workers return condensed findings to the lead | Brief + worktree; result text parsed for findings (the `**H1.**` parser miss in `docs/HANDOVER.md`) |
| **Isolation** | Optional `isolation: worktree`; worktrees are not a security boundary (shared `.git`) [blog] | Same | Cloud: OpenAI-managed container, network off in agent phase; local CLI: OS sandbox [primary] | n/a | Worktree per task, no-push git config, mode ceiling per node, runner commits with `core.hooksPath=/dev/null` |
| **Parallelism** | Background by default, 20 concurrent, nesting depth 3 [primary] | `parallel()`/`pipeline()` primitives | Many cloud tasks at once [primary] | Lead fans out 3–10+ workers by task size | Bounded by account: 1 Codex, 2 Claude per account; deliberate (quota) |
| **Cost control** | `maxBudgetUsd` per query, ends with `error_max_budget_usd` [primary] | Size guideline per run | Plan quota; model and effort per task | Multi-agent ≈15× chat tokens; only for high-value tasks; token usage explains 80% of performance variance [primary] | Budget rules per kind/model (`budget.py`), per-kind token caps (`run_capped`), `model="auto"`; caps **discard** work (`capped` = $32.36 of nothing) |
| **Verification** | Up to the caller | Schema-validated outputs, adversarial verify steps | Tests in sandbox, human PR review | Separate citation/verification pass | Gate (`fleet-gate`) + independent reviewer; independence not checked on handoff (plan 0.2) |

What this says about us:

1. **We match the big pattern.** Orchestrator-worker, fresh context per worker, worktree isolation, tests as the gate
   and an independent reviewer are what the paid systems do too.
2. **We are behind on structured hand-offs.** The paid systems pass typed briefs in and typed results out. We pass
   prose and regex the verdict back out. That costs orchestrator tokens (F4 in the plan) and loses findings.
3. **Our cost cap throws work away.** `maxBudgetUsd` and our caps both stop a run, but neither checkpoints. The
   long-running-harness writeups solve this with a progress file and a resumable plan, not a bigger cap
   ([Anthropic, effective harnesses][harness]).
4. **Our parallelism is quota-bound by design, so idle time is the waste, not concurrency.** The paid systems assume
   elastic capacity. We have fixed windows; unused window is lost value (61.5% idle, AUDIT-B §5).

## 3. What the outside world does with local models

### 3.1 Route by verifying, not by predicting

- A literature review of routing found that routers which predict the right model from the prompt rarely beat a
  trivial baseline (RouterBench: none significantly outperformed the zero router). Cascades that decide *after* the
  cheap answer do better, and **the verifier decides whether a cascade works**: executed tests correlated 0.85 with
  correctness, multi-sample agreement 0.78–0.98, and a same-tier LLM judge correlated *negatively*
  [review] ([Qu, Stanford][qu]).
- Model Cascading for Code: each model writes solutions and tests; escalate to the bigger model when the best
  solution passes too few tests. Cost down 26% on average, up to 70%, at equal or better accuracy
  [primary] ([Chen et al., arXiv 2405.15842][chen]). EcoAssistant uses execution results the same way [review].
- **Implication for us:** our gate is already the strongest verifier available. A cascade keyed on gate pass/fail
  needs no learned router. Do not build a predictive router first.

### 3.2 Cheap first, escalate on signal

- Cheap-model code review as an escalation trigger: escalating to the strong reviewer only when the cheap one raised
  ≥1 finding found 120 verified bugs for $4.13 vs 108 for $4.15 with the strong model alone; ≥3 findings found 99 for
  $1.97. The cheap model's finding count predicted escalation better than a path-keyword rule. 50 PRs, thresholds
  chosen after the fact [vendor] ([Entelligence][entel]).
- Mid-run escalation on Terminal-Bench: a cheap model passed 69–70% vs 81–84% for a frontier model at ~1/28 of the
  cost per trial; triggers are trajectory failure signals with dwell-time and switch-count guards [vendor]
  ([GMI Cloud][gmi]).
- A small model asked to *classify* the request (a skill label; "other" escalates) is reliable, while asking it to
  judge difficulty is not; under 1% escalated in that product [vendor] ([Barnacle][barnacle]).
- Practitioner consensus: escalate on malformed output, failed validation, two failed local attempts, or context
  larger than the local KV budget [blog] ([openclawdc][openclaw], [mindstudio][mindstudio]).

### 3.3 What small local models can and cannot do in coding

- Agentic scaffolds (OpenHands, SWE-agent) need roughly a 32B-class model for scoped tasks; 7B runs but plans badly.
  OpenHands with Qwen3-Coder-30B: 47.6% on SWE-bench Verified in one paper [primary] ([Lita][lita]); fine-tuned
  Qwen2.5-Coder 7B/32B reach 23.4%/36.6% [primary] ([SWE-Dev][swedev]). Local reproduction often falls far short of
  published figures (1/10 resolved via Ollama in one report) [blog] ([mini-swe-agent #798][mswe]).
- **Architect/editor split:** a strong model writes the change in prose; a cheaper model turns it into file edits.
  Weak editors do better with whole-file or simple search/replace formats than unified diffs
  [primary] ([Aider architect mode][aider]).
- Our 16 GB laptops fit at most a 14B dense model (or `qwen3-coder:30b-a3b`, a mixture-of-experts with ~3B active,
  on Laptop 3). None of our local models should run an open-ended agent loop. They can carry **bounded, verifiable**
  work.

### 3.4 Keeping models resident and batched

- Ollama: `keep_alive` per request or `OLLAMA_KEEP_ALIVE` (default 5 min; `-1` pins); `OLLAMA_MAX_LOADED_MODELS`
  (defaults differ by version, check `ollama ps`); `OLLAMA_NUM_PARALLEL` gives each slot its own KV cache, so memory
  is the real ceiling. No continuous batching; multi-node Ollama means independent copies, not model parallelism.
  vLLM is the usual step up past ~5 sustained concurrent requests [blog] ([sitepoint][sp-vllm], [dev.to][devto-ka],
  [dev.to distributed][devto-dist]).
- **Implication:** on a 16 GB laptop, pin one model per node, `NUM_PARALLEL` 1–2, and queue work *per role per node*
  so a node never swaps models mid-batch. `CLAUDE.md` already says this ("batch work per role"); nothing enforces it.

### 3.5 Running many coding agents at once

- Review becomes the bottleneck at around 5–7 concurrent agents; git operations across worktrees should be
  serialised; passing tests per worktree does not prove the merged result works, so merge in an integration tree
  and re-test [blog] ([merge queue][mq], [augment][augment]).
- Worktrees isolate files, not ports, databases or `.git` hooks [blog] ([upsun][upsun]).

## 4. Which fleet work suits the local tier

Each line names a verifier, because the research says the verifier, not the model, makes a cheap tier safe.

| Work | Local model (by measured fit) | Verifier | Escalate when |
|---|---|---|---|
| Commit messages, task summaries, batch reports | `qwen2.5-coder:14b` (fast 100) | Format check; owner reads | Malformed |
| Gate-failure triage: shrink a failing gate log to the failing tests + first error lines for the fix brief | `qwen2.5-coder:14b` / `qwen3:8b` | Each quoted line must exist in the log (grounding quote check, `grounding.py`) | Quote check fails |
| Context gathering for briefs: "which files/lines implement X" over provided excerpts | `qwen2.5-coder:14b` (grounded 97, 0% halluc.) | Quotes verified against files | Abstains or quotes fail |
| Pre-review lint pass on a diff before the paid review | `glm-4.7-flash` (precision 1.00) on master; `qwen3:14b` on laptops | A finding sends the diff back to the builder before the paid review; silence proves nothing | Never replaces the paid review |
| Mechanical edits from an exact spec (rename, boilerplate, test scaffolds) via an edit harness | `qwen2.5-coder:14b`; `qwen3-coder:30b-a3b` on Laptop 3 | Runner applies search/replace blocks, then the gate | Edit fails to apply or gate fails twice → Sonnet |
| Test drafts from acceptance criteria | `qwen2.5-coder:14b` | Tests must run and fail before the build (tests-first) | Tests don't run → Sonnet |
| Real code review verdicts, design, security, multi-file builds | none | — | Paid tier only |

## 5. Gap analysis

| # | Gap | Evidence (ours) | What others do | Proposed fix | Size |
|---|---|---|---|---|---|
| G1 | **Local tier unreachable and unmanaged.** Dead and duplicate nodes stay `approved`; roles silently unrouted | `fleet_status` 2026-11-23; plan F1, 0.4, 0.5 | Health-checked pools; independent copies per worker [blog] | Ship plan 0.4/0.5 first; add a reconnect checklist per laptop; node `stale` after N failures; dedupe by hardware id | S |
| G2 | **Local models cannot produce commits.** One-shot only | `ollama_worker` in `nodes/runner.py` | Architect/editor split with simple edit formats [primary] | **Edit harness**: the runner sends spec + file contents, receives search/replace blocks, applies them in the worktree, commits, runs the gate. The model still gets no tools, shell or filesystem (rule in `CLAUDE.md` kept) | M |
| G3 | **No cascade.** Nothing tries cheap and escalates on the gate | Budget rules only set a floor; plan 3.2 | Verify-then-escalate cascades; 26–70% cost cut on code [primary] | For kinds `draft`, `test` and mechanical `build`: local attempt → gate → on fail, one local retry with the gate output → Sonnet, carrying the failed diff. Record per (kind, model) pass rate | M |
| G4 | **Weak verifier for non-code local output** | Logprob/confidence signals dropped; LLM judges unreliable [review] | Executed tests; multi-sample agreement | Use the gate for code; quote-checking (`grounding.py`) for text; agreement (3 samples) only where calibrated (`qwen3:8b`). Never let a local review verdict gate a merge | S |
| G5 | **Untyped briefs and results** | Findings parser miss (`docs/HANDOVER.md`); orchestrator re-derives briefs | Typed objective/format/boundaries in, schema out [primary] | Plan P2.1 typed briefs + a JSON result schema for `review` kinds, validated by the controller | M |
| G6 | **No residency or per-role batching across nodes** | `warm_models` preference only; per-request `keep_alive` | Pin models, size `NUM_PARALLEL` to memory, queue per model [blog] | Node config pins one model per laptop (`keep_alive: -1`); controller queues local work per (node, model) and drains it in batches; no model swaps mid-batch | S/M |
| G7 | **Capped or limited runs discard work** | `capped` $32.36; limit runs $103.10 (AUDIT-B) | Progress file + resumable plan; durable execution [primary] | Plan 3.3: the runner commits WIP on cap/limit and writes `PROGRESS.md`; the resume brief points at it | M |
| G8 | **Measurement too thin to route on** | `cost_source` NULL 611/633; prompt bytes constant | OTel GenAI attributes per run [primary] | Plan 0.7, unchanged; also record local runs (wall-clock, node, model) so the local tier has a cost too | S/M |
| G9 | **Idle paid windows** | 61.5% idle; windows expire unused | Queues filled ahead of time [blog] | Plan 3.4 backlog queue; local tier drains `draft`/`fast` work 24/7 with no window to lose | M |
| G10 | **Worktree hygiene** | 64 worktrees on a disk that hit 0.6 GB | Auto-clean on merge | Fold into plan 2.4 (merge closes itself) and 0.1 (disk alert) | S |
| G11 | **Integration testing after several merges** | Merges land one by one in `main`; post-merge gate run by hand | Merge queue in an integration tree [blog] | Low priority at our volume (≤3 concurrent builders); revisit if concurrency grows | — |

## 6. Proposed improvements (input to the Codex debate)

Ordered by value per effort. Items 1–3 are the existing P0; this research does not change them.

1. **Finish P0 of the 2026-11-19 plan** (independence on handoff, event rename, instruction cleanup, measurement,
   dead-node retirement, local-role transport). Nothing below is trustworthy without 0.7 and 0.4/0.5.
2. **Bring the laptops back with a pinned-model profile** (G1, G6): Laptop 2 pins `qwen2.5-coder:14b`; Laptop 3 pins
   `qwen3-coder:30b-a3b` (DMR) or `qwen3:14b`; master keeps `glm-4.7-flash` for pre-review. Re-run the invalid
   Laptop 3 benchmark once it is online.
3. **Local text work first, no new harness needed** (section 4 rows 1–4): commit messages, gate-log triage for fix
   briefs, grounded context gathering for briefs, pre-review lint. Each with its verifier. This is the cheapest way
   to move real volume and attacks the two biggest costs: fix rework and orchestrator context.
4. **Edit harness + gate cascade** (G2, G3) for `draft`, `test` and mechanical builds, behind a feature flag, measured
   against Sonnet on the same tasks before it routes real work.
5. **Typed briefs and schema results** (G5, plan P2.1).
6. **Checkpoint on cap/limit** (G7, plan 3.3).
7. **Backlog queue for idle windows** (G9, plan 3.4).

## Sources

[harness]: https://anthropic.com/engineering/effective-harnesses-for-long-running-agents
[mars]: https://www.anthropic.com/engineering/multi-agent-research-system
[sdk]: https://code.claude.com/docs/en/agent-sdk/subagents
[wt]: https://code.claude.com/docs/en/worktrees
[codex]: https://openai.com/index/introducing-codex/
[codexman]: https://developers.openai.com/codex/codex-manual.md
[qu]: https://web.stanford.edu/~shuhuiq/blog/review-routing-cascades.html
[chen]: https://arxiv.org/abs/2405.15842
[entel]: https://entelligence.ai/blogs/cheap-model-first-escalate-code-review
[gmi]: https://www.gmicloud.ai/en/blog/dynamic-model-escalation-for-coding-agents-a-gmi-router-experiment-across-two-harnesses
[barnacle]: https://www.barnacle.ai/blog/2026-08-17-when-the-small-model-knows-to-call-for-help
[openclaw]: https://openclawdc.com/blog/hybrid-routing-local-vs-frontier/
[mindstudio]: https://www.mindstudio.ai/blog/local-ai-vs-cloud-ai-hybrid-routing-strategy
[lita]: https://arxiv.org/pdf/2509.25873
[swedev]: https://arxiv.org/pdf/2506.07636
[mswe]: https://github.com/SWE-agent/mini-swe-agent/issues/798
[aider]: https://aider.chat/2024/09/26/architect.html
[sp-vllm]: https://www.sitepoint.com/ollama-vs-vllm-a-migration-guide-for-scaling-teams/
[devto-ka]: https://dev.to/jovan_chan_9500711396d4e6/ollama-keeps-reloading-the-model-fix-vram-unloading-cold-starts-and-model-swapping-2026-25b7
[devto-dist]: https://dev.to/runcai/ollama-distributed-inference-what-is-possible-what-is-not-and-when-to-move-beyond-local-serving-2ajl
[mq]: https://dev.to/jackm-singularity/ai-coding-agent-merge-queue-ship-parallel-changes-without-hidden-conflicts-46c1
[augment]: https://www.augmentcode.com/guides/how-to-run-a-multi-agent-coding-workspace
[upsun]: https://developer.upsun.com/posts/ai/git-worktrees-for-parallel-ai-coding-agents

- Anthropic, multi-agent research system — [link][mars] [primary]
- Anthropic, effective harnesses for long-running agents — [link][harness] [primary]
- Claude Agent SDK, subagents — [link][sdk]; Claude Code worktrees — [link][wt] [primary]
- OpenAI, introducing Codex — [link][codex]; Codex manual — [link][codexman] [primary]
- Qu, routing: predict the model or verify the answer? — [link][qu] [review]
- Chen et al., Model Cascading for Code — [link][chen] [primary]
- Entelligence, cheap model first code review — [link][entel] [vendor]
- GMI Cloud, dynamic model escalation — [link][gmi] [vendor]
- Barnacle, when the small model knows to call for help — [link][barnacle] [vendor]
- openclawdc, hybrid routing — [link][openclaw]; MindStudio, hybrid routing — [link][mindstudio] [blog]
- Lita — [link][lita]; SWE-Dev — [link][swedev] [primary]; mini-swe-agent issue 798 — [link][mswe] [blog]
- Aider, architect/editor — [link][aider] [primary]
- Ollama serving: [sitepoint][sp-vllm], [dev.to keep-alive][devto-ka], [dev.to distributed][devto-dist] [blog]
- Parallel agents and merge queues: [dev.to merge queue][mq], [Augment][augment], [Upsun][upsun] [blog]

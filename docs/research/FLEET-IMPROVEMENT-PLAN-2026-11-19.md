# Making the fleet a team: findings and improvement plan (2026-11-19)

This document answers the owner's question: what is the fleet actually doing today, where does the money and the
human attention go, and what would turn a pile of models into a team that gets the best out of each node.

Evidence comes from four places, all gathered on 2026-11-19:

- `docs/research/AUDIT-A-control-plane-2026-11-19.md` — the controller's limit, handoff, placement and budget paths,
  read function by function (fleet task `t-fe4f7aa0`).
- `docs/research/AUDIT-B-economics-2026-11-19.md` — every row of `agent_usage` and `tasks` in the live
  `master/controller/fleet.db`, 426 tasks and 633 usage rows (fleet task `t-91bc2e06`).
- `docs/research/AUDIT-C-human-in-the-loop-2026-11-19.md` — every point where a person had to step in, from the
  handovers and token audits (fleet task `t-3c3f70be`).
- A live `fleet_status(full=True)` snapshot taken while writing this, plus three web research passes on orchestration
  patterns, control-plane engineering and agent cost economics (§7).

Nothing in this document changes code. It is a plan for the owner to accept, reject or reorder.

## 1. What the fleet is today

**Nodes (live snapshot).**

| Node | OS | Role today | Health |
|---|---|---|---|
| `master` | macOS | The whole fleet: Claude (`fleet` + `spare` profiles), Codex, 7 Ollama models, `fleet-gate`, ceiling `full`, 4 job slots | Healthy, but **0.6 GB free disk** and `heartbeat: false` |
| `njQNgvoQiz11CNTRL` — MacBook M1 Pro | macOS | Ollama only (`phi4:14b`, `qwen2.5-coder:14b`), 1 job slot | Healthy, but on battery at 10% against a 30% floor, so `power.ok: false` |
| `niCskTF7xM11CNTRL` — laptop-0ir6d81o | Windows | Ollama (`qwen3:4b/8b/14b`) + Docker Model Runner (`gemma4:e2b`, `qwen3.5:9b`, `qwen3-coder:30b-a3b`) | **Unhealthy**, last seen ~5 days ago, 41,113 consecutive runner failures |
| `n1fGTutur811CNTRL` — laptop-0ir6d81o | Windows | Same models, different Tailscale address | **Unhealthy**, last seen ~1.3 days ago, 3,045 failures |

The two Windows rows are the same physical laptop registered twice under two addresses. Nothing prunes a node that
has failed forty thousand times in a row, so the inventory reads as four nodes when the fleet really has one working
machine plus a laptop on battery.

**Local roles are assigned but dead.** All five role assignments (`coder` x2, `fast` x2, `reviewer`) report
`routed: false` with `route_reason: "sync failed: All connection attempts failed"`. The team table in `CLAUDE.md`
hands "small, well-scoped functions, boilerplate, test drafts" to `coder`, "summaries, triage, commit messages" to
`fast` and independent diff review to `reviewer`. **None of that tier has been reachable.** Every one of those jobs
has therefore gone to a paid Claude model, and the economics in §2 follow directly from this one fact.

**Accounts and headroom at the snapshot.** Claude `fleet` 12% of the 5-hour window and 50% weekly; Claude `spare`
27% and **90% weekly**; Codex `gpt-5.6-terra` 27% and 42%. Codex allows 1 concurrent job, Claude 2 per account.

**Control plane.** ~14.8k lines across 27 files in `master/controller/`, a SQLite `fleet.db`, worktree-per-task
branches, an event log, a budget-rules table (version 3) enforced server-side per `kind`, and a Live page.

## 2. The six findings that cost the most

### F1. The local tier is a paper tier, and it is why spend looks like this

Priced spend is **$387.75 over 8.7 days**, every cent of it Claude; Codex and Ollama rows carry no price
(AUDIT-B §1). Of that, **$250.94 is `build` and $46.45 is `fix`** — 77% of all spend on two kinds. 69 Haiku runs cost
$1.18 in total, so cheap models are not the problem; the problem is that the work never reaches them.

With the role tier down, there is no "cheapest capable model" step at all. The budget rules enforce a floor (no Codex
for merges, no paid `check`), but nothing routes *down* into local models, because the local models are unreachable.

### F2. More than half of all spend is rework

Tasks reaching run 2: **163**. Run 3: **74**. Run 4: **33**. Spend on run 2 and later is **$226.06, 58.3% of priced
spend** (AUDIT-B §2). Separately, 246 Claude runs ended with outcome `error` for **$171.75**, the single largest
category, and 50 runs across 19 tasks ended on a usage limit for **$103.10**.

A run that ends in `error` or `limit` and then restarts pays the full context again. The fleet's dominant cost is not
thinking — it is re-reading.

### F3. The control plane has five writers for one decision

AUDIT-A found **five** places that park or move a task when a model hits a limit, not the three we knew about:
`handoff.handle_limit` (handoff.py:791), `capacity.requeue_on_limit` (capacity.py:285),
`tasks.requeue_limit_wait` (tasks.py:1018), the auto-approve hold (tasks.py:1288-1297) and the capacity move
(capacity.py:173-198). They compute three different wait times for the same limit (`resets + 2 min`, `resets`, and
the earliest reset across accounts), apply different eligibility rules, and write different fields.

There are also **three** slot selectors — `candidate_slots` (handoff.py:269), `rank_slots` (handoff.py:359) and
`placement`/`pick_node` (tasks.py:486) — with different data sources, different ordering and different scope. Which
one runs depends on how the task arrived, not on what the task is.

Two consequences are bugs, not just duplication:

- **A handed-off review can land on its builder's model.** `check_independence` runs only at spawn
  (tasks.py:1849, 1494-1514). `_claim_to_insert` (handoff.py:875-898) never calls it. This silently defeats the rule
  the whole pipeline is built on.
- **`requeue_limit_wait` emits `task.rerouted`** (tasks.py:1040) for a plain wait, so no reader of the event log can
  tell a handoff from a park.

### F4. The orchestrator is the most expensive worker in the fleet

One orchestrator chat took 107M of 186M cache reads in a day, 57% of everything (AUDIT-C §5, story 1). The rules in
`CLAUDE.md` and the `fleet-batch` skill require the session to re-derive, every batch: the brief shape, the routing
decision, the reviewer choice, the spend report, the merge steps and the limit picture — all of which are either
already enforced in code or are plain queries on tables the controller owns (AUDIT-C §3).

AUDIT-A §2 lists ten decisions the orchestrator still makes by hand. **Four of them (items 7–10) are already done
server-side** — waiting for a reset, queueing at a concurrency limit, stall reroute, pipeline delta re-review — and
exist only as instructions the session must still read and act on.

### F5. The instructions contradict themselves, so the orchestrator improvises

AUDIT-C §4 found seven live contradictions, including: step 7 of the pipeline says Claude does the final review while
the budget table and rule 1 say Codex; the reviewer table lists "Fable or Sonnet" while practice was Opus and Haiku;
`CLAUDE.md` gives onboard-script merge advice that `HANDOVER.md` calls wrong; the gate is both "never run it
yourself" and "run the tests before claiming it works". Six more rules are stale — they describe incidents the code
has since fixed.

Every contradiction becomes a judgment call, every judgment call becomes orchestrator tokens, and some become a
wrong-model run that costs money (drafts on Sonnet, twice, $4.53).

### F6. The fleet is idle 61.5% of the time, and not because of limits

Zero tasks were running for **129.1 of 209.8 hours** (AUDIT-B §5). Peak concurrency was 8, on 11-13. The paid
accounts were *not* all limited during that idle time — the logged limit windows total 29.4 + 6.1 + 1.9 hours. The
fleet is idle because nobody is driving it, not because it ran out of quota. Capacity and quota are not the
constraint; the orchestrator's attention is.

## 3. The shape to aim for

One sentence: **the controller becomes the scheduler of record, and the orchestrator becomes a thin client that
states intent and reads verdicts.**

Four principles, each matched to a finding:

1. **One decision, one owner, in code.** Every scheduling decision — park, resume, place, rank, choose a reviewer —
   has exactly one implementation that every entry point calls. (F3)
2. **Intent in, verdict out.** The orchestrator says *what* must be true, not *who* does it or *where*. Model, node,
   account, reviewer and wait are the controller's to pick. (F4)
3. **Work goes to the cheapest model that passes the gate, and the fleet proves which that is.** A live local tier,
   a tiered router, and a recorded pass rate per (kind, model). (F1, F2)
4. **The rules live in one place and are machine-checked.** If a rule can be enforced, it is code; if it cannot, it is
   one line in one file, dated, with its reason. (F5)

## 4. The work plan

Each item names the change, the evidence, and the smallest version that is worth doing. Sizes are rough: S = under a
day of fleet time, M = a few tasks, L = a batch of its own.

### P0 — stop the bleeding (do first, all small)

| # | Change | Why | Size |
|---|---|---|---|
| 0.1 | Free disk on `master` and alert under 5 GB | 0.6 GB free on the only node that can build; a full disk fails every task and every gate | S |
| 0.2 | Call `check_independence` inside `_claim_to_insert` (handoff.py:875-898) | A handed-off review can run on its builder's model (F3) | S |
| 0.3 | Rename the event `requeue_limit_wait` emits (tasks.py:1040) from `task.rerouted` to `task.limit_wait` | The event log cannot distinguish a park from a handoff (F3) | S |
| 0.4 | Retire dead nodes: mark a node `stale` after N consecutive runner failures, and de-duplicate two registrations of the same host | 41,113 failures still listed as an approved node (§1) | S |
| 0.5 | Fix the local-role transport (`sync failed: All connection attempts failed`) or mark the roles `unrouted` loudly in `fleet_status` | The whole cheap tier is dead and nothing says so (F1) | S/M |

### P1 — one owner per decision (the control-plane consolidation)

| # | Change | Why | Size |
|---|---|---|---|
| 1.1 | **One limit policy function.** A single `plan_limit(task) -> Park(until) \| Handoff(slot) \| Fail(reason)`; the three parkers plus the auto-approve hold and the capacity move all call it. One wait computation, one eligibility list, one set of fields | Five writers, three wait times (F3) | M |
| 1.2 | **One slot ranker.** Fold `candidate_slots` and `rank_slots` into the placement path, with the differences (fallbacks, cross-vendor, tier floors, builder exclusion) as explicit flags rather than separate code | Three rankers, outcome depends on entry point (F3) | M |
| 1.3 | **Decision records.** Every placement, park and handoff writes one row: inputs, candidates considered, rule applied, chosen slot. Live shows it | Today the only way to know why a task went somewhere is to re-read the code (F3, F4) | M |
| 1.4 | Make the three "already server-side" duties invisible to the orchestrator: delete rules 7, 8 and the stall-reroute line from the instructions | They cost tokens every batch and change nothing (F4, AUDIT-A §2 items 7–10) | S |

### P2 — make the orchestrator thin

| # | Change | Why | Size |
|---|---|---|---|
| 2.1 | **Typed briefs.** `spawn_task` accepts a structured brief: `goal`, `files_allowed`, `acceptance`, `context_refs` (file:line), `done_when`. The controller renders the prompt from a stored template per `kind` | 24 spawns in one night each re-reasoned the same brief shape (F4) | M |
| 2.2 | **`propose_reviewer(task)`.** The controller names the reviewer from `kind`, `build_attempts` and live headroom; the orchestrator passes no model for reviews | Reviewer choice is a rule lookup the controller already has the data for (AUDIT-A §2 item 1) | S/M |
| 2.3 | **`batch_report(ids)`.** One call returns per-task models, commit, gate, verdict, spend and limit hits — the "record per task" and rule-10 spend report, composed by SQL | Both are plain queries the session is told to write by hand (AUDIT-C §3) | S |
| 2.4 | **Merge closes itself.** A merge task ends with `complete_task` + `cleanup_task` and, where `_branch_merged` says so, the branch delete and the service restart | Seven tasks were left waiting on these calls; restarts sat pending for hours (AUDIT-C §2) | M |
| 2.5 | Rewrite `CLAUDE.md` step 7 and the reviewer table; delete the six stale rules AUDIT-C §4 lists; date every remaining rule with its reason | Seven contradictions, each one an improvised decision (F5) | S |

### P3 — get the best out of each model

| # | Change | Why | Size |
|---|---|---|---|
| 3.1 | **Revive the local tier and give it real work:** `qwen2.5-coder:14b` for boilerplate and test drafts, `glm-4.7-flash` for triage and summaries, `qwen3-coder:30b-a3b` on the Windows laptop for first-pass review. Gate output exactly as for paid models | 69 Haiku runs cost $1.18; the local tier costs nothing and has never been tried under load (F1) | M |
| 3.2 | **Tiered routing with recorded pass rates.** Record pass/fail per (kind, model) from the gate; route a kind to the cheapest model whose recorded pass rate clears a floor, and escalate on failure instead of starting at the top | 58.3% of spend is rework; escalation after a cheap attempt is cheaper than a premium first attempt (F2) | L |
| 3.3 | **Cap and checkpoint long runs.** Per-kind context caps exist for `final_review`; extend to all kinds, and have a worker checkpoint its partial result so a cap or a limit does not discard the work | `capped` tasks cost $32.36 and produced nothing; two of this audit's own three fleet tasks failed to deliver a committed document for exactly this reason | M |
| 3.4 | **Batch the fleet's idle hours.** A queue the owner can fill ahead of time, drained by the controller whenever headroom exists, so the 61.5% idle time does paid-for work | Idle time is attention-bound, not quota-bound (F6) | M |
| 3.5 | **Keep sessions warm where it pays.** Prefer `message_task` over a new spawn for a follow-up, and group a kind's tasks on one account so the prompt cache is hit, not refilled | Cached input is 95.5% of Claude input tokens; a cache miss is the expensive event (AUDIT-B §1) | S |

## 5. What stays with the owner

Unchanged, and worth saying plainly because the plan above moves a lot of decisions into code (AUDIT-C §2):

- Pushing to any remote, and all commits in `/lombard` repos.
- Approving a node, raising a mode ceiling, and `budget_override`.
- Cancelling work the owner commissioned.
- Any decision that exposes machines, accounts or a public repo, where a bad call cannot be undone.

What should stop being the owner's job: clearing Bash denials the policy already covers, re-spawning after a limit,
and remembering to restart a service after a merge.

## 6. How we will know it worked

Measure these before and after each phase, from `agent_usage` and `tasks` — all are already recorded:

| Metric | Today | Target after P1–P3 |
|---|---|---|
| Share of priced spend on run 2+ | 58.3% | under 30% |
| Runs ending `error` or `limit` | 296 runs, $274.85 | under a third of that |
| Spend per merged change | $250.94 of build spend, 15 tasks with a merged outcome | tracked per change, falling |
| Orchestrator cache reads per batch | 107M in the worst day | under 20M |
| Idle share of wall-clock | 61.5% | under 35% |
| Work done by local models | effectively zero | at least every `draft`, `fast` and first-pass review |

The last line of defence stays the gate: nothing counts as done because a model said so.

## 7. What the outside world does about this

Three web research passes were run alongside the audits. The patterns that map onto our findings:

- **Orchestrator-worker with a thin orchestrator.** Anthropic's own writing on building effective agents and on their
  multi-agent research system makes the same split we are proposing: the orchestrator decomposes and delegates, and
  each worker gets a clean, bounded context. Our F4 is the failure mode they warn about.
- **MAST, the multi-agent failure taxonomy** (Cemri et al., 2025) sorts failures into specification problems,
  inter-agent misalignment and verification gaps. Our F5 is specification; our F3 independence bug is verification.
- **Durable execution** (Temporal, DBOS, Restate) is the standard answer to "a long task died halfway and we paid for
  the context twice": the work is a workflow with checkpoints, and a retry resumes rather than restarts. That is
  exactly P3.3.
- **Human-in-the-loop as an interrupt, not a block** (LangGraph's interrupt model) is what P2 and AUDIT-C §2 describe:
  pause at the decision that needs a person, keep everything else moving.
- **Quota-aware scheduling.** Token-bucket admission against a known per-account budget, with queueing rather than
  retry storms, is ordinary rate-limit engineering; our budget rules already do the admission half and P1.1 does the
  waiting half.
- **Model cascades and routers** (the FrugalGPT and RouteLLM line of work) are the measured version of P3.2: try the
  cheap model, verify, escalate only on failure. Published results put large cost reductions at comparable quality,
  and the verification step is the part that makes it safe.
- **Author is not approver, and a merge queue gates the trunk.** Our independence rule is the same idea; P0.2 closes
  the hole in it.
- **OpenTelemetry's GenAI semantic conventions** give a standard shape for per-run cost, token and model attributes —
  worth adopting for the decision records in P1.3 rather than inventing fields.

## 8. Risks and open questions

- **Cost figures may be overstated.** AUDIT-B found 5 consecutive identical-cost pairs; if a session total is being
  written to each run, spend is overstated by the duplicated amount. `cost_source` is NULL on 611 of 633 rows. Worth
  one task to settle before using these numbers for a budget.
- **`run_prompt_bytes` is mostly a constant 71**, so per-run prompt size is not actually measured.
- **Idle time is measured from `started_at`/`ended_at`**, which includes queued and review spans; the 61.5% is a
  floor on idleness, not a precise figure.
- **Local model quality is unproven under load.** P3.1 must be gated on the same tests as paid work; if `qwen3-coder`
  cannot pass the gate on real tasks, the tier is worth only drafts and triage.
- **Consolidating five writers into one is a behaviour change.** It needs its own spec, its own tests written first,
  and a shadow-mode run before it takes over.

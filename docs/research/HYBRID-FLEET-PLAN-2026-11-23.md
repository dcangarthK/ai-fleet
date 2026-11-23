# Hybrid fleet plan: making local models carry real work (2026-11-23)

The plan below was argued in two rounds between Claude (proposer) and Codex (opposing reviewer). It is the end
product of the 2026-11-23 research batch.

- **Research and gap analysis:** `docs/research/HYBRID-FLEET-RESEARCH-2026-11-23.md`. Section 6 of that doc holds the
  plan as it stood before the debate.
- **Earlier plan this builds on:** `docs/research/FLEET-IMPROVEMENT-PLAN-2026-11-19.md`, items P0 to P3. Nothing in
  P0 has merged since it was written.
- **Debate:** fleet task `t-a76328b1`. Codex `gpt-5.6-terra`, high effort, read-only, two runs.
  - Round 1 verdict: CHANGES.
  - Round 2: Codex agreed with all four counter-arguments.

## 1. What the debate settled

| # | Point | Outcome |
|---|---|---|
| 1 | A gate-keyed cheap-first cascade (local → gate → paid) is the target design | **Agreed as the end state, not the next step.** The gate signal is incomplete: 268 of 426 tasks have no gate result. It also cannot tell bad code from a flaky or broken environment. Build the cascade last, with failure reasons coded, once measurements justify a threshold |
| 2 | Edit harness (G2): the runner applies search/replace blocks written by a local model, then runs the gate | **Shadow-only.** Needs typed briefs with `files_allowed` and acceptance criteria. Also needs: path allowlist, exclusion of symlinks and `.git`, each replacement matching exactly once, size limits, diff inspection, and paid independent review. The runner must not become the local model's way to write to the filesystem. No production build routing until a comparison against a paid baseline is done and the owner signs off |
| 3 | Local work in §4: commit messages and summaries | **Narrowed and agreed.** Only summaries the orchestrator consumes, and the orchestrator keeps the pointer to the source. Never a final artifact for the owner, never a source of correctness |
| 4 | Local work in §4: gate-log compression | **Agreed for shadow mode now.** The quote check for logs must be built. The existing check in `grounding.py:340` validates review diffs and does not cover logs |
| 5 | Local work in §4: grounded context gathering for briefs | **Contested, then parked.** Quote checks prove where a line came from. They do not prove the gathered set is complete. Revisit once typed briefs exist |
| 6 | Local work in §4: lint pass before the paid review | **Cut.** Silence proves nothing. `qwen3:14b` has no `reviewer_real` score. The measured 14B coder has 0.50 precision and a 0.50 false-positive rate, so it would cause paid rework |
| 7 | Local work in §4: test drafts | **Shadow-only.** A test that runs and fails only proves it fails, not that it is the right acceptance test |
| 8 | Order of the work | **Two parallel tracks**, not one serial chain (see section 2). They collide only if per-role batching changes admission or placement in `pick_node` (`tasks.py:502`, `:530`) |
| 9 | Code claims | `agent=ollama` is one-shot with no tools and no file access: confirmed (`runner.py:4512`). "`warm_models` is the only residency logic": refuted. Placement uses `warm_models_by_engine`, which includes DMR. The runner validates `keep_alive` (`runner.py:3622`). The production worker sends only model, messages and stream (`runner.py:4518`). So residency is observed, not enforced |
| 10 | `run_capped` as a per-kind token cap | **My error, corrected.** `run_capped` (`runner.py:641`) only times out and kills a subprocess. Per-kind caps live in `token_caps` |
| 11 | `qwen3-coder:30b-a3b` fits Laptop 3 | **UNVERIFIED.** It is not on the leaderboard. `glm-4.7-flash` (19.6 GB) runs only on master |

## 2. Priority order (agreed)

Track A changes the control plane (placement, budget, routing). Track B readies the local tier without changing
placement or budget. Item 1 is shared.

| # | Track | Item | Depends on | Done when |
|---|---|---|---|---|
| 1 | A+B | **P0 measurement and node health.** Plan items 0.7 (cost and gate provenance on every run, local runs included), 0.4/0.5 (nodes marked stale after failures, duplicates removed by hardware id, local-role transport) | — | Every run has a cost source and a gate result or a reason. Dead and duplicate nodes are no longer `approved`. Roles route |
| 2 | B | **Enforce residency in the production Ollama worker.** Send `keep_alive` per role. One model per small node. Prove that placement policy is unchanged | 1 (nodes healthy) | `ollama ps` on each laptop shows the pinned model surviving idle periods. `pick_node` is unchanged and its tests pass |
| 3 | B | **Re-benchmark healthy laptops** on a fixed held-out set of real tasks with a fixed comparison protocol. This includes the invalid Laptop 3 run (Ollama and DMR) and `qwen3-coder:30b-a3b` | 2 | Valid rows in `LEADERBOARD.md` per laptop; the merge of eval results does not overwrite good rows when transport errors occur |
| 4 | B | **Text-only shadows.** (a) Gate-log compression with a new quote check for logs. (b) Summaries the orchestrator consumes. Both are called by the orchestrator through `ask_model`, so no controller routing changes. Log both versions and compare | 1; models from 3 | Report on net orchestrator tokens, latency and correction rate per use. Goes to production only on an owner decision |
| 5 | A | **P1:** behavioural spec, regression tests, one decision policy, shadow cutover (plan P1.0–P1.3) | 1 | Plan P1 acceptance criteria |
| 6 | A | **P2.1 typed briefs and results,** including `files_allowed` and acceptance criteria; JSON result schema for review kinds | 5 | The controller validates briefs and results; the findings parser misses go away |
| 7 | A | **Checkpoint and resume on cap or limit** (plan 3.3): commit WIP and write `PROGRESS.md`; the resume brief points at it | 5 | A capped or limited run resumes without losing work |
| 8 | A | **Reason-keyed local → paid escalation**, plus shadow testing of the hardened edit harness against paid baselines | 3, 4, 6, 7 | Pass rates calibrated per (kind, model). Production routing only on an owner decision |

Not scheduled:
- G9, the backlog queue for idle windows (plan 3.4). Revisit after item 8.
- G11, the merge queue. For now only measure a post-merge integration gate.
- G10, worktree hygiene. Already covered by plan items 0.1 and 2.4.

## 3. Owner decisions

1. Approve revived nodes and any raised mode ceiling (item 1).
2. Approve the residency configuration and its memory trade-off on each laptop (item 2). Laptop 2 is proposed to pin
   `qwen2.5-coder:14b`. Laptop 3 is to be decided after item 3.
3. Decide whether a text shadow that succeeds becomes production text assistance (item 4).
4. Authorize production routing through the edit harness, and only after the comparison against a paid baseline
   (item 8).
5. Approve any budget override or rule change that spends paid quota.
6. Physical prerequisite: fix WSL and Tailscale on Laptop 3, and restart the runner on Laptop 2. Only the owner can
   do this. Track B is blocked until then.

## 4. Still contested or open

- **Context gathering for briefs (row 5).** Claude thinks it is useful once briefs are typed. Codex holds that quote
  checks cannot prove completeness. Parked; not on the order list.
- **Savings ceiling.** Real review, design, security and multi-file builds stay on paid models. Track B mainly saves
  orchestrator context and fix-round rework, not build spend. The savings are unknown until item 4 is measured.

## 5. Record

- **Built (docs only):** Claude Opus 5.5, this session.
- **Reviewed:** Codex `gpt-5.6-terra`, high effort, task `t-a76328b1` (kind `final_review`, two runs). The spawn as
  `kind="review"` was refused under budget rule "codex is reserved for [final_review, security_review, build]", so
  the debate ran as `final_review`.
- **Waivers:** none.
- **Code changed:** none.

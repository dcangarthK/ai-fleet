# AUDIT B: fleet economics, first ten days

Source: `/Users/duncansynthesis.co.za/ai-fleet/master/controller/fleet.db`, opened read-only (`mode=ro`) in one transaction. Not copied into the repo. The controller was still writing during the audit, so the snapshot is the state at its last write.

**Snapshot cutoff (UTC):** last `agent_usage` row 2026-11-19 21:12:45 (id 633); 426 `tasks` rows; last `events` row 2026-11-19 21:12:45.
**Window:** first task created 2026-11-11 03:26:55; last 2026-11-19 21:12:14. That is about 8.7 days, not ten. There are no tasks or usage rows on 2026-11-15 (140 events that day).

All costs are USD from `agent_usage.cost_usd`. Codex rows carry no cost, so Codex spend is **unpriced**, not zero. Ollama rows carry no cost or token counts. Priced total: **$387.75** (633 usage rows, 81 unpriced; all priced cost is Claude).

## Assumptions

- Day buckets use `date(ts,'unixepoch')` in UTC.
- **Cached and fresh input are separate.** `cached > input` in 578 rows, so `input` cannot include `cached`. Fresh input = `input`, cached = `cached`. This is an inference from the data; the write path is not checked.
- A **run** is one `agent_usage` row, keyed `(task_id, run)`. `tasks.runs` disagrees with the usage-row count for 63 tasks, so run counts below use usage rows unless stated.
- "Superseded" = `tasks.resolution.kind` of `done_elsewhere`. `handed_off` is reported separately.
- "Commit" is not a column. A proxy: no `outcome` with kind `merged`. Only 15 tasks have `outcome` set.
- Limit windows for idleness come from `budget.limit` events (`data.account`, `data.resets_at`), cut short by `limit_resets`.

## 1. Spend and tokens

**By model**

| Agent | Model | Runs | Unpriced runs | Cost USD | Fresh input | Cached input | Output |
|---|---|---:|---:|---:|---:|---:|---:|
| claude | claude-sonnet-5-5 | 370 | 4 | 192.66 | 7,979,266 | 190,527,212 | 2,575,384 |
| claude | claude-opus-5-5 | 78 | 1 | 153.60 | 3,655,878 | 93,527,679 | 897,010 |
| claude | claude-fable-5-2 | 22 | 0 | 32.73 | 1,024,938 | 3,603,035 | 206,754 |
| claude | claude-haiku-5-5 | 69 | 4 | 1.18 | 1,630,702 | 9,910,504 | 515,236 |
| claude | claude-haiku-4-5 | 20 | 0 | 6.23 | 380,728 | 14,129,146 | 88,163 |
| claude | claude-sonnet-4-6 | 2 | 0 | 1.36 | 73,702 | 326,717 | 11,962 |
| codex | gpt-5.6-terra | 54 | 54 | unpriced | 7,521,809 | 161,282,048 | 891,662 |
| codex | (NULL) | 9 | 9 | unpriced | 1,023,340 | 25,271,936 | 125,215 |
| ollama | 3 models | 9 | 9 | unpriced | no counts | no counts | no counts |

Totals: Claude 561 runs, $387.75, fresh input 14,745,214, cached 312,024,293, output 4,294,509. Codex 63 runs, fresh input 8,545,149, cached 186,553,984, output 1,016,877, all unpriced. Cached input is 95.5% of Claude input tokens.

**By kind** (all kinds with spend; `kind` is never NULL in `agent_usage`)

| Kind | Agent | Runs | Unpriced | Cost USD |
|---|---|---:|---:|---:|
| build | claude | 216 | 2 | 250.94 |
| fix | claude | 145 | 3 | 46.45 |
| review | claude | 64 | 1 | 36.51 |
| final_review | claude | 12 | 0 | 19.64 |
| security_review | claude | 8 | 0 | 12.78 |
| draft | claude | 30 | 0 | 11.79 |
| re_review | claude | 41 | 0 | 6.80 |
| test | claude | 22 | 1 | 2.11 |
| merge | claude | 22 | 2 | 0.73 |
| lead | claude | 1 | 0 | 0.02 |
| build | codex | 32 | 32 | unpriced |
| final_review | codex | 14 | 14 | unpriced |
| security_review | codex | 17 | 17 | unpriced |
| draft / review / check | ollama | 9 | 9 | unpriced |

**By day (UTC)**

| Day | Claude runs | Claude cost USD | Codex runs (unpriced) | Ollama runs |
|---|---:|---:|---:|---:|
| 11-11 | 16 | 58.70 | 4 | 0 |
| 11-12 | 143 | 123.91 | 10 | 7 |
| 11-13 | 107 | 81.14 | 16 | 2 |
| 11-14 | 43 | 22.51 | 7 | 0 |
| 11-15 | 0 | 0.00 | 0 | 0 |
| 11-16 | 93 | 65.57 | 3 | 0 |
| 11-17 | 97 | 26.48 | 17 | 0 |
| 11-18 | 57 | 8.86 | 6 | 0 |
| 11-19 (to 21:12) | 5 | 0.57 | 0 | 0 |

## 2. Rework

- Tasks with at least one usage row: 338 of 426. Tasks with none: 88 (26 have `runs = 0`).
- Tasks reaching run 2: **163**. Run 3: **74**. Run 4: **33**.
- Spend on runs 2 and later: **$226.06 across 295 runs, 58.3% of priced spend.**
- `re_review` rows: 42. Only 3 have `review_of` set, and those point at build or fix tasks, not reviews. So **0 re_review rows are linked to a review task.** The other 39 have `review_of` NULL. Time-based pairing is not checked here (UNVERIFIED).
- Gate results (`tasks.gate_result.state`), 158 tasks with a gate result:

| Kind | Failed | Passed | With gate result | Fail rate |
|---|---:|---:|---:|---:|
| build | 5 | 39 | 69 | 7.2% |
| fix | 6 | 45 | 55 | 10.9% |
| merge | 3 | 6 | 11 | 27.3% |
| test | 1 | 3 | 4 | 25.0% |
| review / draft / none | 0 | 10 | 19 | 0% |

Gate results are NULL for 268 of 426 tasks.

- Spend on work later ended by a cancel, a cap or a supersession. These classes overlap; the union row has no double counting.

| Class | Tasks | Runs | Cost USD | Share of priced |
|---|---:|---:|---:|---:|
| `state = capped` | 15 | 16 | 32.36 | 8.3% |
| `state = cancelled` | 35 | 34 | 6.61 | 1.7% |
| resolution `done_elsewhere` | 18 | 14 | 76.89 | 19.8% |
| resolution `handed_off` (not in union) | 19 | 36 | 50.43 | 13.0% |
| union of capped, cancelled, done_elsewhere | 68 | 64 | 115.86 | 29.9% |

## 3. Waste signals

| Class | Tasks | Runs | Cost USD | Share | Notes |
|---|---:|---:|---:|---:|---|
| Ended `capped` | 15 | 16 | 32.36 | 8.3% | build 9, final_review 3, review 2, test 1 |
| `failure_reason = limit` | 12 | 27 | 80.86 | 20.9% | all 12 end `failed` |
| Any run with outcome `limit` | 19 | 50 | 103.10 | 26.6% | Claude 18 runs, $53.80; Codex 2 runs unpriced |
| `state = cancelled` | 35 | 34 | 6.61 | 1.7% | 27 runs unpriced; 1 task has a merged outcome |
| Run outcome `error` (all agents) | — | 253 | 171.75 | 44.3% | Claude 246 runs, $171.75; Codex 7 runs unpriced |

The `error` row is not one of the classes requested, but it is the largest single spend category: 246 Claude runs with outcome `error`, $171.75. Its runs are counted per run, not per task, so it is not in the task-level union above; a task can have both an `error` run and a `limit` run, so the rows in this table are not additive.

## 4. Prompt size

`agent_usage.prompt_bytes` is non-NULL on all 633 usage rows. `tasks.prompt_bytes` is NULL on 60 tasks. `tasks.run_prompt_bytes` is NULL on 262 tasks, so the run-level column is mostly null and is reported only where present.

| Kind | n (`prompt_bytes`) | p25 | median | p75 | p90 | max |
|---|---:|---:|---:|---:|---:|---:|
| build | 102 | 1,138 | 1,763 | 3,141 | 3,836 | 9,820 |
| fix | 61 | 1,061 | 1,359 | 1,789 | 2,799 | 5,379 |
| review | 67 | 666 | 1,143 | 8,202 | 22,004 | 37,058 |
| re_review | 42 | 676 | 1,089 | 14,930 | 27,424 | 40,870 |
| final_review | 26 | 1,006 | 1,386 | 3,970 | 35,530 | 63,070 |
| security_review | 20 | 786 | 1,473 | 2,358 | 3,970 | 16,834 |
| draft | 21 | 1,417 | 2,182 | 2,569 | 3,208 | 6,221 |
| test | 12 | 284 | 1,342 | 2,907 | 3,213 | 3,689 |
| merge | 13 | 665 | 837 | 903 | 1,128 | 1,140 |

`run_prompt_bytes` median is 71 for build, fix, draft, merge and test. The values are mostly the same small number, so they are probably a placeholder, not a true run prompt (UNVERIFIED).

**Correlation.** Across 552 priced runs with a prompt size, Pearson r between log10(prompt_bytes) and log10(cost) is **0.02**. By size bin (cost per run; "capped/limit share" = run outcome `limit` or task `capped`):

| Prompt bytes | Runs | Median cost USD | Mean cost USD | Capped/limit share |
|---|---:|---:|---:|---:|
| < 5,000 | 501 | 0.339 | 0.690 | 5.4% |
| 5,000 – 9,999 | 23 | 0.804 | 1.358 | 30.4% |
| 10,000 – 19,999 | 7 | 0.302 | 0.427 | 0.0% |
| 20,000 – 39,999 | 17 | 0.119 | 0.292 | 0.0% |
| ≥ 40,000 | 4 | 0.604 | 0.731 | 0.0% |

Large prompts do not track higher cost. The only elevated capped/limit share is in the 5–10 KB bin, and that bin has 23 runs.

## 5. Concurrency and idleness

Busy time per task is taken from `started_at` to `ended_at` (open tasks run to the cutoff). This overstates active agent time, because it includes queued, review and blocked spans (UNVERIFIED). 26 tasks never started.

- Window with any task started: 209.8 hours (2026-11-11 03:26 to 2026-11-19 21:12).
- Peak concurrency: **8 tasks**, on 11-13.
- Zero-running time: **129.1 hours, 61.5% of the window.**

| Day | Peak concurrent | Busy hours |
|---|---:|---:|
| 11-11 | 6 | 9.2 |
| 11-12 | 7 | 23.7 |
| 11-13 | 8 | 13.5 |
| 11-14 | 2 | 4.7 |
| 11-15 | 0 | 0.0 |
| 11-16 | 4 | 8.0 |
| 11-17 | 5 | 19.3 |
| 11-18 | 4 | 2.1 |
| 11-19 | 3 | 0.1 |

Paid-account limit windows, from `budget.limit` events: `claude@master/fleet` 29.4 hours, `codex@master/fleet` 6.1 hours, `claude/spare` 1.9 hours. Zero-running time with at least one paid account outside its logged window is **129.1 hours**, which is all zero-running time. The paid accounts were not all limited at any point with zero tasks running. This relies on `budget.limit` being the only evidence of a limit (UNVERIFIED that no other limit states exist).

## 6. Ten most expensive individual runs

| # | Task | Kind | Model | Run | Cost USD | Outcome | Title |
|---:|---|---|---|---:|---:|---|---|
| 1 | t-fabbd0aa | build | claude-opus-5-5 | 3 | 19.82 | limit | Fleet lead: Part C finish + Part D (Live, heartbeats) — build, reviews, merges |
| 2 | t-c0bbd59e | build | claude-opus-5-5 | 1 | 12.98 | error | Part C: account pools and limit handoff (Opus 5.5 build) |
| 3 | t-c0bbd59e | build | claude-opus-5-5 | 2 | 12.98 | limit | Part C: account pools and limit handoff (Opus 5.5 build) |
| 4 | t-85afa645 | build | claude-opus-5-5 | 2 | 7.44 | ok | Part E controller (rank, detect, reroute) |
| 5 | t-85afa645 | build | claude-opus-5-5 | 3 | 7.44 | limit | Part E controller (rank, detect, reroute) |
| 6 | t-85afa645 | build | claude-opus-5-5 | 1 | 7.03 | error | Part E controller (rank, detect, reroute) |
| 7 | t-fabbd0aa | build | claude-opus-5-5 | 2 | 3.70 | limit | Fleet lead: Part C finish + Part D (Live, heartbeats) — build, reviews, merges |
| 8 | t-55caafe1 | build | claude-opus-5-5 | 2 | 3.66 | error | continue: SharePoint stage 3: gap fixes + full end-to-end simulation |
| 9 | t-8fbffa45 | security_review | claude-opus-5-5 | 4 | 3.36 | ok | Security review, final whole-branch, read-only |
| 10 | t-8fbffa45 | security_review | claude-opus-5-5 | 3 | 3.31 | error | Security review, final whole-branch, read-only |

Caveat on identical costs: t-c0bbd59e runs 1 and 2 both show 12.98, and t-85afa645 runs 2 and 3 both show 7.44. Across all runs there are 5 consecutive identical-cost pairs. These could be real, or a session total repeated on each run. Whether that is so is UNVERIFIED. If it is, spend is overstated by the duplicated amount.

## Data quality

- `cost_source` is NULL on 611 of 633 usage rows. 21 are `result` and 1 is `transcript`.
- Of 426 tasks, `outcome` is set on 15, `resolution` on 57, `failure_reason` on 58, `gate_result` on 158, `prompt_bytes` is NULL on 60, and `run_prompt_bytes` is NULL on 262.
- Several `run_prompt_bytes` values are a constant 71 (see section 4).
- Event counts: `task.capped` 24 and `task.cancelled` 35 (`events.type`). The task-state counts are in section 3.

No recommendations are made here.

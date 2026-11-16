# Part I intelligent controller: build report (16 Nov 2026)

## Evidence and the decisions it led to
| Evidence (see controller-intel-evidence.md) | Decision |
|---|---|
| Fable and Opus final reviews capped with no verdict while Codex sat at 5% | Ranking prefers Codex for final_review and security_review and scores headroom. Codex is allowlisted to final_review, security_review, build and lead only. |
| 11 budget refusals were fixed by hand (e.g. a Codex re_review re-kinded) | Refusals return ranked `suggest` entries, with the round reviewer first for a Codex re_review. |
| Near-limit dispatch plus a duplicate retry wasted 3.5M cached tokens | A near-limit filter and `limited_until` filter run in the ranking; the limit forecast is in `/api/intel/forecast`. |
| Drafts were over-routed to Sonnet | `model="auto"` picks the cheapest eligible model. Local models are ranked for draft, check and first-pass review. |
| `cost_usd` is cumulative per task | Every estimate and saving uses `MAX(cost_usd)` per task. |
| 64 tasks sat in review and 25% of cached tokens went to capped, failed or cancelled tasks | Cleanup candidates are limited to merged branches, rechecked and clean. Savings tracks the wasted share. |

## Spend on this build (fleet.db)
| Model | Tasks | Input+output tokens | Cached tokens | USD |
|---|---|---|---|---|
| Sonnet 5.5 | 5: test, 2 builds, 2 fixes | about 10.8k | about 571k | 2.16 |
| Haiku 5.5 | 1 round review | 91k | 148k | 0.03 |
| Codex gpt-5.6-terra (high) | 1 final review | 112k | 622k | not reported |
| Fable | 0 | | | |

Limit hits: none. Codex was at 5%/16% at the start.

## Measured savings, first use
- **Estimates against actuals.** The history-median estimates are coarse. `build` on Sonnet predicted 2.49M tokens; the actual was about 127k, because the history includes capped long builds. Estimates need a per-prompt-size basis before they are used to gate.
- **Spawn trim.** About 3k tokens less per `spawn_task` response once the MCP server is restarted. This batch made 7 spawns.
- **Bounded briefs.** The Haiku review used 148k cached tokens in 14 turns and gave a verdict. Audit-era final reviews capped at more than 140k with no verdict.
- **Baseline savings** (`intel.savings`, 24h before the epoch against the 6h after): the wasted share fell by 33 points and Codex's share of final reviews rose from 0 to 33%. `auto_picks`, `suggestions` and `cleanups` are 0 because the live controller does not run this code yet.

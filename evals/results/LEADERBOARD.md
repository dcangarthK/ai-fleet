# Local model leaderboard

Scores are % of the role's checks passed. 'Fits 16GB' = loaded size + 3GB headroom <= 16GB. Empty cell = role not evaluated for that model (never counted as 0). Rows are sorted by the mean score over the roles each model has, ties by number of roles. Generated 2026-11-11 19:40.

| Model | Coder | Reviewer | Fast | Reviewer_real | Grounded | Review JSON valid | Hallucination rate | Correct abstain rate | Think | Median s (last run) | Tok/s (last run) | Size GB | Fits 16GB |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen3:14b | 85 | 93 | 97 |  |  | 100 |  |  | unknown | 34.73 | 12.5 | 10.8 | yes |
| qwen3:8b | 74 | 89 | 97 | 62 | 90 | 100 | 13.3% | 86.7% | unknown | 12.02 | 30.4 | 6.0 | yes |
| glm-4.7-flash:latest | 96 | 93 | 100 | 31 | 87 | 100 | 0.0% | 93.3% | unknown | 71.11 | 19.9 | 19.6 | no |
| qwen2.5-coder:7b | 69 | 86 | 81 |  |  | 100 |  |  | unknown | 1.87 | 48.4 | 5.3 | yes |
| qwen2.5-coder:14b |  |  |  | 56 | 97 | 100 | 0.0% | 100.0% | unknown | 12.17 | 21.4 | 10.1 | yes |
| phi4:14b |  |  |  | 66 | 87 | 100 | 6.7% | 93.3% | unknown | 17.96 | 13.1 | 10.2 | yes |
| deepseek-coder-v2:16b | 85 | 61 | 78 |  |  | 100 |  |  | unknown | 1.25 | 108.0 | 11.4 | yes |
| llama3.2:latest | 71 | 64 | 67 |  |  | 100 |  |  | unknown | 1.37 | 87.9 | 3.4 | yes |
| qwen2.5:3b | 54 | 61 | 64 |  |  | 100 |  |  | unknown | 1.61 | 85.2 | 2.5 | yes |
| codellama:latest | 69 | 39 | 56 |  |  | 100 |  |  | unknown | 0.86 | 54.7 | 8.6 | yes |
| fleet-glm-4.7-flash-latest |  |  |  | 31 |  | 100 |  |  | unknown | 167.94 | 21.6 | 19.6 | no |

think: unknown (before think=false) for: qwen3:14b, qwen3:8b, glm-4.7-flash:latest, qwen2.5-coder:7b, qwen2.5-coder:14b, phi4:14b, deepseek-coder-v2:16b, llama3.2:latest, qwen2.5:3b, codellama:latest, fleet-glm-4.7-flash-latest. Their stored role results lack the `think` field, so thinking models may have run with thinking on. Capability per model is not recorded here; no model was re-run.

## Recommendation

- **coder**: glm-4.7-flash:latest (96%); on a 16GB laptop: deepseek-coder-v2:16b (85%)
- **reviewer**: glm-4.7-flash:latest (93%); on a 16GB laptop: qwen3:14b (93%)
- **fast**: glm-4.7-flash:latest (100%); on a 16GB laptop: qwen3:8b (97%)
- **reviewer_real**: phi4:14b (66%)
- **grounded**: qwen2.5-coder:14b (97%)

Treat role scores below ~70% as 'second opinion only'; tests and Claude make the final call.

## reviewer_real: high/medium issues

P = precision, R = recall, FP = clean cases with a serious issue. Holdout decides trusted reviewers.

| Model | Holdout P | Holdout R | Holdout FP | Dev P | Dev R | Dev FP | All P | All R | All FP | Unsupported | Identical fix |
|---|---|---|---|---|---|---|---|---|---|---|---|
| glm-4.7-flash:latest | 1.00 | 0.20 | 0.00 | 1.00 | 0.40 | 0.00 | 1.00 | 0.30 | 0.00 | 0 | 0 |
| fleet-glm-4.7-flash-latest | 1.00 | 0.20 | 0.00 | 1.00 | 0.40 | 0.00 | 1.00 | 0.30 | 0.00 | 0 | 0 |
| qwen3:8b | 0.67 | 0.40 | 0.00 | 0.75 | 0.60 | 0.33 | 0.71 | 0.50 | 0.17 | 4 | 0 |
| qwen2.5-coder:14b | 0.67 | 0.40 | 0.00 | 0.40 | 0.40 | 1.00 | 0.50 | 0.40 | 0.50 | 6 | 0 |
| phi4:14b | 0.60 | 0.60 | 0.33 | 0.67 | 0.80 | 0.67 | 0.64 | 0.70 | 0.50 | 3 | 0 |

## Grounding signal policy

Signals are kept only when a training-selected threshold halves held-out error at >=70% held-out coverage.

| Model | Signal | Decision |
|---|---|---|
| glm-4.7-flash:latest | logprob_min | dropped |
| glm-4.7-flash:latest | logprob_mean | dropped |
| glm-4.7-flash:latest | confidence | dropped |
| glm-4.7-flash:latest | agreement | dropped |
| qwen3:8b | logprob_min | dropped |
| qwen3:8b | logprob_mean | dropped |
| qwen3:8b | confidence | dropped |
| qwen3:8b | agreement | kept |
| phi4:14b | logprob_min | dropped |
| phi4:14b | logprob_mean | dropped |
| phi4:14b | confidence | dropped |
| phi4:14b | agreement | dropped |
| qwen2.5-coder:14b | logprob_min | dropped |
| qwen2.5-coder:14b | logprob_mean | dropped |
| qwen2.5-coder:14b | confidence | dropped |
| qwen2.5-coder:14b | agreement | dropped |

# Local model leaderboard

Scores are % of the role's checks passed. 'Fits 16GB' = loaded size + 3GB headroom <= 16GB. Generated 2026-11-11 17:31.

| Model | Reviewer_real | Review JSON valid | Median s | Tok/s | Size GB | Fits 16GB |
|---|---|---|---|---|---|---|
| phi4:14b | 66 | 100 | 17.96 | 13.1 | 10.2 | yes |
| qwen2.5-coder:14b | 56 | 100 | 12.17 | 21.4 | 10.1 | yes |
| glm-4.7-flash:latest | 31 | 100 | 71.11 | 19.9 | 19.6 | no |
| fleet-glm-4.7-flash-latest | 31 | 100 | 167.94 | 21.6 | 19.6 | no |

## Recommendation

- **reviewer_real**: phi4:14b (66%)

Treat role scores below ~70% as 'second opinion only'; tests and Claude make the final call.

## reviewer_real: high/medium issues

P = precision, R = recall, FP = clean cases with a serious issue. Holdout decides trusted reviewers.

| Model | Holdout P | Holdout R | Holdout FP | Dev P | Dev R | Dev FP | All P | All R | All FP | Unsupported | Identical fix |
|---|---|---|---|---|---|---|---|---|---|---|---|
| glm-4.7-flash:latest | 1.00 | 0.20 | 0.00 | 1.00 | 0.40 | 0.00 | 1.00 | 0.30 | 0.00 | 0 | 0 |
| fleet-glm-4.7-flash-latest | 1.00 | 0.20 | 0.00 | 1.00 | 0.40 | 0.00 | 1.00 | 0.30 | 0.00 | 0 | 0 |
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
| phi4:14b | logprob_min | dropped |
| phi4:14b | logprob_mean | dropped |
| phi4:14b | confidence | dropped |
| phi4:14b | agreement | dropped |
| qwen2.5-coder:14b | logprob_min | dropped |
| qwen2.5-coder:14b | logprob_mean | dropped |
| qwen2.5-coder:14b | confidence | dropped |
| qwen2.5-coder:14b | agreement | dropped |

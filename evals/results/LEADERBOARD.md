# Local model leaderboard

Scores are % of the role's checks passed. 'Fits 16GB' = loaded size + 3GB headroom <= 16GB. Generated 2026-11-11 13:16.

| Model | Grounded | Review JSON valid | Hallucination rate | Correct abstain rate | Median s | Tok/s | Size GB | Fits 16GB |
|---|---|---|---|---|---|---|---|---|
| qwen2.5-coder:14b | 97 |  | 0.0% | 100.0% | 3.09 | 24.7 | 10.1 | yes |
| glm-4.7-flash:latest | 87 | 100 | 0.0% | 93.3% | 23.09 | 26.1 | 19.6 | no |
| phi4:14b | 87 |  | 6.7% | 93.3% | 2.33 | 21.7 | 10.2 | yes |

## Recommendation

- **grounded**: qwen2.5-coder:14b (97%)

Treat role scores below ~70% as 'second opinion only'; tests and Claude make the final call.

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

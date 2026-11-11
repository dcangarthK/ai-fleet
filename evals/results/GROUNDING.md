# Grounding calibration

A signal is kept only when a threshold selected on the deterministic training split keeps at least 70% held-out coverage and cuts held-out error by at least half.

| Model | Score | Hallucination rate | Correct abstain rate | Quote validity |
|---|---:|---:|---:|---:|
| glm-4.7-flash:latest | 86.7% | 0.0% | 93.3% | 100.0% |
| qwen3:8b | 90.0% | 13.3% | 86.7% | 95.7% |
| phi4:14b | 86.7% | 6.7% | 93.3% | 97.4% |
| qwen2.5-coder:14b | 96.7% | 0.0% | 100.0% | 100.0% |

| Model | Signal | Decision | Threshold | Held-out coverage | Held-out error | Baseline error |
|---|---|---|---:|---:|---:|---:|
| glm-4.7-flash:latest | logprob_min | dropped | 0.9964 | 100.0% | 33.3% | 33.3% |
| glm-4.7-flash:latest | logprob_mean | dropped | 0.9991 | 100.0% | 33.3% | 33.3% |
| glm-4.7-flash:latest | confidence | dropped | 1 | 100.0% | 33.3% | 33.3% |
| glm-4.7-flash:latest | agreement | dropped | 0.6667 | 66.7% | 0.0% | 33.3% |
| qwen3:8b | logprob_min | dropped | — | — | — | — |
| qwen3:8b | logprob_mean | dropped | — | — | — | — |
| qwen3:8b | confidence | dropped | 0.99 | 75.0% | 33.3% | 25.0% |
| qwen3:8b | agreement | kept | 0.6667 | 75.0% | 0.0% | 25.0% |
| phi4:14b | logprob_min | dropped | 0.7443 | 75.0% | 33.3% | 50.0% |
| phi4:14b | logprob_mean | dropped | 0.9361 | 100.0% | 50.0% | 50.0% |
| phi4:14b | confidence | dropped | 1 | 100.0% | 50.0% | 50.0% |
| phi4:14b | agreement | dropped | 1 | 75.0% | 33.3% | 50.0% |
| qwen2.5-coder:14b | logprob_min | dropped | 0.9128 | 66.7% | 0.0% | 0.0% |
| qwen2.5-coder:14b | logprob_mean | dropped | 0.9559 | 66.7% | 0.0% | 0.0% |
| qwen2.5-coder:14b | confidence | dropped | 1 | 100.0% | 0.0% | 0.0% |
| qwen2.5-coder:14b | agreement | dropped | 1 | 100.0% | 0.0% | 0.0% |

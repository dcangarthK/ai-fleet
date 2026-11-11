# Local model leaderboard

Scores are % of the role's checks passed. 'Fits 16GB' = loaded size + 3GB headroom <= 16GB. Generated 2026-11-11 05:18.

| Model | Coder | Reviewer | Fast | Review JSON valid | Median s | Tok/s | Size GB | Fits 16GB |
|---|---|---|---|---|---|---|---|---|
| qwen2.5-coder:14b | 93 | 96 | 100 | 100 | 8.72 | 16.3 | 10.9 | yes |
| glm-4.7-flash:latest | 96 | 93 | 100 | 100 | 17.35 | 38.6 | 20.0 | no |
| qwen3:14b | 85 | 93 | 97 | 100 | 34.73 | 12.5 | 10.8 | yes |
| qwen3:8b | 76 | 93 | 97 | 100 | 9.95 | 37.4 | 6.6 | yes |
| phi4:14b | 86 | 96 | 81 | 100 | 11.01 | 11.2 | 11.6 | yes |
| qwen2.5-coder:7b | 69 | 86 | 81 | 100 | 1.87 | 48.4 | 5.3 | yes |
| deepseek-coder-v2:16b | 85 | 61 | 78 | 100 | 1.25 | 108.0 | 11.4 | yes |
| llama3.2:latest | 71 | 64 | 67 | 100 | 1.37 | 87.9 | 3.4 | yes |
| qwen2.5:3b | 54 | 61 | 64 | 100 | 1.61 | 85.2 | 2.5 | yes |
| codellama:latest | 69 | 39 | 56 | 100 | 0.86 | 54.7 | 8.6 | yes |

## Recommendation

- **coder**: glm-4.7-flash:latest (96%); on a 16GB laptop: qwen2.5-coder:14b (93%)
- **reviewer**: phi4:14b (96%)
- **fast**: glm-4.7-flash:latest (100%); on a 16GB laptop: qwen2.5-coder:14b (100%)

Treat role scores below ~70% as 'second opinion only'; tests and Claude make the final call.

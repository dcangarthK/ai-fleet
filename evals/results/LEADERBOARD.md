# Local model leaderboard

Scores are % of the role's checks passed. 'Fits 16GB' = loaded size + 3GB headroom <= 16GB. Generated 2026-11-10 23:35.

| Model | Coder | Reviewer | Fast | Review JSON valid | Median s | Tok/s | Size GB | Fits 16GB |
|---|---|---|---|---|---|---|---|---|
| glm-4.7-flash:latest | 96 | 93 | 100 | 100 | 17.35 | 38.6 | 20.0 | no |
| llama3.2:latest | 71 | 64 | 67 | 100 | 1.37 | 87.9 | 3.4 | yes |
| qwen2.5:3b | 54 | 61 | 64 | 100 | 1.61 | 85.2 | 2.5 | yes |
| codellama:latest | 69 | 39 | 56 | 100 | 0.86 | 54.7 | 8.6 | yes |

## Recommendation

- **coder**: glm-4.7-flash:latest (96%); on a 16GB laptop: llama3.2:latest (71%)
- **reviewer**: glm-4.7-flash:latest (93%); on a 16GB laptop: llama3.2:latest (64%)
- **fast**: glm-4.7-flash:latest (100%); on a 16GB laptop: llama3.2:latest (67%)

Treat role scores below ~70% as 'second opinion only'; tests and Claude make the final call.

# Model evals

Finds which local models are good enough for each fleet role, on your own hardware.

```bash
python3 evals/run_evals.py --models qwen2.5-coder:7b,qwen3:8b
```

Options: `--roles coder,reviewer,fast,grounded`, `--ollama http://<node>:11434` (evaluate on another node),
`--repeat N`, `--out evals/results`. Results accumulate in `evals/results/results.json`, so you can add models
one run at a time; `evals/results/LEADERBOARD.md` is rewritten each run.

## What is scored
| Role | Cases | Score |
|---|---|---|
| coder | `cases/coder.json`: write a function from a spec | share of hidden unit tests the extracted code passes (20s timeout, temp dir) |
| reviewer | `cases/reviewer.json`: 8 diffs with one seeded bug, 3 clean diffs | bug diffs: 0.5 for `request_changes` + 0.5 if an issue names the bug; clean diffs: 1 only if approved with no high/medium issue |
| fast | `cases/fast.json`: commit subjects, ticket labels | length/format/keyword checks; exact label |
| grounded | `cases/grounding.json`: answerable and unanswerable context questions | correct answers plus correct abstentions; hallucination, quote, and risk/coverage metrics are reported separately |

The reviewer prompt and JSON shape are the same as the fleet's `review_diff` tool, so the scores carry over.
Also reported: reviewer JSON validity, median latency, tokens/s, loaded size, and whether the model fits a 16GB
laptop (loaded size + 3GB headroom).

## Adding cases
Append to the JSON files. For coder cases, add a reference solution to `REFERENCE` in `tests/test_evals.py`:
the tests check every case is solvable and has at least 3 test methods (so scores aren't all-or-nothing).
Run `python3 -m unittest tests/test_evals.py`.

Model-written code runs on this machine (isolated temp dir, timeout, no extra environment). Only evaluate
models you chose to install.

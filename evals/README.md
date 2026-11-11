# Model evals

Finds which local models are good enough for each fleet role, on your own hardware.

```bash
python3 evals/run_evals.py --models qwen2.5-coder:7b,qwen3:8b
```

Options: `--roles coder,reviewer,fast`, `--ollama http://<node>:11434` (evaluate on another node),
`--repeat N`, `--out evals/results`. Results accumulate in `evals/results/results.json`, so you can add models
one run at a time; `evals/results/LEADERBOARD.md` is rewritten each run.

## What is scored
| Role | Cases | Score |
|---|---|---|
| coder | `cases/coder.json`: write a function from a spec | share of hidden unit tests the extracted code passes (20s timeout, temp dir) |
| reviewer | `cases/reviewer.json`: 8 diffs with one seeded bug, 3 clean diffs | bug diffs: 0.5 for `request_changes` + 0.5 if an issue names the bug; clean diffs: 1 only if approved with no high/medium issue |
| reviewer_real | `cases/reviewer_real.json`: 16 diffs from this repo's history (10 confirmed defects, 6 clean), split dev/holdout (5 bugs + 3 clean each; both trap cases in holdout) | as reviewer, plus precision/recall/fp_rate (below) |
| fast | `cases/fast.json`: commit subjects, ticket labels | length/format/keyword checks; exact label |

Reviewer roles also store `metrics` per split (`dev`, `holdout`, `all`) in `results.json`, over high/medium issues
that pass the grounding checks: **recall** = bug cases with an issue naming the bug / bug cases; **precision** =
issues naming a bug case's bug / all issues raised (any on a clean case is wrong); **fp_rate** = clean cases with any
such issue / clean cases; plus `unsupported` and `identical_fix` drop counts. A catalog model is a **trusted
reviewer** when its reviewer_real holdout precision >= `FLEET_TRUSTED_PRECISION` (default 0.9) and recall >= 0.5;
a pipeline trusts a role reviewer automatically when every model assigned to that role is a trusted reviewer.

The reviewer prompt and JSON shape are the same as the fleet's `review_diff` tool, so the scores carry over.
Also reported: reviewer JSON validity, median latency, tokens/s, loaded size, and whether the model fits a 16GB
laptop (loaded size + 3GB headroom).

## Adding cases
Append to the JSON files. For coder cases, add a reference solution to `REFERENCE` in `tests/test_evals.py`:
the tests check every case is solvable and has at least 3 test methods (so scores aren't all-or-nothing).
Run `python3 -m unittest tests/test_evals.py`.

Model-written code runs on this machine (isolated temp dir, timeout, no extra environment). Only evaluate
models you chose to install.

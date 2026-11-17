# Token audit, 16 November 2026

Written on 17 November 2026 from `master/controller/fleet.db`. The window is the South African calendar day of
16 November 2026 (epoch 1794780000 to 1794866400). Every figure below comes from the `agent_usage` table, which
records one row per model call with its task, model, account and cost.

## 1. Spend by account

| Account | Calls | Input | Output | Cache reads | Cost (USD) |
|---|---:|---:|---:|---:|---:|
| `claude/spare` | 55 | 1,424,027 | 527,923 | 27,714,171 | 39.42 |
| `claude@master/fleet` | 33 | 1,098,143 | 497,137 | 15,479,642 | 23.58 |
| `codex@master/fleet` | 3 | 350,604 | 27,834 | 3,329,792 | not priced |
| **Total** | **91** | **2,872,774** | **1,052,894** | **46,523,605** | **63.00** |

Codex usage is not priced in `agent_usage`, because the Codex account reports its consumption as a percentage of
a rolling window rather than as a dollar figure. Its three calls are included in the token totals.

## 2. Spend by model

| Model | Calls | Input | Output | Cache reads | Cost (USD) |
|---|---:|---:|---:|---:|---:|
| `claude-sonnet-5-5` | 72 | 2,002,734 | 797,151 | 33,414,223 | 42.84 |
| `claude-opus-5-5` | 6 | 142,214 | 53,710 | 6,299,264 | 13.49 |
| `claude-fable-5-2` | 3 | 99,518 | 29,082 | 409,941 | 6.31 |
| `claude-haiku-5-5` | 7 | 277,704 | 145,117 | 3,070,385 | 0.36 |
| `gpt-5.6-terra` | 3 | 350,604 | 27,834 | 3,329,792 | not priced |

Two observations worth acting on.

The first is that cache reads dominate everything else. Across the day they are 46.5 million tokens against 2.9
million of fresh input: sixteen cache reads for every new token sent. That is the signature of long agent
sessions, where each turn re-reads the whole conversation. It is also why the orchestrator's own chat, not any
individual build, is the fleet's largest consumer.

The second is that Fable cost 6.31 dollars for three calls, or 2.10 dollars each, while Haiku cost 0.36 dollars
for seven. Fable is doing exactly what the budget rules reserve it for — final reviews — and the per-call price
is the reason those rules exist.

## 3. The most expensive tasks

| Task | Kind | Model | Account | Input | Output | Cache reads | Cost (USD) |
|---|---|---|---|---:|---:|---:|---:|
| `t-634b11a4` | build | sonnet-5-5 | spare | 277,276 | 75,422 | 5,774,041 | 9.73 |
| `t-8a007477` | build | opus-5-5 | spare | 135,247 | 46,819 | 5,041,451 | 5.95 |
| `t-22248ad6` | build | sonnet-5-5 | fleet | 128,554 | 75,267 | 2,778,072 | 3.15 |
| `t-f0bcb12a` | build | opus-5-5 | spare | 2,052 | 2,569 | 766,975 | 2.97 |
| `t-596eae55` | build | sonnet-5-5 | spare | 129,349 | 48,606 | 3,477,602 | 2.63 |
| `t-0a7f1c89` | final_review | fable-5-2 | fleet | 99,456 | 24,298 | 136,463 | 2.59 |
| `t-3a6a3254` | draft | sonnet-5-5 | fleet | 96,000 | 67,192 | 2,452,660 | 2.57 |
| `t-05b68a86` | final_review | fable-5-2 | fleet | 31 | 2,030 | 131,466 | 2.01 |
| `t-0b52151f` | build | opus-5-5 | spare | 744 | 1,391 | 300,283 | 1.92 |
| `t-47138efa` | fix | sonnet-5-5 | fleet | 93,812 | 34,415 | 1,414,687 | 1.72 |

Two of these are routing mistakes rather than expensive work.

`t-3a6a3254` is a `draft` that ran on Sonnet and cost 2.57 dollars. The budget rules send drafts and summaries to
Haiku or a local model; leaving the model unset on a `draft` would have produced the same output for a fraction
of the price. Haiku's entire day, seven calls, cost 0.36 dollars.

`t-f0bcb12a` and `t-0b52151f` are Opus builds that between them consumed 1.07 million cache tokens while sending
under 3,000 tokens of fresh input and producing under 4,000 tokens of output. Those are sessions that spent their
budget re-reading context and then did almost nothing. They are the clearest case for keeping briefs bounded and
for pointing a worker at specific files rather than letting it open the repository.

## 4. Why a usage limit produced a blocked task

Only two calls on the whole day were recorded with the outcome `limit`, both on `claude@master/fleet` with
Sonnet. The visible damage, however, was a task that stopped and waited for a human: `t-a724a961`.

Its event trail is short. At 21:01:35 it emitted `task.working`. At 21:02:54 it emitted `task.blocked`, and
immediately afterwards `task.auto_approve_held` carrying the reason "claude account limited until 1:00 AM (try
again after 1:00 AM)".

That sequence matters because of where the limit arrived. The task had asked to use a tool that the fleet denies
by default, so the controller ran `auto_approve_denials` in `master/controller/tasks.py`. That function tries to
re-admit the task; admission raised `NotAdmitted` because the account was limited; the handler records the reason,
emits `task.auto_approve_held`, and returns with the task still in state `blocked`.

Every piece of the controller's limit machinery keys off state `failed`. `handoff.limit_failure()` requires
`state == "failed"` together with a failure reason of `limit`, a known reset time and a provider match. Because
`t-a724a961` never reached `failed`, `handoff.after_settled`, `capacity.requeue_on_limit` and the new
`requeue_limit_wait` are all unreachable for it. The task sat blocked until a person looked at it, which on
16 November meant it sat blocked overnight.

The Codex change delivered as Task A (commit `cfecb70`) covers the run-failure path properly: a limit failure now
requeues to the earliest reset rather than being left failed, a pinned profile waits for its own account, and
dispatch skips any account already at ninety percent of its primary window. It does not cover the admission path
described above, so the acceptance criterion "it never ends as `blocked`" was not met by that commit alone. The
follow-up fix task carries that gap, and makes a limit-caused `NotAdmitted` hold the task as `queued` until the
reset instead of leaving it blocked, while any other reason for `NotAdmitted` still blocks for the owner as before.

## 5. Why the cost estimator overstates a Sonnet build

`intel.estimate()` in `master/controller/intel.py` predicts a task's size as

```python
tokens = _median([(r.get("input") or 0) + (r.get("output") or 0) + (r.get("cached") or 0) for r in rows])
```

It adds cache reads to fresh input and output. Cache reads are not new tokens. They are the same prompt being
read again on every turn of the session, and they bill at roughly a tenth of the input rate. Adding them to the
total conflates "how much context did this session carry" with "how much did this session cost", and because a
long agent session re-reads its context on every turn, the cached figure grows with the number of turns rather
than with the size of the work.

Measured on the thirty-three Sonnet tasks of 16 November:

| Kind | Tasks | Median estimate (incl. cached) | Median fresh (input + output) | Ratio |
|---|---:|---:|---:|---:|
| build | 12 | 880,468 | 78,584 | 11.2x |
| draft | 10 | 555,608 | 73,472 | 7.6x |
| fix | 10 | 497,982 | 46,655 | 10.7x |
| test | 1 | 446,571 | 75,290 | 5.9x |
| All | 33 | 732,689 | 75,084 | 9.8x |

Taking the ratio per task rather than per median, Sonnet builds overstate by a median of 13.3x, a mean of 26.4x,
and as much as 176.8x for the worst session — one of those Opus-style sessions that read a great deal and wrote
almost nothing. The "about 20x" figure in the brief sits between the median and the mean, and both are defensible
summaries of the same skew.

The fix is to estimate from `input + output` and to report cache reads separately, as context carried rather than
as tokens spent. The console's new spawn preview already does this: `previewSpawn` in
`master/controller/ui/src/lib/budget.ts` sums only input and output. Making `intel.estimate()` agree with it would
remove the discrepancy between the preview a person sees before spawning and the estimate the controller uses.

## 6. What to change

1. Change `intel.estimate()` to exclude cache reads from its token figure, and surface cache reads as a separate
   number. This is a small change with a large effect on every estimate the controller shows.
2. Leave `draft` and `summary` tasks without an explicit model so the controller routes them to Haiku. One misrouted
   draft cost more than Haiku's entire day.
3. Keep briefs bounded. The two Opus sessions that spent a million cache tokens for four thousand tokens of output
   are the cost of an unbounded brief, not of an expensive model.
4. Keep the orchestrator session short. Cache reads outnumbered fresh input sixteen to one across the fleet, and
   the orchestrator chat is the longest-lived session in it.

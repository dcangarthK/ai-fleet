# Spare account token audit (week starting Mon 16 Nov 2026 09:00)

Written 17 Nov 2026 at 19:30 SAST. Account shown: claude spare, weekly all-models at 67%, 5-hour window at 16%.

## Sources

- **Session transcripts:** `~/.claude-fleet-spare/projects/*/*.jsonl`. I counted assistant messages once each (de-duplicated by message id) and priced them at API list rates as an equivalent cost. The subscription is not billed per token, so these dollar figures are a way to compare, not money spent.
- **fleet.db:** the `agent_usage`, `tasks` and `events` tables. `cost_usd` is cumulative per task, so I take the per-task maximum.
- **Scope:** everything since the weekly reset, 16 Nov 09:00 SAST.

**Limits of the data:**

- The usage-limit percentage is Anthropic's internal measure, and nothing local shows how tokens map to it.
- The figures below therefore show where usage went, not how many tokens make up the 67%.

## 1. Who used the spare account

All of it came from fleet workers: 32 sessions, every one in a `.ai-fleet/worktrees/t-*` folder. No interactive or orchestrator sessions ran on the spare profile.

| Model | Sessions | Cache reads | Output | API-equivalent |
|---|---:|---:|---:|---:|
| Sonnet 5.5 | 23 | 24.0M | 510k | $19.89 |
| Opus 5.5 | 4 | 13.1M | 115k | $12.72 |
| Fable 5.2 | 1 | 0.19M | 13k | $2.60 |
| Haiku 5.5 | 4 | 2.9M | 167k | $1.51 |
| **Total** | 32 | 40.2M | 805k | **$36.72** |

fleet.db records only $21.97 for the same tasks:

- **Failed runs report partial usage.** t-bd27a1d7 has no tokens recorded at all, but its transcript shows $2.70.
- **Resumed runs in the same session under-count.** For example, Opus shows 13.1M cache reads in the transcripts against 6.2M in fleet.db.

## 2. When it was used

| Window | API-equivalent | Share |
|---|---:|---:|
| 16 Nov 09:00–12:00: SharePoint verification on **Opus** | $12.88 | 35% |
| 16 Nov 12:00–18:00: SharePoint fast-gen build and docs | $9.78 | 27% |
| 16 Nov 18:00–24:00: intel controller and UI revamp | $12.68 | 35% |
| 17 Nov 00:00–06:17: the token fixes themselves | $0.49 | 1% |
| 17 Nov after 06:17, with the fixes live | $0.88 | 2% |

**About 96% of the week's spare usage was spent on Monday, before any of the token fixes existed.** The 67% was reached yesterday. Since the fixes went live, the spare account has run 3 tasks.

## 3. What drove it

### 3.1 One Opus chain: $11.33 (31% of the week)

- **Tasks:** t-0b52151f, then t-f0bcb12a, then t-8a007477, all "SharePoint plan verification harness". The `continue:` tasks resumed the same session.
- **How it ran:** Opus at high effort, pinned explicitly with `model=claude-opus-5-5` and `profile=spare` (there is no `routed_from`). One run took 88 turns and 4.8M cache reads.
- **Review:** the review task t-713ce723 also ran on Opus ($1.40).
- **Against the rules:** the pipeline says builds go to Sonnet, with Opus only for hard builds the owner names.
- **Why the fixes missed it:**
  - The draft-routing fix covers only `kind=draft`.
  - No rule stops an explicit Opus pin on a `build`.
  - These tasks predate the intel controller (`model="auto"`).

### 3.2 Block-and-resume is still happening: 34 of 54 Sonnet runs ended in `error`

- **Runs:** every errored run was a permission block that the controller then auto-approved and resumed. The 21 errored build runs alone hold 16.0M cache reads.
- **After the BASH_RULES fix (06:17):**
  - All 3 spare tasks still blocked at least once.
  - t-9a58ca86 blocked 4 times and never finished on spare.
- **Denials after the fix, from the transcripts:**

| Denied command | Why the rules miss it |
|---|---|
| `cd <dir>; …`, `cd x && …`, "multiple directory changes" | `cd` is not allowed, and any compound containing it is denied. The instruction to workers not to `cd` is not being followed. |
| `… \| head; grep …`, `npm run typecheck 2>&1 \| tail` | Compound commands. Every part must match, and redirects or pipes into non-listed commands fail. |
| `cat > file <<EOF` | A heredoc write. Workers use it instead of the Write tool. |
| `sed -i …` | Deliberately not allowed. |
| `npx tsc -b`, `npm run typecheck` | Not listed. Only `npm ci --ignore-scripts`, `npm run build` and `npx vitest` are. |
| Read/Edit on `~/ai-fleet/.claude/skills/...` (t-9a58ca86) | The brief pointed at the base checkout, outside the worktree. That is a spec error, not a rules gap. |

**Verdict on fix #1:** it removed the plain `ls` / `grep` / `cat` denials. It missed `cd`-prefixed and compound commands, `npx tsc`, `npm run typecheck` and heredoc writes, which are now the main causes.

### 3.3 Fable on a worker: $2.60

- **Task:** t-3b52e06a, a docs final re-review, 16 Nov 16:27.
- **What happened:** pinned to `claude-fable-5-2` on spare, at high effort. It ended in `error` after 4 messages, with no verdict.
- **Rule broken:** final reviews go to Codex. Fable was dropped the next morning, but nothing in code stops a Fable pin.

### 3.4 Cancelled or re-scoped builds: about $5.0

- t-596eae55, t-70fc7b86 and t-bd27a1d7: UI P2 builds cancelled or rerouted after they had started.
- t-634b11a4: 7 runs over 2 hours. Three of them failed after an `ok` run, because they were re-driven with `message_task`.

### 3.5 Drafts on Sonnet: about $1.3

- t-878a555f, t-700dbe6f and t-e998fffa ran on Sonnet before the draft-routing fix (17 Nov 04:36).
- After the fix, no draft has run on spare yet, so the fix is unverified there.

## 4. Did the new work help?

| Fix | Live since | Effect seen on spare | Verdict |
|---|---|---|---|
| Widen BASH_RULES | 17 Nov 06:17 | 3 of 3 tasks still blocked | **Partial.** Compound and `cd` commands still block. |
| Trim MCP lifecycle results | 17 Nov | No effect on spare: it trims orchestrator chats, which run on the main account | Doesn't apply here |
| Draft routing to Haiku | 17 Nov 04:36 | No drafts on spare since | Unverified |
| Intel controller (`model="auto"`) | 17 Nov | Would have stopped the Opus and Fable pins, but only when callers stop pinning | **Gap.** Explicit pins are not checked. |
| Batch pipeline (one controller call) | merged today | No batch run yet | Unverified |

**What is not covered at all:**

1. **Explicit Opus or Fable pins on any kind.** They cost 41% of the week; only drafts are downgraded.
2. **`cd` and compound Bash commands.** This is now the main cause of blocks.
3. **fleet.db under-counts by about 40%.** The Budget page and the estimator see only part of what the limit sees.

## 5. Recommended next fixes

1. **Admission rule:** an Opus model on build or fix needs the owner's override, and Fable is refused on every kind. That would have saved about $14 this week.
2. **Bash rules:**
   - Allow `cd` inside the worktree, plus `npx tsc` and `npm run typecheck`.
   - Add a PreToolUse hook that strips a leading `cd <worktree> &&` or `;`.
   - Tell workers: no heredoc writes, use the Write tool.
3. **Usage recording:** record usage from the session transcript, or from the final `result` of every run including errors, so fleet.db matches what the limit counts.
4. **Brief check:** reject briefs that name paths outside the task worktree, like t-9a58ca86.

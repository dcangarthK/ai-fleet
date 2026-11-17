# Spec: close the three token gaps from the spare-account audit (17 Nov 2026)

Source: `docs/TOKEN-AUDIT-SPARE-2026-11-17.md`. Owner decisions (17 Nov):

- **Fable is paid.** Never use it, on any kind, unless the owner overrides.
- **Opus at high effort** is blocked unless the owner temporarily overrides.
- **Opus at low or medium effort** stays allowed for final review, green light and orchestration.
- **Orchestration** runs on code logic plus the cheapest model that can do it: Sonnet by default, Opus at low effort at most.

The owner override is the existing `budget_override` task field. It is admin scope only and applies to one task, so it is temporary by design.

---

## G1. Model guard at admission (Opus effort and Fable)

**Where:**

- `master/controller/budget.py`: next to `RULE0_KINDS` and `rule0_blocks`, and in `admit()` / `admission()`.
- `master/controller/intel.py`: `CANDIDATES` and `KIND_PREFERENCE`.

**Behaviour.** The guard is hard-coded, like rule 0, so editing the rules on the Budget page cannot switch it off. It applies to `spawn_task`, `run_pipeline`/batch, handoff, fallbacks and reroutes.

1. **Fable on any kind is refused**, unless `budget_override` is set. The error names the rule:
   `"fable is paid: refused (owner override only)"`.
2. **Opus with effort `high`, `xhigh` or `max` is refused**, unless `budget_override` is set.
3. **Opus with no effort given** gets an effort set by kind, never `high`:
   - `medium` for final_review and security_review;
   - `low` for review, re_review and lead.
4. **Opus is allowed only for these kinds:** final_review, security_review, review, re_review and lead (orchestration). Opus on build, fix, test, merge, draft, check or benchmark is refused unless overridden. This would have stopped the $11.33 SharePoint chain.
5. **The intel controller never proposes Fable.** Remove it from `CANDIDATES`.
   - In `KIND_PREFERENCE`, Opus stays only for final_review and security_review, as the fallback after Codex.
   - `lead` becomes `["claude:claude-sonnet-5-5", "claude:claude-opus-5-5"]`. Opus is admitted at `low`.
6. **Fallbacks:**
   - `limits.fallbacks.codex` becomes `["claude:claude-opus-5-5"]` at `medium`, replacing Fable.
   - A fallback that lands on a refused identity is skipped, not admitted.
7. **Admission records the outcome:**
   - a refusal is a 400 with a `refused_by: "model_guard"` field;
   - an overridden admission records `routed_why: "owner override: <rule>"`, so it shows on Live and in the merge summary.
8. **Existing DB rules:** the `claude:claude-fable-*` entry stays, so old rows still resolve, but the guard refuses it first.

**Tests** (new file `master/controller/tests/test_model_guard.py`, added to `scripts/gate.sh`):

- Fable is refused on each kind (one case per kind), and with `budget_override` it is admitted.
- Opus on final_review:
  - with no effort, it is admitted at medium;
  - at high, it is refused;
  - at high with an override, it is admitted at high, and `routed_why` is recorded.
- Opus on build at medium is refused, and admitted with an override.
- Opus on lead with no effort is admitted at low.
- `rank()` never returns Fable for any kind.
- A Codex limit falls back to Opus at medium, not Fable.
- The guard holds when the DB rules list Fable kinds (it ignores rules).
- A refusal through the HTTP spawn path returns 400 with `refused_by`.

---

## G2. Bash: allow `cd` and typecheck, strip a leading `cd`

**Where:**

- `nodes/worker/profile.py` (`BASH_RULES`);
- `nodes/worker/quiet_bash.py` (the PreToolUse hook);
- the worker instructions (README or brief text in `profile.py`);
- re-embed with `python3 nodes/build.py`, then commit `onboard.sh` and `onboard.ps1`.

**Behaviour:**

1. **Hook rewrite of a leading `cd`.** When a command starts with `cd <dir> &&` or `cd <dir>;`, and `<dir>` resolves inside the task worktree (the hook's cwd or `$CLAUDE_PROJECT_DIR`), the hook strips that prefix. It returns `updatedInput.command` with the rest run from the worktree root, prefixed with `cd <dir> && ` only when `<dir>` is not the root.
   - When `<dir>` is outside the worktree, the hook leaves the command alone, so it still asks.
   - Two or more `cd`s are left alone too.
2. **New allow rules.** Add to `BASH_RULES`:
   - `Bash(cd:*)`, which only matters inside the worktree because of 1;
   - `Bash(npx tsc:*)`;
   - `Bash(npm run typecheck)`;
   - `Bash(npm run lint)`.

   Still not allowed: `npm run:*`, `sed -i`, `echo`, `find`.
3. **Heredoc writes.** For `cat > file <<EOF` and `cat >> file <<EOF`, the hook answers with `permissionDecision: "deny"` and the reason `"use the Write/Edit tool, not a heredoc"`. This is a denial message, not a block that stops the run, so the worker retries with Write in the same run.
   - The test that this does not end the run is stated, not assumed: see the tests.
4. **Worker instructions** gain one line: no `cd`, no heredoc writes, no paths outside the worktree.

**Tests** (extend `nodes/worker/tests/test_quiet_bash.py`, or add one if missing; plus `test_onboard`):

- The prefix is stripped for `cd <wt> && ls`, `cd <wt>/master/controller/ui; npx tsc -b` and `cd sub && grep x f`.
- The command is unchanged for `cd /etc && ls`, `cd a && cd b && ls` and `cd ../.. && ls`.
- A heredoc `cat > x <<EOF` gets a deny with the Write hint, and `cat x` passes.
- `BASH_RULES` contains the new entries, and does not contain `npm run:*` or `sed:*`.
- `test_onboard` passes after the re-embed.
- **Replay check** (manual, in the PR body): rerun the denied commands from the audit through the hook and the rules, and list which ones now pass.

---

## G3. Record usage from the session log, so failed runs count in full

**Where:**

- `nodes/runner.py`, the usage parser around line 1935, then re-embed;
- `master/controller` only if the usage row needs a new field.

**Behaviour:**

1. **When a Claude run ends** (any outcome: ok, error, blocked, cancelled, limit), the runner also reads the run's session transcript at `<CLAUDE_CONFIG_DIR>/projects/<cwd-slug>/<session_id>.jsonl`.
   - It sums assistant `usage`, de-duplicated by `message.id`, for the messages timestamped since this run started.
   - It records per-run totals: input plus cache_creation, output, and cache_read.
2. **Recorded usage is the larger of the two**, the `result` event's usage or the transcript sum, field by field. When there is no `result` event, the transcript sum is used.
   - If neither source exists, nothing is recorded, as today.
3. **`cost_usd`:** keep the `result` value when present. Otherwise compute it from the transcript with a price table kept in one place (Sonnet, Opus, Haiku, Fable list prices).
   - Mark the row as estimated, with the column `cost_source = 'result' | 'transcript'`. Add the column in `store.py` migrations if needed.
4. **Model:** taken from the transcript when the result is missing (the model with the most output).
5. **Codex runs are unchanged.** They already sum `turn.completed`.

**Tests** (in the runner tests):

- A fixture transcript with 3 assistant messages, one duplicated id, and no result event. The recorded totals equal the de-duplicated sum, and `cost_source = transcript`.
- A result event that reports less than the transcript: the transcript wins. One that reports more: the result wins.
- Messages before the run's start time are excluded (a resumed session).
- A missing transcript falls back to the result. Neither present records nothing.
- `test_onboard` passes after the re-embed.

**Acceptance check after deploy:** for the next 5 Claude tasks, compare fleet.db against the transcript sums with the audit script. They should agree within 2%.

---

## Assignment (from `fleet_status`, 17 Nov 19:40)

State:

- **Codex:** 5h window at 5%, weekly at 36%, but already past its **daily soft budget** (3.1M against 2M).
- **Claude fleet account:** OK.
- **Claude spare account:** weekly at 67%. Keep it off spare.

| Task | Kind | Builder | Reviewer | Why |
|---|---|---|---|---|
| G1 model guard | build | **Sonnet**, fleet account, medium | **Codex** `security_review` (terra, high) | The guard is policy and cost enforcement, so it is security-sensitive and gets one Codex review. |
| G2 Bash `cd` and typecheck | build | **Sonnet**, fleet account, medium | Opus low `review` | A small change. The reviewer differs from the builder. |
| G3 usage from transcripts | build | **Sonnet**, fleet account, medium | Opus low `review` | Parser and test fixtures, routine. |
| Merge G1, G2, G3 | merge | **Haiku** | — | G2 and G3 both re-embed the runner. At merge, take main's `onboard.*` and re-run `nodes/build.py`. |
| Final whole-batch review | final_review | **Codex**, once (terra, high) | — | Per the rules. It can be folded into G1's security review if Codex's daily soft budget is still exceeded. |

- G1, G2 and G3 have no shared files apart from the generated `onboard.*`, so all three run in parallel. That puts two Claude builds on the fleet account plus one queued (the cap is 2).
- Spawn with `agent="auto"` and `model="auto"`, with `kind` set. The controller's ranking already gives Sonnet for builds.
- Codex is not used for any build: it is over its daily soft budget, and none of these needs it.
- Fix rounds go back to Sonnet, at most 3, then the owner.

**Verdict: CHANGES**

Plan mode is active, so I made no edits and wrote no plan file. This is a read-only review, so I answered directly. I could not run `git diff` because I only had read and search tools. I reviewed the changed files as they stand, plus the direct callees they touch. The gate was not run, per your instruction.

## HIGH

**H1. Independence and refusal exclusion never match Codex keys.** `tasks.py:1489` and `tasks.py:1523` build keys with `budget.identity()`, which returns `agent:model` (`budget.py:365-366`). For Codex with a model set, that is `codex:gpt-5.6-terra`. `rank()` keys Codex as plain `codex` (`intel.py:59`, `intel.py:14`). So `key in exclude` (`intel.py:105`) never fires for a Codex builder, and a refused Codex key is never excluded from `suggest()`. Result: a Codex-built task can be auto-reviewed by Codex, and a refused Codex task can be suggested back. `tasks.py:1496-1497` also flows into this. The fix is to use `intel.key_of(agent, model)` in both places. `intel_routes.py:59-61` already does this correctly. Add a test with a Codex builder that has a model set. The stored `budget_identity` format is UNVERIFIED, but if it uses the same form, the bug is the same.

**H2. The near-limit filter is inert on the create path.** `tasks.py:1469` hardcodes `"used_percent": 0`. So `auto_pick` (`tasks.py:1496`) and `_refusal_detail` (`tasks.py:1523`) never apply the `near_limit_percent` filter that spec R1.5 requires. A model at 99% on its 5-hour window can be auto-picked and suggested. Admission still applies hard limits downstream, so this is a soft-rule gap, not a budget bypass. The fix is to feed real `used_5h` into `_intel_view`, using the same source `intel_routes.py:47-55` uses (`context()`).

**H3. Cleanup "merged" check can pass for branches that were never merged, and the repo fallback is wrong.**
- `intel_routes.py:131` uses `git -C repo or "."` when `repo` is empty. That runs the check against the controller's own working directory, not the task's repo. The spec says any error means "not merged" (spec R8, SPEC line 140), so an empty repo should return False.
- `git merge-base --is-ancestor branch main` is also true when the branch tip equals `main`, which is the case for a task that never committed. `intel.py:40` puts `failed` and `cancelled` in the cleanup set, so a failed task with uncommitted work and a branch sitting at `main` is listed. `cleanup()` (`tasks.py:2267-2274`) then calls the runner's `/cleanup`. Whether the runner preserves uncommitted changes is UNVERIFIED (runner code is out of scope).
- Fix: return False for an empty repo. Refuse when the branch tip equals `main`'s tip. Limit cleanup to `done`, or require a clean worktree check on the runner. Add a test for a failed task with no commits. Also flag the spec R8 terminal-state list to the owner, since it is the design that allows this.

## MED

**M1. `suggest()` is missing the E5 special case.** `intel.py:210-222`: a refused Codex `re_review` should first suggest the original round reviewer's model, if eligible. That is not implemented. The `codex security_review` entry is appended whenever `security=True`, for any refused kind, not only a refused Codex `re_review`. Spec R5 (SPEC lines 116-119) requires the narrower rule.

**M2. `auto_pick` decision is unlinked and can 500 the create.** `tasks.py:1509-1512` records with `task_id=None`, so `GET /api/intel/tasks/{id}` (spec R4) never shows the pick. The record call is also outside the `try` at `tasks.py:1502-1505`. A DB error there escapes as a raw 500 and the task is not created. Pass the task id once it exists, or record after creation, and wrap the write so a failure degrades rather than blocks.

**M3. Two different stats and history inputs for `rank()`.** `tasks.py:1490-1495` keys stats by `budget_identity` (same mismatch as H1), counts `review` as done, and scans every task with no limit. `intel_routes.py:32-35` keys by `key_of` and counts `done` only. Estimates and the admission-path choice can disagree. Use one builder for both.

**M4. Refusal `detail` changes type from str to dict.** `tasks.py:1525` returns `{"error", "suggest"}`. The spec asks for a `suggest` list in the detail, so this is intended. The risk is string consumers. Check `_controller` in the MCP server and any UI code that reads `detail` as text. I have not verified them. Status code and text are preserved on the fallback path (`tasks.py:1526-1528`), so the refusal itself is not changed.

**M5. Blocking git inside an async handler.** `intel_routes.py:151-154` calls `_cleanable()`, which runs `subprocess.run` for up to 300 rows with a 10 s timeout each, on the event loop. Move it to a thread (`asyncio.to_thread`) or make the handler sync.

## LOW

- **L1.** `intel_routes.py:79-94` `auto_pick()` has no callers under `master/`. It duplicates `tasks.auto_pick` with different stats and exclusion. Remove it or route both through one function.
- **L2.** `tasks.py:1486` includes `test` in the exclusion kinds. `intel_routes.py:19` does not, and spec R6 names review kinds only. Make them agree.
- **L3.** `intel.py:62-68` uses its own longest-pattern match and `_kind_ok` instead of `budget.match_key` and `kind_allowed` (spec R1.1). Results probably match today, but they can drift.
- **L4.** `intel.py:90`: a candidate with no `rules` entry is not kind-filtered. This is only safe if budget rules default to deny. UNVERIFIED.
- **L5.** `intel_routes.py:160` catches only `HTTPException` in `cleanup_run`. Any other error stops the loop partway. Earlier cleanups are recorded, so the state is consistent but the response is wrong.
- **L6.** `intel.py:217-221` can add a duplicate `codex` entry to the suggestion list.
- **L7.** `ollama` keys from the DB never match `local:<role>` candidates (`intel.py:55-59`, `intel_routes.py:37-40`), so local load and history are always zero.
- **L8.** `intel_routes.py:150` and `intel_routes.py:197` have no scope check. The spec says admin or operate scope for cleanup (SPEC line 141). The middleware may enforce this, which I could not verify.

## Checked and OK

- Rule 0 Fable list (`intel.py:32`, `intel.py:92-93`) and the Codex never-list (`intel.py:33`, `intel.py:94-95`) match spec items 2 and 3.
- Local kinds (`intel.py:34`, `intel.py:96-100`) match spec item 4.
- `limited_until` and `max_concurrent` handling match spec items 5 and 6 (`intel.py:101-128`).
- `history()` takes `MAX(cost_usd)` per task (`intel.py:158`, `intel.py:166`). Savings does the same (`intel.py:260-261`).
- Codex `usd` is `None` (`intel.py:158`). Estimate medians and the `prompt_bytes // 4` term match spec R2.
- Forecast math matches spec R3 (`intel.py:180-195`).
- SQL is parameterised. The only string concatenation is a static `where` clause (`intel.py:268`, `intel.py:281`).
- No route calls `budget.admit`. Routes only read, or write intel decisions and the merged-branch cleanup.
- `auto_pick` errors return 400, 503 or 409 before admission (`tasks.py:1477-1507`). The refusal status code is kept (`tasks.py:1696`).
- No prompt text is written to `intel_decisions`. The prompt is only read for a length and a substring check (`tasks.py:1497`, `tasks.py:1522`).
- MCP `spawn_task` trimming and `estimate_task` (`server.py:442-471`) look correct.

## UNVERIFIED (would need code outside the changed files)

- The stored format of `budget_identity` (H1).
- Whether `budget.status()` returns `limited_until` as an epoch float. If it is a string, the `>` comparison at `intel.py:101` raises. Inside `auto_pick` that becomes a 503 on every `model=auto` spawn. `_refusal_detail` would silently drop suggestions.
- Whether the runner's `/cleanup` keeps uncommitted worktree changes (H3).
- Whether middleware enforces the scope on `/api/intel/*` (L8).
- Whether `budget.limit_snapshot` returns a dict keyed by agent (`intel_routes.py:47`).

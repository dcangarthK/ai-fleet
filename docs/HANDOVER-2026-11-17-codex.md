# Handover to Codex: 16–17 Nov 2026 (while both Claude accounts are limited)

Both Claude accounts are at or near their 5-hour limits: the fleet account resets at 01:00 SAST. Codex has room,
at 14% of its 5-hour window and 19% weekly. The orchestrator has queued two Codex build tasks through the fleet.
At most one Codex job runs at a time, so B waits for A.

## Rules for Codex (strict)
- Model `gpt-5.6-terra`, effort `medium`.
- Work only in your task worktree and branch (`fleet/<task-id>`). Never push, merge into main, or touch other repos.
- Never approve or unblock tasks. Never use `budget_override`.
- Stay inside the files listed for your task. If more scope is needed, stop and report it as blocked.
- Write tests first. Do not run the gate yourself; the runner runs it once after you finish.
- Commit on your branch before you stop. Commit messages are normal prose.
- Never put credentials, `.env` contents or `ui/dist` content in prompts or logs.
- Changed `nodes/runner.py`? Run `python3 nodes/build.py` and commit `onboard.sh` and `onboard.ps1` too.

## Task A: reroute on usage limits (controller fix)
Repo: `/Users/duncansynthesis.co.za/ai-fleet` (main). Files: `master/controller/tasks.py`, `budget.py`,
`intel.py`, and a new `tests/test_limit_reroute.py` (add it to `scripts/gate.sh`).

Problem: on 16 Nov, task t-a724a961 failed with "claude account limited until 1:00 AM" and was left `blocked`.
The spare Claude account and Codex both had room at the time.

Acceptance:
1. When a task fails because its account hit a usage limit, the controller records `limited_until` for that
   account and requeues the task automatically. It goes to the next eligible account for the same model (for
   example spare), then to a model the budget rules allow for that `kind`. If none is eligible, it waits as
   `queued` until the earliest `resets_at`. It never ends as `blocked`.
2. Before dispatching, the controller skips any account that is limited, or whose `rate_limits.primary.used_percent`
   is 90 or more.
3. Each reroute is logged with its reason and is visible on Live.
4. Tests cover: limit, then reroute to spare; all accounts limited, then queued until reset; a near-limit account
   is skipped before dispatch; an explicit `profile` pin is honoured by waiting rather than rerouting.

## Task B: remaining UI revamp items
Repo: `/Users/duncansynthesis.co.za/ai-fleet/.claude/worktrees/ui-revamp-4fd0ec` (branch `claude/ui-revamp-4fd0ec`).
Files: under `master/controller/ui/src/`, `master/controller/ui_api.py` (additive changes only) and
`tests/test_ui_api.py`.

Acceptance:
1. **Work diff tree.** On the Work detail page, a file tree of the diff (using `splitDiff` in `src/lib/work.ts`).
   Clicking a file scrolls to it. Show +/- counts per file.
2. **"Why?" panel.** On Work detail, explain the routing for each attempt: model, account, kind and budget rule.
   Use data already in the detail response.
3. **Spawn budget preview.** In the spawn and retry dialogs, show the estimated tokens and cost and the
   budget/limit state of the target model before the user confirms.
4. **Add-model dialogs.** Replace the browser-native `prompt` and `confirm` calls with the app's `useConfirm` and a
   form dialog.
5. **LOW finding 1.** In `src/components/work/Actions.tsx:68`, validate commit hashes strictly
   (`/^[0-9a-f]{7,40}$/i`).
6. **LOW finding 2.** `GET /api/work/{root_id}` caps attempts at 200 and returns a `truncated` flag. Add a test.
7. Responsive down to 375px, with no broken panels. Log or work output uses the shared terminal frame.
8. Vitest and `tsc` pass. Run `npm run build` and commit `ui/dist` with `git add -f`.

## After Codex (orchestrator, after 01:05)
- Review each branch with Claude Sonnet: a Claude model reviews Codex's build.
- Fix any HIGH and MED findings with Sonnet.
- Merge A into main locally, and B into `claude/ui-revamp-4fd0ec`.
- Run a final whole-branch review.
- Tell the owner `t-a724a961` is redundant if the earlier restyle covers it. Cancelling it is the owner's call.
- The owner pushes.

# Controller-intelligence evidence (read-only research, 2026-11-16)

Sources: fleet.db snapshot (`master/controller/fleet.db`, copied to scratchpad; 290 tasks, 2026-11-11..16, 392 agent_usage
rows, 2,972 events); Claude Code transcripts mtime >= 2026-11-15 in
`~/.claude/projects/-Users-duncansynthesis-co-za-ai-fleet/` (sessions 764f7558, 7527aebd, 7127507b, 3c176aad, 1d4c7184; sizes
below). Times are the machine's local time. NOTE: only 22 tasks exist for 11-16 and none for 11-15, so where useful I
quote the whole 11-11..16 history and flag it. `docs/TOKEN-AUDIT-2026-11-16.md` already covers 11-16 token cost, final-review
caps and the duplicate retry; this file does not repeat it, it adds the DB-wide counts and the manual-step inventory.
No credentials or .env content read.

## 1. Spawn volume (DB, 290 tasks)
- By agent: claude 227, codex 50, ollama 13. kind NULL on 60 tasks (pre-kind era, 11-11..12).
- By kind (count): review 51, build 53, fix 31, re_review 29, final_review 17, security_review 17, draft 12, merge 10,
  test 9, check 1.
- Terminal state: done 171, review (not yet completed by orchestrator) 64, failed 27, cancelled 16, capped 12.
- By model family, claude: sonnet 76, opus 57, haiku 46, fable 31, model NULL 17. Codex: gpt-5.6-terra 14, model NULL 36. Model strings inconsistent: 'sonnet' x7, 'fable' x3, 'claude-opus-5' x2,
  NULL x many (alias vs full id breaks budget key matching and reporting).
- Codex: only 1 task on 11-16 (t-4c4c329e, security_review). agent_usage terra: 15 build runs, 5 final_review, 3 security_review tasks.
- Cost per model (agent_usage, cumulative-cost caveat in token audit): Opus build 28 runs 74.5M cached; Sonnet build 98
  runs 90.3M; Sonnet fix 67 runs 29.2M; Opus review 33 runs 14.7M.
- Daily cached tokens in fleet tasks: 11-11 46M, 11-12 156M, 11-13 93M, 11-14 26M, 11-16 22M. Total 343M.

## 2. Budget refusals (events type budget.refused = 11; plus 4 more seen only in transcripts as 400s)
All refusals are kind/model mismatches the controller could have re-kinded or re-routed itself:
| when | task asked | refusal text (short) | what the orchestrator did |
|---|---|---|---|
| 11-12 10:30 | haiku `check` "Dashboard watcher" | checks never on paid models | dropped/ran local |
| 11-12 15:44 | codex `review` spec review | codex reserved for final/security/build | re-spawned as claude haiku/opus |
| 11-12 16:00 | opus `re_review` | opus reserved for build/final/sec/review; use haiku for re_review | re-spawned |
| 11-12 21:41 | codex `review`; opus `re_review` (2 in same minute) | same | re-spawned |
| 11-13 06:38 | codex `re_review` x2 (identical, 0 s apart) | codex reserved | **re-kinded by hand to `security_review`**: t-6e01801a (codex terra, title "Delta re-review (read-only...)", parent t-f7e33082, done). Kind relabelled only to pass the gate; task content unchanged. |
| 11-13 15:49 | opus `test` SharePoint fast-gen | opus reserved... use haiku for test | re-spawned |
| 11-14 17:43 | codex `fix` Laptop 3 installer | codex reserved | re-spawned as claude |
| 11-16 09:35 / 07:35 (transcript) | opus `test` SharePoint verification | opus reserved...haiku for test | **3 attempts**: test -> review (t-713ce723, cancelled wrong toolset, 104k cached) -> build (t-0b52151f, capped) -> continue t-f0bcb12a (capped) -> t-8a007477. Over-routed to Opus; final cost ~6.1M cached (token audit 2.6) |
| 11-16 13:06 | codex `test` (review_of t-634b11a4) | codex reserved | re-spawned as claude haiku test (t-19cd4721, capped, 466k cached) |
- Each refusal text already names the substitute ("use claude:claude-haiku-* for re_review") yet the controller does not
  apply it: 11 of 11 needed a hand re-spawn. 2 identical codex re_review refusals within 0 s (11-13 06:38) = wasted call.
- Validation errors hit while spawning/messaging (transcripts 11-14..16, ~20 calls): `stage must be 1-24 lowercase`
  (1, name "win-enroll-bypass-postgate" 26 chars), `independence: haiku built this, can't test it` (1), `independence waiver needs a
  reason` (1), `link must be one of [...]` (1: used "re_review" as link), `error 409: can't message a task that is working`
  (1), `unknown account label fleet|owner|master` on report_activity (3), `auto mode classifier no verdict` on spawn/cancel/
  message_task (4), wrong arg names because tool schema was deferred (wait_tasks `task_ids`/`timeout_s` x3, message_task
  `message` vs `text` x2, task_status `id` x2 = 7 errors, "input schema was not in your prompt").
- budget.large_prompt x3 (11-13 12:57..13:32): final reviews of 62.8-63.1 KB vs 40 KB soft cap. Re-spawned same title x3 ("Final review:
  Analytics Hub terraform analysis" t-20ca45dc, t-73dbcfa1; "AH two-plan review" t-4ea92d16): manual prompt trimming loop.
- Prompt bytes totals (all history): review 408 KB (51 tasks, avg 8.0 KB, max 37 KB), re_review 359 KB (29, avg 12.4 KB, max
  40.9 KB: t-b0ffa8ad, t-7ca068e0, t-c9699816 are Haiku re_reviews of ~40 KB each, i.e. near-full diffs re-sent), final_review 226 KB
  (17, avg 13.3 KB, max 63 KB). 11-16 prompts were small (median 3.6 KB); the problem on 11-16 was tool output, not briefs.
  Average re_review prompt (12.4 KB) is 1.5x average review prompt (8.0 KB): "delta only" is not being enforced.

## 3. Usage-limit hits and what happened (DB budget.limit 12, budget.queued 18, handoff.* 18, failures 'limit' 9)
- 12 budget.limit events: claude@master/fleet 8, claude/spare 2, codex 2. 9 tasks failed with failure_reason=limit, 12 capped.
- 11-12 00:25-00:41: 3 tasks queued "claude account limited until 1:50 AM" (t-5c1fc50f, t-b2886783, t-3a9b8dbd): controller
  queued correctly (budget.queued 18 total, 12 on limit, 6 on slot).
- 11-12 09:17-11:05: 5 tasks queued until 1:20 PM incl. Fable final review (t-68e85d0d) and re_review (t-d3308bf9).
- Codex limit 11-12 09:39: `t-54eebb94` security_review queued "codex:gpt-5.6-sol limited until 1:47 PM" although the rule is terra;
  later failed with limit (error text "try again at 1:47 PM", resets_at parsed from ChatGPT message). Re-queued as t-2cc52234 15:19.
  `limit_resets` table has 1 manual row (actor 'local', 11-12 codex): owner/orchestrator cleared a wrong limit by hand.
- 5x `budget.limit_unrecognised` (t-2642e3cb, t-dbdb5623, t-d1d35598, t-aff60ec2 + 1): the run's final text mentioned "limit"
  (e.g. "Part G ... usage watcher", "limits.near_limit_percent") and a successful build was treated as a possible limit hit
  (false positive): t-d1d35598 ended state failed, failure_reason=limit with a finished commit c00d522 in its result.
- Handoffs: handoff.waiting 9 (reasons: "final_review waits for the reset" t-02b334e4, "this node's runner can't hand off"
  t-fabbd0aa, "no eligible slot" t-62f37b18 and t-85afa645), handoff.done 5, handoff.resumed 4. routed_from set on 5 tasks only.
- Manual vs automatic retry (tasks.link='retry', 14): 5 automatic handoffs (routed_from set), 9 'requeue' continuations
  (all opus capped builds: t-f9b2a0de -> t-6b00ab37 -> t-5a066277; t-4da9e283 -> t-e4bde1f4; t-52eba12c -> t-55caafe1 -> t-4a286266;
  t-0b52151f -> t-f0bcb12a -> t-8a007477) and 1 duplicate: t-7e5a253d spawned by hand 17 s after the controller had handed t-47138efa
  to t-ba303078 (3.5M cached wasted; fixed since per token audit 2.2, see its rec 2).
- 11-14 15:53 `task.requeue_shadow` t-e64b0d94 (Fable final review, "usage limit; would wait for its reset", shadow mode) + 6
  `capacity.shadow_limit` events: queueing-by-reset runs only in shadow, orchestrator waited by hand.
- Manual waiting: 764f7558 ran `while [ $(date +%s) -lt 1794666840 ]; do sleep 30; done; echo reset-passed` style loops (11 Bash
  sleep/poll commands) to wait out a limit reset; 3c176aad had 29 sleep/poll-ish Bash; 7527aebd 8. wait_tasks calls: 53 across
  these sessions (21 in 7527aebd, 27 in 764f7558, 5 in 3c176aad); task_status 18, fleet_status 10.
- Account pressure 11-16 (token audit 1.4): fleet account 100% 5-h / 93% weekly while Codex at 5% / 16%.

## 4. Gate failures (tasks.gate_result: passed 59, failed 8, skipped 17; failure_reason gate_failed 2)
- Gate failures by kind: fix 3, merge 3, build 2. 8 failed gates in total.
- Flaky tests are the dominant cause, not builder defects:
  - `tests.test_limit_handoff.HandoffTests.test_two_triggers_race_for_one_claim` failed in t-d1d35598 (build), t-e6e2ac8f (merge, haiku-4.5,
    gate_failed after 2 runs), t-ce3099a4 (merge), and a post-merge gate re-run needed an `independence="waived"` test task
    (t-a685b50f, event task.independence_waived, 11-14 23:23: "known test_limit_handoff flake (passes 3/3 alone)").
  - `test_a_crash_between_claim_and_insert_is_recovered` (t-ce3099a4), `test_lease_race_between_try_start_and_replace` (t-d1d35598),
    `test_unknown_metrics_raise_and_clear_no_warning_events` (t-731aa651, fix, 3 runs), `test_fix1_unreachable_needs_a_health_report_newer...`
    (t-58cb987b, fix, 3 runs), `test_request_while_an_operation_is_being_started_is_refused` + `test_role_with_settings_waits_for_the_derived_model`
    (t-ef9eea13 merge; t-283c9e1f created only to fix this: "cascade" order dependence), `KeyError: '16 not less than 8'` (t-7a4b18d6, fix, 4 runs).
  - 11-14 13:24-13:30 (t-cadd7916 relay): gate failed twice with test-harness bugs only visible when many files run in one
    process (9 errors in tests/test_relay_routing.py); orchestrator relayed the raw failure by message_task each round.
- Gate runtime: single `scripts/gate.sh` unittest runs of 465-563 tests took 110-279 s per run (t-d1d35598 279 s, t-58cb987b 223 s, t-ce3099a4 169 s).
- Rounds: tasks.runs: 1 run 174, 2 runs 50, 3 runs 27, 4 runs 12, 5 runs 6, 6 runs 3, 7+ runs 5 (t-09c931e4 13 runs, t-216dc9c6 8, t-1db6875a 8,
  t-bcdcbe01 7, t-634b11a4 7). 217 extra runs over 290 tasks (494 runs total); pipeline.escalated 8 ("review rounds exhausted"
  on G2 x2, G5), pipeline.failed 4.
- 11-16 t-634b11a4 (resources repo, gate `pytest -q`): 7 runs, ended gate_failed with 9 failed / 410 passed; the failures were
  cross-repo digest/contract tests (test_data_share_cross_repo_consistency, test_data_share_definitions_contract) unrelated
  to the change; the orchestrator sent 4 fix rounds by message_task (11:15, 11:24, 11:27, 11:28) and a 5th at 13:17
  ("one defect only... do not re-run the gate"); task_overrun event at 15:18 (120 min). Gate re-run each round cost 4.8 s but
  builder context kept growing: cost 0.80 -> 2.44 USD across 7 runs.
- task.loop_cut 1 (t-ed9d619e "Bash refused twice" -> failed): fix round for Codex final review died on a refused tool.
- auto_approvals: 60 across 52 tasks; task.blocked events 109, task.auto_approved 99, denials non-empty on 4 tasks. 3 tasks
  went through task.awaiting_approval (owner-only).

## 5. Capped / cancelled / failed tasks (waste, DB-wide)
- 28 tasks ended capped(12)/failed(11)/cancelled(5): 86.9M cached of 342.7M total = **25%** of all fleet-task cached tokens
  (done 203M, review-state 52.7M).
- capped: Opus build x7 (all SharePoint/networking continuations), Fable final_review x2 + Opus final_review x1 (context/turn cap,
  no verdict), Sonnet review x1 (t-8d79118c "read-only plan mode, no edit tools"), Haiku test x1.
- Read-only tasks that need shell/commit but run read-only: t-8d79118c, t-22aceb3e, t-05b68a86, t-3b52e06a all report
  "no shell, can't commit": capped as if failed, so the controller treats a normal read-only verdict as "capped".
- 'runner is busy' placement errors: t-6743a975, t-c3ef4f30 (ollama qwen3:8b drafts, failed with `could not start: runner:
  runner is busy`); `path not allowed by this node's runner policy` t-fb77887f (Sonnet probe for resources repo, failed 0 runs, cost 0)
  then re-spawned with different path (11:57, 2nd call succeeded t-634b11a4): orchestrator had to discover repo allow-list by trial.
- 11-16 overrun: 4 task.overrun events total (3 build, 1 on 11-16); stall field set on 4 tasks.

## 6. Manual orchestrator work (could be automated)
Counts from transcripts (mcp__local-llm__* tool_use, sessions 764f7558, 7527aebd, 3c176aad, 7127507b, 1d4c7184): spawn_task 44,
wait_tasks 53, task_status 18, message_task 18, complete_task 15, cleanup_task 15, fleet_status 10, cancel_task 4, task_diff 4,
report_activity 4, review_diff 2, gateway_health 3, set_chain 1, assign_role 2.
- Complete/cleanup: 15 complete_task + 15 cleanup_task calls after merges (11-14 764f7558: 14 each in a few batches at 14:48, 19:37,
  21:29, 22:40; every merge needs 3-4 manual completes plus cleanup of siblings, several complete_task calls were duplicated,
  e.g. t-ed71aeb3, t-ce3099a4, t-a685b50f twice). Meanwhile 64 tasks sit in state `review` never completed (11-11 10, 11-12 26, 11-13 10,
  11-14 7, 11-16 11) and `cleaned_at` is never set in DB (0 rows) although 111 task.cleaned_up events exist.
- Chain building: set_chain used 1x (t-cadd7916: build -> security-review -> fix -> final-review); other 40+ spawns carried
  parent_id/link/stage by hand. The same 5-field block (kind, profile, stage, parent_id, link, effort, mode, gate) hand-written
  on each spawn: 44 spawn_task calls, 43 with 8+ args.
- Message relays: 18 message_task calls to builders carrying reviewer findings, most multi-paragraph (e.g. 11-16
  11:15 5,075 chars, 11-16 07:39 2,475 chars) re-typed from review results: "Security review (Codex) returned CHANGES with 2 MED findings. Fix both"
  (11-14 13:44), "Final review (Fable) found one HIGH" (14:35), "Sonnet review returned CHANGES" (18:55), delta re-review
  prompt written by hand (11-14 19:02, 13:51). All are deterministic: findings -> fix prompt -> delta re-review -> merge.
- Re-kinding: test->review->build (11-16 07:35..07:37), codex re_review -> security_review (11-13 06:38), codex test -> haiku test
  (11-16 11:06), claude fix with wrong `link` (re_review not valid) -> relabel (14:24).
- Independence: haiku built, haiku tested -> refused (11-14 21:23) -> orchestrator then set independence waived by hand
  with a hand-written reason (t-a685b50f), then Fable 'docs-final-2' waived-without-reason refused (14:24) then re-spawned without the
  waiver at all. 1 waiver recorded; deterministic gate runs ("report HEAD/status") are a "test" kind that needs a model.
- Limit/gate waiting: 52 sleep/poll Bash commands in the 5 sessions (11 in 764f7558 for a limit reset, 29 in 3c176aad), 40-69 curl
  commands to the controller's own HTTP API (764f7558 42, 1d4c7184 69, 3c176aad 40) to read what MCP tools did not expose.
- Bash share in orchestrator chats: 7527aebd 283 Bash (199 read-files, 65 git, 25 test/gate, 18 python), 764f7558 115,
  3c176aad 194, 1d4c7184 221, 7127507b 125. Many are `git log/diff/status` per merge and `ls ~/.ai-fleet/worktrees`.
- Orchestrator chat cost (cache read): 7527aebd 107.7M (846 turns, 07:27-15:02 on 11-16, 17 wrap_up, 48 update_plan, peak ctx
  169k), 3c176aad 63.6M (506 turns, 11-13 20:54 to 11-14 18:49), 1d4c7184 58.5M (450 turns), 764f7558 49.7M (457 turns),
  7127507b 30.2M (234 turns), all well above fleet-task cost per unit of delivered change. Sum over these files: 313M cache-read.
  7 deferred-tool-schema errors (wrong arg names) wasted turns inside those chats.
- ToolSearch 38 calls across the five sessions (deferred schema loads) and update_plan 87 / wrap_up 58 = per-turn bookkeeping.

## 7. Wasted tokens and duplicates
- 25% of fleet tokens on capped/failed/cancelled (sec. 5); t-47138efa + t-7e5a253d duplicate respawn 3.5M cached; 3 paid final reviews
  that returned no verdict (token audit 2.3: no `final_review` key in DEFAULT_TOKEN_CAPS, turn cap 30 / ctx 100k).
- Duplicate reviews: 4 tasks titled "Security review, final whole-branch, read-only" (t-54eebb94 failed on limit, t-2cc52234 cancelled,
  Opus ones) = same job re-queued per limit; Fable "spec re-review" x2 in the same minute (t-3f892237 11-14 13:08, t-42744395 13:09,
  both link=review parent t-47dbe134; one spawn_task logged twice); "SharePoint plan verification" spawned 3 times.
- Fable used for 31 tasks (final_review 11, re_review 8, review 5, kind NULL 7) although rule says final-only; many still in `review` state, never completed.
- Local models: call_usage 26 rows total (11-16 ~2.8 k tokens, 1 of 2 `fast` failed, `reviewer` failed); LiteLLM gateway down (port 4000,
  Docker not running, `gateway_health` -> "down: ConnectError Errno 61") for part of 11-16; all 5 roles unrouted until 15:58. 13 ollama tasks (6 done, 2 failed
  'runner is busy', 5 in review state); 12 draft tasks mostly went to Sonnet/Haiku (3 of 4 on 11-16 over-routed).
- Codex: soft budget hit 11-13 05:21 (used 2,019,084 vs soft 2,000,000, event budget.soft); Codex tasks 50 vs claude 227.

## 8. Routing / placement problems
- `node.disk_low` 180 events (143 master, 34 njQNgvoQiz11CNTRL, 3 niCskTF7xM11CNTRL): chronic, no action recorded.
- `service.alert` 176 (heartbeat misses up to 147 beats, 181 s failed health checks), `service.restart_failed` 14 vs `service.restarted`
  14, `node.runner_offline/offline` 64 events on 11-14 (41) and 11-16 (23): node churn. 3 enrollments completed, 2 failed.
- All 290 tasks placed on node master (every `placement.accounts` is ["master"]); `task.unreachable` 1 (t-21cb1b8a, gate run "in the main
  checkout" on master). Capacity is single-node plus one node on enrollment; `placement.shadow_move` 1, shadow mode for limit/placement.
- profile split: fleet 196, spare 90, shared 4; the same task often goes to `spare` as a manual reroute (e.g. t-7e5a253d "retry on spare",
  7527aebd spawns with explicit `profile: spare|fleet` x16) because the controller's pick hits a 5-hour limit: account `claude@master/fleet`
  hit its 5-hour limit 8 times (budget.limit) and 7 tasks died mid-run on it. `near_limit_percent` 90 -> 85 applied after (token audit).
- Queue wait is negligible (median 0.27 s, p90 0.48 s, 11-16 max 68 s): no routing latency; slow part is agent time. agent_seconds on 11-16
  e.g. t-634b11a4 636 s, t-8a007477 585 s, t-f0bcb12a 464 s.
- Prompt "path not allowed by runner policy" (t-fb77887f), "runner is busy" (t-6743a975, t-c3ef4f30): no pre-flight against the node's
  policy/allowed repos; caller discovered by failure.
- Model aliases: 'sonnet' x7, 'fable' x3 (3c176aad spawn at 11-14 12:38..13:09) resolved fine but are recorded verbatim, so budget keys for
  `claude:claude-sonnet-*` vs alias may not match (budget.queued key is `claude:claude-sonnet-*`).

## 9. Candidate automation list (from the above)
1. Auto-substitute on refusal: apply the refusal's own "use X for kind" hint or relabel codex re_review -> security_review (11/11 refusals).
2. Pre-flight spawn: validate stage length, link enum, independence/waiver, repo allow-list, message arg names; return a corrected call.
3. Findings -> fix prompt -> delta re-review -> merge -> complete -> cleanup chain from `set_chain`, run by the controller (18 message_task relays,
   15+15 complete/cleanup, 44 spawns hand-configured).
4. Controller-owned wait-for-reset and queue-by-reset (replace 52 sleep loops; promote shadow queueing, 6 shadow_limit + 1 requeue_shadow events).
5. Classify `limit_unrecognised` by exit state not text (5 false positives).
6. Refuse/auto-link duplicate retries and duplicate identical spawns (t-7e5a253d; codex re_review x2; Fable spec re-review x2).
7. Quarantine known-flaky tests (test_two_triggers_race_for_one_claim, lease/race, cascade ordering) or rerun-failed-only in gate; one failing
   test caused 3 gate failures + 1 independence waiver + 2 fix/merge tasks.
8. Enforce delta-only re_review size (12.4 KB avg, 40 KB max) and cap/trim large final_review prompts (63 KB vs 40 KB soft).
9. Give read-only reviewer kinds a proper terminal state (verdict) instead of 'capped'; add `final_review` token caps.
10. Auto-complete/cleanup merged chains; 64 tasks stuck in `review`.
11. Route draft/check to local/Haiku and final reviews to Codex automatically; Opus not used for test/verify.
12. Orchestrator context: controller-held state (plan/handoff) so that a 107M-token session is not needed; trimmed fleet_status (12k tokens/call).

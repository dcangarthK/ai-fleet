# SPEC (Part D): the Live page, and all work through the fleet

Status: **v3, approved by Fable (spec review)** (2026-11-11); **§12 (heartbeats, leases, failover) and §13 are
owner amendments**, approved by Fable after two re-checks (t-dbfabaa5, t-7252bc86, t-f23507d9). v1 and v2 were reviewed by Fable (both "ready
with changes": 16, then 9 findings); §11 maps each finding to its change. Builds on Part C (`docs/SPEC-limit-handoff.md`: profiles,
account labels, slots, `GET /api/resources`). Build after Part C is merged.

## 1. Problem

The owner asked "who is doing what, where, with which model, on which account, and what is Codex doing?", and
could only get the answer by asking the orchestrator. Today that information is:
- **Scattered:** Overview, Budget, Tasks and Nodes each hold part of it.
- **Missing:** account, kind, effort, branch and stage on running work; idle workers; machine memory and swap. A
  16 GB swap episode on 2026-11-11 went unseen until it was investigated by hand.
- **Invisible:** work the orchestrator runs as its own subagents on another Claude account.

Goal: one **Live** page that answers those questions at a glance, and mechanisms so that all paid work runs
through the fleet, or at least reports itself, and shows on it. The existing pages keep their purpose; Live only
links to them.

## 2. All work through the fleet

### 2.1 The orchestrator's account as a fleet profile
- After Part C, the owner registers the orchestrator session's Claude login as a runner profile, e.g.
  `{"id": "spare", "dir": "<its config dir>", "label": "spare"}`, and signs it in once
  (`CLAUDE_CONFIG_DIR=<dir> claude` → `/login`).
- **`profile` on tasks:** `POST /api/tasks` and MCP `spawn_task` accept `profile`. It passes Part A admission and
  Part C slot selection. Admission must key on the **account label**:
  - a limited account → queued, or refused per `limits.on_limit`;
  - an unknown or unready profile → 400.

### 2.2 Orchestrator rule (CLAUDE.md)
- **Fleet tasks:** every build, fix, review, test, merge or benchmark runs as a fleet task, with a `kind`, a
  `profile` where it matters, and `parent_id`/`link` into its work item.
- **In-session subagents:** allowed only for reading and planning that changes nothing.
- **CLAUDE.md wording:**
  - pipeline steps 5 ("Cross-review") and 7 ("Final") are reworded: Claude's own reviews run as fleet tasks on a
    Claude profile, or are self-reported (§2.3) when they can't be;
  - the "Paid-model budget" section points to Live as the place to watch.

### 2.3 Self-reported activity (for what can't be a fleet task)
- **Who can post:** operate scope, so the orchestrator's MCP bridge can post.
- **MCP tools:**
  - `report_activity(title, kind, account_label, model, work_item?, status, tokens?, note?)` →
    `POST /api/activity`;
  - `set_chain(work_item, stages)` → `PUT /api/work/<root>/chain` (§3).
- **Labelling and limits:**
  - records are labelled **"self-reported"** everywhere they show;
  - they never feed auto-resolve, outcomes, pipelines, admission, limits or review independence;
  - `account_label` must be a known label (else 400);
  - text is redacted with the shared redaction module, and capped at 300 characters;
  - posts are rate-limited to 60 a minute per actor (token id, or `local`), with `X-Fleet-Session` as a sub-key;
  - records are kept 30 days, with pruning in `retention_once`.
- **Optional tokens:** a record may carry `tokens: {input, output, cached}`, copied from a subagent's completion
  report. They are shown, and counted in account room (§4.3) as "self-reported".

### 2.4 Orchestrator presence (derived, not remembered)
- **From bridge calls:** the MCP bridge stamps every controller call with the headers `X-Fleet-Session`,
  `X-Fleet-Model` and `X-Fleet-Account`. The bridge reads these from its environment; the orchestrator's settings
  set them.
  - **One helper:** a single shared header helper is used by **every** bridge HTTP call. That includes the calls
    that today bypass `_controller` (`server.py` lines 95, 102, 138, 167, 182, 341).
  - **Who counts:** the controller records last-seen per session only from local or operate-scope callers, never
    from view-scope ones.
  - **Validation:**
    - values must match `^[A-Za-z0-9._:-]{1,64}$`;
    - an unknown account label is ignored, never answered with a 400;
    - at most 20 sessions are kept, the oldest evicted first.
  - **Model label:** the model is shown as "configured model", because static environment can't follow a model
    switch mid-session.
- **Waiting on:** an in-memory registry of open `GET /api/tasks/wait` calls per session. `wait` gains a `Request`
  parameter, and the entry is removed in `finally`. Live shows the open wait's task ids, or the last wait's ids and
  how long ago it was.
- **No events:** presence and metrics updates never emit SSE events.
- **Doing:** an optional one-line `note` via `report_activity(kind: "orchestrator")`.
- **Display:** Live shows "Orchestrator (Opus 5.5, spare), last seen 2 min ago, waiting on t-c0bbd59e". It is
  stale after 15 minutes.
- **Optional hook:** a Claude Code hook on the Agent tool could post subagent start and stop automatically. That
  is an **optional** follow-up, because whether the hook payload carries the model and title is unverified. Manual
  `report_activity` is the fallback.

## 3. Stages

### 3.1 Pipelines
A pipeline's stage strip is **derived from the pipeline row**: its state, round and history. Gate and fix rounds
are steps in that history, not separate tasks: `build → gate → review(s) → fix → gate → review → ready/merged`. No
new task field is needed.

### 3.2 Ad-hoc chains (orchestrator-run)
- **Chain field:** the work item's root may carry a descriptive `chain`, set with `set_chain` or
  `PUT /api/work/<root>/chain`, operate scope. It is a list of `[{stage, agent, model?, profile?}]`, where
  `stage` matches `^[a-z0-9-]{1,24}$`. It never starts anything and never affects admission.
- **Stage on tasks:** each task may carry `stage` (same pattern). Task creation validates it, and the MCP
  `spawn_task` accepts it.
- **Stage states:** for each chain stage, the **latest attempt with that stage** decides its state:
  - `done`: it ended `review` or `done` with no `resolution`;
  - `failed`: it ended failed or interrupted with no resolution;
  - `current`: it is still running;
  - `superseded`: it carries a lineage `resolution`;
  - `next`: no attempt yet.
- **Repeats:** a stage repeated after a fix (e.g. `test-1` again) shows the latest attempt, with a small count
  ("×2").
- **Separate from kind and link:** `stage` is display only. `kind` stays the budget class and `link` the lineage
  relation.

## 4. Data

### 4.1 Runner metrics (cached, off the request path)
- **Background collection:** a daemon thread collects every 15 s, and `/health` serves the cached values with
  `age_s`.
  - Each command has a 3 s timeout, and the child process is killed when it runs over.
  - The Ollama probe currently made inside `/health` (`runner.py` ~222) moves into this thread.
  - When `age_s` is over 60 s, warnings show "unknown" instead of a stale value.
  - The memory page size is read from `vm_stat`'s header.
- **Values:**
  - memory total; memory used, defined on macOS as active + wired + compressed pages from `vm_stat`;
  - swap used (`sysctl vm.swapusage`);
  - disk free (reusing `free_disk_gb`);
  - the 5-minute load average and the core count;
  - Ollama up or down, and its loaded models (reusing the `/api/ps` listing).
- **Missing values:** null on Windows and wherever a value can't be read.
- **Timeouts:** collection never runs inside `/health`, so a swapping node doesn't time out the controller's 3 s
  health check.

### 4.2 `GET /api/live` (view scope)
Built from the controller's already-polled state (node polls, the DB). It makes no runner calls on the request
path, and the work-item query is bounded to the last 24 hours. It returns:
- `orchestrator`: the sessions from §2.4.
- `workers`: **one row per running task**, plus **one idle row per (node, agent, profile)** that has no running
  task.
  - A running row has: the task's id, title (the task title, which the owner chose and may come from the
    prompt's first line), agent, model, account label, kind, effort, stage, work item, branch, started_at, and
    the wait reason when queued.
  - An idle row has: `idle_since` (the last task end on that profile, or null), plus `limited_until` and the
    signed-in state from Part C.
  - Codex always gets an idle row when nothing runs on it.
  - Live and Part C's Resources board use the same helper.
- `queue`: queued tasks with their wait reason (budget slot, limit until T, approval, node capacity), oldest first.
- `chains`: active pipelines (§3.1), and work items with a `chain` or any attempt in the last 24 h (§3.2).
- `accounts`: per account label (§4.3).
- `self_reported`: active records and records ended within the last 24 h.
- `machines`: per node, the §4.1 values with `age_s`, plus warnings:
  - swap used > 4 GB or more than 25% of RAM;
  - disk free < 10 GB;
  - 5-minute load above 2× cores.
- `alerts`: the 10 most recent events of these types:
  - `task.awaiting_approval`, `task.blocked`, `task.failed` (gate or error);
  - `budget.limit` (**new**: emitted by `record_usage` when a provider limit is recorded),
    `budget.rules_invalid`, `budget.limit_unrecognised`;
  - `node.swap_high`, `node.disk_low`.

  The node events are new. They are emitted when a warning starts and when it clears, and deduplicated per node
  and type. That state is kept in memory, so one re-emit after a controller restart is accepted.

Nothing in the response carries prompt text beyond the task title, nor results, logs, or paths other than branch
names.

### 4.3 Account room (facts only)
- **Recording:** `agent_usage` gains `account_label`, written by `record_usage`.
- **Per account label,** `accounts` shows:
  - **last limit:** when it was hit and its `resets_at`;
  - **settled tokens since then:** input, output and cached, from fleet tasks, plus self-reported tokens listed
    separately;
  - **running:** the number of tasks running now;
  - **status:** one of "limited until T", "limit hit less than 5 h ago (reset passed)" or "no recent limit".
- **No window estimate:** the 5-hour window start isn't estimated, because use outside the fleet moves it. The page
  says so in one line.

## 5. The Live page (console `/live`)
- **Navigation:** placed first, above Overview. `/` stays Overview, and all other pages stay as they are. Live rows
  link to task detail, work items, Pipelines, Budget and Nodes.
- **Layout,** top to bottom:
  1. **Orchestrator presence**, plus the alerts strip.
  2. **Accounts:** one compact row per label.
  3. **Workers:** running rows (title, account and model, a `kind · effort · stage` line, branch, elapsed time),
     then idle rows. The text is kept short, and the detail shows on hover or tap.
  4. **Chains:** stage strips; the current stage is highlighted, and a failed stage is red with a link.
  5. **Queue.**
  6. **Machines and local models:** meters, loaded models, warnings, and the age of the data.
  7. **Self-reported activity**, in a distinct style.
- **Updates:** `useQuery` plus the existing SSE invalidation (`ui/src/lib/data.ts`), with a 20 s fallback poll
  that overrides the 30 s default.
  - The Live queries set `enabled` from the page's visibility, so a hidden tab neither refetches on invalidation nor
    polls.
  - Presence and metrics updates don't emit SSE events (§2.4). Node-derived data is as fresh as the latest heartbeat
  (about 10 s, §12) or, for nodes that don't push, the controller's node poll (up to 30 s), and the page shows
  "updated N s ago".
- **Phone width:** no horizontal page scroll; rows collapse to cards. Every section has an explanatory empty state.
- **Style:** the console's design system.

## 6. Security
- `/api/live` is view scope, and carries no prompts (beyond titles), results, logs or paths.
- Activity and chain endpoints are operate scope, validated, redacted, capped and rate-limited.
- Self-reported records never affect enforcement.
- **Profile pinning** can't bypass admission or slot rules.
- **Independence for ad-hoc tasks:**
  - **New kind:** `test` is added to `budget.KINDS` and to the seeded budget rules' kind lists, for every Claude
    rule; Codex stays reserved.
  - **When it applies:** to a task with `kind` review, re_review, final_review, security_review or test, and a
    `parent_id`, that was **not** created by the pipeline engine. Pipeline review tasks keep their existing check
    and Part C's escalation path.
  - **The check:** it runs **after admission**, against the resolved agent and model, including the runner's
    reported default when no model is named. It compares them with the work item's recorded builders (Part C
    §4.7).
  - **Match:** a match is refused with 400, unless the task carries `independence: "waived"` and a reason.
  - **Unknown model:** if the model still can't be resolved, the task is refused unless waived.
  - **Waivers** are **advisory**, because the orchestrator that sets them is the one being constrained. A waiver
    emits `task.independence_waived`, shows on Live and in the timeline, and is listed in the merge summary. The
    owner sees every waiver.

## 7. Acceptance criteria
1. **The `/api/live` response** returns orchestrator, workers, queue, chains, accounts, self_reported, machines and
   alerts, with no prompt text beyond titles, and no result or log text.
   - Two running fleet tasks (Opus and Sonnet on the fleet account) and an idle Codex give two running rows and an
     idle Codex row. Their account, kind, effort, stage, branch and elapsed time are correct.
2. **Idle rows** show `idle_since`, `limited until T` and "not signed in" correctly. One profile running 2 tasks
   gives two running rows.
3. **Queue rows** carry the right reason for budget slot, limit, approval and node capacity.
4. **Pipeline strips:** they come from pipeline history, fix rounds and gates included.
5. **Ad-hoc chains:**
   - they show done, current, next, failed and superseded per the §3.2 rules, with the repeat count;
   - stage and chain are validated;
   - MCP `spawn_task` accepts `profile`, `stage`, `link` and `kind`.
6. **Self-reported activity:** operate scope; an unknown label gives 400; redacted, capped and rate-limited; pruned
   after 30 days. It shows as "self-reported" on Live and in the timeline, never changes enforcement, and
   self-reported tokens appear separately in accounts.
7. **Orchestrator presence:** derived from bridge headers and open waits, and shown as stale after 15 minutes.
8. **Runner metrics** are collected in the background and served with their age. `/health` timing doesn't change
   under a slow `vm_stat` (test with a stalled fake). Warnings fire at the thresholds, and `node.*` events are
   deduplicated.
9. **Accounts** show the last limit, settled tokens since it, the running count and the status per §4.3.
   `agent_usage.account_label` is recorded.
10. **Profile pinning:** `profile: "spare"` runs under that profile. An unknown or unready profile is refused. A
    limited account is queued or refused per `on_limit`.
11. **Ad-hoc independence:** a review or test task whose agent and model match a recorded builder is refused,
    unless independence is waived with a reason, and the waiver shows on Live.
12. **The Live page** renders all seven sections with real data and empty states, uses SSE invalidation plus a 20 s
    fallback, and pauses when hidden. At 375 px wide there is no horizontal page scroll.
13. **No regressions:** no existing page loses a feature, and its tests pass unchanged. CLAUDE.md is updated
    (§2.2).
14. Gate, tsc, vitest and vite build pass, and existing tests are not weakened.

## 8. Test cases
- **Controller (fake runner):**
  - `/api/live` for each worker state, queue reason, pipeline strip and chain state;
  - no leakage;
  - activity auth, validation, rate limit, redaction, cap and prune;
  - presence from headers and waits;
  - accounts aggregation;
  - profile pinning accepted, refused and limited;
  - independence refused and waived.
- **Runner:** the background metrics thread with fake `vm_stat`, `sysctl` and `df`, including a stalled command
  and nulls; `/health` serving cached values with their age.
- **MCP bridge:** the session headers are stamped, and the new tools call the right endpoints.
- **Vitest:** row text per state, stage-strip states, warning display, the SSE/fallback refresh and pause
  (fake visibility), and phone layout classes.

## 9. Delivery (owner's chain for this part)
1. **Spec review:** Fable on the spare account reviews until satisfied.
2. **Build:** Opus 5.5 on the fleet (owner's personal) account, as a fleet task with `kind: build` and gate
   `fleet-gate` (pytest, tsc, vitest and vite build).
   - **Placement:** pin the profile explicitly. Use fleet only if it has no live limit and no other build running
     on it. Otherwise ask the owner whether to use spare or wait.
3. **Local hints (phase 1):** the local reviewer reads the diff per file against §7. Only findings that quote the
   diff exactly are kept, and they go to the builder as hints. This phase doesn't gate the merge, and Live labels
   it `hints`, not `test`.
4. **Test phase 2:** Opus 5.5 on the spare account runs the tests, then starts a temporary-DB controller on a
   spare port, inside its own worktree, and checks the page in a browser at desktop and 375 px widths.
   - It reports pass or fail with evidence for each acceptance criterion.
   - The tester's model matches the builder's, so this runs with `independence: "waived"` and the reason "owner
     chose Opus as tester".
   - Until Part C lands, phase 2 runs as an orchestrator subagent and is self-reported once §2.3 exists, or
     summarised to the owner before that.
5. **Final:** Fable on the spare account does the final review and test pass, and gives the green light for the
   merge.
6. **Fixes:** every issue goes back to the builder. Whoever found the issue re-checks only the changes, and Fable
   re-checks before the green light.
7. **Merge and restart:** a fleet task merges. The orchestrator then restarts the controller (`launchctl kickstart`),
   not the merge task, because a task can't restart the service that's supervising it.

## 10. Out of scope
- A hook that captures in-session subagents automatically (optional follow-up, §2.4).
- History charts for machine metrics.
- Controlling work from Live; those actions stay on the existing pages.
- Estimating the 5-hour window start.

## 11. Changes from v1 (Fable review)
1. **External posts were admin-only** → operate scope, "self-reported", never enforced, label-validated, with MCP
   tools (§2.3).
2. **The heartbeat relied on discipline** → presence derived from bridge headers and open waits; the hook is an
   optional follow-up (§2.4).
3. **Workers vs slots** → one row per running task plus idle rows per profile (§4.2).
4. **Stage taxonomy** → pipeline strips derived from history; ad-hoc stages validated; latest attempt wins;
   superseded = resolution (§3).
5. **Account room not computable** → facts only, and `agent_usage.account_label` (§4.3).
6. **Placement duplicated Part C** → pin the profile explicitly, else ask (§9 step 2).
7. **Metrics could time out `/health`** → a background thread serving cached values (§4.1).
8. **Polling vs SSE** → SSE invalidation with a 20 s fallback; bounded chains query (§5, §4.2).
9. **Alerts** → named event types, deduplicated node events (§4.2).
10. **Independence claim** → defined for ad-hoc tasks, with a waiver (§6, AC11).
11. **§9 coherence** → `hints` not `test`, gate named, phase-2 setup, restart owner.
12-16. Title carve-out; `tokens_so_far` cut; a shared helper with the Resources board; `/` stays Overview;
    CLAUDE.md reconciled; macOS memory defined; Windows nulls; prune in `retention_once`.

**v2 re-check (9 findings) → v3:**
1. `test` kind added, and `re_review` is covered.
2. The independence check runs after admission on the resolved model; unknown models are refused unless waived;
   pipeline-created tasks are exempt; waivers are advisory, with an event.
3. Presence headers go through one shared bridge helper, only from local or operate callers, validated and capped;
   the model is labelled "configured".
4. Open waits use an in-memory registry, plus the last wait's ids and age.
5. Presence and metrics don't emit SSE; queries are gated on visibility; the 20 s fallback overrides 30 s.
6. The rate limit keys on the actor, with the session as a sub-key.
7. A `budget.limit` event is added; node event dedupe allows one re-emit after a restart.
8. The metrics thread: daemon, per-command timeout with kill, the Ollama probe moved, "unknown" after 60 s.
9. `fleet-gate` is confirmed in the master's `~/.ai-fleet/runner.json` (profiles: fleet-gate, pytest, unittest).

**Fable's notes on approving v3 (for the builder):**
- (a) The rate limit is 60 a minute per actor in total; the session id is used only to attribute posts.
- (b) The independence check runs before the task row is inserted and before `log_decision`, so a refused task
  leaves no trace.
- (c) The shared bridge helper must keep `wait_tasks` at local/operate scope, so presence from waits still counts.

**Owner's pasted ideation (2026-11-11): rejected parts, with reasons.** Each conflicts with a decision already made.
- **API SDK clients and API keys.** Paid work runs through the signed-in `claude` and `codex` CLIs on subscription
  profiles (Part C). The fleet holds no API keys, and the bridge must never be handed one (CLAUDE.md Rules).
- **Rewriting `config.toml` on each call.** Several jobs share one profile, so a per-call rewrite races between them.
  Model, effort and profile are passed per job as CLI arguments and environment (`CODEX_HOME`, `CLAUDE_CONFIG_DIR`)
  instead.
- **`--max-turns`.** The installed `claude` CLI has no such flag. Turn limits stay in `launch_extra.max_turns`, which
  only runners that support it apply, and `limits_applied` records what really happened.
- **Docker-mounted profiles.** Profiles are the runner's own directories, chosen by id from `runner.json` (Part C
  §3). Mounting them into containers would copy auth state, which Part C forbids.
- **A fixed 1-hour default cooldown.** The exact `resets_at` is parsed from the provider's limit message (Parts A
  and C). A guessed cooldown would either hold work back after the reset or retry too early.
- **`/tmp` databases.** `fleet.db` holds the budget rules, lineage and the audit trail, and must survive reboots.
  Only tests use temporary databases.

## 12. Heartbeats, leases and failover (owner amendment, 2026-11-11; v2 after Fable's spec review)
ZooKeeper-style liveness, adapted to this fleet, where the controller assigns work and runners don't pull it.
Leader election is out of scope: there is one controller.

### 12.1 Worker heartbeats
- **Who pushes.** A runner pushes when its `runner.json` has `controller` (the controller's base URL) **and** it
  has learned its node id.
  - **The URL** must be `https://…`, or `http://` to a loopback or tailnet address (100.64.0.0/10 or a `*.ts.net`
    name). Anything else is ignored, with one log line.
  - **The node id** comes from the controller. Every `/health` poll sends the header `X-Fleet-Node-Id` (pattern
    `^[A-Za-z0-9._:-]{1,64}$`), and the runner keeps the latest value in memory. A runner restart learns it again
    at the next poll (≤ 30 s). There's no enrolment change, duplicate host names don't matter, and the master
    learns `master`.
  - The runner honours `X-Fleet-Node-Id` **only on a request that passed its runner-token check** (today `/health`
    needs the token), so no one can rename a runner and break its heartbeats.
  - **The master** pushes to `http://127.0.0.1:8700` once its `runner.json` has `controller`. The onboarding
    scripts write `controller` for new nodes; existing nodes need the owner to add it.
- **What is pushed.** `POST /api/nodes/heartbeat` about every 10 s (±2 s jitter). The body is the runner's cached
  `/health` (§4.1, including `age_s` and Ollama's `loaded` models). Without `controller` or an id, the runner doesn't
  push and behaves as today.
- **Auth, without sending the token.** The runner signs each post. The headers are:
  - `X-Fleet-Node: <id>` and `X-Fleet-Ts: <unix seconds>`;
  - `X-Fleet-Sig: hex(HMAC-SHA256(runner_token, id + "\n" + ts + "\n" + hex(sha256(body))))`.

  The controller does the following, in order:
  1. Looks up the key with the same function it uses to call that runner (`node_runner_token`, including the
     master's file fallback). An empty key is a failure.
  2. Checks the signature with `hmac.compare_digest`.
  3. Rejects a skew of more than 60 s, and any `ts` not greater than the last accepted one for that node (replay).
     The last accepted `ts` is persisted in `nodes.heartbeat_ts`, so a controller restart doesn't reopen the
     replay window.
  4. Requires the node to be approved.

  Every failure gets the same `401 {"detail": "heartbeat refused"}`, so the endpoint can't be used to probe which
  node ids exist. The raw token never goes on the wire.
- **Middleware.** The exact path `/api/nodes/heartbeat` is added to `PUBLIC_PATHS`, so `access_control` lets it
  through on both ports without `X-Fleet` or a user token. The handler does all the auth and gives no API scope. The
  audit actor is `node:<id>`. A runner token or signature is worth nothing on any other path (test: 403 elsewhere on
  the public port).
- **Limits.**
  - The body is capped at 64 KB (413). This is checked before the signature, but the response gives nothing
    away.
  - Authenticated posts: at most 1 per 2 s per node (429, nothing stored except `heartbeat_ts`, which a
    validly signed post always advances, so a rate-limited post can't be replayed later).
  - Failed attempts: at most 30 a minute per source address (429 for that source's *failed* attempts only).
    A correctly signed post from the same address is still accepted, so a bad actor behind a shared address
    can't block real heartbeats.
  - Bad-signature posts naming node X never use up X's own allowance (test).
- **What a heartbeat updates.**
  - `nodes.runner`, parsed and stored by the same code as a polled `/health`, so a heartbeat can claim nothing a
    poll couldn't. Unknown fields are dropped.
  - The new `nodes.heartbeat_at` (acceptance time) and `nodes.runner_age_s` (the body's `age_s`).
  - It **never** resets `runner_fails`, because a heartbeat proves only runner-to-controller reachability, and
    starting work needs controller-to-runner. Only a successful poll resets it. A successful poll also sets the
    new `nodes.polled_at`, and writes `runner_age_s` from the same parse path.

### 12.2 Liveness: one rule, one helper
- `tasks.runner_offline(node, now) -> str | None` is the only definition. `eligible()`, `handoff._offline`, the
  console's `runner_ok` (`controller.py` ~2248) and Live all use it. A node is **OFFLINE** when any of these holds:
  1. no runner report, or `runner_fails >= 3` (today's poll rule, kept for every runner);
  2. **heartbeat expiry:** the node has an accepted heartbeat (`heartbeat_at` not null) **and**
     `now - heartbeat_at > 25 s` (`FLEET_HEARTBEAT_TTL`) **and** `now - polled_at > POLL_SECONDS + 5` (35 s).
     A NULL `polled_at` (after migration, or no successful poll since) counts as stale; the startup grace covers
     the window just after the migration.
- **Why the poll term:** the 30 s poll can't refute a 25 s expiry on its own, so expiry needs the poll to be stale
  too.
  - A node that can be polled but whose pushes fail stays online, and doesn't flap. Live shows "heartbeat lost,
    polling".
  - A dead node is OFFLINE within 25 to 35 s of its last contact. Rule 1 still catches it at 3 failed polls.
  - A runner that never got a heartbeat accepted (wrong URL, no `controller`) is judged by rule 1 only. Its own
    `heartbeat` flag is never trusted.
- **The sweep.** A 5 s loop in the controller owns the transitions:
  - it evaluates `runner_offline` for each node and compares the result with its in-memory last state;
  - it emits **`node.runner_offline`** and **`node.runner_online`** on a change. These are new names, because the
    existing `node.offline` is the Ollama engine probe (`healthy()`), which keeps its meaning;
  - on **every** pass after the grace, it attempts failover (12.6) for each queued, unpinned task whose node is
    OFFLINE **now**. This is level-triggered, not only on the transition, so a task that found no node at first is
    re-placed when one appears, and a node that was already offline at controller start doesn't strand its queue.
    The conditional UPDATE makes repeated attempts idempotent.
- **Startup grace.** For `FLEET_HEARTBEAT_TTL + POLL_SECONDS` (55 s) after the controller starts, rule 2 isn't
  applied and the sweep only seeds its state, emitting nothing. A restart is then no burst of offline events, and
  `recover()` doesn't re-place work on stale timestamps.
- **Alerts.** `node.runner_offline` joins the §4.2 alerts list. Heartbeats themselves never emit SSE events (§2.4).

### 12.3 Warm models
- The warm list is the Ollama `loaded` list from the latest `/health`, by poll or heartbeat. Its age is
  `now - max(heartbeat_at, polled_at) + runner_age_s`, and it is used only while that is under 60 s.
- **Routing.** For `agent: "ollama"`, `pick_node` sorts by (can start now, **model not warm**, running tasks,
  −RAM). Model names match `node["models"]` exactly. Eligibility is unchanged: a node still needs the model
  installed.
- Live's Machines section marks warm models. This adds no model loading and no new Ollama calls.

### 12.4 Paid accounts
Passive only. Account state comes from real task output: limit messages, `resets_at` and auth failures, which Parts A
and C already record, plus the owner's reset (§13 b). The fleet **never** sends a heartbeat prompt, a probe or an
availability check to a paid model (CLAUDE.md budget rule 7).

### 12.5 Leases on task starts
- **The invariant.** After creation, `tasks.node_id` changes only through the conditional re-place UPDATE (12.6),
  and only while `state='queued'`.
- **One claim per start.** `transition()` gains `where_node`. `try_start` moves `queued → starting` with
  `WHERE id=? AND state='queued' AND node_id=?` (the node of the snapshot it checked), and checks the rowcount: Part
  C's claim pattern. `start()` then re-reads the row (`tasks.py` ~584), so it sends the job to the node that won.
- **Runner idempotency** stays: the job id is the task id, and a repeat `POST /jobs` is a 409 that the controller
  adopts.
- Together these mean a task can't be started on two nodes, even when a re-placement races a start.

### 12.6 Failover
- **Placed or pinned.** A new `tasks.placement` (JSON) is written at creation. It holds the inputs `pick_node` needs
  to run again: role, skills, escalation, the request's own policy layer, and the account restriction. It also
  holds `pinned`. A task is pinned when any of these is true:
  - the request named `node`;
  - it has `review_of` (it reads that node's worktree);
  - it is a handoff or retry from a checkpoint (`base_branch`/`source_job`, which live on that node);
  - it has a `cwd` from an earlier run;
  - its `placement` is NULL (tasks queued before this change): it waits, and is never re-placed.
- **Queued, placed work moves.** On each sweep pass (12.2), each `queued`, unpinned task on an OFFLINE node is
  re-placed:
  - `pick_node` runs again with the stored inputs, offline nodes excluded, and the same account restriction (the
    account's slots on other nodes);
  - one conditional UPDATE (`WHERE id=? AND state='queued' AND node_id=<old>`) writes `node_id`, `policy`,
    `policy_hash`, `instructions`, `profile`, `account_label` and `account`;
  - it emits `task.replaced {from, to}`, on success only.

  If no online node qualifies, or the account has no slot on one, the task stays where it is with the reason
  "node offline".
- **Pinned and started work stays.** A pinned queued task waits ("node offline"). A `starting` or `working` task
  keeps its state, because its worktree and session are on that node, and a second copy would mean two writers and
  double the paid work.
  - Live shows "node offline since T".
  - When the node returns, the existing poll and `recover()` pick the task up. If the runner lost the job, the task
    becomes `interrupted` as today, and the owner can retry it or hand it off.
  - Part C's checkpoint needs the node, so there is no automatic handoff from a dead node.
- **Not in scope:** leader election, a second controller, and moving a running session between nodes.

### 12.7 Acceptance criteria (additions to §7)
15. **Heartbeat push and refusal.**
    - A runner with `controller` set and a learned id pushes `/health` about every 10 s, signed. The raw token is
      never sent.
    - Each of these gets the same 401, with no 429 leaking which ids exist: bad signature, stale or replayed `ts`,
      unknown node, unapproved node, empty key.
    - A post over 64 KB gets 413. A second authenticated post within 2 s gets 429. Bad-signature posts for node X
      don't 429 X's real heartbeat.
    - The path needs no user token on the public port, and the signature is worth nothing on any other path.
16. **Liveness.**
    - `runner_offline` is the single rule (12.2). A heartbeat never resets `runner_fails`.
    - A node with heartbeats goes OFFLINE (`node.runner_offline` emitted once) when both its heartbeat (25 s) and
      its poll (35 s) are stale (a NULL `polled_at` counts as stale). It comes back online (`node.runner_online`
      once) when `runner_offline` returns None: on an accepted heartbeat if `runner_fails < 3`, otherwise only on
      a successful poll. With `runner_fails >= 3`, resumed heartbeats and failing polls leave it OFFLINE.
    - A node whose pushes fail but whose polls succeed never goes OFFLINE.
    - Nothing is judged OFFLINE by expiry during the startup grace, and a restart emits no burst.
17. **Warm routing.** Ollama routing prefers a node where the model is warm, but only while the warm data, by its
    12.3 age, is under 60 s old.
18. **No paid probes.** No heartbeat, probe or availability call to a paid model exists (test: no runner call for
    paid agents outside real tasks).
19. **Leases and failover.**
    - Starts are leased: a start racing a re-placement starts on at most one node.
    - A placed, queued task on an OFFLINE node is re-placed, with the new node's policy, profile and account.
    - A pinned one (named node, `review_of`, checkpoint, NULL `placement`) waits. Started work stays where it is.
    - Re-placement is level-triggered: a task with no qualifying node at first is re-placed once one appears, and a
      node already offline at controller start (after the grace) has its placed queue re-placed.

### 12.8 Tests (additions to §8)
- **Controller:**
  - heartbeat auth: good signature, bad signature, skew, replay, unknown node, unapproved node, empty key;
  - the size cap, both rate limits, and per-node isolation from bad posts;
  - the public-port middleware path;
  - `runner_offline` cases with a fake clock: push-broken-but-pollable, dead node, startup grace, NULL
    `polled_at`, and `runner_fails >= 3` with resumed heartbeats;
  - the replay check surviving a restart (persisted `heartbeat_ts`);
  - single-emit events;
  - warm-model order and the age cut-off;
  - the lease race (start versus re-place);
  - re-placement rewriting policy, profile and account;
  - pinned versus placed, including a queued review of an offline node's build and a NULL `placement`;
  - level-triggered re-placement: no node at first then one appears, and a node offline at controller start.
- **Runner:**
  - learns its id from the poll header, and only from a token-checked request;
  - signs posts (verified against the controller's check);
  - keeps the jitter, survives controller errors, and does nothing without `controller`, an id, or an allowed URL.

## 13. Owner requirement (2026-11-11 20:40): the console shows what's happening now (v2 after Fable's spec review)
- **(a) Limit state from the account.**
  - **Paused** means either:
    - failed, with `failure_reason='limit'` and no resolution; or
    - queued, with `budget_wait='limit'`.
  - **Where the state comes from.** A paused task's "Paused: limit, resets T" comes from the **account's** current
    limit state (`budget.limit_snapshot`, per account and identity), not from the copy saved on the task row.
  - **Clearing.** A limit is cleared by either of these:
    - a later successful run of the **same identity on the same account**, as `budget.py` does today (a Sonnet
      success never clears an Opus limit);
    - the owner reset in (b).

    Once the account's limit for that identity has cleared, a paused task shows "Limit cleared: resume or hand
    off" (attention). It never shows a stale reset time.
- **(b) Owner reset.** `POST /api/budget/accounts/reset {account}` (`account` as in `budget.account_key`, e.g.
  `claude/owner-max`), **admin only**: the full path `/api/budget/accounts/reset` is added to `ADMIN_PATHS`, and an
  operate token gets 403. An unknown `account` gets 404, and no `limit_resets` row is written.
  - **What it records.** An appended, audited row in a new `limit_resets(account, at, actor)` table, and the event
    `budget.limit_reset`.
  - **Effect.** `limit_snapshot` ignores every limit on that account, for all its identities, whose usage row is
    dated at or before the reset. It changes enforcement and display alike, so queued tasks waiting on that limit
    start at the scheduler's next pass. A task that settles later with a newer limit message limits the account
    again, as it should.
  - **Where the action lives.** On the Budget page. Live shows the account's state and links to Budget for the
    reset, so v3 §10 (no controlling work from Live) stands.
  - **Status.** §4.3 gains "reset by owner at T".
- **(c) Handoffs shown automatically.** When work moves to another task, the source shows where it went, with no one
  recording it:
  - "Handed off to <task>" for Part C's handoff;
  - "Retried as <task>" for a child with `parent_id` + `link: retry`.

  The newest such child is shown. The existing auto-resolve ("Resolved by …") still wins once the retry succeeds.

### 13.1 Acceptance criteria (additions to §7)
20. **Stale reset times.** A paused task whose row carries a stale `resets_at`, on an account whose limit has
    cleared (by a same-identity success or an owner reset), shows "Limit cleared: resume or hand off". A
    different-identity success leaves it limited.
21. **Owner reset.** It is admin only (operate gets 403; an unknown account gets 404 with no row), records a `limit_resets` row and a `budget.limit_reset`
    event, releases tasks queued on that limit, and is overridden by a newer limit.
22. **Automatic display.** The source shows "Handed off to <task>" for a Part C handoff, and "Retried as <task>"
    for a `link: retry` child (the newest one), with no `/resolve` call.

The tester's independence waiver (§9 step 4) is set by whoever spawns the tester task, with the reason "owner
chose Opus as tester". It isn't recorded automatically.

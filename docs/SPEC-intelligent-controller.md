# Spec: intelligent controller (Part I)

Status: 2026-11-16. Decided and built without owner input, per the owner's brief (deliver by 22:00).
Builds on Parts G (grounding, self-heal, handover) and H (token budget), `SPEC-paid-budget.md` and
`docs/TOKEN-AUDIT-2026-11-16.md`.

## 1. Evidence (15–16 Nov 2026)

Sources: `docs/TOKEN-AUDIT-2026-11-16.md` (all profiles, `fleet.db` `agent_usage`/`tasks`), the orchestrator
transcripts in `~/.claude/projects/-Users-duncansynthesis-co-za-ai-fleet/` and the controller's `fleet.db`. The
detailed transcript and task-history extract is in section 9.

| # | Finding | Cost | What the controller lacked |
|---|---|---|---|
| E1 | One orchestrator chat read 106.9M of 186.4M cached tokens (57%) in 838 turns; 281 Bash calls, 24 spawns each re-reasoning model, kind and brief. | largest pool | A way to ask "who should do this, at what cost" in one call. |
| E2 | Three paid final reviews (2 Fable, 1 Opus) capped with no verdict, 360k cached; Codex sat at 5% of its window while the fleet Claude account hit 100%. | 360k + a duplicate review | Routing by kind that prefers the idle scarce model for the kind it is reserved for. |
| E3 | Doc drafts ran on Sonnet (3 of 4, 3.0M cached) although `draft` belongs to local/Haiku. A verification harness ran on Opus (6.1M cached over 3 records). | ~8M | A default pick when the orchestrator names a model out of habit; visible "over-routed" metrics. |
| E4 | `t-47138efa` was started on an account already near its 5-hour limit and died mid-run (1.4M cached); a manual duplicate retry cost 2.1M more. | 3.5M | A forecast of account headroom before dispatch; recorded reasons for every reroute. |
| E5 | A Codex `re_review` was refused (Codex's kinds exclude `re_review`) and the orchestrator re-kinded it by hand as `security_review`. | a turn + a mislabelled task | A refusal that says what to do instead. |
| E6 | `agent_usage.cost_usd` is cumulative per task; summing it overstates spend by 59%. Codex cost is not reported. | wrong numbers | Savings metrics computed correctly (max per task). |
| E7 | Local roles did ~2.8k tokens all day (gateway down until ~15:58). | every local-able job went paid | Local-first visible in the decision, and counted. |
| E8 | Manual pipeline steps every batch: pick models, build the chain, `complete_task` + `cleanup_task` per merged branch. | orchestrator turns | One call for a routed pipeline template; one call for merged-branch cleanup. |

Already built and therefore **not** rebuilt here: limit handoff (`handoff.py`), stall reroute (`stall.py`), auto
continuation of capped builds, gate bounce to the builder (`pipeline.GATE_BOUNCES = 3`), delta re-reviews,
prompt stripping and chunking (`budget.strip_prompt`), duplicate-retry refusal (owner's working tree, audit #2),
trimmed `wait_tasks`/`task_status`. This Part adds the *deciding* layer on top of them and makes it explainable.

## 2. Principle

The controller already enforces rules (refuse, queue, hand off). It does not yet *choose*. Part I adds one pure
module that ranks candidates for a task from evidence (rules, history, headroom, load, cost) and returns the pick
with its reasons, and one router that exposes the picks, the forecasts and the savings. Every automatic choice is
recorded with its explanation. Nothing here overrides a budget rule, approves a task, or uses `budget_override`.

## 3. Design

- `master/controller/intel.py`: pure functions (no FastAPI import) plus small DB helpers that take a sqlite
  connection. Unit-testable without the controller.
- `master/controller/intel_routes.py`: `router = APIRouter()` with the `/api/intel/*` routes below; included by
  `controller.py` with one `app.include_router(...)` line.
- Table `intel_decisions` (created by `intel.ensure_schema(conn)`, idempotent, called from the router on first
  use): `id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, task_id TEXT, kind TEXT, action TEXT, chosen TEXT,
  candidates TEXT (json), reasons TEXT (json), est_tokens INTEGER, est_usd REAL`.
- Hooks in existing code are kept to a few lines each (the owner has uncommitted edits in `tasks.py`,
  `budget.py`, `tokens.py`, `master/mcp/server.py`; small hooks merge cleanly).

## 4. Requirements

### R1 Candidate ranking — `intel.rank(kind, *, rules, stats, headroom, load, prompt_bytes=0, exclude=())`

Returns a list of candidate dicts, best first: `{"key", "agent", "model", "score", "eligible", "est_tokens",
"est_usd", "reasons": [str]}`. `key` is a budget-rules key style identity such as `claude:claude-sonnet-5-5`,
`codex` or `local:fast`.

Candidates: the concrete models in `CANDIDATES` (`local:fast`, `local:coder`, `claude:claude-haiku-5-5`,
`claude:claude-sonnet-5-5`, `claude:claude-opus-5-5`, `claude:claude-fable-5-2`, `codex`).

Hard filters (ineligible, kept in the list with `eligible: False` and the reason, sorted last):
1. Kind not allowed for the model by `rules` (use `budget.match_key` + `budget.kind_allowed` semantics; for a
   pure test, `rules["models"]` patterns are matched with `fnmatch`).
2. Rule 0: Fable never `build`, `fix`, `test`, `merge`, `benchmark`, `draft`, `check`.
3. Codex never `merge`, `benchmark`, `check`, `draft`, `re_review`, `test`.
4. Local roles only for `draft`, `check`, `review` (first-pass only) and summaries; and only when
   `headroom["local:fast"]`/`["local:coder"]` says `routed: True`.
5. `headroom[key]["limited_until"]` in the future, or `used_percent >= near_limit_percent` (rules
   `limits.near_limit_percent`, default 85).
6. Any key in `exclude` (the builder's key for review kinds: independence).

Score (higher is better), for eligible candidates:
- `preference`: position in `KIND_PREFERENCE[kind]` (the CLAUDE.md routing table): first = 40, then 30, 20, 10,
  others 0. `KIND_PREFERENCE`: build/fix → sonnet, opus, codex; test → sonnet, haiku; review → sonnet, opus,
  haiku; re_review → haiku, sonnet; final_review → codex, opus; security_review → codex, opus; merge, benchmark,
  check → haiku, sonnet; draft → local:coder, local:fast, haiku, sonnet; lead → sonnet.
- `success`: `stats[(kind, key)]` = `{"done": n, "bad": m}`; rate = (done+1)/(done+bad+2) (Laplace), contributes
  `30 * rate`; with fewer than 3 samples it contributes `15` (neutral) and the reason says "no history".
- `headroom`: `-0.3 * used_percent` of the account's 5-hour window (0 when unknown).
- `load`: `-10` per running task on that key, `-100` when at `max_concurrent`.
- `cost`: tier penalty cheap 0, standard -5, premium -15, scarce -10 (scarce is reserved, not expensive in money;
  its preference already limits it to its kinds).

Every term that moved the score adds a short reason, e.g. `"preferred for final_review"`, `"5h at 92% (>= 85)"`,
`"done 7 / bad 1 for build"`.

### R2 Cost estimate — `intel.estimate(kind, key, history, prompt_bytes=0)`

`history` is a list of per-task rows `{"kind", "key", "input", "output", "cached", "cost_usd"}` where `cost_usd`
is already the **max per task** (E6). Returns `{"tokens": int, "usd": float | None, "basis": str, "samples": n}`:
median of `input+output+cached` and of `cost_usd` over rows with the same kind and key; else same key any kind
(basis `"model"`); else `DEFAULT_ESTIMATE[kind]` tokens with `usd: None` (basis `"default"`). Codex `usd` is
always `None` (not reported, E6). `prompt_bytes` adds `prompt_bytes // 4` tokens.

`intel.history(conn, since)` builds those rows from `agent_usage` joined to `tasks`, aggregating per task:
`SUM(input)`, `SUM(output)`, `SUM(cached)`, `MAX(cost_usd)`.

### R3 Account forecast — `intel.forecast(accounts, now)`

`accounts` maps an account key to `{"used_5h", "resets_5h", "used_week", "resets_week", "samples": [(ts,
used_5h), ...]}`. Returns per account `{"status": "ok" | "at_risk" | "limited", "used_5h", "projected_5h",
"exhausts_at", "resets_5h", "advice"}`:
- `rate` = %/hour from the oldest and newest sample within the last hour (None with fewer than 2).
- `projected_5h` = `used_5h + rate * hours until resets_5h` (capped at 100; equals `used_5h` without a rate).
- `exhausts_at` = when `used_5h` reaches 100 at that rate, if before `resets_5h`.
- status `limited` if `used_5h >= 100` or `used_week >= 100`; `at_risk` if `projected_5h >= 85` or `used_5h >=
  85`; else `ok`. `advice` is one sentence, e.g. `"hold new paid work on claude/fleet until 19:30; use claude/spare"`.

### R4 Decisions with explanations

`intel.record(conn, *, task_id, kind, action, chosen, candidates, reasons, est_tokens=None, est_usd=None)`
inserts into `intel_decisions`. Actions: `auto_pick`, `suggest`, `forecast_hold`, `cleanup`. `GET
/api/intel/decisions?task_id=&limit=50` returns newest first with `candidates` and `reasons` decoded.
`GET /api/intel/tasks/{task_id}` returns that task's decisions (empty list for none).

### R5 Refusals that say what to do — `intel.suggest(kind, refused_key, rules, headroom, load, security=False)`

Returns up to 3 eligible alternatives as `{"agent", "model", "kind", "why"}` from `rank`. Special case (E5): a
refused Codex `re_review` suggests (a) the original round reviewer's model if eligible, else the top `re_review`
candidate, and (b) only when `security=True`, `codex` with `kind: "security_review"`. The controller's existing
budget refusal (HTTP 4xx from `POST /api/tasks`) gains a `suggest` list in its detail. No automatic re-kinding.

### R6 Auto pick — `model: "auto"`

`POST /api/tasks` (and MCP `spawn_task`) with `model == "auto"` (agent may be `"auto"` or a concrete agent)
replaces agent/model with the top eligible `rank` candidate for the task's `kind` (kind required, else 400),
excluding the parent's builder key for review kinds. Local picks are not taken for an agent task: when the top
candidate is `local:*`, the first non-local eligible candidate is used and the reason says so. The choice is
recorded (`auto_pick`) before admission; admission still runs unchanged and may still refuse or queue.

### R7 Pipeline template — `GET /api/intel/template?kind=build&security=0`

Returns a `run_pipeline`-shaped spec: `{"builder": {"agent", "model"}, "reviewers": [{"agent", "model"}],
"final": {"agent", "model"}, "gate_bounces": 3, "reasons": {...}}`: builder = top build candidate; reviewer =
top `review` candidate excluding the builder; final = top `final_review` candidate (Codex by preference;
`security_review` when `security=1`). One call replaces the per-batch hand routing (E8).

### R8 Merged-branch cleanup — `intel.cleanup_candidates(tasks, is_merged)`

Pure: from task rows (`id`, `state`, `branch`, `repo`, `node_id`, `worktree`) return those in a terminal state
(`done`, `failed`, `cancelled`) with a worktree on node `master` whose `branch` `is_merged(repo, branch)` returns
True. `GET /api/intel/cleanup` lists them (`is_merged` = `git -C repo merge-base --is-ancestor branch main`, any
error = False). `POST /api/intel/cleanup` (admin or operate scope) calls the existing cleanup for each and records
a `cleanup` decision per task. It never touches unmerged branches.

### R9 Savings — `intel.savings(conn, since, baseline_since, baseline_until)`

`GET /api/intel/savings?since=&baseline_since=&baseline_until=` (defaults: since = 2026-11-16 20:00 SAST epoch
`INTEL_EPOCH`, baseline = the 48 h before it). For each window returns:
- `paid_tasks`, `paid_tokens` (input+output+cached over claude/codex), `tokens_per_task`
- `wasted_tokens` and `wasted_share`: tokens of tasks ending `capped`, `failed` or `cancelled`
- `final_reviews_on_codex`: share of `final_review` tasks whose agent is codex
- `drafts_off_sonnet`: share of `draft` tasks not on Sonnet/Opus
- `usd` = sum of per-task `MAX(cost_usd)` (E6), `codex_usd: "not reported"`
- `local_tokens` from `call_usage`
- `auto_picks`, `suggestions`, `cleanups` counts from `intel_decisions`
plus `delta` = current minus baseline for each numeric field.

### R10 Estimate before spawn — `POST /api/intel/estimate`

Body `{kind, agent?, model?, prompt_bytes?, parent_id?}`. Returns `{"candidates": rank(...),
"chosen_estimate": estimate(...) for the named model or the top candidate, "forecast": forecast for that
account}`. MCP tools `estimate_task(kind, model="", prompt_bytes=0)` and `intel_savings()` call it; both return
trimmed text (top 3 candidates, one line each).

### R11 Forecast route — `GET /api/intel/forecast`

`forecast()` over accounts built from `budget.limit_snapshot` / usage records, plus the `rank` view of which
account a new paid build would go to now.

## 5. API for the UI (ui-revamp session)

All JSON, read-only GETs, safe to poll at the Live cadence:

| Route | Shows |
|---|---|
| `GET /api/intel/decisions?task_id=&limit=` | Why each task went where it went |
| `GET /api/intel/tasks/{id}` | Decision panel on a task |
| `GET /api/intel/forecast` | Per-account headroom, projected use at reset, advice |
| `GET /api/intel/savings` | Current vs baseline window, deltas |
| `GET /api/intel/template` | Suggested pipeline routing |
| `GET /api/intel/cleanup` | Merged branches ready to clean |
| `POST /api/intel/estimate` | Pre-spawn ranking and cost |

## 6. Safety

- No route approves, unblocks, or overrides budget. `POST /api/intel/cleanup` acts only on merged branches.
- `rank` never makes a refused kind eligible; admission still runs after an auto pick.
- No prompt text is stored in `intel_decisions`.

## 7. Acceptance criteria

1. `rank("final_review", ...)` with Codex at 5% and Claude fleet at 100% puts `codex` first; Fable/Opus follow.
2. `rank("draft", ...)` puts a routed local role first, else Haiku; Sonnet ranks below Haiku.
3. `rank("build", ...)` never has Fable or Codex-`re_review`-style forbidden pairs eligible; Fable is ineligible
   with reason "rule 0".
4. A candidate at 90% 5-hour use with `near_limit_percent` 85 is ineligible with a reason naming both numbers.
5. A review kind with `exclude=[builder]` never ranks the builder eligible.
6. `estimate` uses the same-kind median, falls back to model, then default; Codex usd is None.
7. `forecast` marks an account `at_risk` when its projection crosses 85 before reset, `limited` at 100.
8. `suggest("re_review", "codex", ...)` offers a Claude re_review model first and `security_review` on Codex only
   when `security=True`.
9. `model="auto"` on `POST /api/tasks` fills a concrete model and writes one `auto_pick` decision.
10. `cleanup_candidates` returns only terminal, master-node, merged tasks.
11. `savings` uses `MAX(cost_usd)` per task (a task with runs 0.8, 1.0, 2.4 counts 2.4, not 4.2).
12. All `/api/intel/*` routes respond 200 on an empty database.
13. The full gate (`scripts/gate.sh`) passes with `tests/test_intel.py` added to it.

## 8. Test cases (`tests/test_intel.py`)

Pure tests (no FastAPI): one per acceptance criterion 1–8, 10, 11 with literal inputs. Route tests (FastAPI
`TestClient`, temp `FLEET_DB`, as `tests/test_controller.py` does): criteria 9 and 12, plus decisions list and
task decisions round trip.

## 9. Transcript and task-history extract

See `docs/research/controller-intel-evidence.md` (copied from the session notes).

## 10. Delivery

Fleet tasks under one parent: T1 tests (Sonnet, kind `test`), T2 core + routes (Sonnet, `build`), T3 hooks in
`tasks.py`, MCP tools and gate (Sonnet, `build`), round review (Haiku, differs from builder), final whole-branch
review (Codex `gpt-5.6-terra`). The orchestrator uses `estimate_task`, `model="auto"` and `/api/intel/savings` on
its own batch as the first user and reports the numbers in `docs/HANDOVER.md`.

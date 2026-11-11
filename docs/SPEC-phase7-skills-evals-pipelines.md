# Phase 7: skills, evals in the console, the build pipeline

Part of [SPEC-control-plane.md](SPEC-control-plane.md) §5, §6 and §9. Builds on Phase 6 (instruction snapshots
are the delivery channel for skills) and 5b (catalog, installs).

Tests: Phase 7 section of `tests/test_controller.py`, `tests/test_pipeline.py` (pure state machine).

## 1. Skills

A skill is a versioned, reviewable instruction bundle. Stored in the controller DB; `skills/<id>/SKILL.md` files
in the repo are imported on startup (front-matter + body), so skills can live in git too.

```
---
id: code-review
version: 3
description: Review a diff against its spec
agents: [claude, codex, ollama]
risk: prompt-only
---
<markdown body, ≤ 24 KB>
```

- `id` `^[a-z][a-z0-9-]{1,40}$`; `risk` must be `prompt-only` in this phase (`tool-aware` and `executable` are
  refused with 400 "not supported yet").
- `skills(id, version, description, agents JSON, risk, body, digest, author, created_at, PK(id, version))`; the
  newest version is current. Editing creates a version; a version is immutable.
- **Bindings** `skill_bindings(skill_id, scope, scope_id)` with the Phase 6 scopes plus `task` (task creation
  takes `skills: [id]`).
- **Delivery:** at task creation the effective instructions get, after the layer instructions, one section per
  bound skill that lists the task's agent: `## skill: <id> v<version>\n<body>`. The task records
  `skills: [{id, version, digest}]`. Nothing else changes on the runner: the Phase 6 stdin/system-message channel
  carries it.
- Import is idempotent: a file whose digest matches an existing version adds nothing; a changed file adds a
  version with `author: "repo"`.

API: `GET /api/skills` (current versions + bindings) [view] · `GET /api/skills/{id}/history` [view] ·
`PUT /api/skills/{id}` `{description, agents, risk, body}` [admin] · `POST /api/skills/{id}/bindings` and
`DELETE …/bindings` `{scope, scope_id}` [admin]. `POST /api/policies/preview` also returns `skills`.

## 2. Evals in the console

- `eval_runs(id, model, node, roles JSON, state queued|running|done|failed, started_at, ended_at, scores JSON,
  errors JSON, log_tail)`.
- `POST /api/evals` `{model, node, roles?}` [operate]: runs `evals/run_evals.py --models <model> --roles <roles>
  --ollama http://<node ip>:11434` as a subprocess on the master (master node → `127.0.0.1`), one at a time
  (others queue). The model must be installed on that node (409 otherwise). On exit, scores come from
  `results.json` (the run's own entry, matched by `evaluated_at` after start).
- `GET /api/evals` [view] → `{runs: [...], leaderboard: [<results.json rows, slimmed: model, roles{score},
  size_gb, fits_16gb, median_s, tokens_per_s, errors>]}`.
- `GET /api/evals/recommendations` [view] → per node and role the best installed (or installable and fitting)
  model by score, ties broken by smaller size, excluding rows with errors.
- The catalog's `evals` field is refreshed from `results.json` after each run.

## 3. The build pipeline (the CLAUDE.md pipeline as a template)

A pipeline runs one feature through build → gate → cross-review → resolve, as linked tasks.

```json
POST /api/pipelines
{"title": "...", "spec": "<acceptance criteria / prompt>", "repo": "~/code/x", "gate": "pytest",
 "builder": {"agent": "codex", "node": null},
 "reviewers": [{"kind": "role", "role": "reviewer"}, {"kind": "agent", "agent": "claude"}],
 "max_rounds": 3, "mode": "auto-edit"}
```

State machine (`master/controller/pipeline.py`, pure `step(pipeline, event) -> (pipeline, actions)`):

| State | Enter when | Actions |
|---|---|---|
| `building` | created, or a resolve round starts | builder task (round 1: spawn with `spec`; later rounds: `message` the same task with the merged review issues) |
| `reviewing` | builder task reaches `review` (gate passed) | one review per reviewer: `role` reviewers get a one-shot model call (LiteLLM role, the diff + spec, JSON review schema); `agent` reviewers get a `read-only` task on the builder's branch with a review prompt |
| `resolving` | all reviews in, any high/medium issue | merge issues (dedupe by file + problem text), round += 1 → `building`, unless round > `max_rounds` → `escalated` |
| `ready` | all reviews in, no high/medium issue | — (a person merges) |
| `failed` | builder task `failed`/`interrupted` and its gate failed 3 times in a row, or any task cancelled | — |
| `escalated` | rounds exhausted | needs a person (shows in the inbox) |

- Review JSON schema (same as evals): `{"verdict": "approve|request_changes", "issues": [{"severity":
  "high|medium|low", "file": "...", "problem": "...", "evidence": "exact quote from the diff", "fix": "..."}]}`.
  **An issue whose `evidence` isn't found verbatim in the diff is dropped and counted as `unsupported`** (the
  phi4 review of Phase 4 showed why). Invalid JSON = that reviewer `abstained`; a pipeline needs at least one
  valid review to leave `reviewing`, else `escalated`.
- Reviewers never share a builder: a reviewer equal to the builder agent is refused at creation (400).
- `pipelines(id, title, state, round, max_rounds, spec, repo, gate, mode, builder JSON, reviewers JSON,
  build_task, reviews JSON, history JSON, created_at, updated_at)`.
- API: `POST /api/pipelines` [operate], `GET /api/pipelines`, `GET /api/pipelines/{id}` [view],
  `POST /api/pipelines/{id}/cancel` [operate]. Events `pipeline.<state>`.
- MCP: `run_pipeline(...)` and `pipeline_status(id)`.

## Console
- **Skills page:** list (id, description, agents, version, bindings), editor with markdown preview, version
  history, bind to fleet/node/role/agent.
- **Evals page:** leaderboard (per role scores, size, speed, fits 16 GB), run an eval (model + node), runs list
  with live state, recommendations per node with "Assign" buttons.
- **Pipelines** (under Tasks, a tab): list with state and round; detail shows the builder task, each review with
  verdict, dropped unsupported issues, and the history.

## Not in this phase
Executable skills, native Claude/Codex skill folders, scheduled evals, canary-before-routing (Phase 8 decides).

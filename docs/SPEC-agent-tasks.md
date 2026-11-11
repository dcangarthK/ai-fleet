# Spec: agent tasks (phases 1-3)

Goal: the fleet runs **agent work**, not just model calls. Claude (master) delegates tasks to agent CLIs
(Claude Code, Codex) or models (Ollama) on any approved node, watches them, approves risky steps, and
reviews results in isolated git worktrees.

## Components
| Component | Where | Owner (build) |
|---|---|---|
| Runner (`nodes/runner.py`) | every node + the master | Codex |
| Controller tasks/events/scheduler/approvals/tokens/plugins | master | Claude |
| MCP delegation tools | master | Claude |
| Dashboard (attention view, inbox, tasks) | master | Claude |
| Onboarding installs runner | nodes | Claude |

## Task lifecycle (controller)
`queued` → `awaiting_approval` (supervised mode, or a mode above the default) → `working` →
one of `blocked` (agent was denied permissions and needs a user decision), `review` (agent exited 0 and gate
passed or no gate), `failed` (agent or gate failed), `interrupted` (process lost, e.g. reboot; can resume).
From `review`/`failed`/`blocked`/`interrupted`: `message` (resume same agent session with a follow-up) → `working`.
Terminal: `done` (accepted), `cancelled`. Parent/child tasks via `parent_id`.

## Permission modes (hard ceiling)
| Mode | Claude Code flags | Codex flags | Needs user approval to start |
|---|---|---|---|
| `read-only` | `--permission-mode plan` | `-s read-only` | no |
| `supervised` | default permission mode (edits denied → task `blocked` with denials) | `-s read-only` | yes |
| `auto-edit` (default) | `--permission-mode acceptEdits` | `-s workspace-write` | no |
| `full` | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` | yes, always |

Ceiling = min(node ceiling set by the user in the dashboard, runner's local `max_mode`). Requests above the
ceiling are rejected. MCP tools can request a mode but can never approve or raise a ceiling.
Approving a `blocked` task's denials resumes the session with those tools added (`--allowedTools`).

## Runner HTTP contract (stdlib-only Python 3.9+, macOS/Linux/Windows)
Listens on `RUNNER_BIND` (default `127.0.0.1:11436`; nodes use their tailnet IP). State in
`FLEET_DIR/jobs/<id>/` (default `~/.ai-fleet`). Agent processes are detached (own session/process group) and
write output to files, so they **survive runner restarts**; a wrapper records the exit code to a file.
On startup the runner reloads jobs; a job whose process is gone without an exit code is `lost`.
Local config `FLEET_DIR/runner.json`: `{"max_mode": "auto-edit"}` (created with that default).

All responses JSON. Errors: `{"detail": "..."}` with 4xx/5xx.
- `GET /health` → `{"ok": true, "version": "1", "max_mode": "...", "agents": {"claude": "/path", "codex": "/path", "ollama": "http://..."}, "running": <int>}`
  Agents found via PATH plus per-user dirs (`~/.local/bin`, `~/.claude/local`, npm/bun/cargo/volta/nvm, `/opt/homebrew/bin`, `/usr/local/bin`) and `/Applications/Codex.app/Contents/Resources/codex`; ollama via `OLLAMA_URL` (default `http://127.0.0.1:11434`) answering `/api/tags`.
- `POST /jobs` body:
  `{"id": "t-abc", "agent": "claude|codex|ollama", "prompt": "...", "mode": "read-only|supervised|auto-edit|full",
    "repo": "/abs/path or null", "worktree": true, "cwd": null, "model": null, "gate": "pytest -q" | null,
    "allowed_tools": [], "resume_session": null}`
  - Rejects mode above `max_mode` (403). Rejects unknown agent (400) or duplicate id (409).
  - If `repo` and `worktree`: `git -C repo worktree add -b fleet/<id> FLEET_DIR/worktrees/<id>`; cwd = worktree.
    Else cwd = `cwd` or `repo` or a fresh `FLEET_DIR/jobs/<id>/work`.
  - ollama agent: one chat call to `OLLAMA_URL/api/chat` with `model` (required), no tools; output saved like other agents.
  → job object.
- Job object: `{"id","agent","mode","state":"running|exited|lost|cancelled","exit_code","session_id","cwd","worktree","branch",
   "started_at","ended_at","result" (final assistant text, trimmed to 20k chars),"denials":[{"tool","input"}],
   "gate":{"cmd","state":"pending|running|passed|failed|skipped","exit_code","output_tail"} | null,"runs": <int>}`
  - `session_id`: Claude `stream-json` init/result `session_id`; Codex `--json` `thread.started.thread_id`.
  - `denials`: Claude `result.permission_denials` (tool_name, tool_input). Codex: none.
  - Gate runs (shell, in cwd, 15 min timeout) only after exit code 0; then `state` is still `exited`.
- `GET /jobs` → list of job objects. `GET /jobs/<id>` → job object.
- `GET /jobs/<id>/log?offset=N` → `{"offset": next, "text": "..."}` human-readable lines (assistant text, tool calls, errors) parsed from the agent's JSON events.
- `GET /jobs/<id>/diff` → `{"stat": "...", "diff": "..."}` (`git diff` vs the worktree base incl. untracked files; 200k cap) or 400 if no git.
- `POST /jobs/<id>/message` `{"text": "...", "allowed_tools": [], "mode": null}` → resumes the agent session
  (Claude `--resume <session_id>`, Codex `exec resume <thread_id>`), runs++ , state running. 409 if running.
- `POST /jobs/<id>/cancel` → kills the process group, state cancelled.
- `POST /jobs/<id>/cleanup` → removes the worktree (keeps the branch), 409 if running.
Security: runner never logs prompts to stdout; job dirs are 0700. Optional `RUNNER_TOKEN` env: if set, every request
needs header `X-Runner-Token`.

## Controller additions (summary)
Tables `tasks`, `events`, `tokens`, node `ceiling`. Scheduler picks a node whose runner reports the agent (and model,
for ollama), within ceiling, least running tasks, then most RAM. The master's own runner appears as node `master`.
APIs: tasks CRUD-ish, `/api/tasks/wait` (long-poll on events), `/api/events` (SSE), approvals inbox, usage.
Plugins: `master/plugins/*.json` manifests `{events, command}` get event JSON on stdin (built-in: macOS notification).
Remote access: scoped tokens (view/operate/admin) for non-localhost hosts via `tailscale serve`.

## Acceptance tests
1. Runner unit tests with fake agent binaries: spawn, log, result, session id, denials, gate pass/fail, diff,
   message/resume, cancel, survive runner restart, lost detection, mode ceiling, cleanup.
2. Controller tests with a fake runner: lifecycle transitions, approvals, scheduler choice, wait, ceiling, tokens/scopes, plugin fire.
3. Live: master runner + real Claude Code, Codex and local Ollama each complete a small coding task in a worktree of a
   scratch repo; Claude reviews via diff; a supervised task is blocked then approved; restart runner mid-task.

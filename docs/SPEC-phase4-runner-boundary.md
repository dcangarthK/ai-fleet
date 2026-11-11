# Phase 4: secure runner boundary

Part of [SPEC-control-plane.md](SPEC-control-plane.md) §9. Goal: a node decides locally what the fleet may do on
it, and a compromised master or a leaked token can't go beyond that.

Tests: `tests/test_runner_security.py` (runner) and the gate/token cases in `tests/test_controller.py`.

## Node-local config: `$FLEET_DIR/runner.json`

Read only from disk on the node. Nothing in a request can change it.

```json
{
  "max_mode": "auto-edit",
  "allowed_roots": ["~/code"],
  "denied_paths": ["~/Documents/private"],
  "gate_profiles": {
    "pytest": ["python3", "-m", "pytest", "-q"],
    "unittest": ["python3", "-m", "unittest", "-q"]
  },
  "allow_raw_gates": false,
  "max_jobs": 2,
  "max_job_minutes": 120
}
```

| Key | Default when missing | Meaning |
|---|---|---|
| `max_mode` | `auto-edit` | unchanged |
| `allowed_roots` | `[]` | A job's `repo`/`cwd` must resolve under one of these. Empty → every job that names a `repo` or `cwd` is refused (jobs with neither still run, in a scratch dir) |
| `denied_paths` | `[]` | Added to the **built-in denied list**, which can't be removed: `$FLEET_DIR`, `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gcloud`, `~/.kube`, `~/.docker`, `~/Library/Keychains` |
| `gate_profiles` | `{}` | Named gate commands, each an argv list |
| `allow_raw_gates` | `false` | Legacy: accept a gate that isn't a profile name and run it through the shell |
| `max_jobs` | `2` | Running jobs at once; more → 429 |
| `max_job_minutes` | `120` | Wall-clock limit per run (float allowed); the runner kills the job's process group, `exit_code` 124, `result` mentions the timeout |

Invalid values fall back to the safe default (same as `max_mode` today), never to "allow everything".

## Paths

`check_path(p)` is applied to `repo` (before `git worktree add`) and to `cwd`, and to the repo's git common dir
(`git rev-parse --git-common-dir`, so a `.git` file can't point into a denied place).

1. Must be absolute after `expanduser`; else 400.
2. `os.path.realpath`, which follows symlinks (and junctions on Windows); must exist and be a directory.
3. Compare with `os.path.normcase` and, on macOS and Windows (case-insensitive file systems), `casefold()`.
4. Allowed iff it is equal to or inside some realpath'd allowed root, **and** it is not equal to or inside any
   denied path, **and** no denied path is inside it (so `cwd = ~` is refused when `~/.ssh` is denied, even if `~`
   is an allowed root).
5. Refusal → **403** `"path not allowed by this node's runner policy"`. The detail doesn't echo the config.

The runner's own worktrees (`$FLEET_DIR/worktrees/<id>`) are created by the runner and aren't subject to the
check; requests can't name them because `$FLEET_DIR` is denied.

## Gates

The `gate` field in `POST /jobs` is a **profile name** (`^[a-z0-9][a-z0-9_-]{0,40}$`). Optional `gate_args`: list of
≤ 20 strings, each matching `^[A-Za-z0-9_./:=@+,-]{1,200}$`, not absolute, no `..` segment.

- Known profile → argv = profile + `gate_args`; `argv[0]` resolved with `shutil.which` on the runner's agent
  search path; run with `shell=False` in the job's cwd.
- Unknown name and `allow_raw_gates: false` → **400** `"unknown gate profile …; raw gate commands are disabled on
  this node"`.
- Unknown name and `allow_raw_gates: true` → legacy behaviour (shell string), recorded as `"raw": true`.
- Job state stores `gate: {profile, argv, raw, state, exit_code, output_tail}`; `cmd` is kept as a display
  string.
- On startup, a saved job whose gate is a raw string, while raw gates are off, has its gate marked `failed` with
  `"raw gate commands are disabled on this node"` instead of being re-run.

## Credential

- The runner reads its token from `RUNNER_TOKEN` or else `$FLEET_DIR/runner.token`. If neither exists it
  **generates** one (`secrets.token_urlsafe(32)`), writes `runner.token` with mode 0600 and logs
  "register this node's runner token on the master". It never runs without a token.
- Compare with `hmac.compare_digest`. Every route, `/health` included, requires it.

## Request hygiene

- `POST /jobs` accepts exactly: `id, agent, prompt, mode, repo, worktree, cwd, model, gate, gate_args,
  allowed_tools, resume_session`. Any other key → **400** naming it (a field the node doesn't understand is
  never silently ignored, e.g. a pushed `allowed_roots`).
- `/health` → `version: "2"` and adds `gate_profiles` (names only), `allow_raw_gates`, `allowed_roots` (as
  configured), `max_jobs`.

## Node audit log: `$FLEET_DIR/audit.jsonl` (0600)

One JSON line per refusal: `{ts, event, detail, remote}` with `event` ∈ `auth_failed`, `path_denied`,
`gate_denied`, `mode_denied`, `busy`, `bad_field`. Appended; never sent anywhere.

## Controller

- `nodes.runner_token` column (migration). `runner_call` / `runner_health` send the node's own token; fallback
  `RUNNER_TOKEN` env (transition). For the master node, also fallback to reading `~/.ai-fleet/runner.token`.
- The token is **never** returned by any API (`/api/fleet`, node detail, events).
- CLI: `controller.py runner-token set <node>` (reads the token from stdin), `runner-token clear <node>`.
- `eligible()`: if the task has a gate and the node's runner health is v2, the node must list that profile in
  `gate_profiles` or have `allow_raw_gates`; otherwise reason `"gate profile <name> not on this node"`. v1 runners
  are unchanged.
- MCP `spawn_task` doc: `gate` is a gate profile name defined on the node (e.g. `pytest`); `fleet_status` shows
  each node's profiles.

## Setup scripts

- `master/setup.sh`: if `runner.json` has no `allowed_roots`, add `[<repo dir of ai-fleet>]` and a `pytest` and
  `unittest` profile; register the master runner's token with the controller DB.
- `nodes/onboard.sh` / `onboard.ps1`: ask which folders agents may work in (repeatable `--allow-root PATH` for
  non-interactive use; no default), write default profiles, start the runner, print the token once with the
  command to register it on the master. Re-embed the runner with `nodes/build.py`.

## Not in this phase

Policy push from the master, OS-level isolation (dedicated user), network control, per-request instruction
snapshots. Those are phases 6 and later.

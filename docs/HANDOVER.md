# Current state: 2026-11-12 00:50 SAST (read this first)

**Repo:** `main` is the working branch, pushed by the owner to `github.com/dcangarthK/ai-fleet` over the SSH alias
`github-personal`. The commit email is `dcankoolbye@gmail.com`. The orchestrator can't push; the owner pushes.

**Merged and live:**
- G1–G6 (grounding).
- Task lineage.
- Paid-model budget, Parts A and B: the Budget page, admission and limits.
- Part C: account pools, limit handoff, auto-resume.
- Part D: the Live page (`/app/live`), heartbeats, failover, presence, account room.
- Laptop 2 onboarding fixes, plus `docs/LAPTOP2-JOIN.md`.
- The evals leaderboard and `think: false` fix.
- Live alerts showing only what still needs attention.

**Owner rules:** CLAUDE.md, including the "Paid-model budget" section.
- Rule 0: Fable only reviews; it never builds, fixes or tests.
- Codex keeps the owner's personal profile.
- Cheapest capable model first; use local models for drafts, summaries and commit messages.

**Accounts:**
- **Fleet Claude:** the owner's personal account, used by the runner. Its 5-hour limit resets often.
- **This orchestrator session's account ("spare"):** control plane and standby. It is not yet a registered fleet
  profile.
- **Codex:** the owner's personal profile.

The Live and Budget pages show limits. The controller queues work for a limited account automatically.

**In flight overnight (watch `~/.ai-fleet/lead-status.md` and `~/.ai-fleet/overnight-report.md`):**
- `t-939f6b2a`: Codex (medium effort) scoping networking, writing `docs/SPEC-networking.md` in its worktree. Inputs
  are `docs/research/`.
- `t-5c1fc50f`: fleet Sonnet, three Part D test follow-ups. Queued until the fleet reset.
- `t-3a9b8dbd`: the overnight lead on fleet Sonnet. It reviews the networking scope (Sonnet), builds it in phases,
  gets a Codex security review and a Fable final review, merges into main, updates this handover, and writes the
  overnight report. Queued until the fleet reset, then it starts automatically.

**If you're taking over:**
1. Read the status file.
2. `GET /api/live` and `/api/tasks?limit=20`.
3. Resume a stopped lead with `POST /api/tasks/<id>/message`, or start a new one with the same brief, which is in the
   lead task's `prompt`.
4. Send the presence headers on controller calls: `X-Fleet-Session`, `X-Fleet-Model`.

**Owner actions pending:**
- Register the spare account as a runner profile.
- Install Tailscale on the master and join Laptop 2 (`docs/LAPTOP2-JOIN.md`).
- Push `main` after overnight merges.
- Optionally apply the leftover G1 patch at `~/ai-fleet-leftover-g1-t-59b834a4.patch`.

---

# Handover: ai-fleet, 2026-11-11 10:35 SAST

For the next Claude session (any account) picking up from Claude Opus 5.5's session. Read this file first, then
`CLAUDE.md`, then the specs listed below. Everything here was true at the time above; check live state with the
commands in "Look before acting".

## 1. What this is

A secure multi-machine AI team on the owner's Macs:
- **Master (this Mac, 48 GB):** fleet controller (FastAPI + SQLite), web console at http://127.0.0.1:8700/app/,
  agent runner, Ollama, LiteLLM gateway (Docker), Claude Code + Codex CLIs.
- **Nodes:** Laptop 2 (M1 16 GB) will join over Tailscale (not yet: Tailscale isn't installed on the master).
- **Claude orchestrates; Claude, Codex and local models build and review each other's work through pipelines.**

Repo: `~/ai-fleet`, branch **`feat/agent-tasks`** (≈75 commits ahead of `main`; nothing pushed, nothing merged to
`main`; merging to `main` needs the owner's OK).

## 2. The owner's standing instructions (most recent last)

1. Follow `CLAUDE.md` (spec → tests first → build → gate → cross-review by a *different* agent → resolve → final).
2. Never approve `blocked` / `awaiting_approval` tasks yourself; the owner approves (console or notification).
3. **"Don't do anything by yourself."** The orchestrator only assigns work, reads results, gives review notes and
   restarts services. Building, fixing, *merging* and benchmark runs are fleet tasks.
4. **Main reviewers: Claude and Codex.** When Codex is rate-limited, use **Fable 5.1** (`claude-fable-5-1`) as the
   reviewer instead of waiting (owner's instruction).
5. Use the local models for drafting work with clear, self-contained instructions (they have no tools).
6. Keep the owner informed in plain language; notifications must be meaningful (no test noise).

## 3. Where the docs are

| Doc | What it holds |
|---|---|
| `CLAUDE.md` | Team, delegation workflow, pipeline rules |
| `README.md` | Setup, services, console, tasks, policies, models, skills, evals, pipelines |
| `docs/SPEC-control-plane.md` | Overall design (merged from Claude + Codex) |
| `docs/SPEC-agent-tasks.md` | Runner HTTP contract, task lifecycle (phases 1–3) |
| `docs/SPEC-phase4-runner-boundary.md` … `docs/SPEC-phase8-operations.md` | Phases 4–8 (all built and merged) |
| `docs/SPEC-grounding.md` | **Current work:** hallucination controls G1–G6 |
| `docs/grounding-peers/` | The five independent plans (Claude, Codex, glm, phi4, qwen) the grounding spec came from |
| `evals/results/LEADERBOARD.md`, `GROUNDING.md`, `results.json` | Model eval results |
| `~/.claude/projects/-Users-duncansynthesis-co-za-ai-fleet/memory/` | Session memory (e.g. local-model strengths) |

## 4. Live system on the master

| Piece | How it runs | Notes |
|---|---|---|
| Controller + console | launchd `local.ai-fleet.controller` (uv run master/controller/controller.py) | 127.0.0.1:8700 (local = admin), :8701 for `tailscale serve` (tokens). Restart: `launchctl kickstart -k gui/$(id -u)/local.ai-fleet.controller` |
| Runner | launchd `local.ai-fleet.runner` (nodes/runner.py) | 127.0.0.1:11436, config `~/.ai-fleet/runner.json` (allowed_roots = ~/ai-fleet, gate profiles incl. `fleet-gate` = `bash scripts/gate.sh`, max_jobs 4, models.remote_management true, power.min_battery_pct 0, max_mode auto-edit) |
| Ollama | `brew services` | models: qwen2.5-coder:14b, phi4:14b, glm-4.7-flash, fleet-glm-4.7-flash-latest (derived: num_ctx 32768, temp 0.1), llama3.2, qwen2.5:3b, codellama |
| LiteLLM | Docker compose in `master/litellm` | 127.0.0.1:4000; virtual key in Keychain service `litellm-laptop1` |
| Fleet Claude | runner env `CLAUDE_CONFIG_DIR=~/.claude-fleet` (logged in by the owner) | uses the Desktop app's bundled `claude`; **same account limits as the owner's** |
| Codex | `/Applications/Codex.app/Contents/Resources/codex` | sandbox: no network unless allowed; **`.git` is always read-only for Codex → Codex can't commit/merge; give merges to Claude** |

Roles: `coder` and `fast` → qwen2.5-coder:14b; `reviewer` → glm-4.7-flash (derived, 32k ctx), fallback `coder`.
Fleet policy (`fleet/*`): `auto_approve {agents: [claude, codex], denied_tools: true}` (denied tools auto-approved,
persist for the task, cap 3 consecutive). Full-mode bypass exists but is **off** (needs policy `full_mode` + node ceiling full).
Notifications: macOS plugin `master/plugins/macos-notify.py`; rules on Settings page; tests never load real plugins.

Rebuild/redeploy after merging: `bash master/setup.sh --services-only` (the full setup.sh also re-tags
Tailscale, without `--reset` unless you pass `--reset-tailscale`, and re-registers the MCP servers). Rebuild the console bundle when `ui/src` changes:
`cd master/controller/ui && npx vite build` (node_modules is a symlink to `~/ai-fleet-ui/master/controller/ui/node_modules`).

## 4a. Where the fleet runs, and how to start it

**Where:** everything runs on the master Mac (`duncansynthesis.co.za`'s MacBook, macOS, 48 GB), under the owner's
user account. Nothing runs in the cloud. Files:

| Path | What |
|---|---|
| `~/ai-fleet` | The repo; the controller and runner run **from this checkout** (merging into it changes the live code after a restart) |
| `~/ai-fleet/master/controller/` | Controller (`controller.py`, `tasks.py`, …), console source `ui/src`, built console `ui/dist` |
| `~/ai-fleet/master/controller/fleet.db` | Controller database (tasks, pipelines, policies, catalog, skills…) |
| `~/ai-fleet/master/litellm/` | LiteLLM `docker-compose.yml`, `.env` (secrets, mode 0600: never print or send it) |
| `~/ai-fleet/nodes/runner.py` | Agent runner (the master runs it directly; other nodes get it via `nodes/onboard.sh`) |
| `~/.ai-fleet/` | Runner state: `runner.json` (node policy), `runner.token` (secret), `jobs/`, `operations/`, `worktrees/<task-id>` (each task's git worktree), `audit.jsonl` |
| `~/.claude-fleet/` | The fleet's own Claude Code profile (login) |
| `~/Library/LaunchAgents/local.ai-fleet.{controller,runner}.plist` | Login services |
| `~/Library/Logs/ai-fleet-controller.log`, `ai-fleet-runner.log` | Logs |

**Start it up** (after a reboot or if anything is down), in this order:

```bash
# 1. Docker (for LiteLLM) and the gateway containers
open -a Docker            # wait until `docker info` works
cd ~/ai-fleet/master/litellm && docker compose start
curl -s http://127.0.0.1:4000/health/liveliness          # → "I'm alive!"

# 2. Ollama (login service)
brew services start ollama
curl -s http://127.0.0.1:11434/api/version

# 3. Controller + runner (login services; this also rewrites their launchd files and registers the runner token)
cd ~/ai-fleet && bash master/setup.sh --services-only
curl -s http://127.0.0.1:8700/api/me                      # → {"scope":"admin","label":"local",...}
curl -s http://127.0.0.1:8700/api/fleet | python3 -c "import json,sys; n=json.load(sys.stdin)['nodes'][0]; print(n['id'], n['runner_ok'], n['healthy'], n['runner']['agents'])"
```

Restart just one piece: `launchctl kickstart -k gui/$(id -u)/local.ai-fleet.controller` (or `.runner`).
Stop: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/local.ai-fleet.controller.plist` (same for runner);
`docker compose stop` in `master/litellm`; `brew services stop ollama`.
Console: http://127.0.0.1:8700/app/ (Overview, Tasks, Pipelines, Models, Roles, Nodes, Policies, Skills, Evals,
Activity, Settings). If `~/.cache` or `~/.docker` lose their execute bit again (seen twice), `chmod u+x` them.
Laptop power: Claude/Codex tasks are refused on battery below `power.min_battery_pct` (set to 0 on the master).

## 4b. Delegating the remaining work (copy-paste starters)

Each of these hands the next step to the fleet; read the result, then pass it on (review → fix → merge).
Replace nothing except where marked. Claude-account tasks work again after **2:30 PM SAST**; Codex after **1 PM**.

```bash
B=http://127.0.0.1:8700; H=(-H 'Content-Type: application/json' -H 'X-Fleet: 1'); R=/Users/duncansynthesis.co.za/ai-fleet

# A) Resend the fleet-fixes batch to its Claude builder (same worktree, same brief)
curl -s -X POST $B/api/tasks/t-c0998ec1/message "${H[@]}" -d '{"text":"Your previous run stopped at the account usage limit before making changes. Do the four fixes from your original brief now, then run bash scripts/gate.sh twice; both must pass."}'

# B) Review G4 (built by Claude). After 1 PM prefer Codex; otherwise Fable 5.1.
#    Build the prompt with python (brief + diff + schema) exactly as section 5 describes, then:
curl -s -X POST $B/api/tasks "${H[@]}" -d '{"agent":"codex","mode":"read-only","worktree":false,"repo":"'$R'","node":"master","title":"Review: G4","prompt":"<brief + /api/tasks/t-002eb9b6/diff + JSON schema; also tell it the full changed files are under /Users/duncansynthesis.co.za/.ai-fleet/worktrees/t-002eb9b6>"}'

# C) Restart G6 as a new pipeline from the old record's spec
python3 - <<'EOF'
import json, urllib.request
B = "http://127.0.0.1:8700"
old = json.load(urllib.request.urlopen(B + "/api/pipelines/p-f7c0f0c9"))
body = {k: old[k] for k in ("title", "spec", "repo", "gate", "mode", "max_rounds")} | {
    "builder": {"agent": "claude"}, "reviewers": [{"kind": "agent", "agent": "codex"}]}
r = urllib.request.Request(B + "/api/pipelines", data=json.dumps(body).encode(), method="POST",
                           headers={"Content-Type": "application/json", "X-Fleet": "1"})
print(json.load(urllib.request.urlopen(r))["id"])
EOF

# D) After fixes (A) are reviewed and merged: finish the G3 benchmark and commit results (Claude task)
curl -s -X POST $B/api/tasks "${H[@]}" -d '{"agent":"claude","mode":"auto-edit","worktree":false,"repo":"'$R'","node":"master","title":"Finish G3 benchmark","prompt":"Run python3 evals/run_evals.py --models glm-4.7-flash:latest --roles grounded in this repo (qwen2.5-coder:14b and phi4:14b results are already in evals/results, uncommitted). Check GROUNDING.md and LEADERBOARD.md were regenerated by code, run bash scripts/gate.sh (must pass), then git add evals/results and commit with message \"G3 benchmark: grounded eval on local models\" and the Co-Authored-By trailer. Report the table."}'

# E) Then the G2 reviewer benchmark (Claude task), same shape as D with:
#    python3 evals/run_evals.py --models qwen2.5-coder:14b,phi4:14b,glm-4.7-flash:latest,fleet-glm-4.7-flash-latest --roles reviewer_real
```

Watching work without polling by hand: run a background loop with `sleep 30` inside that reads
`/api/pipelines/<id>` or `/api/tasks/<id>` and exits when the state leaves `building/reviewing/working`.
Merges after an approval: a Claude task with the exact merge steps in section 5, then restart the controller.

## 5. How to delegate (the orchestrator's toolkit)

All via the controller API on the local port (send header `X-Fleet: 1` on writes):

```bash
# a pipeline: builder → gate (fleet-gate) → reviewers; reviewer must differ from the builder
curl -s -X POST http://127.0.0.1:8700/api/pipelines -H 'Content-Type: application/json' -H 'X-Fleet: 1' -d '{
  "title": "...", "spec": "<clear acceptance criteria>", "repo": "/Users/duncansynthesis.co.za/ai-fleet",
  "gate": "fleet-gate", "mode": "auto-edit", "max_rounds": 3,
  "builder": {"agent": "codex"}, "reviewers": [{"kind": "agent", "agent": "claude"}]}'
# a single task (Claude/Codex/ollama); "model" works for claude (e.g. claude-fable-5-1) and ollama
curl -s -X POST http://127.0.0.1:8700/api/tasks -H 'Content-Type: application/json' -H 'X-Fleet: 1' -d '{
  "agent": "claude", "model": "claude-fable-5-1", "mode": "read-only", "worktree": false,
  "repo": "/Users/duncansynthesis.co.za/ai-fleet", "node": "master", "title": "...", "prompt": "..."}'
# resume a builder with review findings (same session and worktree)
curl -s -X POST http://127.0.0.1:8700/api/tasks/<id>/message -H 'Content-Type: application/json' -H 'X-Fleet: 1' -d '{"text": "..."}'
# retry an escalated pipeline's reviews
curl -s -X POST http://127.0.0.1:8700/api/pipelines/<id>/retry -H 'X-Fleet: 1'
# read: /api/pipelines/<id>, /api/tasks/<id>, /api/tasks/<id>/diff, /api/tasks/<id>/log?offset=0, /api/fleet, /api/events/list?limit=1000
```

Patterns that work:
- **Review with Fable** when Codex is out: a read-only Claude task with `"model": "claude-fable-5-1"`, prompt =
  build brief + review history + accepted limits + the full diff (`/api/tasks/<build>/diff`) + the JSON reviewer
  schema `{"verdict": "approve|request_changes|abstain", "issues": [{"severity","file","problem","evidence","expected","actual","fix"}]}`.
  Pipelines can't yet name a reviewer model (that's fix #4 below), so Fable reviews run as separate tasks.
- **Merging:** a Claude task in `~/ai-fleet` (auto-edit, worktree false) with exact steps: `git -C <worktree> add -A`,
  commit with attribution, `git merge --no-ff fleet/<task>`, resolve keeping both sides, rebuild dist if ui/src changed,
  `bash scripts/gate.sh` must pass. Afterwards restart the controller.
- **Local models:** `{"agent": "ollama", "model": "qwen2.5-coder:14b" | "glm-4.7-flash:latest", "mode": "read-only"}` with a
  self-contained prompt and an exact output format; review their drafts before passing them to a builder
  (qwen once copied the formatting rules into the content; glm skipped requested examples).
- Wait loops: always `sleep` inside the loop (one earlier loop forgot and hammered the API).

## 6. Where the work stands

**Done and merged (and live):** Phases 4–8; G1 (one reviewer contract), trusted reviews, per-file review of big
diffs, review reasons/fallback/retry, readable notifications, trusted-agent bypass, approval persistence,
**G2** (real-diff reviewer benchmark + trusted reviewer models), **G3** (grounding calibration eval),
**G5** (fleet instruction `master/instructions/fleet.md` + skills `skills/grounded-answer`, `skills/reviewer`).

**In flight when both accounts hit limits (Claude account: resets 2:30 PM SAST; Codex: resets 1:00 PM SAST):**

| Item | State | Where the work is | Next step |
|---|---|---|---|
| **G4** grounded `ask_model` + escalation (built by Claude, pipeline `p-1cb3436a`) | build done, gate **passed**; pipeline escalated only because its reviewer was Codex (rate-limited) | worktree `~/.ai-fleet/worktrees/t-002eb9b6`, uncommitted | Review it: Fable task (or Codex after 1 PM). The Fable review `t-f166949d` died at the limit. **Its diff is >200 KB and hits the runner's 200 KB diff cap**, so tell the reviewer to read files in the worktree path too, or review in two halves (code vs dist). Then a Claude merge task. |
| **Fleet fixes batch** (task `t-c0998ec1`) | died at the limit before changing anything | `~/.ai-fleet/worktrees/t-c0998ec1` (clean) | Resend (message the task or start a new one). The four fixes: (1) GROUNDING/LEADERBOARD report tests must accept generated reports instead of the placeholder; (2) make `test_pipeline_end_to_end_resolution_ready_escalation_cancel_and_events` deterministic; (3) validate scope ids of repo-seeded instructions (Fable's low note on G5); (4) pipeline reviewers may name a model (`{"kind":"agent","agent":"claude","model":"claude-fable-5-1"}`), allowed when agent **or** model differs from the builder; console shows it. **Fix (1) must land before the benchmark results are committed**, or the gate fails. |
| **G3 benchmark** (task `t-27214ddd`) | qwen2.5-coder:14b and phi4:14b ran; glm-4.7-flash not yet | uncommitted results in `~/ai-fleet/evals/results/*` | After fix (1) is merged: a Claude task runs `python3 evals/run_evals.py --models glm-4.7-flash:latest --roles grounded`, then commits `evals/results`. So far: **qwen2.5-coder:14b 96.7, hallucination 0%, correct-abstain 100%, quotes 100%; phi4:14b 86.7, hallucination 6.7%, correct-abstain 93%, quotes 97%.** |
| **G6** console grounding views (pipeline `p-f7c0f0c9`) | build failed at the limit, no changes | `~/.ai-fleet/worktrees/t-24627a05` (clean) | Start a new G6 pipeline (spec is in the pipeline record: `GET /api/pipelines/p-f7c0f0c9`), builder Claude, reviewer Codex/Fable. |
| **G2 benchmark** | not started | — | After G6 or in parallel: Claude task runs `python3 evals/run_evals.py --models qwen2.5-coder:14b,phi4:14b,glm-4.7-flash:latest,fleet-glm-4.7-flash-latest --roles reviewer_real`, commits results. Decides which local model (if any) becomes a trusted reviewer (held-out precision ≥ 0.9, recall ≥ 0.5). |

**Then, to finish the grounding work:** final whole-branch review (Claude + Codex/Fable), summary for the owner,
owner's OK before merging `feat/agent-tasks` → `main`.

## 7. Known issues and gotchas

- **Account limits:** fleet Claude tasks share the owner's Claude account; Codex has its own limit. When a task's
  result says "You've hit your … limit", nothing is wrong with the work; resend it after the reset.
- **Codex can't write `.git`** (its sandbox) → never give Codex commits or merges.
- **Runner diff cap 200 KB** → large diffs (mostly rebuilt `ui/dist`) get truncated for reviewers.
- **Timing-sensitive tests** fail on a loaded machine (many fleet jobs at once): fix #2 above; until then, rerun the gate.
- **glm-4.7-flash** is slow on big diffs (timed out at 20 min twice); per-file chunking helps; it's untrusted until G2's benchmark.
- **Old worktrees** created before the notification fix had their old plugin silenced locally (`git update-index --skip-worktree`); don't commit `master/plugins/macos-notify.sh` from them.
- **Laptop 2 hasn't joined:** owner must install Tailscale on the master and sign in, run `bash master/setup.sh` once (full), then `nodes/onboard.sh` on Laptop 2.
- **Open owner decisions:** approval-enforcement layers against Claude (hooks / action-bound approvals / separate macOS user / Touch ID); merging to `main`.
- Untracked and not ours: `AI Fleet.md`, `.claude/` in the repo root. Leave them.

## 8. Look before acting

```bash
cd ~/ai-fleet && git log --oneline -5 && git status --short
curl -s http://127.0.0.1:8700/api/fleet | python3 -m json.tool | head -60
curl -s "http://127.0.0.1:8700/api/tasks?limit=20" | python3 -c "import json,sys; [print(t['id'], t['state'], t['agent'], t['title'][:60]) for t in json.load(sys.stdin)]"
curl -s http://127.0.0.1:8700/api/pipelines | python3 -c "import json,sys; [print(p['id'], p['state'], p['round'], p['title'][:60]) for p in json.load(sys.stdin)]"
launchctl list | grep ai-fleet; curl -s http://127.0.0.1:4000/health/liveliness; ollama list
```

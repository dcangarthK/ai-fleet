# AI fleet: setup

One **master** (Laptop 1: Claude Code, Codex, LiteLLM gateway, fleet controller) and any number of
**nodes** (machines serving models). Everything talks over Tailscale, so it works the same at home and away.

```
Claude Code ──MCP──► local-llm bridge ──► LiteLLM :4000 (roles) ──tailnet──► nodes (Ollama / vLLM / LM Studio)
     │                     └──────────► fleet controller :8700 ──tailscale status + probes──┘
     └──MCP──► Codex                        └─ dashboard http://127.0.0.1:8700
```

## 1. Tailscale (once)
1. Install Tailscale on the master and sign in.
2. Admin console → Access controls: paste `tailscale/policy.hujson`, save (its tests must pass).
3. Admin console → DNS: enable MagicDNS.

## 2. Master (Laptop 1)
1. Start Docker Desktop.
2. `./master/setup.sh`. First run stops and asks you to set `LITELLM_TAG` in `master/litellm/.env`
   to a current LiteLLM release you've checked against their security advisories. Set it, re-run.
3. Open http://127.0.0.1:8700. Restart Claude Code.

## 3. Add a node (copy one file, run it)
| OS | Copy | Run |
|---|---|---|
| macOS / Linux | `nodes/onboard.sh` | `bash onboard.sh` |
| Windows | `nodes/onboard.ps1` | elevated PowerShell: `powershell -ExecutionPolicy Bypass -File .\onboard.ps1` |

Options: `--yes` / `-Yes` (accept defaults), `--models "qwen3:8b qwen3:4b"` / `-Models qwen3:8b,qwen3:4b`,
`--scan-only` / `-ScanOnly` (rescan without changing anything).

What it does:
1. Installs Tailscale if missing and joins the tailnet as `tag:llm-node`.
2. Installs Ollama if missing (asks first) and makes it listen on the tailnet address only.
   Offers models sized to the machine's RAM/GPU if it has none.
3. **Scans for everything AI on the machine:** running model servers (Ollama, LM Studio, vLLM, llama.cpp,
   Jan, GPT4All, any OpenAI-compatible server) and their models, installed engines, GGUF and Hugging Face
   model files, LLM Docker containers, agent CLIs (Claude Code, Codex, Gemini, Qwen Code, Kimi, Aider,
   OpenCode, Goose, ...) and desktop AI apps.
4. Publishes that inventory on the tailnet (port 11435, file paths removed) and rescans hourly.
   It never reads API keys, credentials or chat history.

Within 30s the node shows in the dashboard as **pending**, with its hardware, models and agents.
Approve it, then assign roles (dashboard, or tell Claude "make laptop2's qwen3:8b the reviewer").

Servers other than Ollama that only listen on localhost are flagged "localhost-only" in the dashboard.
Bind them to the machine's Tailscale IP (`tailscale ip -4`) on port 8000, 1234 or 8080 to use them; the controller then picks them up.
Agent CLIs on nodes are listed for visibility; Claude can only call agents on the master for now.

## Agent tasks
Claude delegates work to agents across the fleet with the MCP task tools (see `CLAUDE.md`):
the controller picks a node, the node's **runner** (port 11436, installed by setup/onboard) starts the agent
CLI as that machine's user, in its own git worktree, and reports back. Agents keep running if the runner,
the controller or your connection restarts; after a reboot the task shows **interrupted** and can resume its session.

| State | Meaning |
|---|---|
| needs approval | supervised/full mode: approve in the dashboard |
| working | agent running (gate runs after it) |
| blocked | agent was denied tools; approve to grant them and resume |
| ready for review | finished and the gate passed: read the diff, then merge |
| failed / interrupted | agent or gate failed / process lost: message it to retry |

**Permission modes:** `read-only` < `supervised` < `auto-edit` (default) < `full`. Each node has a ceiling
(dashboard) and the node owner caps it again in `~/.ai-fleet/runner.json` (`max_mode`). The stricter one wins.

### Runner policy and credentials

Each node owns its policy in `$FLEET_DIR/runner.json` (normally `~/.ai-fleet/runner.json`). `allowed_roots`
lists the only folders tasks may use; `denied_paths` adds local exclusions; `gate_profiles` maps safe names such
as `pytest` to argv lists. Tasks request the profile name, not a shell command. The same file controls
`allow_raw_gates`, `max_jobs`, `max_job_minutes`, and `max_mode`; restart the runner after editing it.

The runner creates a private `runner.token` on first start. Register the token once on the master without putting
it in argv: `uv run --script master/controller/controller.py runner-token set <node>` reads it from stdin.
Use `runner-token clear <node>` to remove a stored node credential. The onboarding scripts print the full
registration command once.

### Policies

The Policies page stores versioned instructions and restrictions at fleet, node, role, and agent scope. New
tasks merge those layers with an optional task policy, snapshot the effective instructions/policy/hash, and keep
that snapshot for follow-up messages and resumes. Allowed folders and Claude tools intersect; denials and quiet
hours accumulate; mode, time, concurrency, and daily budgets take the strictest value. The node runner applies
the snapshot only as a further restriction—its local `runner.json` remains the hard ceiling for paths, mode,
network, job time, and gates. Policy/instruction edits require an `admin` token.

### Skills

Skills are versioned, prompt-only instruction bundles managed on the Skills page. Bind them to the fleet, a
node, role or agent, or list skill IDs when creating a task. Each task snapshots the selected versions and
digests and receives their Markdown through the same instruction channel as policy text; skills never widen
runner permissions. Git-managed skills in `skills/<id>/SKILL.md` are imported idempotently when the controller
starts.

### Evals

The Evals page queues local model evaluations one at a time, shows role scores and speed from
`evals/results/results.json`, and suggests the best evaluated model that is installed or fits each node. A run
is accepted only for a model already installed on its selected node. The controller invokes `evals/run_evals.py`
on the master and refreshes the model catalog scores when the run finishes.

### Build pipelines

Pipelines link a builder task, its gate, and independent role or agent reviews. Review claims must quote text
that exists in the builder diff; unsupported claims are dropped. High and medium issues return to the same
builder session for another round, while clean reviews become ready for a person to merge. Exhausted rounds
are escalated. Pipelines can be started and inspected from the console or with the MCP `run_pipeline` and
`pipeline_status` tools.

**Dashboard:** http://127.0.0.1:8700 shows what needs you, tasks by state with live activity, diffs, nodes and usage.
Updates are pushed live. macOS notifications fire when a task needs you or finishes (`master/plugins/`).

**Plugins:** drop `master/plugins/<name>.json` with `{"events": ["task.review", ...], "command": [...]}`; the
command gets the event as JSON on stdin. Events: `task.<state>`, `task.approved`, `task.unreachable`, `node.ceiling`.

**From your phone:** the controller also listens on `127.0.0.1:8701`, where every request needs a token.
On the master run `tailscale serve --bg --https=8443 http://127.0.0.1:8701` (never point it at 8700, which
trusts all local callers), set `FLEET_PUBLIC_HOST=<master>.<tailnet>.ts.net` for nicer sign-in links, then
`uv run --script master/controller/controller.py token create admin phone` and open the printed link on the phone.
Scopes: `view` (read), `operate` (start/steer/cancel tasks), `admin` (approvals, nodes, roles, ceilings).
Revoke with `token list` / `token revoke <id>`.

**Editing the runner:** change `nodes/runner.py`, then `python3 nodes/build.py` to re-embed it into the onboard scripts.

## Models

The controller keeps an Ollama model catalog and can request background installs or removals on nodes whose
local `runner.json` sets `models.remote_management` to `true` (it is off by default). The runner checks disk and
RAM reserves, reports durable progress, configures optional per-model parameters, smoke-tests the model, and only
then activates waiting role assignments. Models that existed before fleet management are marked `pre-existing`
and are never removed without explicit confirmation.

## Model picks by memory
| Node memory | coder | reviewer | fast |
|---|---|---|---|
| 16GB Mac (Laptop 2) | qwen2.5-coder:14b | qwen3:8b | qwen3:4b |
| 32GB Mac | qwen3-coder:30b | qwen3:14b | qwen3:4b |
| 24GB+ GPU PC | qwen3-coder:30b | gpt-oss:20b | qwen3:8b |

## How it behaves
- Several nodes on one role = LiteLLM load-balances between them.
- A node that misses 3 checks (~90s) leaves routing; it returns automatically when it answers again.
- **Drain** takes a node out of routing but keeps its role assignments.
- Nodes can't reach each other or the master; only the master can reach node model, inventory and runner ports.
- The controller and dashboard listen on 127.0.0.1 only and have no login, so anyone with access to the master laptop can use them.

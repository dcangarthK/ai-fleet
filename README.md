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
- Nodes can't reach each other or the master; only the master can reach node model ports.
- The controller and dashboard listen on 127.0.0.1 only and have no login, so anyone with access to the master laptop can use them.

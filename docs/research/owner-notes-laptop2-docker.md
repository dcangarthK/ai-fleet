# Owner notes: Laptop 2 and the networking goal (2026-11-12)

## Goal
Networking that makes it easy to connect other nodes to the AI fleet and run AI models on them, with security inside
the network: a firewall-like design.

Target nodes:
- **Laptop 2:** MacBook Pro M1, 16 GB.
- **A Windows machine.**

Laptop 1 (the master, where ai-fleet runs) is the controller.

## Laptop 2 today: Docker Model Runner (Docker Desktop)
Docker Desktop's Model Runner serves an OpenAI-compatible API, by default on host port 12434 once host-side TCP is
enabled in Settings. Models on Laptop 2:

| Model | Status | Approx. size | Candidate role |
|---|---|---|---|
| `ai/gemma3:4B-Q4_0` | runs (`docker model run ai/gemma3:4B-Q4_0`) | ~2.8 GB | fast triage, summaries |
| `ai/qwen3:latest` | downloaded, not yet run | ~5.2 GB | general / structured JSON |
| `ai/deepseek-r1-distill-llama` | tested, works | ~5 GB | local reasoning, first-pass logic review |
| `ai/deepcoder-preview` | downloading; intended as Laptop 2's main fleet model | ~9 GB | local coding |

These are candidates only. Fleet roles are assigned from benchmarks (evals/), not from this table.

## Ideas from outside research (unvetted; check against existing decisions)
- **Router layer:** a static DHCP lease and local DNS name for Laptop 2, and a router rule blocking Laptop 2 from
  the WAN while allowing LAN traffic to Laptop 1.
- **Model Runner exposure:** enable host-side TCP for Model Runner on port 12434 and reach it from Laptop 1.
- **Failover:** short connect timeouts, and fall back to Laptop 1's models when Laptop 2 is asleep.
- **Already decided, and conflicting with some of these suggestions:**
  - the fleet uses Tailscale with tags, ACLs and enrolment, not raw LAN exposure;
  - agents run as CLIs on subscription accounts, with no API SDKs or keys;
  - Part D's heartbeats, liveness and failover already exist;
  - the runner exposes only its own token-checked API.

# Brief: where do hallucination controls fit in our AI fleet?

## Our fleet (facts)
- Master laptop (48 GB) runs a controller (FastAPI), the Claude Code and Codex CLIs (subscriptions, no API keys), Ollama, and a LiteLLM gateway that exposes roles (coder, reviewer, fast) as model names.
- Nodes (a 16 GB M1 laptop, maybe Windows/Linux later) run Ollama and a runner that executes agent tasks in git worktrees.
- Already built: tasks for Claude/Codex/Ollama with permission modes; gate profiles (tests must pass); a model catalog with per-model params (temperature, context, thinking on/off); layered instructions and policy (fleet/node/role/agent/task) delivered to agents; versioned skills (instruction bundles) bound to roles; an eval harness that scores local models on coder/reviewer/fast cases (sandboxed code tests, planted-bug reviews with strict JSON); a build pipeline: builder agent -> gate -> cross-review by different models -> issues back to builder; reviews must quote the diff ("evidence") and issues whose quote isn't in the diff are dropped.
- An MCP bridge gives the orchestrator tools: ask_model(role, prompt), review_diff(diff), spawn_task(agent, prompt, ...).
- Measured: Ollama 0.17.7 returns token logprobs. In "Paris." the answer token had p=0.998, the "." p=0.59.
- Measured: a local 14B reviewer, on real diffs, produced mostly false positives; requiring exact quotes removed fabricated quotes but not wrong claims (one issue's "fix" was identical to the existing code).

## The research the owner collected (summary)
1. Prompt guardrails: explicit permission to say "I don't know"; quote-first extraction (claims must cite verbatim quotes); epistemic tags ESTABLISHED / INFERRED / UNKNOWN; temperature 0.0-0.3 for factual/coding/routing.
2. Confidence signals: token logprobs (average token probability, reject below 0.80); self-consistency (3-5 samples at temperature 0.5, disagreement => escalate); self-reported confidence in JSON (answer only if >= 0.85, else ABSTAIN).
3. Architecture: LLM-as-judge (one model audits another); deterministic validation (JSON schemas, linters, unit tests in isolated containers) instead of trusting the model; clean, concise tool outputs (no raw UUIDs/HTML) to reduce hallucinated tool arguments; confidence-based escalation from local models to Claude/Codex.
4. Proposed artifacts: a JSON-only system prompt for all models (status ANSWER|ABSTAIN, confidence_score, evidence_quotes, reasoning_trace, final_response, escalation_reason); a Python router that calls Ollama, averages logprobs, checks self-confidence, runs generated Python with subprocess + asserts, and escalates to the Anthropic/OpenAI APIs with API keys; a FastAPI router service on the LAN (port 8000, macvlan container, firewall-blocked WAN); a Continue IDE config.

## Your task
As an expert peer (not a reviewer), write your own plan (max ~600 words, Markdown):
1. For each research item, say where it belongs in OUR fleet (instructions/skills, model params, eval harness, MCP ask_model, pipeline reviews, gates, policy, console) or why it should not be adopted as-is.
2. Point out any claim in the research you think is wrong, risky or unverified for our setup, and why.
3. Propose the 3-5 most valuable things to build first, each with how we'd know it works (a measurable check).
Be concrete. If you are not sure about something, say "unverified" instead of guessing.

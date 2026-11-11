# Grounding and hallucination controls: where they fit in ai-fleet (Claude's independent draft)

## What we measured on this machine (facts, not claims from the research)
- Ollama 0.17.7 returns token logprobs natively (`"logprobs": true, "top_logprobs": n` on /api/chat) and on /v1/chat/completions.
- In a one-word answer ("Paris."), the answer token had p=0.9985 and the trailing "." p=0.59. An average over all
  tokens (the pasted router's `calculate_token_confidence`) is dominated by formatting/JSON tokens. Score only the
  answer's content tokens.
- Local reviewer behaviour on real diffs (phi4:14b, 3 reviews of our own phases):
  - Phase 4: 5 issues, none quoted; 4 described code that already did the thing; 1 partly valid.
  - Phase 5b (with "evidence must be an exact line"): 1 issue, quote real, claim false (its fix = the existing code).
  - Phase 6: 5 issues; 2 dropped by the evidence check (quote not in diff); 3 kept were false positives.
  → Evidence checking removes fabricated quotes, not wrong reasoning. Need a second deterministic check and a
    calibrated threshold per model.

## Mapping each guideline to the fleet

| Guideline (from the research) | Where it lands | Phase/part | Status |
|---|---|---|---|
| Permission to abstain, "I don't know" | Fleet-wide instruction layer (Phase 6) + a `grounded-answer` skill (Phase 7) | instructions | new content |
| Quote-first extraction | Skill for analysis/review tasks; enforced deterministically for one-shot answers and reviews | skill + checker | checker exists for pipeline reviews |
| Epistemic tags ESTABLISHED/INFERRED/UNKNOWN | Skill text; parsed and counted in `ask_model(grounded=True)` replies | skill + MCP | new |
| Temperature 0–0.3 | Catalog defaults / per-assignment params (Phase 5b) | params | exists; set defaults |
| Token logprobs | `ask_model(grounded=True)`: score = mean/min p over tokens of `final_response` only | MCP bridge / gateway | verified available |
| Self-consistency (3–5 samples) | Only for short outputs (fast role: labels, yes/no, one-line); off for code | MCP option | new |
| Self-reported confidence ≥0.85 | Record it, but don't trust it alone: calibrate per model with an eval | eval + router | new |
| LLM-as-judge / cross-model audit | Pipeline cross-review (Phase 7) with evidence check | pipeline | built (spec+tests) |
| Deterministic validation (schemas, linters, tests) | Gate profiles (Phase 4), eval sandbox, Ollama `format` JSON schema | gates | exists; add JSON schema in ask_model |
| Clean tool outputs | Audit our MCP tool responses (task_log, fleet_status…) for noise; trim IDs/blobs | MCP server | review task |
| Confidence-based escalation | Role fallbacks (Phase 8) + escalate to a Codex/Claude *task* when local abstains/fails | router in MCP bridge | new |
| Offline mode | Policy `network.agents` (Phase 6) + escalation disabled flag | policy | exists |

## What NOT to adopt from the pasted code
- Separate `fleet_router.py` FastAPI service on LAN port 8000 with no auth, macvlan/OpenWrt container: conflicts with
  the fleet's model (Tailscale + scoped tokens + runner boundary). Put the router logic in the MCP bridge / controller
  where auth, policy and audit already exist.
- `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` escalation: the fleet uses the Claude Code and Codex CLIs (subscriptions), and
  CLAUDE.md forbids sending keys to models. Escalation = spawn a Codex/Claude task (read-only for answers).
- Forcing every model to answer in one JSON schema: right for one-shot model calls and reviews, wrong for coding
  agents (Claude/Codex tasks), which must keep their native tool use.
- Running generated code with plain `subprocess` (router v2's `verify_python_code`): use the eval sandbox
  (`sandbox-exec`/Docker, rlimits, process-group kill, nonce result line) we already have.
- Bugs in the pasted v2: `choice = response.choices` (list, not `[0]`), `message.content.text` (Anthropic content is a
  list), averaging logprobs over JSON structure, thresholds 0.80/0.85 picked without data.
- Model names (`gemma4:12b`, `qwen3.5:9b`, `gpt-5.3-codex`, `claude-sonnet-4-6`) are UNVERIFIED for this setup; the
  catalog validates real names against the registry.

## Proposed build (as fleet pipelines)
1. **Calibration eval** (`evals/cases/grounding.json`, new role `grounded`): ~20 cases, each with context + question;
   half answerable from the context, half not (the answer isn't in it). Scores: correct answers, correct abstentions,
   hallucinations (answered when it should abstain or answered wrong), quote validity, tag use. Also record logprob
   score and self-reported confidence per case → per-model ROC → threshold that keeps hallucination rate ≤ 5%.
   *Builder:* local coder model drafts cases (fleet test), Codex reviews; Claude curates.
2. **`grounding.py`** (pure, shared by MCP and controller): response schema, `check_quotes(response, context)`,
   `content_logprob_score(logprobs, response_text)`, `consistency(samples)`, `decide(scores, thresholds) ->
   answer|escalate|abstain`. *Builder:* Codex; reviewers: local reviewer + Claude.
3. **`ask_model(role, prompt, context, grounded=True)`** in the MCP bridge: Ollama `format` JSON schema, temperature
   from params, logprobs on, optional `samples=3` for short outputs, quote check, decide; on escalate → role fallback,
   then (if allowed by policy) a read-only Codex task with the failure reason; records call stats (Phase 8) with
   `outcome`. *Builder:* Codex.
4. **Fleet instructions + skill**: fleet-wide instruction text (short, agent-appropriate) and `grounded-answer` skill
   bound to reviewer/fast roles. *Builder:* Claude drafts, local fast model condenses, Codex reviews.
5. **Review hardening** in the pipeline: besides the quote check, drop issues whose `fix` equals the quoted evidence
   and require `expected` vs `actual`. *Builder:* local coder model (small, well-scoped), Codex reviews.
6. **Console**: Evals page shows the grounding role and hallucination rate; Overview call stats show escalations.

## Open questions for the peers
- Is logprob scoring meaningful through LiteLLM (does it pass logprobs through)? Must test.
- Thinking models (qwen3): logprobs over thinking tokens must be excluded.
- Self-consistency cost on the 16 GB node: limit to N=3 and ≤ 64 output tokens.

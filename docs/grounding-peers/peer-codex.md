# Plan

The fleet should treat hallucination control as a layered quality system—not a new router service. Extend the existing instructions → evals → MCP → pipeline → gates path.

## 1. Where the research belongs

| Research item | Placement in this fleet |
|---|---|
| “I don’t know”, epistemic tags | Versioned role skills/instructions. Use `ESTABLISHED / INFERRED / UNKNOWN` for research and synthesis roles, and `ABSTAIN` for reviewers. Avoid imposing it on ordinary coder output. |
| Quote-first extraction | Reviewer skill, `review_diff`, pipeline review schema, and reviewer evals. An exact quote proves location, not correctness, so retain semantic validation afterward. |
| Temperature 0–0.3 | Per-role/model parameters. `_chat()` currently hardcodes `0.2`, while evals use `0`; make this purpose-specific and record it with eval results. No universal value. |
| Token logprobs | Eval harness and telemetry first. Collect sequence/token statistics and measure whether they predict errors per model, role and response type. Do not gate production yet. |
| Self-consistency | Optional eval experiment for ambiguous reviewer/fast cases. Three to five local generations are expensive and may reproduce the same misconception; only deploy if measured risk/coverage improves. |
| Self-reported confidence | Structured output telemetry, not trusted evidence. Calibrate it against labelled cases before using a threshold. |
| LLM-as-judge | Existing cross-model pipeline reviews, as a second opinion—not a release gate by itself. Correlated model errors remain possible. |
| Deterministic validation | Existing gates, schemas, sandboxed coder tests and evidence filtering. This remains the primary authority. Add focused validators where possible, such as detecting a proposed “fix” identical to existing code. |
| Clean tool outputs | MCP schemas and controller adapters. Return compact, typed fields and redact irrelevant noise, but retain required opaque IDs. |
| Confidence escalation | `ask_model` plus controller task orchestration and console. A local model should return a structured abstention; the orchestrator can continue itself or spawn a read-only Claude/Codex task. |
| Universal JSON system prompt | Do not adopt as-is. Use role-specific schemas. `reasoning_trace` is unverifiable, adds tokens, and should be replaced by a short `rationale` or `verification_needed` field. |
| Proposed Python/API-key router | Do not adopt. It duplicates LiteLLM/controller routing, bypasses fleet policy/gates, and assumes API keys that this subscription-based fleet does not have. Never run generated Python through a plain subprocess; reuse the existing sandbox/gates. |
| LAN FastAPI/macvlan service | Redundant with the controller and LiteLLM. macvlan security and operational value here are unverified. |
| Continue config | IDE convenience outside hallucination control; only add if Continue becomes a chosen fleet client. |

The `0.80` average-token-probability rule is unsupported. Token probabilities are not correctness probabilities, are uncalibrated across models/tokenizers, and are distorted by punctuation and answer length—the measured `Paris.` punctuation result demonstrates this. Likewise, `confidence >= 0.85` and majority agreement are unverified thresholds, not guarantees.

There is also a contract mismatch: pipeline specifications require `evidence`, but the current [MCP review schema](/Users/duncansynthesis.co.za/ai-fleet/master/mcp/server.py:82) and [reviewer eval prompt](/Users/duncansynthesis.co.za/ai-fleet/evals/run_evals.py:38) omit it. Thus eval scores do not currently test the production evidence contract.

## 2. Highest-value builds

1. **Unify the reviewer contract.** Add `evidence`, abstention, strict schema validation and identical-fix regression cases to MCP, pipeline and evals.  
   **Works when:** every accepted issue quotes the diff; malformed/unsupported/identical-fix issues are rejected; planted-bug recall does not regress.

2. **Build a real-diff reviewer benchmark.** Add human-labelled historical diffs, especially clean diffs and plausible-but-wrong claims. Report high/medium precision, recall and false-positive rate separately.  
   **Works when:** a promoted reviewer reaches an agreed precision threshold—suggested ≥90%—on a held-out real-diff set, not merely planted bugs.

3. **Calibrate confidence signals offline.** Record logprob summaries, self-confidence and three-sample disagreement alongside model digest, parameters and outcomes. Produce risk-versus-coverage curves.  
   **Works when:** a predeclared rule materially reduces held-out error at useful coverage; otherwise discard that signal.

4. **Add role-specific abstention and escalation.** `ask_model` should validate its role schema and return `ANSWER|ABSTAIN` plus a concise reason. Policy decides whether the caller handles it or spawns Claude/Codex.  
   **Works when:** end-to-end eval error falls without silent failures, and the console reports abstention/escalation rate, latency and final outcome.

Checkout caveat: this branch contains Phase 7 specs, tests and console surfaces, but I could not find the tracked pipeline/skills/evals backend modules described by them. Whether deployed code differs is **unverified**; reconcile that before implementation.
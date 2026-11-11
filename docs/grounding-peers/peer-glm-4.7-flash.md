<!-- glm-4.7-flash:latest, 126s -->
### 1. Mapping Research to Fleet Components

*   **Prompt Guardrails (Explicit "I don't know", quote-first, epistemic tags):** These belong in **Instructions/Skills**. The existing "layered instructions" and "versioned skills" are the ideal place to bake these constraints into the agent's system prompt.
*   **Confidence Signals (Logprobs, self-consistency):** These fit into **MCP ask_model** (as required output fields) and **Eval Harness** (as new metrics for scoring the 14B reviewer).
*   **Architecture (LLM-as-judge, deterministic validation):** The "LLM-as-judge" is already implemented in your **Pipeline Reviews** (cross-review). The "deterministic validation" (JSON schemas, linters) fits best into **Gates**.
*   **Proposed Artifacts (JSON-only prompt, Python router):** The JSON-only prompt is a new **Skill Bundle**. The Python router logic should be integrated into the **MCP ask_model** implementation to handle the averaging and escalation.

### 2. Critical Assessment

*   **Risk:** **Temperature 0.0.** While 0.0 maximizes factual consistency, it often causes "mode collapse" or rigidity in reasoning tasks. I recommend a range of 0.1–0.3 for the "coder" role to allow for necessary reasoning flexibility.
*   **Unverified:** **The 0.80 Logprob Threshold.** Your measurement shows the token "." had a probability of 0.59. If the threshold is 0.80, the model would likely refuse to output punctuation or the metric is not calibrated for the specific model's output distribution. This needs empirical tuning.
*   **Risky:** **Self-reported confidence (0.85).** The research notes the 14B reviewer produced false positives. Relying on the model's *self-reported* confidence score to trigger escalation is dangerous; the model can be confidently wrong. You should use the *average token logprobs* as the primary trigger, not the self-report.

### 3. Proposed First Builds

1.  **JSON Output Schema (Instructions/Skills):** Implement the proposed JSON-only system prompt for the `ask_model` tool. It must return `status` (ANSWER/ABSTAIN), `confidence_score`, and `evidence_quotes`.
    *   *Check:* The router successfully parses the JSON and halts execution when `status` is `ABSTAIN`.
2.  **Quote-First Validator (Pipeline/Gates):** Enforce the "evidence" requirement in the review pipeline. If a review lacks a verbatim quote from the diff, the gate rejects it.
    *   *Check:* The build pipeline successfully drops reviews that fail the quote extraction test.
3.  **Escalation Router (MCP/Policy):** Build the Python logic that averages logprobs and triggers the Anthropic/OpenAI APIs when the local model's confidence drops below the calibrated threshold.
    *   *Check:* The router successfully escalates to Claude/Codex on the "Paris" test case and on the 14B reviewer's false positives.
4.  **Logprob Calibration (Eval Harness):** Run the eval harness on the 14B model with the new JSON schema to find the actual probability threshold that minimizes false positives without rejecting valid code.
    *   *Check:* The calibrated threshold reduces false positives by >50% compared to the current "strict JSON" approach.
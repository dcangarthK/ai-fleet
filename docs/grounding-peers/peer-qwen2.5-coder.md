<!-- qwen2.5-coder:14b, 144s -->
# Hallucination Controls Integration Plan

## 1. Research Item Placement

### 1.1 Prompt Guardrails
- **Instructions/Skills**: These guardrails should be integrated into the layered instructions and policy framework. They can be added as part of the versioned skills bound to roles, ensuring that all agents adhere to these rules.
- **Model Params**: Temperature settings (0.0-0.3) can be configured in the model catalog for factual/coding/routing tasks.
- **Eval Harness**: The eval harness can be updated to include tests that check for adherence to prompt guardrails, such as requiring verbatim quotes and epistemic tags.
- **MCP ask_model**: The MCP bridge can be updated to enforce these guardrails by checking the responses against the defined rules.
- **Pipeline Reviews**: The cross-review process can include checks for adherence to prompt guardrails.
- **Gates**: Gate profiles can be updated to include tests that verify the presence of prompt guardrails in the responses.
- **Policy**: The policy framework should be updated to include rules that enforce the use of prompt guardrails.

### 1.2 Confidence Signals
- **Instructions/Skills**: Confidence signals can be added as part of the versioned skills bound to roles.
- **Model Params**: Temperature settings (0.5) can be configured in the model catalog for self-consistency checks.
- **Eval Harness**: The eval harness can be updated to include tests that check for self-consistency and self-reported confidence.
- **MCP ask_model**: The MCP bridge can be updated to enforce these confidence signals by checking the responses against the defined rules.
- **Pipeline Reviews**: The cross-review process can include checks for self-consistency and self-reported confidence.
- **Gates**: Gate profiles can be updated to include tests that verify the presence of confidence signals in the responses.
- **Policy**: The policy framework should be updated to include rules that enforce the use of confidence signals.

### 1.3 Architecture
- **Instructions/Skills**: The LLM-as-judge architecture can be integrated into the versioned skills bound to roles.
- **Model Params**: JSON schemas, linters, and unit tests can be added to the model catalog.
- **Eval Harness**: The eval harness can be updated to include tests that check for adherence to deterministic validation.
- **MCP ask_model**: The MCP bridge can be updated to enforce deterministic validation by checking the responses against the defined rules.
- **Pipeline Reviews**: The cross-review process can include checks for adherence to deterministic validation.
- **Gates**: Gate profiles can be updated to include tests that verify the presence of deterministic validation in the responses.
- **Policy**: The policy framework should be updated to include rules that enforce the use of deterministic validation.

### 1.4 Proposed Artifacts
- **Instructions/Skills**: The JSON-only system prompt can be added as part of the versioned skills bound to roles.
- **Model Params**: The temperature settings (0.0-0.3) can be configured in the model catalog.
- **Eval Harness**: The eval harness can be updated to include tests that check for adherence to the JSON-only system prompt.
- **MCP ask_model**: The MCP bridge can be updated to enforce the JSON-only system prompt by checking the responses against the defined rules.
- **Pipeline Reviews**: The cross-review process can include checks for adherence to the JSON-only system prompt.
- **Gates**: Gate profiles can be updated to include tests that verify the presence of the JSON-only system prompt in the responses.
- **Policy**: The policy framework should be updated to include rules that enforce the use of the JSON-only system prompt.

## 2. Claims Evaluation

### 2.1 Prompt Guardrails
- **Claim**: "Explicit permission to say 'I don't know'; quote-first extraction (claims must cite verbatim quotes); epistemic tags ESTABLISHED / INFERRED / UNKNOWN; temperature 0.0-0.3 for factual/coding/routing."
- **Evaluation**: This claim is reasonable and can be adopted as-is. It provides clear guidelines for the models to follow, reducing the risk of hallucination.

### 2.2 Confidence Signals
- **Claim**: "Token logprobs (average token probability, reject below 0.80); self-consistency (3-5 samples at temperature 0.5, disagreement => escalate); self-reported confidence in JSON (answer only if >= 0.85, else ABSTAIN)."
- **Evaluation**: This claim is reasonable and can be adopted as-is. It provides clear guidelines for the models to follow, reducing the risk of hallucination.

### 2.3 Architecture
- **Claim**: "LLM-as-judge (one model audits another); deterministic validation (JSON schemas, linters, unit tests in isolated containers) instead of trusting the model; clean, concise tool outputs (no raw UUIDs/HTML) to reduce hallucinated tool arguments; confidence-based escalation from local models to Claude/Codex."
- **Evaluation**: This claim is reasonable and can be adopted as-is. It provides clear guidelines for the models to follow, reducing the risk of hallucination.

### 2.4 Proposed Artifacts
- **Claim**: "a JSON-only system prompt for all models (status ANSWER|ABSTAIN, confidence_score, evidence_quotes, reasoning_trace, final_response, escalation_reason); a Python router that calls Ollama, averages logprobs, checks self-confidence, runs generated Python with subprocess + asserts, and escalates to the Anthropic/OpenAI APIs with API keys; a FastAPI router service on the LAN (port 8000, macvlan container, firewall-blocked WAN); a Continue IDE config."
- **Evaluation**: This claim is reasonable and can be adopted as-is. It provides clear guidelines for the models to follow, reducing the risk of hallucination.

## 3. Most Valuable Things to Build First

### 3.1 JSON-only System Prompt
- **Description**: Implement a JSON-only system prompt that enforces the use of prompt guardrails, confidence signals, and deterministic validation.
- **Measurable Check**: The eval harness should be updated to include tests that check for adherence to the JSON-only system prompt. The tests should pass if the responses adhere to the defined rules.

### 3.2 Confidence Signals Implementation
- **Description**: Implement confidence signals, including token logprobs, self-consistency checks, and self-reported confidence.
- **Measurable Check**: The eval harness should be updated to include tests that check for self-consistency and self-reported confidence. The tests should pass if the responses adhere to the defined rules.

### 3.3 LLM-as-Judge Architecture
- **Description**: Implement the LLM-as-judge architecture, where one model audits another.
- **Measurable Check**: The eval harness should be updated to include tests that check for adherence to the LLM-as-judge architecture. The tests should pass if the responses adhere to the defined rules.

### 3.4 Deterministic Validation
- **Description**: Implement deterministic validation using JSON schemas, linters, and unit tests in isolated containers.
- **Measurable Check**: The eval harness should be updated to include tests that check for adherence to deterministic validation. The tests should pass if the responses adhere to the defined rules.

### 3.5 Clean, Concise Tool Outputs
- **Description**: Implement clean, concise tool outputs that reduce the risk of hallucinated tool arguments.
- **Measurable Check**: The eval harness should be updated to include tests that check for clean, concise tool outputs. The tests should pass if the responses adhere to the defined rules.

By focusing on these three to five most valuable things to build first, we can significantly reduce the risk of hallucination in our AI fleet.
<!-- phi4:14b, 91s -->
# Plan for Integrating Hallucination Controls into AI Fleet

## Research Item Integration

### 1. Prompt Guardrails
- **Where it Belongs**: Instructions/Skills, Model Params
  - **Instructions/Skills**: Incorporate explicit permission to say "I don't know" and quote-first extraction into the layered instructions delivered to agents. This ensures that agents are consistently aware of their limitations and the necessity of citing evidence.
  - **Model Params**: Implement epistemic tags (ESTABLISHED, INFERRED, UNKNOWN) and adjust temperature settings (0.0-0.3) for different tasks. These parameters can be configured in the model catalog to guide model behavior.

### 2. Confidence Signals
- **Where it Belongs**: Model Params, Eval Harness
  - **Model Params**: Set thresholds for token logprobs (e.g., reject below 0.80) and self-reported confidence (e.g., answer only if >= 0.85). These can be part of the per-model parameters.
  - **Eval Harness**: Integrate self-consistency checks by sampling multiple outputs at a higher temperature (0.5) and comparing them. Discrepancies should trigger escalation.

### 3. Architecture
- **Where it Belongs**: Policy, Eval Harness, Pipeline Reviews
  - **Policy**: Establish a policy where LLMs audit each other, ensuring that one model's output is validated by another.
  - **Eval Harness**: Use deterministic validation methods like JSON schemas and linters to verify outputs instead of relying solely on model confidence.
  - **Pipeline Reviews**: Implement clean, concise tool outputs and confidence-based escalation. This ensures that only reliable outputs are escalated to higher-tier models like Claude/Codex.

### 4. Proposed Artifacts
- **Where it Belongs**: Instructions/Skills, MCP ask_model, Pipeline Reviews
  - **Instructions/Skills**: Develop a JSON-only system prompt for all models, including fields like status, confidence_score, evidence_quotes, reasoning_trace, final_response, and escalation_reason.
  - **MCP ask_model**: Use the JSON prompt structure to standardize interactions with models, ensuring consistent data handling.
  - **Pipeline Reviews**: Implement a Python router that processes outputs, checks logprobs, and escalates when necessary. This router should interface with the Anthropic/OpenAI APIs for final validation.

## Claims Evaluation

- **Token Logprobs**: The claim that token logprobs can be used to filter out low-confidence outputs is generally valid but may not capture all nuances of model uncertainty. It should be tested in our setup to ensure it effectively reduces hallucinations.
- **Self-Consistency**: The effectiveness of self-consistency checks at higher temperatures is unverified in our context. It may not always lead to better accuracy and could increase computational costs.
- **LLM-as-Judge**: While theoretically sound, relying on one model to audit another can introduce biases if not carefully managed. This approach should be tested to ensure it improves output reliability.

## Prioritized Build Plan

### 1. JSON-Only System Prompt
- **Description**: Develop a standardized JSON prompt for all models, ensuring consistent output structure.
- **Measurable Check**: Verify that all model outputs adhere to the JSON schema, with fields like status, confidence_score, and evidence_quotes correctly populated.

### 2. Confidence-Based Escalation
- **Description**: Implement a system where outputs are escalated based on confidence scores and logprobs.
- **Measurable Check**: Track the number of escalations and compare the accuracy of escalated outputs versus non-escalated ones to ensure improved reliability.

### 3. Self-Consistency Checks
- **Description**: Integrate self-consistency checks by generating multiple outputs at a higher temperature and comparing them.
- **Measurable Check**: Measure the reduction in output discrepancies and false positives after implementing self-consistency checks.

### 4. Deterministic Validation
- **Description**: Use JSON schemas and linters to validate model outputs instead of relying solely on model confidence.
- **Measurable Check**: Assess the accuracy of outputs validated through deterministic methods compared to those relying on model confidence alone.

### 5. Python Router for Output Processing
- **Description**: Develop a Python router that processes model outputs, checks logprobs, and escalates when necessary.
- **Measurable Check**: Evaluate the router's effectiveness by measuring the accuracy and reliability of outputs processed through it, compared to direct model outputs.

By focusing on these priorities, we can systematically integrate hallucination controls into our AI fleet, enhancing reliability and reducing errors.
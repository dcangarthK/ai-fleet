# SPEC: Grounding and hallucination controls

Status: **draft for approval**. Sources: the owner's research notes; independent plans by Claude, Codex, and three
local models (glm-4.7-flash, phi4:14b, qwen2.5-coder:14b), all from the same brief; measurements on this machine.
"Review notes" at the end say who contributed what.

## Principle

Hallucination control is a **layered quality system on the paths the fleet already has**: instructions → model
params → evals → `ask_model`/`review_diff` → pipeline reviews → gates. Deterministic checks (tests, schemas, quote
checks) stay the authority. Model-reported signals (self-confidence, logprobs, agreement) are **telemetry until an
eval shows they predict errors**; only then do they gate anything. No separate router service.

## What we measured (2026-11-11)

- Ollama 0.17.7 returns token logprobs (`/api/chat` `logprobs`/`top_logprobs`, and `/v1/chat/completions`).
- "Paris." → answer token p = 0.998, final "." p = 0.59. Averaging over all tokens measures formatting, not truth.
- Local reviewers on our own Phase 4/5b/6 diffs (phi4:14b): 11 issues raised, 0 confirmed. Exact-quote checks
  dropped 2 (quote not in the diff); the rest quoted real lines with wrong claims, one proposing a "fix" identical
  to the existing code. Codex's reviews of the same diffs found 15 real defects, all fixed.
- As planning peers, local models differed sharply: glm-4.7-flash was critical and specific; phi4 and
  qwen2.5-coder endorsed every research item "as-is", including ones the brief said conflict with the fleet.

## Where each guideline lands

| Guideline | Lands in | Gating? |
|---|---|---|
| Permission to abstain; epistemic tags (ESTABLISHED / INFERRED / UNKNOWN) | Role-specific **skills**: `grounded-answer` (research, synthesis, Q&A) and `reviewer` (abstain = "no defensible issue"). Not imposed on coding agents' normal output | no |
| Quote-first | `review_diff`, pipeline reviews, reviewer evals (**one contract**, §G1); `ask_model(grounded)` quotes checked against the supplied context | yes: unsupported quotes dropped |
| Temperature 0–0.3 | Per-role/model params in the catalog; recorded with every eval result (today `_chat()` hardcodes 0.2 and evals use 0) | no |
| Token logprobs | Telemetry in evals and calls: min/mean probability over **answer-content tokens only** (excluding JSON structure, punctuation and any thinking tokens) | not until §G3 shows it predicts errors |
| Self-consistency (3 samples) | Eval experiment for short outputs (fast labels, yes/no, one-liners); never for code | same |
| Self-reported confidence | Recorded, never trusted alone | same |
| LLM-as-judge | Existing cross-model pipeline review: a second opinion, not a release gate by itself | no |
| Deterministic validation | Gates, schemas, sandboxed tests, quote check, identical-fix check | yes (already) |
| Clean tool outputs | MCP tool responses: compact typed fields, no noise, keep IDs that are needed | no |
| Escalation | `ask_model` returns `ANSWER\|ABSTAIN` + reason; **policy** decides: role fallback, then a read-only Claude/Codex task | yes, by policy |

**Not adopted:** a universal JSON-only prompt for all models (role schemas instead; `reasoning_trace` replaced by a
short `rationale`); the API-key router (the fleet uses the Claude Code/Codex subscriptions; CLAUDE.md forbids
sending keys to models); running generated code with a plain `subprocess` (the eval sandbox exists); a LAN FastAPI
service on port 8000 behind macvlan (no auth, duplicates the controller/LiteLLM, conflicts with the Tailscale +
token model); fixed thresholds 0.80/0.85 (uncalibrated). The Continue config is out of scope unless Continue
becomes a fleet client (then it points at the LiteLLM roles). Model names `gemma4:12b`, `qwen3.5:9b`,
`gpt-5.3-codex`, `claude-sonnet-4-6` are unverified here; the catalog validates names against the registry.

## Builds (each runs through the fleet pipeline: builder → gate → cross-review)

### G1. One reviewer contract
Schema used by `review_diff` (MCP), pipeline reviews and the reviewer eval:
`{"verdict": "approve|request_changes|abstain", "issues": [{"severity", "file", "problem", "evidence",
"expected", "actual", "fix"}]}`. Checks, in `master/controller/grounding.py` (shared): evidence must be a verbatim
quote from one added/removed/context line, or span consecutive lines within one hunk, after whitespace
normalisation; drop issues whose `fix` equals the `evidence` (or the unchanged line); `abstain` is valid and counts
as no issues. The reviewer eval prompt and scoring use the same schema and checks.
**Done when:** malformed, unquoted and identical-fix issues are rejected in all three places (tests); planted-bug
recall on `evals/cases/reviewer.json` doesn't regress for any installed model.

### G2. Real-diff reviewer benchmark
`evals/cases/reviewer_real.json`: diffs from this repo's own history with labels from what was actually confirmed
and fixed (Phases 4–6 give ~15 true defects, plus clean diffs and the local models' rejected claims as
negatives). Report high/medium **precision, recall and false-positive rate** separately.
**Done when:** the eval reports these per model; a model can only hold the `reviewer` role for pipeline gating if
precision ≥ 0.9 on the held-out half (threshold set from data, reviewed by the owner).

### G3. Grounding calibration eval
`evals/cases/grounding.json`: ≥ 30 cases of context + question, half answerable from the context, half not
(answer absent or contradicted). Per answer record: correct / correct-abstain / hallucinated, quote validity, tags,
content-token logprob min/mean, self-confidence, and 3-sample agreement for short answers. Output risk-vs-coverage
curves per model and signal.
**Done when:** the leaderboard shows hallucination rate and correct-abstain rate per model; for each signal the
report says whether a threshold cuts held-out errors materially at ≥ 70% coverage. Signals that don't are dropped.

### G4. `ask_model(role, prompt, context?, grounded=True)`
Role-specific JSON schema via Ollama `format`; temperature from params; logprobs on (telemetry); quote check against
`context` when given; returns `{status: ANSWER|ABSTAIN, answer, quotes, tags, reason}`. On `ABSTAIN` or a failed
check: try the role's fallback once (Phase 8), then, if the effective policy allows escalation, spawn a read-only
Claude/Codex task with the question, context and failure reason. Call stats gain `outcome` (answered, abstained,
escalated, check_failed). Gating on logprobs/agreement only for models and roles G3 calibrated.
**Done when:** on G3 cases, end-to-end hallucination rate (after escalation) is lower than raw local answers, with
no silent failures; the console shows abstain/escalation rates.

### G5. Instructions and skills
Fleet instruction (short, every agent): say "I don't know" rather than guess; cite files/lines for claims about the
codebase; never invent APIs, paths or flags (mark UNVERIFIED). Skills `grounded-answer` and `reviewer` bound to
roles. **Done when:** G2/G3 scores don't drop with the skills on (A/B in the eval) and the snapshot shows them.

### G6. Console
Evals page: grounding columns (hallucination rate, correct-abstain rate, reviewer precision); Overview call stats
show outcomes; pipeline reviews show identical-fix drops next to unsupported-quote drops.

## Who builds what (from measured strengths)

| Work | Builder | Reviewers |
|---|---|---|
| G1, G4 code | Codex | glm-4.7-flash (reviewer role on the master) + Claude |
| G2 labels | Claude (needs judgment over our own review history) | Codex |
| G3 cases | qwen2.5-coder:14b drafts case JSON in a fixed shape; each case's answerability is verified by a deterministic check (the answer string present/absent in the context) | glm-4.7-flash, Claude curates |
| G3 harness | Codex | Claude + glm-4.7-flash |
| G5 text | Claude drafts, glm-4.7-flash condenses | Codex |
| G6 console | Claude | Codex |

Local 14B models don't get design or gating-review roles until G2 shows they meet the bar.

## Review notes
- **Codex:** keep signals as telemetry until calibrated; unify the reviewer contract (found that `review_diff` and
  the reviewer eval omit `evidence`); real-diff benchmark with precision; replace `reasoning_trace`; reject the
  router service and API-key escalation.
- **glm-4.7-flash:** self-reported confidence is dangerous ("confidently wrong"); logprob threshold needs per-model
  calibration (used our "Paris." measurement); JSON schema for `ask_model`, not all models; calibration eval.
- **Claude:** content-token logprob scoring; identical-fix check; reuse the eval sandbox; escalation as read-only
  tasks under policy; real labels from this repo's own review history.
- **phi4:14b, qwen2.5-coder:14b:** endorsed every item including the API-key router; phi4 flagged self-consistency as
  unverified. Their output is why local 14B models are kept off design and gating roles for now.

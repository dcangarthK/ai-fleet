---
id: grounded-answer
version: 1
description: Answer questions strictly from the provided context quotes, abstaining when the context does not answer.
agents: [claude, codex, ollama]
risk: prompt-only
bind: [role:fast, agent:ollama]
---
Answer a question using only the provided context:

1. Copy verbatim every quote from the context that bears on the question.
2. Build the answer only from those quotes. Never fill gaps from memory.
3. Tag each claim ESTABLISHED (a direct quote), INFERRED (follows from the quotes) or UNKNOWN (not in the context).
4. If the quotes do not answer the question, abstain: "I don't know. The context does not say <what is missing>."
   Answering a related question the context does cover is not an answer.

Example (abstain):
Context: "The API uses a GET method. It returns a JSON object."
Question: What rate limit does the API enforce?
Answer: I don't know. The context does not say whether the API has a rate limit or what it is;
it only gives the method (GET) and the response format (JSON).

Example (answered):
Context: "The API uses a GET method. It returns a JSON object."
Question: How do I call the API and what comes back?
Answer: ESTABLISHED ("The API uses a GET method.") Call it with GET.
ESTABLISHED ("It returns a JSON object.") It returns a JSON object.

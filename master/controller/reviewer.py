"""Reviewer selection policy.  This module only reads budget state and task lineage."""
from fastapi import HTTPException

import budget
from store import db


CODEX_MODEL = "gpt-5.6-terra"
CLAUDE_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5")


def _limited(status: dict, rules: dict, agent: str, model: str) -> bool:
    key = budget.match_key(rules, agent, model)
    if key is None:
        return True
    row = next((item for item in status.get("models", []) if item.get("key") == key), {})
    return row.get("state") == "limited" or bool(row.get("limited_until"))


def _excluded_by_builder(rules: dict, builder: dict, agent: str, model: str) -> bool:
    if builder["agent"] != agent:
        return False
    builder_key = budget.match_key(rules, agent, builder["model"])
    candidate_key = budget.match_key(rules, agent, model)
    # An absent or generic Claude identity cannot prove model independence, so no Claude reviewer is safe.
    if builder["model"] is None or builder_key is None or builder_key == f"{agent}:*":
        return True
    return builder_key == candidate_key


def propose(task_id: str) -> dict:
    """Choose an independent paid reviewer; Fable is intentionally never a candidate."""
    with db() as c:
        task = c.execute("SELECT id, kind, agent, model, parent_id, review_of FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, f"unknown task {task_id}")
        builder, seen = task, {task["id"]}
        while builder["kind"] in budget.REVIEW_KINDS:
            previous_id = builder["review_of"] or builder["parent_id"]
            if not previous_id or previous_id in seen:
                break
            previous = c.execute("SELECT id, kind, agent, model, parent_id, review_of FROM tasks WHERE id=?", (previous_id,)).fetchone()
            if not previous:
                break
            builder, seen = previous, seen | {previous_id}
    status = budget.status()
    rules = budget.current_rules()
    kind = task["kind"]
    if kind in {"final_review", "security_review"} and not _excluded_by_builder(rules, builder, "codex", CODEX_MODEL) and not _limited(status, rules, "codex", CODEX_MODEL):
        return {"agent": "codex", "model": CODEX_MODEL, "profile": "fleet",
                "reason": "Codex has live headroom for final/security review and is independent of the builder"}
    for model in CLAUDE_MODELS:
        if _excluded_by_builder(rules, builder, "claude", model) or _limited(status, rules, "claude", model):
            continue
        return {"agent": "claude", "model": model, "profile": "fleet",
                "reason": "Claude reviewer has live headroom and differs from the builder; Fable is excluded"}
    raise HTTPException(409, "no independent non-Fable reviewer has live budget headroom")

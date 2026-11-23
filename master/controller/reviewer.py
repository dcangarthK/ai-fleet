"""Reviewer selection policy.  This module only reads budget state and task lineage."""
from fastapi import HTTPException

import budget
from store import db


CODEX_MODEL = "gpt-5.6-terra"
CLAUDE_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5")


def _limited(status: dict, agent: str, model: str) -> bool:
    key = budget.match_key(budget.current_rules(), agent, model)
    row = next((item for item in status.get("models", []) if item.get("key") == key), {})
    return row.get("state") == "limited" or bool(row.get("limited_until"))


def propose(task_id: str) -> dict:
    """Choose an independent paid reviewer; Fable is intentionally never a candidate."""
    with db() as c:
        task = c.execute("SELECT id, kind, agent, model, parent_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, f"unknown task {task_id}")
        builder = task
        if task["kind"] in budget.REVIEW_KINDS and task["parent_id"]:
            parent = c.execute("SELECT id, kind, agent, model, parent_id FROM tasks WHERE id=?", (task["parent_id"],)).fetchone()
            if parent:
                builder = parent
    builder_pair = (builder["agent"], builder["model"])
    status = budget.status()
    kind = task["kind"]
    if kind in {"final_review", "security_review"} and builder_pair != ("codex", CODEX_MODEL) and not _limited(status, "codex", CODEX_MODEL):
        return {"agent": "codex", "model": CODEX_MODEL, "profile": "fleet",
                "reason": "Codex has live headroom for final/security review and is independent of the builder"}
    for model in CLAUDE_MODELS:
        if builder_pair == ("claude", model) or _limited(status, "claude", model):
            continue
        return {"agent": "claude", "model": model, "profile": "fleet",
                "reason": "Claude reviewer has live headroom and differs from the builder; Fable is excluded"}
    raise HTTPException(409, "no independent non-Fable reviewer has live budget headroom")

"""Typed task briefs and their deterministic prompt rendering."""
import re
from typing import Any


CONTEXT_REF_RE = re.compile(r"^[^\s:][^:\n]*:\d+$")
BUILD_KINDS = {"build", "fix", "test"}
REVIEW_KINDS = {"review", "re_review", "final_review", "security_review"}


def _strings(value: Any, name: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or (required and not value):
        raise ValueError(f"brief.{name} must be a non-empty list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"brief.{name} must contain non-empty strings")
    return [item.strip() for item in value]


def validate(value: Any, kind: str) -> dict:
    """Return a normalized brief, rejecting ambiguous structured input."""
    if not isinstance(value, dict):
        raise ValueError("brief must be an object")
    allowed = {"goal", "files_allowed", "acceptance", "context_refs", "done_when", "gate"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"brief has unknown field(s): {', '.join(sorted(unknown))}")
    goal = value.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("brief.goal is required")
    files = _strings(value.get("files_allowed", []), "files_allowed", required=kind in BUILD_KINDS)
    acceptance = _strings(value.get("acceptance"), "acceptance", required=True)
    refs = _strings(value.get("context_refs", []), "context_refs")
    if any(not CONTEXT_REF_RE.fullmatch(ref) for ref in refs):
        raise ValueError("brief.context_refs must use file:line references")
    done_when = value.get("done_when", "")
    gate = value.get("gate", "")
    if done_when is not None and (not isinstance(done_when, str) or not done_when.strip()):
        raise ValueError("brief.done_when must be a non-empty string when provided")
    if gate is not None and not isinstance(gate, str):
        raise ValueError("brief.gate must be a string")
    return {"goal": goal.strip(), "files_allowed": files, "acceptance": acceptance,
            "context_refs": refs, "done_when": (done_when or "").strip(), "gate": (gate or "").strip()}


def render(value: dict, kind: str) -> str:
    """Render a bounded brief using the batch skill's Build or fix / Review structure."""
    label = "Review" if kind in REVIEW_KINDS else "Build or fix"
    parts = [f"# {label}", "", f"Goal: {value['goal']}"]
    if value["files_allowed"]:
        parts += ["", "Files allowed:", *[f"- {path}" for path in value["files_allowed"]]]
    parts += ["", "Acceptance:", *[f"- {item}" for item in value["acceptance"]]]
    if value["context_refs"]:
        parts += ["", "Context references:", *[f"- {ref}" for ref in value["context_refs"]]]
    if value["done_when"]:
        parts += ["", f"Done when: {value['done_when']}"]
    if value["gate"]:
        parts += ["", f"Gate: {value['gate']}"]
    return "\n".join(parts)

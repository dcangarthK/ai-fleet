"""Typed task briefs and their deterministic prompt rendering."""
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any


CONTEXT_REF_RE = re.compile(r"^[^\s:][^:\n]*:\d+$")
BUILD_KINDS = {"build", "fix", "test"}
REVIEW_KINDS = {"review", "re_review", "final_review", "security_review"}


def _single_line(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"brief.{name} must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise ValueError(f"brief.{name} must be a single line")
    return value.strip()


def _strings(value: Any, name: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or (required and not value):
        raise ValueError(f"brief.{name} must be a non-empty list")
    return [_single_line(item, f"{name} item") for item in value]


def _safe_path(path: str) -> str:
    segments = path.replace("\\", "/").split("/")
    if PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute() or ".." in segments or ".git" in segments:
        raise ValueError("brief.files_allowed must not contain absolute paths, .. segments, or .git paths")
    return path


def validate(value: Any, kind: str) -> dict:
    """Return a normalized brief, rejecting ambiguous structured input."""
    if not isinstance(value, dict):
        raise ValueError("brief must be an object")
    allowed = {"goal", "files_allowed", "acceptance", "context_refs", "done_when", "gate"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"brief has unknown field(s): {', '.join(sorted(unknown))}")
    if "goal" not in value:
        raise ValueError("brief.goal is required")
    goal = _single_line(value["goal"], "goal")
    files = [_safe_path(path) for path in _strings(value.get("files_allowed", []), "files_allowed", required=kind in BUILD_KINDS)]
    acceptance = _strings(value.get("acceptance"), "acceptance", required=True)
    refs = _strings(value.get("context_refs", []), "context_refs")
    if any(not CONTEXT_REF_RE.fullmatch(ref) for ref in refs):
        raise ValueError("brief.context_refs must use file:line references")
    done_when = _single_line(value["done_when"], "done_when") if value.get("done_when") is not None else None
    gate = _single_line(value["gate"], "gate") if "gate" in value else None
    return {"goal": goal, "files_allowed": files, "acceptance": acceptance,
            "context_refs": refs, "done_when": done_when, "gate": gate}


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

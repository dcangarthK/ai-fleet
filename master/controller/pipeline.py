"""Pure build-pipeline state machine and evidence-checked review parsing."""
import copy
import re
import time

import grounding


TERMINAL = {"ready", "failed", "escalated", "cancelled"}


def _now() -> float:
    return time.time()


def _history(p: dict, state: str, **extra) -> None:
    p["history"].append({"round": p["round"], "state": state, "at": _now(), **extra})


def new(spec: dict) -> dict:
    """Validate a pipeline specification and return JSON-serialisable initial state."""
    builder = spec.get("builder")
    reviewers = spec.get("reviewers")
    if not isinstance(builder, dict) or not isinstance(builder.get("agent"), str):
        raise ValueError("builder.agent is required")
    if not isinstance(reviewers, list) or not reviewers:
        raise ValueError("at least one reviewer is required")
    clean_reviewers = []
    for reviewer in reviewers:
        if not isinstance(reviewer, dict) or reviewer.get("kind") not in ("role", "agent"):
            raise ValueError("reviewer kind must be role or agent")
        field = reviewer["kind"]
        name = reviewer.get(field)
        if not isinstance(name, str) or not name:
            raise ValueError(f"reviewer {field} is required")
        if field == "agent" and name == builder["agent"]:
            raise ValueError("builder cannot review itself")
        clean_reviewers.append({"kind": field, field: name})
    maximum = spec.get("max_rounds", 3)
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 20:
        raise ValueError("max_rounds must be between 1 and 20")
    require_trusted = spec.get("require_trusted", True)
    if not isinstance(require_trusted, bool):
        raise ValueError("require_trusted must be a boolean")
    trusted_roles = spec.get("trusted_roles", [])
    if (not isinstance(trusted_roles, list)
            or any(not isinstance(role, str) or not role for role in trusted_roles)):
        raise ValueError("trusted_roles must be a list of role names")
    bypass = spec.get("bypass", False)
    if not isinstance(bypass, bool):
        raise ValueError("bypass must be a boolean")
    if bypass and builder.get("agent") not in ("claude", "codex"):
        raise ValueError("only a Claude or Codex builder can skip permission prompts")
    mode = spec.get("mode", "auto-edit")
    return {
        "title": spec.get("title", ""), "state": "building", "round": 1,
        "max_rounds": maximum, "spec": spec.get("spec", ""),
        "repo": spec.get("repo"), "gate": spec.get("gate"), "mode": mode,
        "require_trusted": require_trusted, "bypass": bypass, "trusted_roles": list(dict.fromkeys(trusted_roles)),
        "builder": copy.deepcopy(builder), "reviewers": clean_reviewers,
        "build_task": None, "reviews": {}, "history": [],
    }


def start(pipeline: dict) -> tuple[dict, list[dict]]:
    p = copy.deepcopy(pipeline)
    if p.get("history"):
        return p, []
    p["state"] = "building"
    _history(p, "building")
    return p, [{"do": "spawn_build"}]


def _normalised(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_review(raw: str, diff: str) -> dict:
    """Apply the shared review contract, retaining the legacy invalid status name."""
    parsed = grounding.check_review(raw, diff)
    if parsed["status"] == "invalid":
        parsed["status"] = "abstained"
    return parsed


def _resolution_text(issues: list[dict]) -> str:
    lines = ["Resolve these independently reviewed issues, keep the existing task context, and rerun the gate:"]
    for issue in issues:
        where = f" ({issue['file']})" if issue.get("file") else ""
        lines.append(f"- [{issue['severity']}] {issue['problem']}{where}")
        if issue.get("fix"):
            lines.append(f"  Suggested fix: {issue['fix']}")
    return "\n".join(lines)


def step(pipeline: dict, event: dict) -> tuple[dict, list[dict]]:
    """Apply one event and return the new state plus controller actions."""
    p = copy.deepcopy(pipeline)
    kind = event.get("type")
    if kind == "retry" and p.get("state") == "escalated":
        # A person decided to try the reviews of this round again (e.g. after plugging in or fixing a model).
        p["state"] = "reviewing"
        p["reviews"] = {str(i): {"status": "pending"} for i in range(len(p["reviewers"]))}
        _history(p, "reviewing", note="reviews retried")
        return p, [{"do": "spawn_review", "reviewer": i} for i in range(len(p["reviewers"]))]
    if p.get("state") in TERMINAL:
        return p, []
    if kind == "cancel":
        p["state"] = "cancelled"
        _history(p, "cancelled", note="cancelled by user")
        return p, []
    if kind == "build":
        if p.get("state") != "building":
            return p, []
        state = event.get("state")
        if state == "review":
            p["state"] = "reviewing"
            p["reviews"] = {str(i): {"status": "pending"} for i in range(len(p["reviewers"]))}
            _history(p, "reviewing")
            return p, [{"do": "spawn_review", "reviewer": i} for i in range(len(p["reviewers"]))]
        if state in ("failed", "interrupted", "cancelled"):
            p["state"] = "failed"
            _history(p, "failed", note=f"builder task {state}")
        return p, []
    if kind != "review" or p.get("state") != "reviewing":
        return p, []
    index = event.get("reviewer")
    key = str(index)
    if not isinstance(index, int) or key not in p["reviews"] or p["reviews"][key].get("status") != "pending":
        return p, []
    parsed = parse_review(event.get("raw", ""), event.get("diff", ""))
    if event.get("task_id"):
        parsed["task_id"] = event["task_id"]
    if event.get("reason"):
        parsed["reason"] = str(event["reason"])[:300]
    fallback = event.get("fallback") is True
    reviewer = p["reviewers"][index]
    usable = parsed.get("status") == "valid" or parsed.get("verdict") == "abstain"
    parsed["fallback"] = fallback
    # Roles are trusted when the pipeline names them, or when the controller reports (from benchmark data, at
    # review time) that every model holding the role is a trusted reviewer.
    auto_trusted = event.get("trusted_roles") if isinstance(event.get("trusted_roles"), list) else []
    role_trusted = reviewer["kind"] == "role" and (reviewer.get("role") in p.get("trusted_roles", [])
                                                   or reviewer.get("role") in auto_trusted)
    parsed["trusted"] = bool(usable and not fallback and (
        (reviewer["kind"] == "agent" and reviewer.get("agent") in {"claude", "codex"}) or role_trusted
    ))
    if parsed["trusted"] and reviewer["kind"] == "role" and reviewer.get("role") not in p.get("trusted_roles", []):
        parsed["trusted_by"] = "benchmark"
    p["reviews"][key] = parsed
    if any(review.get("status") == "pending" for review in p["reviews"].values()):
        return p, []
    valid = [review for review in p["reviews"].values()
             if review.get("status") == "valid" or review.get("verdict") == "abstain"]
    unsupported = sum(review.get("unsupported", 0) for review in p["reviews"].values())
    identical_fix = sum(review.get("identical_fix", 0) for review in p["reviews"].values())
    actionable = []
    seen = set()
    for review in valid:
        for issue in review.get("issues", []):
            if issue["severity"] not in ("high", "medium"):
                continue
            identity = (issue.get("file", ""), _normalised(issue["problem"]).lower())
            if identity not in seen:
                seen.add(identity)
                actionable.append(issue)
    if p.get("require_trusted", True) and not any(review.get("trusted") for review in p["reviews"].values()):
        p["state"] = "escalated"
        reasons = "; ".join(f"reviewer {k}: {r['reason']}" for k, r in sorted(p["reviews"].items()) if r.get("reason"))
        _history(p, "escalated", issues=len(actionable), unsupported=unsupported,
                 identical_fix=identical_fix, note="no trusted review" + (f" ({reasons})" if reasons else ""))
        return p, []
    if not valid:
        p["state"] = "escalated"
        reasons = "; ".join(f"reviewer {k}: {r['reason']}" for k, r in sorted(p["reviews"].items()) if r.get("reason"))
        _history(p, "escalated", unsupported=unsupported, identical_fix=identical_fix,
                 note="no usable review" + (f" ({reasons})" if reasons else " (all replies were invalid)"))
        return p, []
    if not actionable:
        p["state"] = "ready"
        _history(p, "ready", issues=0, unsupported=unsupported, identical_fix=identical_fix)
        return p, []
    _history(p, "resolving", issues=len(actionable), unsupported=unsupported, identical_fix=identical_fix)
    p["round"] += 1
    if p["round"] > p["max_rounds"]:
        p["round"] = p["max_rounds"]
        p["state"] = "escalated"
        _history(p, "escalated", issues=len(actionable), unsupported=unsupported, identical_fix=identical_fix,
                 note="review rounds exhausted")
        return p, []
    p["state"] = "building"
    p["reviews"] = {}
    _history(p, "building", issues=len(actionable), unsupported=unsupported, identical_fix=identical_fix,
             note="review issues sent to builder")
    return p, [{"do": "message_build", "text": _resolution_text(actionable)}]

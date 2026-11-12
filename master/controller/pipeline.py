"""Pure build-pipeline state machine and evidence-checked review parsing."""
import copy
import re
import time

import grounding


TERMINAL = {"ready", "failed", "escalated", "cancelled"}


GATE_BOUNCES = 3  # automatic gate-failure rounds back to the builder (pipeline step 4)


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
    builder = copy.deepcopy(builder)
    builder_model = builder.get("model")
    if builder_model is not None and not isinstance(builder_model, str):
        raise ValueError("builder model must be a non-empty string")
    builder_model = builder_model.strip() if builder_model else None
    if builder_model:
        builder["model"] = builder_model
    else:
        builder.pop("model", None)
        builder_model = None
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
        model = reviewer.get("model")
        if model is not None and (field != "agent" or not isinstance(model, str) or not model.strip()):
            raise ValueError("reviewer model must be a non-empty string and only applies to agent reviewers")
        if model is not None:
            model = model.strip()
        # A same-agent reviewer must name a model different from the builder's. With no model it would run
        # on the node default, which may be the builder's model. When the builder names no model we assume
        # the reviewer's named model differs from the node default (accepted risk, orchestrator decision).
        if field == "agent" and name == builder["agent"] and (model is None or model == builder_model):
            raise ValueError("builder cannot review itself: a reviewer must differ in agent or model")
        clean_reviewers.append({"kind": field, field: name} | ({"model": model} if model else {}))
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
    sections = spec.get("spec_sections")
    if sections is not None and (not isinstance(sections, list)
                                 or any(not isinstance(x, str) or not x.strip() for x in sections)):
        raise ValueError("spec_sections must be a list of section numbers")
    spec_path = spec.get("spec_path")
    if spec_path is not None and not isinstance(spec_path, str):
        raise ValueError("spec_path must be a string")
    mode = spec.get("mode", "auto-edit")
    return {
        "spec_sections": [x.strip() for x in sections] if sections else None, "spec_path": spec_path or None,
        "title": spec.get("title", ""), "state": "building", "round": 1,
        "max_rounds": maximum, "spec": spec.get("spec", ""),
        "repo": spec.get("repo"), "gate": spec.get("gate"), "mode": mode,
        "require_trusted": require_trusted, "bypass": bypass, "trusted_roles": list(dict.fromkeys(trusted_roles)),
        "builder": builder, "reviewers": clean_reviewers,
        "build_task": None, "reviews": {}, "history": [], "budget": {},
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


def resolution_text(issues: list[dict]) -> str:
    """The review fix-round message. The runner runs the gate after the builder finishes, so it never asks for that.
    A failed gate is a separate bounce (Part G 6): the build event carries `gate_text`, a summary of the failing
    tests, and the builder is messaged with that instead; a review fix round never carries gate output."""
    lines = ["Resolve these independently reviewed issues and keep the existing task context. "
             "You don't need to run the gate: the runner runs the gate after you finish."]
    for issue in issues:
        where = f" ({issue['file']})" if issue.get("file") else ""
        lines.append(f"- [{issue['severity']}] {issue['problem']}{where}")
        if issue.get("fix"):
            lines.append(f"  Suggested fix: {issue['fix']}")
    return "\n".join(lines)


_resolution_text = resolution_text  # older name


def _actionable(reviews) -> list[dict]:
    actionable, seen = [], set()
    for review in reviews:
        for issue in review.get("issues", []):
            if issue["severity"] not in ("high", "medium"):
                continue
            identity = (issue.get("file", ""), _normalised(issue["problem"]).lower())
            if identity not in seen:
                seen.add(identity)
                actionable.append(issue)
    return actionable


def _send_back(p: dict, actionable: list[dict], note: str, trusted: list[dict] | None = None,
               **counts) -> tuple[dict, list[dict]]:
    """Another build round with these issues, or escalate when the rounds are used up."""
    _history(p, "resolving", issues=len(actionable), **counts)
    p["round"] += 1
    if p["round"] > p["max_rounds"]:
        p["round"] = p["max_rounds"]
        p["state"] = "escalated"
        _history(p, "escalated", issues=len(actionable), note="review rounds exhausted", **counts)
        return p, []
    p["state"] = "building"
    p["reviews"] = {}
    # Only trusted findings are remembered for the next round's paid re-review prompt (AC19). With none, round 2
    # gets a full-diff review rather than a delta re-review of nothing.
    remembered = actionable if trusted is None else trusted
    p.setdefault("budget", {})["last_findings"] = remembered or None
    _history(p, "building", issues=len(actionable), note=note, **counts)
    return p, [{"do": "message_build", "text": resolution_text(actionable)}]


def _parsed_review(p: dict, index: int | None, role: str | None, event: dict) -> dict:
    """Apply the review contract to one reply and decide whether it is trusted."""
    parsed = parse_review(event.get("raw", ""), event.get("diff", ""))
    if event.get("task_id"):
        parsed["task_id"] = event["task_id"]
    if event.get("reason"):
        parsed["reason"] = str(event["reason"])[:300]
    fallback = event.get("fallback") is True
    reviewer = p["reviewers"][index] if index is not None else {"kind": "role", "role": role}
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
    return parsed


def _local_review(p: dict, event: dict) -> tuple[dict, list[dict]]:
    """Stage 1 (docs/SPEC-paid-budget.md 4.1): a free local review before any paid one.

    Trusted and asking for high/medium changes: back to the builder before a paid round. Trusted approval of a diff
    the opt-in skip rule covers: ready without a paid review. Otherwise the paid reviewers run; only trusted findings
    become hints for them. An untrusted local reviewer never blocks or skips anything.
    """
    index = event.get("reviewer_index") if isinstance(event.get("reviewer_index"), int) else None
    parsed = _parsed_review(p, index, event.get("role"), event)
    trusted = parsed["trusted"]
    budget = p.setdefault("budget", {})
    findings = parsed.get("issues", []) if parsed.get("status") == "valid" else []
    budget["stage1"] = {"round": p["round"], "role": event.get("role"), "model": event.get("model"),
                        "trusted": trusted, "verdict": parsed.get("verdict"), "findings": findings,
                        "hints": findings if trusted else [], "reason": parsed.get("reason")}
    budget["local_reviews"] = budget.get("local_reviews", 0) + 1
    actionable = _actionable([parsed]) if parsed.get("status") == "valid" else []
    if trusted and parsed.get("verdict") == "request_changes" and actionable:
        budget["paid_rounds_avoided"] = budget.get("paid_rounds_avoided", 0) + 1
        return _send_back(p, actionable, "local reviewer sent fixes back before paid review")
    if trusted and parsed.get("verdict") == "approve" and event.get("skip_paid") is True:
        budget["paid_reviews_skipped"] = budget.get("paid_reviews_skipped", 0) + 1
        p["state"] = "ready"
        _history(p, "ready", issues=0, note="paid review skipped by budget rule")
        return p, []
    actions = []
    for i in range(len(p["reviewers"])):
        if i == index:
            p["reviews"][str(i)] = parsed  # the pipeline's own role reviewer already answered as stage 1
        elif p["reviews"].get(str(i), {}).get("status") == "pending":
            actions.append({"do": "spawn_review", "reviewer": i, "after_local": True})
    if not actions:
        return _finish_round(p)
    return p, actions


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
        if state == "capped":
            # Hit its turn/context cap (Part H 3.2): settled, not a failure. Automatic continuation comes later
            # (Part H 3.3); until then the owner decides, so the pipeline stops building.
            p["state"] = "escalated"
            _history(p, "escalated", note="builder task capped (turn or context cap); needs a continuation")
            return p, []
        if state == "failed" and event.get("failure_reason") == "gate_failed" and event.get("gate_text"):
            # Gate bounce (Part G 6): the stateless count (tasks.gate_failures) is the one fix-round counter.
            failures = int(event.get("gate_failures") or 1)
            if failures > GATE_BOUNCES:
                p["state"] = "escalated"
                _history(p, "escalated", note=f"gate failed {failures} times; bounce rounds used up")
                return p, []
            _history(p, "building", note=f"gate failed: bounce {failures} of {GATE_BOUNCES} to the builder")
            return p, [{"do": "message_build", "text": event["gate_text"],
                        "gate_bounce": {"task_id": event.get("gate_task"), "title": event.get("gate_title"),
                                        "round": failures}}]
        if state in ("failed", "interrupted", "cancelled"):
            p["state"] = "failed"
            _history(p, "failed", note=f"builder task {state}")
        return p, []
    if kind == "build_limited":
        return _build_limited(p, event)
    if kind == "build_stalled":
        return _build_stalled(p, event)
    if kind == "review_limited" and p.get("state") == "reviewing":
        return _review_limited(p, event)
    if kind == "review_resume" and p.get("state") == "review_waiting":
        actions = []
        for key, review in sorted(p["reviews"].items()):
            if review.get("waiting_until") is not None:
                p["reviews"][key] = {"status": "pending"}
                actions.append({"do": "spawn_review", "reviewer": int(key)})
        p["state"] = "reviewing"
        _history(p, "reviewing", note="the limited review restarts after the reset (same round)")
        return p, actions
    if kind == "local_review" and p.get("state") == "reviewing":
        return _local_review(p, event)
    if kind != "review" or p.get("state") != "reviewing":
        return p, []
    index = event.get("reviewer")
    key = str(index)
    if not isinstance(index, int) or key not in p["reviews"] or p["reviews"][key].get("status") != "pending":
        return p, []
    p["reviews"][key] = _parsed_review(p, index, None, event)
    if any(review.get("status") == "pending" for review in p["reviews"].values()):
        return p, []
    return _finish_round(p)


def _build_limited(p: dict, event: dict) -> tuple[dict, list[dict]]:
    """The build stopped at a usage limit (docs/SPEC-limit-handoff.md 4.6). With a handoff the pipeline follows the
    new attempt; while waiting it stays building; left for the owner, it escalates. Never "failed"."""
    if p.get("state") != "building":
        return p, []
    action = event.get("action")
    budget = p.setdefault("budget", {})
    if action == "handoff" and event.get("task_id"):
        if p.get("build_task") == event["task_id"]:
            return p, []
        old = p.get("build_task")
        p["build_task"] = event["task_id"]
        budget.pop("waiting_build", None)
        _history(p, "building", note=f"build {old} stopped at a usage limit; handed off to {event['task_id']}",
                 handoff_from=old, handoff_to=event["task_id"])
        return p, []
    if action == "wait":
        if budget.get("waiting_build") == p.get("build_task"):
            return p, []
        budget["waiting_build"] = p.get("build_task")
        _history(p, "building", note="build stopped at a usage limit; waiting for the reset to resume it")
        return p, []
    if action == "left":
        p["state"] = "escalated"
        _history(p, "escalated", note="build stopped at a usage limit and was left for the owner"
                 + (f": {event['reason']}" if event.get("reason") else ""))
    return p, []


def _build_stalled(p: dict, event: dict) -> tuple[dict, list[dict]]:
    """The build stalled or was interrupted (docs/SPEC-stall-reroute.md 12.13). Not a gate-failure round: it uses
    none of the fix rounds. A reroute is followed; waiting for a free slot keeps building; escalated or left for
    the owner, the pipeline escalates."""
    if p.get("state") != "building":
        return p, []
    action = event.get("action")
    budget = p.setdefault("budget", {})
    reason = event.get("reason") or "stalled"
    if action == "handoff" and event.get("task_id"):
        if p.get("build_task") == event["task_id"]:
            return p, []
        old = p.get("build_task")
        p["build_task"] = event["task_id"]
        budget.pop("waiting_build", None)
        _history(p, "building", note=f"build {old} stalled; rerouted to {event['task_id']}",
                 handoff_from=old, handoff_to=event["task_id"])
        return p, []
    if action == "wait":
        if budget.get("waiting_build") == p.get("build_task"):
            return p, []
        budget["waiting_build"] = p.get("build_task")
        _history(p, "building", note=f"build stalled; waiting for a free slot ({reason})")
        return p, []
    if action in ("escalated", "left"):
        p["state"] = "escalated"
        _history(p, "escalated", note=f"build stalled and was left for the owner: {reason}")
    return p, []


def _review_limited(p: dict, event: dict) -> tuple[dict, list[dict]]:
    """A paid review stopped at a usage limit: not a finished review and not a paid round; wait for the reset."""
    index = event.get("reviewer")
    key = str(index)
    if not isinstance(index, int) or p["reviews"].get(key, {}).get("status") != "pending":
        return p, []
    p["reviews"][key] = {"status": "pending", "waiting_until": event.get("resets_at"),
                         "limited_task": event.get("task_id")}
    budget = p.setdefault("budget", {})
    others = [k for k, review in p["reviews"].items() if k != key and review.get("task_id")
              and p["reviewers"][int(k)].get("kind") == "agent"]
    if not others and p["round"] in budget.get("paid_rounds", []):
        budget["paid_rounds"].remove(p["round"])
    if event.get("codex") and p["round"] in budget.get("codex_rounds", []):
        budget["codex_rounds"].remove(p["round"])
    p["state"] = "review_waiting"
    _history(p, "review_waiting", note=f"reviewer {key} stopped at a usage limit; it reviews again after the reset",
             resets_at=event.get("resets_at"))
    return p, []


def _finish_round(p: dict) -> tuple[dict, list[dict]]:
    """Every reviewer of this round has answered: ready, escalate, or send the issues back."""
    valid = [review for review in p["reviews"].values()
             if review.get("status") == "valid" or review.get("verdict") == "abstain"]
    unsupported = sum(review.get("unsupported", 0) for review in p["reviews"].values())
    identical_fix = sum(review.get("identical_fix", 0) for review in p["reviews"].values())
    actionable = _actionable(valid)
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
    return _send_back(p, actionable, "review issues sent to builder",
                      trusted=_actionable([review for review in valid if review.get("trusted")]),
                      unsupported=unsupported, identical_fix=identical_fix)

"""Validation and deterministic merging for layered fleet policy."""
import hashlib
import json
import math
import os
import re


MODES = ("read-only", "supervised", "auto-edit", "full")
TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\(.{0,200}\))?$")
QUIET_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")
TOP_KEYS = {"filesystem", "tools", "network", "mode_ceiling", "limits", "auto_approve", "escalation"}
AUTO_APPROVE_AGENTS = {"claude", "codex"}
ESCALATION_AGENTS = {"claude", "codex"}
NESTED_KEYS = {
    "filesystem": {"allowed_roots", "denied"},
    "tools": {"claude_allow", "claude_deny"},
    "network": {"agents"},
    "limits": {"task_minutes", "max_concurrent", "agent_minutes_per_day", "quiet_hours"},
}


def _unknown(value: dict, allowed: set[str], label: str) -> None:
    extra = sorted(set(value) - allowed)
    if extra:
        raise ValueError(f"unknown {label} key: {extra[0]}")


def _string_list(value, label: str, check) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not check(item) for item in value):
        raise ValueError(f"{label} must be a valid string list")


def _path(value: str) -> bool:
    return value.startswith("~/") or os.path.isabs(value)


def _quiet(value: str) -> bool:
    match = QUIET_RE.fullmatch(value)
    return bool(match and int(match[1]) < 24 and int(match[2]) < 60
                and int(match[3]) < 24 and int(match[4]) < 60)


def validate(body: dict) -> dict:
    """Validate one policy body and return it unchanged.

    This deliberately does not fill defaults: an unset field does not participate
    in the narrowing merge.
    """
    if not isinstance(body, dict):
        raise ValueError("policy must be an object")
    _unknown(body, TOP_KEYS, "policy")
    for section in ("filesystem", "tools", "network", "limits"):
        if section not in body:
            continue
        value = body[section]
        if not isinstance(value, dict):
            raise ValueError(f"{section} must be an object")
        _unknown(value, NESTED_KEYS[section], section)
    filesystem = body.get("filesystem", {})
    for field in ("allowed_roots", "denied"):
        if field in filesystem:
            _string_list(filesystem[field], f"filesystem.{field}", _path)
    tools = body.get("tools", {})
    for field in ("claude_allow", "claude_deny"):
        if field in tools:
            _string_list(tools[field], f"tools.{field}", TOOL_RE.fullmatch)
    network = body.get("network", {})
    if "agents" in network and network["agents"] not in ("allow", "deny"):
        raise ValueError("network.agents must be allow or deny")
    if "auto_approve" in body:
        value = body["auto_approve"]
        if not isinstance(value, dict):
            raise ValueError("auto_approve must be an object")
        _unknown(value, {"agents", "full_mode", "denied_tools", "tools"}, "auto_approve")
        if "agents" in value:
            _string_list(value["agents"], "auto_approve.agents", lambda a: a in AUTO_APPROVE_AGENTS)
        if "tools" in value:  # granted at launch when denied tools are auto-approved anyway (SPEC-paid-budget 3.8)
            _string_list(value["tools"], "auto_approve.tools", TOOL_RE.fullmatch)
        for flag in ("full_mode", "denied_tools"):
            if flag in value and not isinstance(value[flag], bool):
                raise ValueError(f"auto_approve.{flag} must be true or false")
    if "escalation" in body:
        value = body["escalation"]
        if not isinstance(value, dict):
            raise ValueError("escalation must be an object")
        _unknown(value, {"agents"}, "escalation")
        if "agents" in value:
            _string_list(value["agents"], "escalation.agents", lambda a: a in ESCALATION_AGENTS)
    if "mode_ceiling" in body and body["mode_ceiling"] not in MODES:
        raise ValueError(f"mode_ceiling must be one of {list(MODES)}")
    limits = body.get("limits", {})
    for field in ("task_minutes", "agent_minutes_per_day"):
        value = limits.get(field)
        if field in limits and (not isinstance(value, (int, float)) or isinstance(value, bool)
                                or not math.isfinite(value) or value <= 0):
            raise ValueError(f"limits.{field} must be a positive number")
    value = limits.get("max_concurrent")
    if "max_concurrent" in limits and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
        raise ValueError("limits.max_concurrent must be a positive integer")
    if "quiet_hours" in limits:
        _string_list(limits["quiet_hours"], "limits.quiet_hours", _quiet)
    return body


def _comparable(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.expanduser(path)))


def _within(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath([_comparable(path), _comparable(parent)]) == _comparable(parent)
    except (OSError, ValueError):
        return False


def _dedupe(items: list) -> list:
    return list(dict.fromkeys(items))


def _intersect_roots(left: list[str], right: list[str]) -> list[str]:
    result = []
    for a in left:
        for b in right:
            if _within(a, b):
                result.append(a)
            elif _within(b, a):
                result.append(b)
    return _dedupe(result)


def _set_nested(result: dict, section: str, field: str, value) -> None:
    result.setdefault(section, {})[field] = value


def merge(layers: list[dict]) -> dict:
    """Merge ordered ``{layer, instructions, policy}`` dictionaries narrowly."""
    instructions = []
    bodies = []
    for layer in layers:
        label = layer.get("layer")
        text = layer.get("instructions")
        body = layer.get("policy") or {}
        validate(body)
        if text:
            instructions.append(f"## {label}\n{text}")
        bodies.append((label, body))

    result: dict = {}
    sources: dict = {}
    instruction_sources = [layer.get("layer") for layer in layers if layer.get("instructions")]
    if instruction_sources:
        sources["instructions"] = instruction_sources

    def list_field(section: str, field: str, intersection: bool) -> None:
        selected = [(label, body[section][field]) for label, body in bodies
                    if section in body and field in body[section]]
        if not selected:
            return
        value = list(selected[0][1])
        for _, items in selected[1:]:
            value = (_intersect_roots(value, items) if intersection and section == "filesystem"
                     else [item for item in value if item in items] if intersection
                     else _dedupe(value + list(items)))
        _set_nested(result, section, field, value)
        sources[f"{section}.{field}"] = [label for label, _ in selected]

    list_field("filesystem", "allowed_roots", True)
    list_field("filesystem", "denied", False)
    list_field("tools", "claude_allow", True)
    list_field("tools", "claude_deny", False)
    list_field("limits", "quiet_hours", False)

    networks = [(label, body["network"]["agents"]) for label, body in bodies
                if "network" in body and "agents" in body["network"]]
    if networks:
        denied = [item for item in networks if item[1] == "deny"]
        winner = denied[-1] if denied else networks[-1]
        _set_nested(result, "network", "agents", winner[1])
        sources["network.agents"] = winner[0]

    # Auto-approval only ever narrows: agents intersect across the layers that set it; a flag is on only if
    # every layer that mentions it says so.
    approvals = [(label, body["auto_approve"]) for label, body in bodies if "auto_approve" in body]
    if approvals:
        merged = {}
        lists = [set(a["agents"]) for _, a in approvals if "agents" in a]
        if lists:
            merged["agents"] = sorted(set.intersection(*lists))
        for flag in ("full_mode", "denied_tools"):
            values = [a[flag] for _, a in approvals if flag in a]
            if values:
                merged[flag] = all(values)
        tool_lists = [a["tools"] for _, a in approvals if "tools" in a]
        if tool_lists:
            merged["tools"] = [t for t in _dedupe(tool_lists[0]) if all(t in other for other in tool_lists[1:])]
        result["auto_approve"] = merged
        sources["auto_approve"] = [label for label, _ in approvals]

    # Escalating failed grounded ask_model calls to read-only agent tasks also only narrows: agents intersect,
    # keeping the first setting layer's preference order.
    escalations = [(label, body["escalation"]["agents"]) for label, body in bodies
                   if "agents" in body.get("escalation", {})]
    if escalations:
        allowed = set.intersection(*(set(agents) for _, agents in escalations))
        result["escalation"] = {"agents": _dedupe([a for a in escalations[0][1] if a in allowed])}
        sources["escalation.agents"] = [label for label, _ in escalations]

    ceilings = [(label, body["mode_ceiling"]) for label, body in bodies if "mode_ceiling" in body]
    if ceilings:
        winner = min(ceilings, key=lambda item: MODES.index(item[1]))
        result["mode_ceiling"] = winner[1]
        sources["mode_ceiling"] = winner[0]

    for field in ("task_minutes", "max_concurrent", "agent_minutes_per_day"):
        values = [(label, body["limits"][field]) for label, body in bodies
                  if "limits" in body and field in body["limits"]]
        if values:
            winner = min(values, key=lambda item: item[1])
            _set_nested(result, "limits", field, winner[1])
            sources[f"limits.{field}"] = winner[0]

    rendered = "\n\n".join(instructions)
    digest_body = json.dumps({"instructions": rendered, "policy": result}, sort_keys=True,
                             separators=(",", ":"), ensure_ascii=False).encode()
    return {"instructions": rendered, "policy": result, "sources": sources,
            "hash": hashlib.sha256(digest_body).hexdigest()}


def in_quiet_hours(windows: list[str], minute: int) -> bool:
    """Return whether a local minute-of-day is inside any half-open window."""
    for window in windows:
        match = QUIET_RE.fullmatch(window)
        if not match:
            continue
        start = int(match[1]) * 60 + int(match[2])
        end = int(match[3]) * 60 + int(match[4])
        if start < end and start <= minute < end:
            return True
        if start > end and (minute >= start or minute < end):
            return True
    return False

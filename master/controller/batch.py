"""Batch pipeline: validation, routing table and per-item state machine (docs/SPEC-batch-pipeline.md).

Pure functions over JSON-serialisable state: no I/O, no HTTP. Each item runs as one pipeline (pipeline.py does the
build, gate, review and fix loop); this module decides which items start, when, and on what model, mirrors each
pipeline's state back into its item, and gates the single final review. The controller applies the actions.
The module never resumes, releases or accepts a task, and never touches git.
"""
import copy
import os

import pipeline as pipeline_machine

GATE_BOUNCES = pipeline_machine.GATE_BOUNCES
SETTLED = {"ready", "escalated", "failed", "cancelled"}
ACTIVE = {"building", "reviewing"}
FABLE_NEVER_BUILDS = True  # CLAUDE.md rule 0: Fable gives guidance only. Checked by the routing tests.

SONNET, OPUS, FABLE = "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2"
TERRA = "gpt-5.6-terra"

# The routing table is data (AC6). Rows are in preference order; a limited row is skipped for the next one.
BUILD_ROWS = {
    "normal": [{"agent": "claude", "model": SONNET, "effort": None}],
    "hard": [{"agent": "claude", "model": OPUS, "effort": None},
             {"agent": "codex", "model": TERRA, "effort": "medium"}],
}
CODEX_BUILD = {"agent": "codex", "model": TERRA, "effort": "medium"}
CLAUDE_REVIEW_ROWS = [{"agent": "claude", "model": FABLE, "effort": "medium"},
                      {"agent": "claude", "model": SONNET, "effort": "medium"},
                      {"agent": "claude", "model": OPUS, "effort": "medium"}]
SECURITY_REVIEW = {"agent": "codex", "model": TERRA, "effort": "high"}

DEFAULT_POLICY = {
    "max_fix_rounds": 3,
    "gate_bounces": GATE_BOUNCES,
    "reviewer_must_differ": True,
    "model": "auto",
    "final_review": {"agent": "codex", "model": TERRA, "effort": "high", "once": True},
    "on_limit": "wait",
    "limit_wait_max": 6 * 3600,
    "concurrency": {"codex": 1, "claude": 2},
    "require_trusted": True,
}
BUILDERS = ("auto", "claude", "codex")


# ---------- validation ----------

def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def normalise_item(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("each item must be an object")
    ident = _text(raw.get("id"), "item id")
    builder = raw.get("builder", "auto")
    if builder not in BUILDERS:
        raise ValueError(f"item {ident}: builder must be one of {list(BUILDERS)}")
    model = raw.get("builder_model")
    if model is not None:
        model = _text(model, f"item {ident}: builder_model")
        if "fable" in model.lower():
            raise ValueError(f"item {ident}: Fable never builds")
    spec, spec_path = raw.get("spec"), raw.get("spec_path")
    if not (isinstance(spec, str) and spec.strip()) and not (isinstance(spec_path, str) and spec_path.strip()):
        raise ValueError(f"item {ident}: spec or spec_path is required")
    repo = _text(raw.get("repo"), f"item {ident}: repo")
    if not os.path.isabs(repo):
        raise ValueError(f"item {ident}: repo must be an absolute path")
    sections = raw.get("spec_sections")
    if sections is not None and (not isinstance(sections, list) or any(not isinstance(s, str) for s in sections)):
        raise ValueError(f"item {ident}: spec_sections must be a list of strings")
    files = raw.get("files") or []
    deps = raw.get("depends_on") or []
    if (not isinstance(files, list) or any(not isinstance(f, str) for f in files)
            or not isinstance(deps, list) or any(not isinstance(d, str) for d in deps)):
        raise ValueError(f"item {ident}: files and depends_on must be lists of strings")
    security = raw.get("security_sensitive", False)
    if not isinstance(security, bool):
        raise ValueError(f"item {ident}: security_sensitive must be a boolean")
    return {
        "id": ident, "title": str(raw.get("title") or ident), "spec": spec or "", "spec_path": spec_path or None,
        "spec_sections": sections, "repo": repo, "gate": _text(raw.get("gate"), f"item {ident}: gate"),
        "files": files, "builder": builder, "builder_model": model, "security_sensitive": security,
        "depends_on": list(deps), "state": "queued", "pipeline_id": None, "wait_until": None, "note": None,
        "escalation": None, "rounds": 0, "verdict": "", "open_findings": [], "models": {}, "spend": None,
        "branch": None, "build": None, "review": None,
    }


def _policy(raw) -> dict:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("policy must be an object")
    unknown = set(raw) - set(DEFAULT_POLICY)
    if unknown:
        raise ValueError(f"unknown policy keys: {sorted(unknown)}")
    policy = copy.deepcopy(DEFAULT_POLICY)
    for key, value in raw.items():
        if isinstance(policy[key], dict) and isinstance(value, dict):
            policy[key].update(value)
        else:
            policy[key] = value
    if (not isinstance(policy["max_fix_rounds"], int) or isinstance(policy["max_fix_rounds"], bool)
            or not 0 <= policy["max_fix_rounds"] <= 19):
        raise ValueError("max_fix_rounds must be between 0 and 19")
    if policy["on_limit"] not in ("wait", "escalate"):
        raise ValueError("on_limit must be wait or escalate")
    if not isinstance(policy["limit_wait_max"], (int, float)) or policy["limit_wait_max"] < 0:
        raise ValueError("limit_wait_max must be a number of seconds")
    if policy["model"] != "auto":
        raise ValueError("only model auto is supported")
    if policy["final_review"] != DEFAULT_POLICY["final_review"]:
        raise ValueError("final_review is fixed: codex, gpt-5.6-terra, high, once")
    if policy["gate_bounces"] != GATE_BOUNCES or isinstance(policy["gate_bounces"], bool):
        raise ValueError(f"gate_bounces is fixed at {GATE_BOUNCES} (the pipeline's own constant)")
    conc = policy["concurrency"]
    if (not isinstance(conc, dict) or not set(conc) <= {"codex", "claude"}
            or any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in conc.values())):
        raise ValueError("concurrency must map codex and claude to integers of at least 1")
    return policy


def new_batch(body: dict) -> dict:
    """Validate a whole submit. Any problem raises ValueError and nothing is created."""
    if not isinstance(body, dict) or not isinstance(body.get("items"), list) or not body["items"]:
        raise ValueError("items must be a non-empty list")
    items = [normalise_item(raw) for raw in body["items"]]
    ids = [i["id"] for i in items]
    if len(set(ids)) != len(ids):
        raise ValueError("item ids must be unique")
    for i in items:
        for dep in i["depends_on"]:
            if dep not in ids or dep == i["id"]:
                raise ValueError(f"item {i['id']}: unknown or self dependency {dep}")
    _no_cycle({i["id"]: i["depends_on"] for i in items})
    return {"state": "running", "policy": _policy(body.get("policy")), "items": items, "final": None}


def _no_cycle(graph: dict) -> None:
    done, path = set(), set()

    def visit(node):
        if node in path:
            raise ValueError("depends_on has a cycle")
        if node in done:
            return
        path.add(node)
        for dep in graph[node]:
            visit(dep)
        path.discard(node)
        done.add(node)

    for node in graph:
        visit(node)


def check_reviewer(builder: dict, reviewer: dict) -> None:
    """A reviewer must differ from the builder in agent or model (AC5)."""
    if builder.get("agent") == reviewer.get("agent") and (
            not reviewer.get("model") or reviewer.get("model") == builder.get("model")):
        raise ValueError("builder cannot review itself: a reviewer must differ in agent or model")


def pipeline_spec(item: dict, policy: dict, build: dict, review: dict, tag: str | None = None) -> dict:
    """The body for a pipeline. `max_rounds = max_fix_rounds + 1`: round 1 is the first build (section 10 item 1)."""
    if policy.get("reviewer_must_differ", True):
        check_reviewer(build, review)
    spec = item["spec"] or f"Implement the spec at {item['spec_path']}."
    if item.get("spec_sections"):
        spec += f"\nSections: {', '.join(item['spec_sections'])}."
    builder = {"agent": build["agent"], "model": build["model"]}
    reviewer = {"kind": "agent", "agent": review["agent"], "model": review["model"]}
    if build.get("effort"):
        builder["effort"] = build["effort"]
    if review.get("effort"):
        reviewer["effort"] = review["effort"]
    # `batch` tags the pipeline so a restart finds it instead of starting a second one (AC12).
    return {"title": item["title"], "spec": spec, "repo": item["repo"], "gate": item["gate"], "mode": "auto-edit",
            "builder": builder, "reviewers": [reviewer],
            "batch": {"tag": tag, "security": item["security_sensitive"]} if tag else None,
            "max_rounds": policy["max_fix_rounds"] + 1, "require_trusted": policy["require_trusted"],
            "spec_sections": item.get("spec_sections"), "spec_path": item.get("spec_path")}


# ---------- routing ----------

def limited_until(limits: dict, agent: str, model: str | None, now: float) -> float | None:
    """When this agent/model is usable again, or None when it is usable now."""
    until = max((limits.get("accounts") or {}).get(agent, 0),
                (limits.get("models") or {}).get(f"{agent}:{model}" if model else agent, 0))
    return until if until > now else None


def _pick(rows: list[dict], limits: dict, now: float) -> dict:
    waits = []
    for row in rows:
        until = limited_until(limits, row["agent"], row["model"], now)
        if until is None:
            return dict(row)
        waits.append(until)
    return {"wait_until": min(waits)}


def route_build(item: dict, limits: dict, now: float) -> dict:
    if item.get("builder_model"):
        agent = "codex" if item["builder"] == "codex" or item["builder_model"].startswith("gpt") else "claude"
        return _pick([{"agent": agent, "model": item["builder_model"], "effort": "medium" if agent == "codex" else None}],
                     limits, now)
    rows = BUILD_ROWS["hard" if item["security_sensitive"] else "normal"]
    if item["builder"] == "codex":
        rows = [CODEX_BUILD]
    elif item["builder"] == "claude":
        rows = [r for r in rows if r["agent"] == "claude"]
    return _pick(rows, limits, now)


def route_review(build: dict, security: bool, limits: dict, now: float) -> dict:
    """One reviewer per round. It differs from the builder; Codex only reviews security-sensitive Claude builds."""
    if security and build["agent"] == "claude":
        rows = [SECURITY_REVIEW]
    else:
        rows = [r for r in CLAUDE_REVIEW_ROWS if not (r["agent"] == build["agent"] and r["model"] == build["model"])]
    return _pick(rows, limits, now)


# ---------- state machine ----------

def all_settled(batch: dict) -> bool:
    return all(i["state"] in SETTLED for i in batch["items"])


def _slots(batch: dict) -> dict:
    """Running jobs per agent: a building item holds its builder's slot, a reviewing item its reviewer's, and the
    spawned final review its agent's. The routing rows carry no account, so the count is per agent (one account
    each); a per-account key needs the account on the routing row."""
    used = {}

    def take(agent):
        used[agent] = used.get(agent, 0) + 1

    for i in batch["items"]:
        if i["state"] == "building" and i.get("build"):
            take(i["build"]["agent"])
        elif i["state"] == "reviewing" and i.get("review"):
            take(i["review"]["agent"])
    if (batch.get("final") or {}).get("state") == "spawned":
        take(batch["final"]["agent"])
    return used


def _overlaps(item: dict, others: list[dict]) -> bool:
    mine = set(item["files"])
    return bool(mine) and any(mine & set(o["files"]) for o in others)


def step(batch: dict, event: dict) -> tuple[dict, list[dict]]:
    b = copy.deepcopy(batch)
    kind = event.get("type")
    if kind == "pipeline":
        for item in b["items"]:
            if item["id"] == event.get("id"):
                _sync(item, event.get("pipeline") or {}, event)
        return b, []
    if kind == "final_done":
        b["final"] = {**(b.get("final") or {}), "state": "done", "verdict": event.get("verdict"),
                      "model": event.get("model"), "findings": list(event.get("findings") or [])}
        b["state"] = "done"
        return b, []
    if kind == "final_limited" and (b.get("final") or {}).get("state") == "spawned":
        # The final review stopped at a usage limit: park until its reset, then run it again (decision 3).
        final = b["final"]
        b["final"] = {"state": "parked", "until": event["until"], "attempt": final.get("attempt", 0) + 1}
        return b, []
    if kind == "tick" and b["state"] != "done":
        return _tick(b, float(event.get("now") or 0), event.get("limits") or {"models": {}, "accounts": {}})
    return b, []


def _tick(b: dict, now: float, limits: dict) -> tuple[dict, list[dict]]:
    policy, actions = b["policy"], []
    by_id = {i["id"]: i for i in b["items"]}
    for item in b["items"]:
        if item["state"] != "queued":
            continue
        bad = [d for d in item["depends_on"] if by_id[d]["state"] in SETTLED and by_id[d]["state"] != "ready"]
        if bad:
            item["state"] = "escalated"
            item["escalation"] = f"dependency {bad[0]} is {by_id[bad[0]]['state']}, not ready"
    used = _slots(b)
    started = [i for i in b["items"] if i["state"] in ACTIVE]
    for item in b["items"]:
        if item["state"] != "queued":
            continue
        if any(by_id[d]["state"] != "ready" for d in item["depends_on"]):
            continue
        if item["wait_until"] and now < item["wait_until"]:
            continue  # never retry a limited model before its reset (AC10)
        if _overlaps(item, started):
            continue
        build = route_build(item, limits, now)
        review = route_review(build, item["security_sensitive"], limits, now) if "wait_until" not in build else build
        if "wait_until" in review:  # no usable builder or reviewer: park until the earliest reset, never pick a limited one
            waited = review["wait_until"] - now
            if policy["on_limit"] == "escalate" or waited > policy["limit_wait_max"]:
                item["state"] = "escalated"
                item["escalation"] = f"budget limit until {review['wait_until']:.0f}: not waiting"
            else:
                item["wait_until"] = review["wait_until"]
                item["note"] = "waiting for a usage limit to reset"
            continue
        if used.get(build["agent"], 0) >= policy["concurrency"].get(build["agent"], 1):
            continue
        used[build["agent"]] = used.get(build["agent"], 0) + 1
        item.update(state="building", wait_until=None, note=None, build=build, review=review)
        started.append(item)
        actions.append({"do": "start_item", "id": item["id"], "agent": build["agent"], "model": build["model"],
                        "effort": build["effort"], "review": review})
    if all_settled(b) and (b["final"] is None or b["final"].get("state") == "parked"):
        actions += _final(b, now, limits)
    return b, actions


def _final(b: dict, now: float, limits: dict) -> list[dict]:
    if not all_settled(b):
        return []
    ready = [i["id"] for i in b["items"] if i["state"] == "ready"]
    if not ready:
        b["state"] = "done"
        return []
    cfg = b["policy"]["final_review"]
    parked = b["final"] or {}
    if parked.get("state") == "parked" and now < parked["until"]:
        return []
    until = limited_until(limits, cfg["agent"], cfg["model"], now)
    if until is not None:
        # Codex is limited: wait for the reset. Never route the final review to Claude (decision 3).
        b["state"] = "final_review"
        b["final"] = {"state": "parked", "until": until, "attempt": parked.get("attempt", 0)}
        return [{"do": "park_final", "until": until}]
    b["state"] = "final_review"
    b["final"] = {"state": "spawned", "ids": ready, "agent": cfg["agent"], "model": cfg["model"],
                  "effort": cfg["effort"], "attempt": parked.get("attempt", 0)}
    return [{"do": "spawn_final_review", "ids": ready, "agent": cfg["agent"], "model": cfg["model"],
             "effort": cfg["effort"]}]


_ITEM_STATE = {"building": "building", "resolving": "building", "reviewing": "reviewing",
               "review_waiting": "reviewing", "ready": "ready", "escalated": "escalated", "failed": "failed",
               "cancelled": "cancelled"}


def _sync(item: dict, p: dict, event: dict) -> None:
    """Mirror one pipeline into its item. A settled item never moves again."""
    if item["state"] in SETTLED or p.get("state") not in _ITEM_STATE:
        return
    item["state"] = _ITEM_STATE[p["state"]]
    item["rounds"] = p.get("round", item["rounds"])
    waiting = (p.get("budget") or {}).get("waiting_approval")
    item["note"] = "waiting for controller auto-approve" if waiting and item["state"] == "building" else None
    notes = [h.get("note") for h in p.get("history") or [] if h.get("note")]
    last = notes[-1] if notes else ""
    if item["state"] in ("escalated", "failed"):
        item["escalation"] = last or item["state"]
    findings = (p.get("budget") or {}).get("last_findings") or []
    item["open_findings"] = [{"severity": f.get("severity"), "problem": f.get("problem"), "file": f.get("file")}
                             for f in findings]
    item["verdict"] = {"ready": "ready: no actionable findings", "escalated": f"escalated: {last}",
                       "failed": f"failed: {last}", "cancelled": "cancelled"}.get(item["state"], "")
    item["pipeline_id"] = p.get("id") or item["pipeline_id"]
    for key in ("branch", "models", "spend", "gate_tail"):
        if event.get(key) is not None:
            item[key] = event[key]


# ---------- output ----------

TRIM_KEYS = ("id", "title", "state", "branch", "rounds", "verdict", "open_findings", "models", "spend",
             "escalation", "note")
MAX_FINDINGS = 5


def summary(batch: dict, full: bool = False) -> dict:
    items = []
    counts: dict[str, int] = {}
    for item in batch["items"]:
        for roles in (item.get("models") or {}).values():
            for model, n in roles.items():
                counts[model] = counts.get(model, 0) + n
        if full:
            items.append(copy.deepcopy(item))
        else:
            row = {k: copy.deepcopy(item.get(k)) for k in TRIM_KEYS}
            row["open_findings"] = (row["open_findings"] or [])[:MAX_FINDINGS]
            items.append(row)
    return {"id": batch.get("id"), "state": batch["state"], "items": items, "final": batch.get("final"),
            "task_counts": counts}

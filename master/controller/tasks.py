"""Agent tasks: scheduling onto node runners, lifecycle, approvals, waiting, usage.

A task is one piece of delegated work for an agent (claude, codex) or a model (ollama) on some node.
The controller decides where it runs and what state it is in; the node's runner owns the process.
See docs/SPEC-agent-tasks.md for the lifecycle and permission modes.

State changes are compare-and-swap (they only apply if the task is still in the expected state) and
everything that also touches a runner holds the task's lock, so approvals, messages, cancels and the
poller can't overwrite each other.
"""
import asyncio
import datetime
import json
import os
import re
import secrets
import time
from collections import defaultdict
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from events import changed, emit, events_after, events_newest, last_event_id, snapshot
import budget
import policy as fleet_policy
import presence
import usage as capacity_usage
import skills as fleet_skills
from lineage import (  # noqa: F401  (re-exported: the lineage helpers are part of this module's surface)
    CHAIN_LINKS, FAILURE_STATES, HANDOFF_KINDS, LINKS, OUTCOME_KINDS, RESOLUTION_KINDS, RUNNING, clock, display_status,
    failure_reason,
    parse_reset, provider_limit, resolution_targets, tail, valid_commit,
)
from lineage import _json as lineage_json
from store import db, task_root

RUNNER_PORT = 11436
RUNNER_TOKEN = os.environ.get("RUNNER_TOKEN", "")
FLEET_DIR = os.environ.get("FLEET_DIR")
LOCAL_RUNNER = os.environ.get("FLEET_LOCAL_RUNNER", f"http://127.0.0.1:{RUNNER_PORT}").rstrip("/")
MASTER_ID = "master"
POLL_SECONDS = float(os.environ.get("FLEET_TASK_POLL_SECONDS", "3"))

MODES = ["read-only", "supervised", "auto-edit", "full"]
DEFAULT_MODE = "auto-edit"
NEEDS_APPROVAL = {"supervised", "full"}
AGENTS = {"claude", "codex", "ollama"}
ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
ACTIVE = {"queued", "awaiting_approval", "starting", "working"}
SETTLED = {"review", "failed", "blocked", "interrupted", "done", "cancelled", "capped"}  # capped: Part H, not a failure
RESUMABLE = {"review", "failed", "blocked", "interrupted", "capped"}

router = APIRouter()
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def rank(mode: str) -> int:
    return MODES.index(mode)


def local_minute() -> int:
    """Master-local minute of day; kept injectable for scheduler tests."""
    current = datetime.datetime.now().astimezone()
    return current.hour * 60 + current.minute


def effective_policy(node: str, role: str | None, agent: str, task_policy: dict | None = None,
                     skill_ids: list[str] | None = None) -> dict:
    """Load the newest applicable layers and compute the immutable task snapshot."""
    wanted = [("fleet", "*", "fleet"), ("node", node, f"node:{node}")]
    if role:
        wanted.append(("role", role, f"role:{role}"))
    wanted.append(("agent", agent, f"agent:{agent}"))
    layers = []
    with db() as c:
        for scope, scope_id, label in wanted:
            instruction = c.execute(
                "SELECT text FROM instructions WHERE scope=? AND scope_id=? ORDER BY version DESC LIMIT 1",
                (scope, scope_id)).fetchone()
            policy = c.execute(
                "SELECT body FROM policies WHERE scope=? AND scope_id=? ORDER BY version DESC LIMIT 1",
                (scope, scope_id)).fetchone()
            if instruction or policy:
                layers.append({"layer": label, "instructions": instruction["text"] if instruction else None,
                               "policy": json.loads(policy["body"]) if policy else {}})
    if task_policy is not None:
        fleet_policy.validate(task_policy)
        layers.append({"layer": "task", "instructions": None, "policy": task_policy})
    effective = fleet_policy.merge(layers)
    return fleet_skills.add_to_effective(effective, fleet_skills.snapshot(node, role, agent, skill_ids))


# ---------- runner client ----------

def runner_url(node: dict) -> str:
    return LOCAL_RUNNER if node["id"] == MASTER_ID else f"http://{node['ip']}:{RUNNER_PORT}"


class RunnerError(HTTPException):
    """`transport` is True when the runner never answered (connection refused, timeout): not an HTTP error answer."""
    def __init__(self, status_code: int, detail: str, transport: bool = False):
        super().__init__(status_code, detail)
        self.transport = transport


def node_runner_token(node: dict | str) -> str:
    node_id = node if isinstance(node, str) else node["id"]
    with db() as c:
        row = c.execute("SELECT runner_token FROM nodes WHERE id=?", (node_id,)).fetchone()
    if row and row["runner_token"]:
        return row["runner_token"]
    if RUNNER_TOKEN:
        return RUNNER_TOKEN
    if node_id == MASTER_ID:
        token_path = Path(FLEET_DIR).expanduser() / "runner.token" if FLEET_DIR else Path.home() / ".ai-fleet/runner.token"
        try:
            return token_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return ""


async def runner_call(node: dict, method: str, path: str, body: dict | None = None, timeout: float = 20) -> dict:
    token = node_runner_token(node)
    headers = {"X-Runner-Token": token} if token else {}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            r = await client.request(method, runner_url(node) + path, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise RunnerError(502, f"runner unreachable: {e.__class__.__name__}", transport=True)
    if r.is_error:
        detail = r.json().get("detail", r.text) if r.content else r.reason_phrase
        raise RunnerError(502 if r.status_code >= 500 else r.status_code, f"runner: {detail}")
    return r.json()


async def runner_health(client: httpx.AsyncClient, base: str, node: dict | str = MASTER_ID) -> dict | None:
    token = node_runner_token(node)
    headers = {"X-Runner-Token": token} if token else {}
    node_id = node if isinstance(node, str) else node["id"]
    if NODE_ID_RE.fullmatch(node_id or ""):
        # The runner learns its node id from us (docs/SPEC-live-monitor.md 12.1), for its signed heartbeats.
        headers["X-Fleet-Node-Id"] = node_id
    try:
        r = await client.get(f"{base}/health", timeout=3, headers=headers)
        r.raise_for_status()
        body = r.json()
        return clean_health(body) if isinstance(body, dict) and body.get("ok") else None
    except (httpx.HTTPError, ValueError):
        return None


NODE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
# The /health fields the controller keeps, whether polled or pushed in a heartbeat (live monitor 12.1): a
# heartbeat can claim nothing a poll couldn't, and unknown fields are dropped.
HEALTH_FIELDS = {"ok", "version", "digest", "power", "max_mode", "agents", "engines", "running", "gate_profiles",
                 "allow_raw_gates", "allowed_roots", "max_jobs", "models", "policy", "network", "budget",
                 "codex_profile", "claude_profile", "profiles", "checkpoint", "default_models", "metrics", "heartbeat",
                 "age_s", "codex_rate_limits", "capacity"}


MAX_HEALTH_JOBS = 32
JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
ACTIVITY_SOURCES = ("output", "files", "cpu", "session")


def _number_or_none(value, cap: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0:
        return None
    return float(min(value, cap))


def clean_jobs(value) -> list[dict] | None:
    """The runner's per-job activity (docs/SPEC-stall-reroute.md 12.8), parsed explicitly: at most 32 entries, only
    the contract's fields, wrong types become null. None when the value isn't a list (the field is dropped)."""
    if not isinstance(value, list):
        return None
    out = []
    for entry in value:
        if len(out) >= MAX_HEALTH_JOBS:
            break
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not JOB_ID_RE.fullmatch(entry["id"]):
            continue
        state = entry.get("state")
        out.append({"id": entry["id"],
                    "state": state if isinstance(state, str) and len(state) <= 16 else None,
                    "alive": entry["alive"] if isinstance(entry.get("alive"), bool) else None,
                    "gate_alive": entry["gate_alive"] if isinstance(entry.get("gate_alive"), bool) else None,
                    # computed on the runner's clock, so clock skew doesn't matter; capped at a week
                    "idle_s": _number_or_none(entry.get("idle_s"), 7 * 86400),
                    "activity_src": entry["activity_src"] if entry.get("activity_src") in ACTIVITY_SOURCES else None,
                    "cpu_s": _number_or_none(entry.get("cpu_s"), 1e9)})
    return out


def clean_health(body: dict) -> dict:
    out = {k: v for k, v in body.items() if k in HEALTH_FIELDS}
    if "codex_rate_limits" in out:
        out["codex_rate_limits"] = budget.clean_codex_rate_limits(out["codex_rate_limits"])
    if "capacity" in out:  # Part G 1: observed_at is on this controller's clock (the runner's age_s, not its time)
        out["capacity"] = capacity_usage.clean_capacity(out["capacity"], time.time())
    jobs = clean_jobs(body.get("jobs")) if "jobs" in body else None
    if jobs is not None:
        out["jobs"] = jobs
    return out


def merge_health(stored: dict, body: dict) -> dict:
    """A heartbeat's report over the stored one. A report without "jobs" (an older runner) removes the stored list,
    so stale job data never looks fresh (its verdict is "unknown")."""
    out = {**stored, **clean_health(body)}
    if "jobs" not in clean_health(body):
        out.pop("jobs", None)
    return out


def health_age(body: dict | None) -> float | None:
    """The runner's own age of its cached metrics (the body's age_s, or the metrics block's)."""
    if not isinstance(body, dict):
        return None
    for value in (body.get("age_s"), (body.get("metrics") or {}).get("age_s") if isinstance(body.get("metrics"), dict) else None):
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    return None


# ---------- scheduling ----------

def node_rows() -> list[dict]:
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM nodes")]
        running = dict(c.execute(
            "SELECT node_id, COUNT(*) FROM tasks WHERE state IN ('starting','working') GROUP BY node_id").fetchall())
    # A snapshot: sync endpoints call this from a worker thread while the loop adds and removes reservations.
    for _key, _account, node_id in list(budget.RESERVED.values()):  # admitted continuations hold the node's slot too
        running[node_id] = running.get(node_id, 0) + 1
    for n in rows:
        n.pop("runner_token", None)
        n["runner"] = json.loads(n["runner"]) if n.get("runner") else None
        n["models"] = json.loads(n["models"] or "[]")
        n["inventory"] = json.loads(n["inventory"]) if n.get("inventory") else None
        n["running_tasks"] = running.get(n["id"], 0)
    return rows


def ceiling(node: dict) -> str:
    """Effective mode ceiling: the stricter of the dashboard setting and the node's own runner config."""
    runner_max = (node["runner"] or {}).get("max_mode", "read-only")
    mine = node.get("ceiling") or DEFAULT_MODE
    return min(mine, runner_max if runner_max in MODES else "read-only", key=rank)


def eligible(node: dict, agent: str, model: str | None, mode: str, gate: str | None = None,
             policy: dict | None = None, escalation: bool = False, engine: str = "ollama") -> str | None:
    """None if the node can take the task, else the reason it can't.

    ``escalation`` marks a grounded-ask escalation task. It only ever adds a requirement: the agent must still be
    in the merged (fleet, node, role, agent, task) ``escalation.agents`` allow-list. Default is none.
    """
    if node["status"] != "approved":
        return "not approved"
    offline = runner_offline(node)
    if offline:
        return offline
    dmr = agent == "ollama" and engine == "dmr"
    dmr_status = (node["runner"].get("engines") or {}).get("dmr") or {}
    if dmr:  # DMR is reported only under engines, never as the ollama agent
        if dmr_status.get("reachable") is not True:
            return "no dmr engine on this node"
    elif not (node["runner"].get("agents") or {}).get(agent):
        return f"no {agent} on this node"
    power = node["runner"].get("power") or {}
    if (agent in ("claude", "codex") and power.get("on_battery") is True and power.get("ok") is False):
        pct = power.get("battery_pct")
        return f"on battery ({pct}%)" if isinstance(pct, int) else "on battery"
    if agent == "ollama":
        if engine not in ("ollama", "dmr"):
            return f"unknown engine {engine}"
        if engine == "dmr":
            engine_models = {item.get("reference") for item in dmr_status.get("models") or [] if isinstance(item, dict)}
            available = engine_models
        else:  # only the ollama engine may use the node's ollama model list
            available = set(node["models"])
        if model not in available:
            return f"model {model} not on this node's {engine} engine"
    runner = node["runner"]
    if (gate and str(runner.get("version")) == "2"
            and gate not in (runner.get("gate_profiles") or []) and not runner.get("allow_raw_gates")):
        return f"gate profile {gate} not on this node"
    if rank(mode) > rank(ceiling(node)):
        return f"mode {mode} is above this node's ceiling ({ceiling(node)})"
    policy_ceiling = (policy or {}).get("mode_ceiling")
    if policy_ceiling and rank(mode) > rank(policy_ceiling):
        return f"mode {mode} is above the effective policy ceiling ({policy_ceiling})"
    if escalation and agent not in ((policy or {}).get("escalation") or {}).get("agents", []):
        return f"node policy does not allow escalation to {agent}"
    return None


HEARTBEAT_TTL = float(os.environ.get("FLEET_HEARTBEAT_TTL", "25"))
NODE_POLL_SECONDS = float(os.environ.get("FLEET_POLL_SECONDS", "30"))  # the controller's node poll (controller.py)


def in_startup_grace(now: float | None = None) -> bool:
    import live  # local import: live builds on this module
    now = time.time() if now is None else now
    return now - live.STARTED_AT < HEARTBEAT_TTL + NODE_POLL_SECONDS


def runner_offline(node: dict, now: float | None = None, grace: bool | None = None) -> str | None:
    """The one liveness rule (docs/SPEC-live-monitor.md 12.2): why this node's runner is OFFLINE, or None.
    1. no runner report, or 3 failed polls in a row (every runner);
    2. heartbeat expiry: an accepted heartbeat, more than HEARTBEAT_TTL old, and the last good poll (or none) more
       than two poll intervals + 5 s old (a tick runs past its sleep, so a healthy poll can be older than one). Not applied during the controller's startup grace."""
    now = time.time() if now is None else now
    if not node.get("runner") or (node.get("runner_fails") or 0) >= 3:
        return "runner offline"
    beat = node.get("heartbeat_at")
    if beat is None:
        return None
    if grace is None:
        grace = in_startup_grace(now)
    if grace:
        return None
    polled = node.get("polled_at")
    if now - beat > HEARTBEAT_TTL and (polled is None or now - polled > 2 * NODE_POLL_SECONDS + 5):
        return f"runner offline: no heartbeat for {int(now - beat)} s"
    return None


def warm_models(node: dict, now: float | None = None) -> set[str]:
    """Ollama models loaded on the node per its latest /health (poll or heartbeat), only while that report is under
    60 s old by `now - max(heartbeat_at, polled_at) + runner_age_s` (12.3)."""
    now = time.time() if now is None else now
    seen = max([t for t in (node.get("heartbeat_at"), node.get("polled_at")) if t] or [0])
    if not seen or now - seen + (node.get("runner_age_s") or 0) >= 60:
        return set()
    metrics = (node.get("runner") or {}).get("metrics") or {}
    loaded = (metrics.get("ollama") or {}).get("loaded") if isinstance(metrics, dict) else None
    return {m for m in loaded or [] if isinstance(m, str)}


def warm_models_by_engine(node: dict, now: float | None = None) -> set[tuple[str, str]]:
    """Fresh, directly observed warm models, keeping engine identity in the routing key."""
    warm = {("ollama", model) for model in warm_models(node, now)}
    now = time.time() if now is None else now
    seen = max([t for t in (node.get("heartbeat_at"), node.get("polled_at")) if t] or [0])
    if not seen or now - seen + (node.get("runner_age_s") or 0) >= 60:
        return warm
    engines = (node.get("runner") or {}).get("engines") or {}
    for engine, status in engines.items():
        for item in (status or {}).get("models") or []:
            if isinstance(item, dict) and item.get("warm") is True and isinstance(item.get("reference"), str):
                warm.add((engine, item["reference"]))
    return warm


def ram(node: dict) -> int:
    return ((node["inventory"] or {}).get("hardware") or {}).get("ram_gb") or 0


def temporary_reason(node: dict, policy: dict) -> str | None:
    """Why a hard-eligible node cannot start this snapshot right now."""
    limits = policy.get("limits") or {}
    maximum = limits.get("max_concurrent")
    if maximum is not None and node["running_tasks"] >= maximum:
        return "max concurrent tasks reached"
    budget = limits.get("agent_minutes_per_day")
    if budget is not None:
        with db() as c:
            used = c.execute(
                "SELECT COALESCE(SUM(agent_seconds),0) FROM tasks WHERE node_id=? AND created_at>=?",
                (node["id"], time.time() - 86400)).fetchone()[0]
        if used >= budget * 60:
            return "daily agent budget used"
    if fleet_policy.in_quiet_hours(limits.get("quiet_hours") or [], local_minute()):
        return "quiet hours"
    return None


def stored_escalation_policy(node: str, role: str | None, agent: str, snapshot: dict, task_policy: dict | None) -> dict:
    """Policy used for the escalation check: a task's own layer may narrow escalation.agents but never grant it,
    so when the request carries a policy the check also needs the stored layers alone to opt in."""
    if task_policy is None:
        return snapshot["policy"]
    stored = effective_policy(node, role, agent, None)["policy"].get("escalation") or {}
    merged = snapshot["policy"].get("escalation") or {}
    allowed = [a for a in merged.get("agents", []) if a in stored.get("agents", [])]
    return snapshot["policy"] | {"escalation": {"agents": allowed}}


def _esc_view(node: dict, role, agent, snapshot, task_policy, escalation: bool) -> dict:
    return (stored_escalation_policy(node["id"], role, agent, snapshot, task_policy)
            if escalation else snapshot["policy"])


# ---------- slots: (node, agent, profile) places paid work can run (docs/SPEC-limit-handoff.md 3) ----------

LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def profile_support(node: dict) -> bool:
    """A runner that takes `profile`, `base_branch` and checkpoints (older runners get no handoffs)."""
    runner = node.get("runner") or {}
    return isinstance(runner.get("profiles"), dict) and bool(runner.get("checkpoint"))


def signed_out() -> dict[tuple[str, str, str], str]:
    """Slots marked "not signed in" after an auth failure, until the owner clears them."""
    with db() as c:
        return {(r["node"], r["agent"], r["profile"]): r["reason"] or ""
                for r in c.execute("SELECT * FROM slot_flags WHERE state='not_signed_in'")}


def node_slots(node: dict, agent: str, flags: dict | None = None) -> list[dict]:
    """This node's profiles for the agent, in its runner's order. A runner that reports none has one: `fleet`."""
    reported = (node.get("runner") or {}).get("profiles") if profile_support(node) else None
    entries = reported.get(agent) if isinstance(reported, dict) else None
    legacy = not isinstance(entries, list)
    if legacy:
        entries = [{"id": "fleet", "label": None, "ready": True}]
    flags = signed_out() if flags is None else flags
    out = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not PROFILE_RE.fullmatch(entry["id"]):
            continue
        label = entry.get("label") if isinstance(entry.get("label"), str) and LABEL_RE.fullmatch(entry["label"]) else None
        flagged = (node["id"], agent, entry["id"]) in flags
        out.append({"node": node["id"], "node_name": node.get("name"), "agent": agent, "profile": entry["id"],
                    "label": label, "ready": entry.get("ready") is not False, "signed_out": flagged,
                    "legacy": legacy, "account": budget.account_key(agent, node["id"], entry["id"], label)})
    return out


def usable(slot: dict) -> bool:
    return slot["ready"] and not slot["signed_out"]


def placement(agent: str, wanted: str | None) -> list[dict]:
    """Usable slots for this agent on eligible-looking nodes (the named node only, when one is named), least busy
    node first, then each node's own profile order."""
    nodes = [n for n in node_rows() if n["status"] == "approved" and n.get("runner")
             and ((n["runner"].get("agents") or {}).get(agent)) and runner_offline(n) is None]
    if wanted:
        nodes = [n for n in nodes if n["id"] == wanted or n["name"] == wanted]
    nodes.sort(key=lambda n: (n["running_tasks"], -ram(n)))
    flags = signed_out()
    return [s for n in nodes for s in node_slots(n, agent, flags) if usable(s)]


def accounts_of(slots: list[dict], agent: str) -> list[str]:
    return list(dict.fromkeys(s["account"] for s in slots if s["agent"] == agent))


def pick_node(agent: str, model: str | None, mode: str, wanted: str | None, gate: str | None = None,
              role: str | None = None, task_policy: dict | None = None,
              skill_ids: list[str] | None = None, escalation: bool = False,
              allowed: set[str] | None = None, engine: str = "ollama") -> tuple[dict, dict, str | None]:
    nodes = node_rows()
    if allowed is not None and not wanted:
        nodes = [n for n in nodes if n["id"] in allowed] or nodes
    if wanted:
        match = [n for n in nodes if n["id"] == wanted] or [n for n in nodes if n["name"] == wanted]
        if not match:
            raise HTTPException(404, f"unknown node {wanted}")
        if len(match) > 1:
            raise HTTPException(409, f"several nodes are named {wanted}; use an id: {[n['id'] for n in match]}")
        snapshot = effective_policy(match[0]["id"], role, agent, task_policy, skill_ids)
        why = eligible(match[0], agent, model, mode, gate,
                       _esc_view(match[0], role, agent, snapshot, task_policy, escalation), escalation, engine)
        if why:
            raise HTTPException(409, f"{wanted}: {why}")
        return match[0], snapshot, temporary_reason(match[0], snapshot["policy"])
    candidates = [(n, effective_policy(n["id"], role, agent, task_policy, skill_ids)) for n in nodes]
    ok = [(n, snapshot) for n, snapshot in candidates
          if not eligible(n, agent, model, mode, gate, _esc_view(n, role, agent, snapshot, task_policy, escalation),
                          escalation, engine)]
    if not ok:
        reasons = {n["name"]: eligible(n, agent, model, mode, gate,
                                       _esc_view(n, role, agent, snapshot, task_policy, escalation), escalation, engine)
                   for n, snapshot in candidates}
        raise HTTPException(409, f"no node can run {agent} in {mode} mode: {reasons}")
    # Prefer a node that can start now, then (Ollama) one with the model already loaded (live monitor 12.3), then
    # least busy, then the bigger machine.
    now = time.time()
    node, snapshot = sorted(ok, key=lambda item: (
        temporary_reason(item[0], item[1]["policy"]) is not None,
        agent == "ollama" and (engine, model) not in warm_models_by_engine(item[0], now),
        item[0]["running_tasks"], -ram(item[0])))[0]
    return node, snapshot, temporary_reason(node, snapshot["policy"])


# ---------- task helpers ----------

def get_task(task_id: str) -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"unknown task {task_id}")
    return task_view(row)


def task_view(row, ctx: "DisplayContext | None" = None) -> dict:
    t = dict(row)
    t["denials"] = json.loads(t["denials"] or "[]")
    t["approved_tools"] = json.loads(t.get("approved_tools") or "[]")
    t["gate_result"] = json.loads(t["gate_result"]) if t["gate_result"] else None
    t["policy"] = json.loads(t.get("policy") or "{}")
    t["skills"] = json.loads(t.get("skills") or "[]")
    # A malformed stored value reads as "set, but unreadable": truthy, so the task still counts as settled.
    for field in ("resolution", "outcome"):
        value = lineage_json(t[field]) if t.get(field) else None
        t[field] = value if value is not None or not t.get(field) else {"kind": "unknown", "raw": t[field]}
    t["hidden"] = bool(t.get("hidden"))
    for field, fallback in (("usage", None), ("prompt_omitted", []), ("launch_extra", {}), ("limits_applied", None),
                            ("independence", None), ("placement", None), ("chain", None), ("stall", None)):
        try:
            t[field] = json.loads(t[field]) if t.get(field) else fallback
        except (TypeError, ValueError):
            t[field] = fallback
    derive_reason(t)
    t["display"] = live_display(t, ctx=ctx)
    return t


class DisplayContext:
    """What live_display needs from the database, loaded once per request and shared by every row: the work-item
    tree (two queries: tasks and limit runs), then limit snapshots per account on demand. The count of queries does
    not depend on how many rows are shown (docs/SPEC-dashboard-truth.md 5, batch performance)."""

    def __init__(self, now: float | None = None):
        self.now = time.time() if now is None else now
        with db() as c:
            rows = c.execute("SELECT id,parent_id,link,state,resolution,outcome,hidden,created_at,rowid AS rid FROM tasks").fetchall()
            self.limit_tasks = {r[0] for r in c.execute("SELECT DISTINCT task_id FROM agent_usage WHERE outcome='limit'")}
        self.items = {r["id"]: dict(r) for r in rows}
        self.retry_child: dict[str, str] = {}
        for r in sorted(rows, key=lambda r: (r["created_at"] or 0, r["rid"])):
            if r["link"] == "retry" and r["parent_id"]:
                self.retry_child[r["parent_id"]] = r["id"]  # the newest retry child wins
        parents = {r["id"]: r["parent_id"] for r in rows}
        self.roots = {i: root_in(parents, i) for i in parents}
        self._by_root: dict[str, dict[str, dict]] = {}
        for i, item in self.items.items():
            self._by_root.setdefault(self.roots[i], {})[i] = item
        self._rules: dict | None = None
        self._snapshots: dict[str, dict] = {}

    def hidden(self, task_id: str) -> bool:
        """The task, or the root of its work item, is hidden."""
        root = self.items.get(self.roots.get(task_id, task_id))
        return bool((self.items.get(task_id) or {}).get("hidden") or (root and root.get("hidden")))

    def root_outcome(self, task_id: str) -> dict | None:
        root = self.items.get(self.roots.get(task_id, task_id))
        value = lineage_json(root["outcome"]) if root else None
        return value if isinstance(value, dict) else None

    def superseded_by(self, t: dict) -> str | None:
        """Row 5: the first later successful attempt in the same work item that follows on from `t`."""
        group = self._by_root.get(self.roots.get(t["id"], t["id"]), {})
        me = group.get(t["id"])
        if not me:
            return None
        for item in sorted(group.values(), key=lambda r: (r["created_at"] or 0, r["rid"])):
            if item["state"] in ("review", "done") and (item["created_at"] or 0, item["rid"]) > (me["created_at"] or 0, me["rid"]) \
                    and t["id"] in resolution_targets(item, group):
                return item["id"]
        return None

    def limit_until(self, t: dict) -> float | None:
        if self._rules is None:
            self._rules = budget.current_rules()
        account = budget.task_account(t)
        if account not in self._snapshots:
            self._snapshots[account] = budget.limit_snapshot(self._rules, self.now, account)
        ident = t.get("budget_identity") or budget.identity(t["agent"], t.get("model"))
        return budget.limited_for(self._snapshots[account], t["agent"], ident, self._rules)[0]


def live_display(t: dict, now: float | None = None, ctx: DisplayContext | None = None) -> dict:
    """The one place a task's label and tone are derived (docs/SPEC-dashboard-truth.md 2.1). display_status holds the
    rows that need only the task; this adds what only the database knows: a merged work item (row 1), a later
    successful attempt (row 5), a retry child (row 6) and the account's current limit state (rows 9-10).
    Pass one `ctx` for a batch of rows."""
    now = time.time() if now is None else now
    shown = display_status(t, now=now)
    state = t.get("state")
    ctx = ctx or DisplayContext(now)
    # Row 1 wins for every state: a finished attempt of a merged work item reads Merged too. A running attempt
    # is still running.
    if state not in RUNNING:
        merged = ctx.root_outcome(t["id"])
        if merged and merged.get("kind") == "merged":
            return outcome_display(merged)
    if state not in ("failed", "interrupted", "blocked"):
        return shown  # running, review, done, awaiting_approval, cancelled: no failure to rescue
    if t.get("resolution"):
        return shown
    later = ctx.superseded_by(t)
    if later:
        return {"tone": "muted", "label": f"Superseded by {later}"}
    child = ctx.retry_child.get(t["id"])
    if child:
        return {"tone": "resolved", "label": f"Retried as {child}"}
    own_limit = (t.get("failure_reason") == "limit" and t.get("agent") in budget.PAID_AGENTS and t["id"] in ctx.limit_tasks)
    if not own_limit or (t.get("resets_at") is not None and t["resets_at"] <= now):
        return shown  # no provider limit on record for this run, or its reset has passed: as before
    until = ctx.limit_until(t)
    if until:
        return {"tone": "paused", "label": f"Paused: limit, resets {clock(until)}"}
    return {"tone": "attention", "label": "Limit cleared: resume or hand off"}


def derive_reason(t: dict) -> dict:
    """Read-path only: a failed task stored before lineage has no failure_reason; work it out from its text.
    The database is not rewritten."""
    if t.get("state") in FAILURE_STATES and not t.get("failure_reason"):
        t["failure_reason"] = failure_reason(t)
        if t["failure_reason"] == "limit" and t.get("resets_at") is None:
            ended = t.get("ended_at") or t.get("updated_at")
            if ended:
                t["resets_at"] = parse_reset(f"{tail(t.get('result'))}\n{tail(t.get('error'))}", ended)
    return t


def with_root(task: dict) -> dict:
    with db() as c:
        task["root_id"] = task_root(c, task["id"])
    return task


def approvable_tools(task: dict, tools) -> list[str]:
    """Return a stable tool list without anything the task's policy permanently denies."""
    denied = set((((task.get("policy") or {}).get("tools") or {}).get("claude_deny") or []))
    return sorted({tool for tool in tools if isinstance(tool, str) and tool and tool not in denied})


def denied_tool_names(task: dict) -> list[str]:
    return approvable_tools(task, (denial.get("tool_name") or denial.get("tool")
                                   for denial in task["denials"]))


def node_of(task: dict) -> dict:
    for n in node_rows():
        if n["id"] == task["node_id"]:
            return n
    raise HTTPException(409, f"task node {task['node_id']} is gone")


def update(task_id: str, **fields) -> None:
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))


def transition(task: dict, expected: set[str], state: str, *, where_node: str | None = None, quiet: bool = False,
               **fields) -> bool:
    """Move the task to `state` only if it is still in one of `expected` (and, with `where_node`, still on that
    node: a start's lease, docs/SPEC-live-monitor.md 12.5). Returns whether it moved."""
    now = time.time()
    if state == "working":
        fields.setdefault("run_started_at", now)
    elif state in SETTLED:
        if task["state"] in ACTIVE:
            fields.setdefault("ended_at", now)
        if task.get("run_started_at") and "agent_seconds" not in fields:
            # Agent time counts only while a run is going, not while a human reviews.
            fields.update(close_run(task, now))
    if state in FAILURE_STATES:
        merged = {**task, **fields}
        reason = failure_reason(merged, None, state)
        fields["failure_reason"] = reason
        fields["resets_at"] = None
        if reason == "limit":
            text = f"{tail(merged.get('result'))}\n{tail(merged.get('error'))}"
            fields["resets_at"] = parse_reset(text, merged.get("ended_at") or now)
    else:
        fields.setdefault("failure_reason", None)
        fields.setdefault("resets_at", None)
    fields["updated_at"] = now
    cols = ", ".join(f"{k}=?" for k in fields)
    marks = ",".join("?" * len(expected))
    with db() as c:
        node_sql, node_args = (" AND node_id=?", (where_node,)) if where_node is not None else ("", ())
        moved = c.execute(f"UPDATE tasks SET state=?, {cols} WHERE id=? AND state IN ({marks}){node_sql}",
                          (state, *fields.values(), task["id"], *expected, *node_args)).rowcount == 1
    if moved and state != task["state"] and not quiet:  # quiet: the caller emits its own event (a stall nudge)
        emit(task["id"], f"task.{state}", {"title": task["title"], "from": task["state"], "node": task["node_id"],
                                           "agent": task["agent"]})
    if moved and state in ("review", "done"):
        auto_resolve(task["id"])
    return moved


def lineage_rows(c, root_id: str) -> dict[str, dict]:
    """Every task in the work item rooted at `root_id` (the root and all its descendants), keyed by id."""
    items: dict[str, dict] = {}
    frontier = [root_id]
    while frontier:
        marks = ",".join("?" * len(frontier))
        rows = c.execute(f"SELECT id,parent_id,link,title,state,resolution FROM tasks WHERE id IN ({marks}) "
                         f"OR parent_id IN ({marks})", (*frontier, *frontier)).fetchall()
        frontier = []
        for row in rows:
            if row["id"] not in items:
                items[row["id"]] = dict(row)
                frontier.append(row["id"])
    return items


def auto_resolve(task_id: str) -> None:
    """A task just ended well: mark the earlier failed attempts it supersedes as resolved by it."""
    with db() as c:
        items = lineage_rows(c, task_root(c, task_id))
        success = items.get(task_id)
        targets = resolution_targets(success, items) if success else []
        blob = json.dumps({"kind": "resolved_by", "task_id": task_id, "title": success["title"] if success else None,
                           "at": time.time()})
        targets = [t for t in targets
                   if c.execute("UPDATE tasks SET resolution=? WHERE id=? AND resolution IS NULL", (blob, t)).rowcount]
    for target in targets:
        emit(target, "task.resolved", {"title": items[target]["title"], "kind": "resolved_by", "by_task": task_id})


def close_run(task: dict, end: float) -> dict:
    """Fields that add the current run's duration to agent_seconds and mark the run closed."""
    start = task.get("run_started_at")
    return {"agent_seconds": (task.get("agent_seconds") or 0) + max(0, end - start), "run_started_at": None}


def iso_epoch(value) -> float | None:
    try:
        return datetime.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def job_fields(job: dict) -> dict:
    # The runner's own error line (a Codex usage-limit message, say) is kept for a run that has ended, so the
    # failure reason and reset time can be read from it; a run in progress has none.
    error = job.get("error") if isinstance(job.get("error"), str) and job.get("state") != "running" else None
    fields = {"session_id": job.get("session_id"), "result": job.get("result"),
            "denials": json.dumps(job.get("denials") or []), "exit_code": job.get("exit_code"),
            "gate_result": json.dumps(job["gate"]) if job.get("gate") else None,
            "branch": job.get("branch"), "cwd": job.get("cwd"), "error": error or None,
            # what the agent CLI reported for this run (runner 2.1); null when it reported nothing
            "usage": json.dumps(job["usage"]) if isinstance(job.get("usage"), dict) else None,
            "num_turns": job.get("num_turns") if isinstance(job.get("num_turns"), int) else None,
            "limits_applied": json.dumps(job["limits_applied"]) if isinstance(job.get("limits_applied"), dict) else None}
    # Which profile and account ran it, when the runner says (otherwise the stored default stays).
    if isinstance(job.get("profile"), str) and job["profile"]:
        fields["profile"] = job["profile"][:64]
    if isinstance(job.get("account_label"), str) and job["account_label"]:
        fields["account_label"] = job["account_label"][:64]
    return fields


def budget_support(node: dict) -> bool:
    """Runners that understand effort/tools/max_turns and reviewing a build worktree read-only."""
    return bool((node.get("runner") or {}).get("budget"))


async def start(task: dict) -> None:
    """Start (or adopt) the runner job. Caller holds the task lock and has moved it to 'starting'."""
    node = node_of(task)
    policy = task["policy"]
    runner_policy = {name: policy[name] for name in ("filesystem", "tools", "network") if name in policy}
    if "task_minutes" in (policy.get("limits") or {}):
        runner_policy["limits"] = {"task_minutes": policy["limits"]["task_minutes"]}
    body = {"id": task["id"], "agent": task["agent"], "prompt": task["prompt"], "mode": task["mode"],
            "repo": task["repo"], "worktree": bool(task["worktree"]), "cwd": None, "model": task["model"],
            "gate": task["gate"], "allowed_tools": list(task.get("approved_tools") or []), "resume_session": None,
            "instructions": task["instructions"], "policy": runner_policy, "policy_hash": task["policy_hash"]}
    if task["agent"] == "ollama" and (task.get("engine") or "ollama") != "ollama":
        body["engine"] = task["engine"]  # omitted for plain ollama: older runners reject unknown job fields
    support = budget_support(node)
    extra = task.get("launch_extra") or {}
    if profile_support(node) and task["agent"] in budget.PAID_AGENTS:
        # Only the profile id: the runner maps it to a directory from its own config.
        if task.get("profile"):
            body["profile"] = task["profile"]
        if extra.get("base_branch"):
            body.update(base_branch=extra["base_branch"], source_job=extra.get("source_job"))
    if support:
        if task.get("kind"):
            body["kind"] = task["kind"]  # picks the runner's lean profile and caps (Part H)
        if task.get("effort"):
            body["effort"] = task["effort"]
        if isinstance(extra.get("tools"), list):  # an empty list is "no tools", not "unrestricted"
            body["tools"] = extra["tools"]
        if extra.get("max_turns"):
            body["max_turns"] = extra["max_turns"]
        if task.get("review_of"):
            # Review the build's worktree itself (read-only), so any file the reviewer opens is current.
            source = get_task(task["review_of"])
            if source.get("cwd"):
                body.update(repo=None, cwd=source["cwd"], worktree=False)
    try:
        job = await runner_call(node, "POST", "/jobs", body)
    except RunnerError as e:
        if e.status_code == 409:  # the runner already has it (e.g. we crashed mid-start): adopt it
            job = await runner_call(node, "GET", f"/jobs/{task['id']}")
        elif e.status_code == 502:
            update(task["id"], error=str(e.detail))  # stays 'starting'; recover() retries it
            return
        else:
            transition(task, {"starting"}, "failed", error=f"could not start: {e.detail}")
            return
    applied = {}
    if not support:
        applied["effort"] = None  # this runner can't pass effort to the CLI: record that, never fake it
    elif isinstance(job.get("limits_applied"), dict):
        applied["effort"] = job["limits_applied"].get("effort")
    transition(task, {"starting"}, "working", started_at=time.time(), runs=max(task["runs"] or 0, 1),
               **job_fields(job), **applied)


async def try_start(task: dict) -> bool:
    """Start one queued task if its stored snapshot currently admits it."""
    if task["state"] != "queued":
        return False
    node = node_of(task)
    why = eligible(node, task["agent"], task["model"], task["mode"], task["gate"], task["policy"],
                   engine=task.get("engine") or "ollama")
    why = why or temporary_reason(node, task["policy"])
    wait = None
    held_until = task.get("not_before")  # Part G req 4: a limit failure waits for its reset
    if not why and held_until and capacity_usage.now() < held_until:
        wait, why = "limit", task.get("error") or f"waiting for the usage limit reset at {budget.clock(held_until)}"
    if not why:
        wait, why = budget.start_wait(task, capacity_usage.now())
    if why:
        fields = {}
        if task.get("error") != why:
            fields["error"] = why
        if task.get("budget_wait") != wait:  # only a task waiting on the budget holds a place in its FIFO
            fields["budget_wait"] = wait
        if fields:
            update(task["id"], **fields)
        return False
    if held_until and (task.get("runs") or 0) >= 1:  # a re-queued limit failure continues its own session
        import capacity
        return await capacity.restart_requeued(task)
    # The lease: claimed only on the node this check was for; a re-placement in between wins (12.5).
    if not transition(task, {"queued"}, "starting", where_node=task["node_id"], error=None, budget_wait=None):
        return False
    await start(get_task(task["id"]))
    return True


async def recover() -> None:
    """After a controller restart: finish starting tasks that were mid-start or never started, and release
    handoff claims a crash left behind (those tasks wait for their reset instead)."""
    import handoff  # local import: handoff builds on this module
    await handoff.recover_claims()
    with db() as c:
        pending = [r[0] for r in c.execute("SELECT id FROM tasks WHERE state IN ('queued','starting')")]

    async def one(task_id: str) -> None:
        async with _locks[task_id]:
            task = get_task(task_id)
            if task["state"] == "queued":
                await try_start(task)
                return
            if get_task(task_id)["state"] == "starting":
                await start(get_task(task_id))

    await asyncio.gather(*(one(i) for i in pending), return_exceptions=True)


# ---------- polling runners ----------

def outcome(task: dict, job: dict) -> str:
    """Map a runner job onto a task state."""
    state = job.get("state")
    if state == "running":
        return "working"
    if state == "cancelled":
        return "cancelled"
    if state == "capped":
        return "capped"  # hit its turn/context cap (Part H 3.2): settled, with a handoff summary; not a failure
    if state == "lost":
        return "interrupted"
    if job.get("denials") and task["mode"] != "read-only":
        return "blocked"
    if job.get("exit_code") != 0:
        return "failed"
    if not task["gate"]:
        return "review"
    gate = (job.get("gate") or {}).get("state")
    if gate in ("pending", "running"):
        return "working"
    return "review" if gate == "passed" else "failed"  # a requested gate must actually pass


async def poll_task(task_id: str) -> None:
    settled = await _poll_task(task_id)
    if settled:
        # Outside the task lock: a handoff takes the work-item lock first (lock order: pipeline, work item, task).
        import handoff
        await handoff.after_settled(task_id)


# Consecutive GET /jobs/{id} polls per working task that the runner answered with an HTTP error
# (docs/SPEC-stall-reroute.md 3: 3 in a row while the runner's /health still answers is an "unreachable" job).
# Transport errors (the runner restarting, the network) never count. UNREACHABLE_SINCE holds when the run of
# failures began: only a /health report newer than that proves the runner itself is up. In memory: a restart
# starts counting again.
UNREACHABLE_POLLS: dict[str, int] = {}
UNREACHABLE_SINCE: dict[str, float] = {}


def clear_unreachable(task_id: str) -> None:
    UNREACHABLE_POLLS.pop(task_id, None)
    UNREACHABLE_SINCE.pop(task_id, None)


async def _poll_task(task_id: str) -> bool:
    """Poll one working task; True when it just settled as failed or interrupted (both may be handed on)."""
    async with _locks[task_id]:
        task = get_task(task_id)
        if task["state"] != "working":
            clear_unreachable(task_id)
            return False
        try:
            job = await runner_call(node_of(task), "GET", f"/jobs/{task_id}", timeout=5)
        except HTTPException as e:
            if getattr(e, "transport", False):
                clear_unreachable(task_id)  # the runner itself is unreachable: Part D's concern, not this job's
            else:
                UNREACHABLE_SINCE.setdefault(task_id, time.time())
                UNREACHABLE_POLLS[task_id] = UNREACHABLE_POLLS.get(task_id, 0) + 1
            if not task["error"]:
                update(task_id, error=str(e.detail))
                emit(task_id, "task.unreachable", {"title": task["title"], "node": task["node_id"]})
            return False
        clear_unreachable(task_id)
        new = outcome(task, job)
        fields = job_fields(job)
        agent_end = iso_epoch(job.get("ended_at"))
        if agent_end and task.get("run_started_at"):
            fields.update(close_run(task, agent_end))  # the gate's run time isn't agent time
        if new == "working":
            update(task_id, **fields)
        elif transition(task, {"working"}, new, **fields):
            record_usage(task_id)
            if new == "blocked":
                await auto_approve_denials(get_task(task_id))
            return new in ("failed", "interrupted")
    return False


def record_usage(task_id: str, outcome_name: str | None = None) -> None:
    """One agent_usage row per settled run: the tokens the CLI reported (null when it reported none). `outcome_name`
    overrides the derived outcome (a stall nudge's run is "nudged", not an error)."""
    forced = outcome_name
    t = get_task(task_id)
    if not t.get("started_at"):
        return  # never ran
    usage = t.get("usage") or {}
    if t["state"] in ("review", "done"):
        outcome_name = "ok"
    elif t.get("failure_reason") == "limit" and provider_limit(t):
        outcome_name = "limit"  # only the provider's own message counts, never a report that discusses limits
    elif t["state"] == "cancelled":
        outcome_name = "cancelled"
    else:
        outcome_name = "error"
    outcome_name = forced or outcome_name
    run = t.get("runs") or 1
    prompt_bytes = t.get("prompt_bytes") if run <= 1 else t.get("run_prompt_bytes")
    row = {"ts": time.time(), "task_id": task_id, "run": run, "agent": t["agent"],
           "model": usage.get("model") or t.get("model"), "kind": t.get("kind"), "input": usage.get("input"),
           "output": usage.get("output"), "cached": usage.get("cached"), "cost_usd": usage.get("cost_usd"),
           "prompt_bytes": prompt_bytes, "effort": t.get("effort"), "outcome": outcome_name,
           "num_turns": t.get("num_turns"), "identity": t.get("budget_identity") or budget.identity(t["agent"], t.get("model")),
           "resets_at": t.get("resets_at") if outcome_name == "limit" else None,
           "account": budget.task_account(t) if t["agent"] in budget.PAID_AGENTS else None,
           "account_label": t.get("account_label") if t["agent"] in budget.PAID_AGENTS else None}
    with db() as c:
        inserted = c.execute(f"INSERT OR IGNORE INTO agent_usage ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                             tuple(row.values())).rowcount
    if inserted and outcome_name == "limit":
        # The provider's own limit message (docs/SPEC-live-monitor.md 4.2 alerts).
        emit(task_id, "budget.limit", {"agent": t["agent"], "model": row["model"], "account": row["account"],
                                       "label": row["account_label"], "resets_at": row["resets_at"],
                                       "title": t.get("title")})
    if inserted and t.get("failure_reason") == "limit" and outcome_name != "limit":
        # Shown as a limit but not the provider's own message: say so once per run, so a new wording can be added.
        text = f"{tail(t.get('result'))}\n{tail(t.get('error'))}".strip()
        emit(task_id, "budget.limit_unrecognised", {"agent": t["agent"], "model": row["model"], "run": run,
                                                    "text": redact_secrets(text)[:200]})


_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|xox[a-z])[-_][A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization)(\s*[=:]\s*)\S+"),
    re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}"),
)


def redact_secrets(text: str) -> str:
    """Text with anything key-like replaced (the runner's error_line patterns), whitespace collapsed."""
    text = " ".join(str(text or "").split())
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + m.group(2) + "[redacted]") if m.lastindex == 2 else "[redacted]",
                           text)
    return text


AUTO_APPROVE_ROUNDS = 3


async def auto_approve_denials(task: dict) -> None:
    """If the task's policy pre-approves denied tools for its agent, resume with them (a few rounds at most)."""
    auto = (task.get("policy") or {}).get("auto_approve") or {}
    if not (auto.get("denied_tools") and task["agent"] in (auto.get("agents") or [])):
        return
    tools = denied_tool_names(task)
    if not tools:
        return
    if (task.get("auto_approvals") or 0) >= AUTO_APPROVE_ROUNDS:
        emit(task["id"], "task.auto_approve_exhausted", {"title": task["title"], "rounds": AUTO_APPROVE_ROUNDS})
        return
    rounds = task.get("auto_approvals") or 0
    update(task["id"], auto_approvals=rounds + 1)
    try:
        await resume(get_task(task["id"]), "Policy pre-approves the tools you were denied: " + ", ".join(tools)
                     + ". Continue the task.", tools, auto_approved=True)
    except NotAdmitted as exc:
        # Admission deferred or refused it: the task stays blocked for the owner and the round isn't spent.
        update(task["id"], auto_approvals=rounds, error=str(exc.detail))
        emit(task["id"], "task.auto_approve_held", {"title": task["title"], "tools": tools,
                                                    "reason": str(exc.detail)})
        return
    emit(task["id"], "task.auto_approved", {"title": task["title"], "tools": tools, "agent": task["agent"]})


async def poll_loop() -> None:
    recovering: asyncio.Task | None = None
    while True:
        try:
            # Recovery retries can wait on unreachable runners; run them beside polling, one round at a time.
            if recovering is None or recovering.done():
                recovering = asyncio.create_task(recover())
            with db() as c:
                ids = [r[0] for r in c.execute("SELECT id FROM tasks WHERE state='working'")]
                finished = {r[0] for r in c.execute("SELECT id FROM tasks WHERE state IN ('done','cancelled')")}
            await asyncio.gather(*(poll_task(i) for i in ids), return_exceptions=True)
            import capacity
            await capacity.tick()
            import leader
            await leader.tick()
            for task_id in [i for i in list(_locks) if i in finished and not _locks[i].locked()]:
                _locks.pop(task_id, None)
        except Exception as e:  # keep polling whatever happens
            print(f"task poll failed: {e!r}")
        await asyncio.sleep(POLL_SECONDS)


# ---------- API ----------

REPLACE_REASONS = ("retry", "handoff", "reroute", "requeue")


class TaskIn(BaseModel):
    agent: str
    prompt: str
    title: str | None = None
    model: str | None = None
    engine: str | None = None
    mode: str = DEFAULT_MODE
    repo: str | None = None
    worktree: bool = True
    gate: str | None = None
    node: str | None = None
    parent_id: str | None = None
    link: str | None = None  # why this task exists relative to parent_id: retry, resume, review, fix, merge, check
    replaces: str | None = None  # a task this one re-queues: parent_id/link=retry are set from it, it is handed_off
    link_reason: str | None = None  # with replaces: retry, handoff, reroute or requeue (default requeue)
    role: str | None = None
    policy: dict | None = None
    skills: list[str] = Field(default_factory=list)
    escalation: bool = False  # grounded-ask escalation: node selection also requires escalation.agents to allow it
    trusted_bypass: bool = False  # Claude/Codex: run in full mode without permission prompts
    # Paid-model budget (docs/SPEC-paid-budget.md 3.1): what the task is for, and how hard the agent should think.
    kind: str | None = None  # default: build with a gate, otherwise draft
    effort: str | None = None  # low, medium or high; default from the budget rules
    budget_override: str | None = None  # admin only: why the kind check is bypassed (recorded)
    review_of: str | None = None  # read-only review run inside that task's worktree
    origin_kind: str | None = None  # a re_review's originating review kind (review, final_review, security_review)
    # Live monitor (docs/SPEC-live-monitor.md 2.1, 3.2, 6): pin a runner profile (its account), a display-only
    # stage, and an advisory independence waiver ("waived" + a reason) for an ad-hoc review or test.
    profile: str | None = None
    stage: str | None = None
    independence: str | None = None
    independence_reason: str | None = None
    pin: bool = False  # Part G: never move this queued task to another account or model

    @field_validator("prompt")
    @classmethod
    def prompt_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("prompt is empty")
        return v


class MessageIn(BaseModel):
    text: str


class ModeIn(BaseModel):
    mode: str


def local_engine(agent: str, model: str | None, role: str | None, node: str | None,
                 requested: str | None) -> str | None:
    if agent != "ollama":
        if requested is not None:
            raise HTTPException(400, "engine is only valid for ollama tasks")
        return None
    if requested is not None and requested not in ("ollama", "dmr"):
        raise HTTPException(400, f"unknown engine {requested}")
    candidates = []
    with db() as c:
        if role:
            query = ("SELECT DISTINCT a.engine FROM assignments a LEFT JOIN nodes n ON n.id=a.node "
                     "WHERE a.role=? AND a.state='active'")
            args = [role]
            if model:
                query += " AND a.model=?"
                args.append(model)
            if node:
                query += " AND (a.node=? OR n.name=?)"
                args.extend((node, node))
            candidates = [row[0] or "ollama" for row in c.execute(query, args)]
        if not candidates and model:
            candidates = [row[0] or "ollama" for row in c.execute(
                "SELECT DISTINCT engine FROM model_catalog WHERE reference=?", (model,))]
    candidates = list(dict.fromkeys(candidates))
    if requested:
        if role and candidates and requested not in candidates:
            raise HTTPException(400, f"role {role} is assigned to {candidates}, not {requested}")
        return requested
    # Omitted engine means ollama (SPEC-networking §14.1), so a same-named dmr model never changes old callers.
    if len(candidates) > 1 and "ollama" not in candidates:
        raise HTTPException(400, f"model {model} exists on several engines; specify engine")
    return "ollama" if "ollama" in candidates or not candidates else candidates[0]


@router.get("/api/tasks")
def list_tasks(state: str | None = None, parent_id: str | None = None, limit: int = 200) -> list[dict]:
    q, args = "SELECT * FROM tasks WHERE 1=1", []
    if state:
        q += f" AND state IN ({','.join('?' * len(state.split(',')))})"
        args += state.split(",")
    if parent_id:
        q += " AND parent_id=?"
        args.append(parent_id)
    ctx = DisplayContext()  # one load for every row's display
    with db() as c:
        views = [task_view(r, ctx) for r in c.execute(q + " ORDER BY created_at DESC LIMIT ?", (*args, limit))]
        parents = {r["id"]: r["parent_id"] for r in c.execute("SELECT id,parent_id FROM tasks")}
    for view in views:
        view["root_id"] = root_in(parents, view["id"])
    return views


def root_in(parents: dict, task_id: str) -> str:
    seen = {task_id}
    while parents.get(task_id) in parents and parents[task_id] not in seen:
        task_id = parents[task_id]
        seen.add(task_id)
    return task_id


def gate_failures(parent_id: str | None) -> int:
    """Gate rounds failed so far in the work item `parent_id` belongs to (failed attempts and resumed runs)."""
    if not parent_id:
        return 0
    with db() as c:
        ids = list(lineage_rows(c, task_root(c, parent_id)))
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        failed = c.execute(f"SELECT COUNT(*) FROM tasks WHERE id IN ({marks}) AND failure_reason='gate_failed'",
                           ids).fetchone()[0]
        resumed = c.execute(f"SELECT data FROM events WHERE type='task.resumed' AND task_id IN ({marks})", ids).fetchall()
    return failed + sum(1 for r in resumed if (json.loads(r["data"] or "{}") or {}).get("failure_reason") == "gate_failed")


def launch_tools(agent: str, policy: dict, mode: str | None = None) -> list[str]:
    """Tools a stored policy pre-approves, granted at launch instead of after a denial (SPEC-paid-budget 3.8).
    Only when the policy would auto-approve denied tools for this agent anyway; claude_deny still wins.
    A read-only task never had them, so it gets none."""
    if mode == "read-only":
        return []
    auto = policy.get("auto_approve") or {}
    if agent not in (auto.get("agents") or []) or auto.get("denied_tools") is not True:
        return []
    return approvable_tools({"policy": policy}, auto.get("tools") or [])


@router.post("/api/tasks")
async def create_task_route(t: TaskIn, request: Request) -> dict:
    return await create_task(t, scope=getattr(request.state, "scope", "admin"))


STAGE_RE = re.compile(r"^[a-z0-9-]{1,24}$")
INDEPENDENT_KINDS = budget.REVIEW_KINDS | {"test"}


def profile_slots(agent: str, profile: str, wanted_node: str | None) -> list[dict]:
    """Usable slots of one pinned profile; 400 when no node has it, or none has it ready and signed in."""
    usable_slots = [s for s in placement(agent, wanted_node) if s["profile"] == profile]
    if usable_slots:
        return usable_slots
    flags = signed_out()
    known = [s for n in node_rows() if n["status"] == "approved" and n.get("runner")
             for s in node_slots(n, agent, flags) if s["profile"] == profile]
    if not known:
        raise HTTPException(400, f"unknown {agent} profile {profile}")
    raise HTTPException(400, f"{agent} profile {profile} isn't ready or isn't signed in")


def check_independence(t: "TaskIn", kind: str, agent: str, model: str | None, node: dict, pipeline: bool,
                       profile: str | None = "fleet") -> dict | None:
    """Ad-hoc review/test independence (docs/SPEC-live-monitor.md 6), after admission, on the resolved model. Raises
    400 on a match with a recorded builder, or an unresolvable model, unless waived; returns the waiver or None.
    The runner's reported default describes its fleet profile only: with no model named on any other profile the
    model can't be resolved."""
    if pipeline or kind not in INDEPENDENT_KINDS or not t.parent_id:
        return None
    if t.independence == "waived":
        import redaction  # local import: keeps this module's imports as they were
        reason = " ".join(redaction.redact(t.independence_reason or "", strict=True).split())
        return {"waived": True, "reason": reason[:300]}
    import handoff  # local import: handoff builds on this module
    resolved = model or (budget.node_default_model(agent, node["id"])
                         if agent in budget.PAID_AGENTS and (profile or "fleet") == "fleet" else None)
    if not resolved:
        raise HTTPException(400, f"independence: can't tell which {agent} model this {kind} would run, so it can't be "
                                 "checked against the builders; name a model, or set independence 'waived' with a reason")
    if (agent, resolved) in handoff.build_attempts(t.parent_id):
        raise HTTPException(400, f"independence: {agent}:{resolved} built this work, so it can't {kind} it; use another "
                                 "model, or set independence 'waived' with a reason (shown to the owner)")
    return None


def allocate(t: "TaskIn") -> None:
    """agent="auto" (docs/SPEC-stall-reroute.md 12.12): the best slot from handoff.rank_slots (the one ranking,
    shared with stall reroutes) fills in agent, model and profile; admission then runs on it as for any task. An
    explicit model or profile isn't allowed with "auto": name a real agent for that."""
    import handoff  # local import: handoff builds on this module
    if t.model or t.profile:
        raise HTTPException(400, "agent 'auto' lets the controller pick the model and account: leave model and "
                                 "profile out, or name the agent")
    kind = t.kind or ("build" if t.gate else "draft")
    if kind not in budget.KINDS:
        raise HTTPException(400, f"kind must be one of {list(budget.KINDS)}")
    node_id = None
    if t.review_of:
        with db() as c:
            row = c.execute("SELECT node_id FROM tasks WHERE id=?", (t.review_of,)).fetchone()
        node_id = row["node_id"] if row else None
    elif t.node:
        found = [n["id"] for n in node_rows() if n["id"] == t.node or n.get("name") == t.node]
        node_id = found[0] if len(found) == 1 else t.node
    choices = handoff.rank_slots(kind, (), {"id": t.parent_id or t.review_of, "node_id": node_id})
    if not choices:
        raise HTTPException(409, f"no free slot can take a {kind} task now")
    slot = choices[0]
    t.agent, t.model, t.profile = slot["agent"], slot["model"], slot["profile"]


async def create_task(t: TaskIn, scope: str = "admin", *, forbidden: set | None = None,
                      pipeline: bool = False) -> dict:
    """Create (and try to start) a task. `scope` is the caller's access; in-process callers are the controller.
    `forbidden` (in-process only): (agent, model) pairs a budget fallback may never reroute this task to.
    `pipeline` (in-process only): the pipeline engine, whose reviews keep their own independence check."""
    allocated = t.agent == "auto"
    if allocated:
        allocate(t)  # docs/SPEC-stall-reroute.md 12.12: the controller picks agent, model and account
    if t.agent not in AGENTS:
        raise HTTPException(400, f"agent must be one of {sorted(AGENTS)}")
    if t.mode not in MODES:
        raise HTTPException(400, f"mode must be one of {MODES}")
    if t.agent == "ollama" and not t.model:
        raise HTTPException(400, "ollama tasks need a model")
    if t.trusted_bypass:
        if t.agent not in ("claude", "codex"):
            raise HTTPException(400, "skipping permission prompts is only for Claude and Codex tasks")
        t.mode = "full"
    if t.gate and rank(t.mode) < rank("auto-edit"):
        raise HTTPException(400, "a gate runs shell commands, so it needs auto-edit or full mode")
    if t.link_reason is not None and not t.replaces:
        raise HTTPException(400, "link_reason needs replaces")
    if t.replaces:
        # Part F 5: a task made in place of another is a retry of it; the old one settles as handed_off.
        if t.link_reason is not None and t.link_reason not in REPLACE_REASONS:
            raise HTTPException(400, f"link_reason must be one of {list(REPLACE_REASONS)}")
        if t.parent_id and t.parent_id != t.replaces or t.link not in (None, "retry"):
            raise HTTPException(400, "replaces sets parent_id and link itself")
        with db() as c:
            old = c.execute("SELECT state, resolution FROM tasks WHERE id=?", (t.replaces,)).fetchone()
        if not old:
            raise HTTPException(400, f"unknown task {t.replaces} to replace")
        if old["state"] in RUNNING:
            raise HTTPException(409, f"{t.replaces} is still {old['state']}; stop it before replacing it")
        if old["resolution"]:
            raise HTTPException(409, f"{t.replaces} is already resolved")
        t.parent_id, t.link = t.replaces, "retry"
    if t.link is not None and t.link not in LINKS:
        raise HTTPException(400, f"link must be one of {list(LINKS)}")
    if t.link and t.link != "check" and not t.parent_id:
        raise HTTPException(400, "a link needs a parent_id (only 'check' may stand alone)")
    if t.parent_id:
        with db() as c:
            if not c.execute("SELECT 1 FROM tasks WHERE id=?", (t.parent_id,)).fetchone():
                raise HTTPException(400, f"unknown parent task {t.parent_id}")
    if t.role is not None and not ROLE_RE.fullmatch(t.role):
        raise HTTPException(400, "role must be lowercase letters, digits, dashes")
    if t.stage is not None and not STAGE_RE.fullmatch(t.stage):
        raise HTTPException(400, "stage must be 1-24 lowercase letters, digits or dashes")
    if t.profile is not None:
        if t.agent not in budget.PAID_AGENTS:
            raise HTTPException(400, "profile is for Claude and Codex tasks")
        if not PROFILE_RE.fullmatch(t.profile):
            raise HTTPException(400, "profile must be a runner profile id")
        if t.profile != "fleet" and not t.model:
            # A named profile has its own config dir: the runner's reported default (the fleet profile's) may not be
            # what it runs, so budget identity, kinds, limits and independence need the model named (Codex review).
            raise HTTPException(400, f"name a model for a task pinned to profile {t.profile}")
    if t.independence is not None:
        if t.independence != "waived":
            raise HTTPException(400, "independence can only be 'waived'")
        if not (t.independence_reason or "").strip():
            raise HTTPException(400, "an independence waiver needs a reason")
    requested_node = t.node
    if t.policy is not None and "auto_approve" in t.policy:
        # Approval authority comes only from the stored (admin-only) policy layers, never from the request.
        raise HTTPException(403, "a task can't grant itself auto-approval; set it on the Policies page")
    # ---- budget (docs/SPEC-paid-budget.md 3.1-3.4): only ever queues, reroutes or refuses ----
    kind = t.kind or ("build" if t.gate else "draft")
    if kind not in budget.KINDS:
        raise HTTPException(400, f"kind must be one of {list(budget.KINDS)}")
    if t.effort is not None and t.effort not in budget.EFFORTS:
        raise HTTPException(400, f"effort must be one of {list(budget.EFFORTS)}")
    if t.origin_kind is not None and (kind != "re_review" or t.origin_kind not in budget.ORIGIN_KINDS):
        raise HTTPException(400, f"origin_kind is only for a re_review, and must be one of {list(budget.ORIGIN_KINDS)}")
    override = (t.budget_override or "").strip()[:300] or None
    if override and scope != "admin":
        raise HTTPException(403, "budget_override is the owner's decision: it needs admin access")
    if t.review_of:
        with db() as c:
            source = c.execute("SELECT node_id, cwd FROM tasks WHERE id=?", (t.review_of,)).fetchone()
        if not source:
            raise HTTPException(400, f"unknown task {t.review_of} to review")
        if t.mode != "read-only" or t.worktree:
            raise HTTPException(400, "reviewing another task's worktree is read-only and uses no worktree of its own")
        t.node = source["node_id"]
    rules = budget.current_rules()
    clean = budget.strip_prompt(t.prompt, rules["prompt"])
    prompt = clean["text"]
    max_bytes = int(rules["prompt"]["max_kb"] * 1024)
    hard_bytes = int(rules["prompt"]["hard_max_kb"] * 1024)
    parts, large = [prompt], False
    if len(prompt.encode()) > max_bytes:
        if budget.has_diff(prompt):
            parts = budget.split_prompt(prompt, max_bytes)  # one task per file chunk
        else:
            large = True
        biggest = max(len(part.encode()) for part in parts)
        if biggest > hard_bytes:
            raise HTTPException(400, f"prompt is {biggest // 1024} KB after removing generated files; the limit is "
                                     f"{rules['prompt']['hard_max_kb']} KB (prompt.hard_max_kb on the Budget page)")
    failures = gate_failures(t.parent_id) if kind in (rules.get("effort_ladder") or {}) else 0
    slot_cache: dict[str, list[dict]] = {}

    def slots_for(of_agent: str) -> list[dict]:
        if of_agent not in slot_cache:
            if t.profile and of_agent == t.agent:
                slot_cache[of_agent] = profile_slots(of_agent, t.profile, t.node)
            else:
                found = placement(of_agent, t.node) if of_agent in budget.PAID_AGENTS else []
                if of_agent == t.agent and of_agent in budget.PAID_AGENTS and not t.model:
                    # No model named: admission accounts it at the fleet default, which only the fleet profile
                    # runs (a named profile has its own config dir), so only fleet slots are candidates.
                    found = [s for s in found if s["profile"] == "fleet"]
                slot_cache[of_agent] = found
        return slot_cache[of_agent]

    if t.profile:
        slots_for(t.agent)  # an unknown or unready profile is a 400 before admission
    elif t.agent in budget.PAID_AGENTS and not t.model and not slots_for(t.agent):
        if any(s["profile"] != "fleet" for s in placement(t.agent, t.node)):
            raise HTTPException(400, f"no usable fleet profile for {t.agent}: name a model to run on another profile")

    try:
        decision = budget.admit(agent=t.agent, model=t.model, kind=kind, effort=t.effort, override=bool(override),
                                failures=failures, node_id=t.node, origin_kind=t.origin_kind,
                                accounts_for=lambda of_agent: accounts_of(slots_for(of_agent), of_agent),
                                forbidden=forbidden, reroute=not t.profile)  # a pinned profile never moves
    except budget.Refusal as refusal:
        budget.log_decision("refused", refusal.detail, agent=t.agent, model=t.model, kind=kind,
                            title=(t.title or t.prompt.strip().splitlines()[0])[:80])
        raise HTTPException(refusal.status, refusal.detail)
    agent, model = decision["agent"], decision["model"]
    engine = local_engine(agent, model, t.role, t.node, t.engine)
    task_policy = t.policy
    review_exec = decision.get("review_exec") or {}
    if review_exec.get("task_minutes"):
        # The review time limit narrows the task layer like any task policy (the smaller value wins).
        task_policy = dict(t.policy or {})
        limits = dict(task_policy.get("limits") or {})
        limits["task_minutes"] = min(limits.get("task_minutes", review_exec["task_minutes"]), review_exec["task_minutes"])
        task_policy["limits"] = limits
    on_account = [s for s in slots_for(agent) if s["account"] == decision.get("account")]
    try:
        if task_policy is not None:
            fleet_policy.validate(task_policy)
        node, effective, queued_reason = pick_node(agent, model, t.mode, t.node, t.gate, t.role, task_policy,
                                                   t.skills, t.escalation,
                                                   allowed={s["node"] for s in on_account} if on_account else None,
                                                   engine=engine or "ollama")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    # The profile this task runs under: the chosen account's slot on the chosen node.
    slot = next((s for s in on_account if s["node"] == node["id"]), None)
    if slot is None and agent in budget.PAID_AGENTS:
        mine = node_slots(node, agent)
        if not t.model and agent == t.agent:
            mine = [s for s in mine if s["profile"] == "fleet"]
        slot = next((s for s in mine if usable(s)), mine[0] if mine else None)
    account = (slot["account"] if slot else None) if agent in budget.PAID_AGENTS else None
    # Before the row and any budget decision is logged, so a refused task leaves no trace (Fable's note b).
    waiver = check_independence(t, kind, agent, model, node, pipeline, slot["profile"] if slot else "fleet")
    placed = {"pinned": bool(requested_node or t.review_of), "role": t.role, "skills": list(t.skills),
              "escalation": t.escalation, "policy": t.policy, "profile": t.profile,
              "accounts": [s["node"] for s in on_account] if on_account else None, "allocated": allocated,
              "pin": bool(t.pin), "model_pinned": bool(t.model)}
    now = time.time()
    title = (t.title or t.prompt.strip().splitlines()[0])[:80]
    auto = (effective["policy"].get("auto_approve") or {})
    pre_approved = agent in (auto.get("agents") or []) and bool(auto.get("full_mode"))
    if t.trusted_bypass and not pre_approved:
        raise HTTPException(403, f"policy doesn't let {agent} skip permission prompts here; turn on auto-approval "
                                 "for it on the Policies page (and the node must allow full mode)")
    state = "awaiting_approval" if t.mode in NEEDS_APPROVAL and not pre_approved else "queued"
    waiting = decision["reason"] if decision["wait"] else queued_reason
    tools_at_launch = launch_tools(agent, effective["policy"], t.mode)
    extra = ({"tools": review_exec.get("tools"), "max_turns": review_exec.get("max_turns")} if review_exec else {})
    # Part G req 11 / Part H req 4: a controller lead is always a short-lived session.
    if kind == "lead":
        extra["max_turns"] = 30
    ids: list[str] = []
    for index, part in enumerate(parts):
        task_id = "t-" + secrets.token_hex(4)
        # Chunks of one split prompt share the given parent (a pipeline's build task); without one, the first
        # chunk is the parent of the rest.
        if index == 0:
            parent_id, link = t.parent_id, (t.link or "review") if t.parent_id and len(parts) > 1 else t.link
        else:
            parent_id, link = (t.parent_id, t.link or "review") if t.parent_id else (ids[0], "review")
        part_title = title if len(parts) == 1 else f"{title[:68]} (part {index + 1}/{len(parts)})"
        row = {
            "id": task_id, "parent_id": parent_id, "title": part_title, "agent": agent, "model": model,
            "engine": engine,
            "prompt": part, "mode": t.mode, "repo": t.repo, "worktree": int(t.worktree), "gate": t.gate,
            "node_id": node["id"], "state": state, "created_at": now, "updated_at": now,
            "instructions": effective["instructions"], "policy": json.dumps(effective["policy"]),
            "policy_hash": effective["hash"], "role": t.role, "error": waiting if state == "queued" else None,
            "skills": json.dumps(effective["skills"]), "trusted_bypass": int(t.trusted_bypass), "link": link,
            "hidden": int(link == "check"), "kind": kind, "effort": decision["effort"], "budget_override": override,
            "routed_from": decision["routed_from"], "budget_key": decision["key"],
            "budget_identity": decision["identity"], "prompt_bytes": len(part.encode()),
            "prompt_stripped_bytes": clean["stripped_bytes"] if index == 0 else 0,
            "prompt_omitted": (json.dumps(clean["omitted"] + [f"long lines: {p}" for p in clean["long_lines"]])
                               if index == 0 else None),
            "review_of": t.review_of, "launch_extra": json.dumps(extra) if extra else None,
            "budget_wait": decision["wait"] if state == "queued" else None,
            "approved_tools": json.dumps(tools_at_launch),
            "profile": slot["profile"] if slot else "fleet", "account_label": slot["label"] if slot else None,
            "account": account, "stage": t.stage, "independence": json.dumps(waiver) if waiver else None,
            "placement": json.dumps(placed),
            "link_reason": (t.link_reason or "requeue") if t.replaces and index == 0 else None,
        }
        with db() as c:
            c.execute(f"INSERT INTO tasks ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
            if t.replaces and index == 0:
                resolution = json.dumps({"kind": "handed_off", "task_id": task_id, "link": "retry",
                                         "reason": row["link_reason"], "agent": agent, "model": model, "at": now})
                if c.execute("UPDATE tasks SET resolution=?, updated_at=? WHERE id=? AND resolution IS NULL",
                             (resolution, now, t.replaces)).rowcount != 1:
                    raise HTTPException(409, f"{t.replaces} was resolved meanwhile")  # rolls back the insert
            if model:
                c.execute("UPDATE node_models SET state='ready',reason=NULL,updated_at=? WHERE node=? AND catalog_id IN "
                          "(SELECT id FROM model_catalog WHERE reference=?)",
                          (now, node["id"], model))
            for skill_id in dict.fromkeys(t.skills):
                c.execute("INSERT OR IGNORE INTO skill_bindings VALUES (?, 'task', ?)", (skill_id, task_id))
        ids.append(task_id)
        emit(task_id, f"task.{state}", {"title": part_title, "node": node["id"], "agent": agent, "mode": t.mode})
        if waiver:
            emit(task_id, "task.independence_waived", {"title": part_title, "kind": kind, "agent": agent,
                                                       "model": model, "reason": waiver["reason"]})
        if index == 0:
            if allocated:
                emit(task_id, "task.allocated", {"title": part_title, "agent": agent, "model": model,
                                                 "label": slot["label"] if slot else None, "node": node["id"],
                                                 "kind": kind})
            if decision["routed_from"]:
                budget.log_decision("rerouted", f"{decision['routed_from']} is limited; sent to {decision['identity']}",
                                    task_id=task_id, title=part_title, routed_from=decision["routed_from"],
                                    to=decision["identity"], kind=kind)
            if decision["wait"]:
                budget.log_decision("queued", decision["reason"], task_id=task_id, title=part_title,
                                    wait=decision["wait"], kind=kind, key=decision["key"])
            if large:
                emit(task_id, "budget.large_prompt", {"title": part_title, "bytes": len(part.encode()),
                                                      "max_kb": rules["prompt"]["max_kb"]})
        if state == "queued":
            async with _locks[task_id]:
                await try_start(get_task(task_id))
    view = with_root(get_task(ids[0]))
    view["split"] = ids if len(ids) > 1 else []
    return view


@router.get("/api/tasks/wait")
async def wait(ids: str, request: Request, timeout: float = 300, mode: str = "all") -> dict:
    """Long-poll until all (or any) of the tasks leave the active states, or the timeout passes. A bridge session's
    open wait is shown on Live (docs/SPEC-live-monitor.md 2.4), from local or operate+ callers only."""
    wanted = [i for i in ids.split(",") if i]
    for i in wanted:
        get_task(i)
    counted = getattr(request.state, "scope", "admin") in ("operate", "admin")
    session = presence.session_of(request.headers) if counted else None
    presence.wait_started(session, wanted)
    try:
        return await _wait(wanted, timeout, mode)
    finally:
        presence.wait_ended(session, wanted)


async def _wait(wanted: list[str], timeout: float, mode: str) -> dict:
    deadline = time.monotonic() + min(max(timeout, 1), 1800)
    while True:
        since = snapshot()  # before reading state, so an event in between still wakes us
        tasks = [get_task(i) for i in wanted]
        n_settled = sum(t["state"] not in ACTIVE for t in tasks)
        done = n_settled > 0 if mode == "any" else n_settled == len(tasks)
        if done or time.monotonic() >= deadline:
            return {"timed_out": not done,
                    "tasks": [{k: t[k] for k in ("id", "title", "state", "agent", "node_id", "exit_code", "error")}
                              | {"result": (t["result"] or "")[:2000]} for t in tasks]}
        await changed(since, min(30, deadline - time.monotonic()))


@router.get("/api/tasks/{task_id}")
def read_task(task_id: str) -> dict:
    import handoff
    view = with_root(get_task(task_id))
    view["planned"] = handoff.planned_action(view)  # what happens to a limited task (docs/SPEC-limit-handoff.md 5)
    return view


@router.get("/api/tasks/{task_id}/log")
async def task_log(task_id: str, offset: int = 0) -> dict:
    task = get_task(task_id)
    if task["state"] in ("queued", "awaiting_approval", "starting"):
        return {"offset": 0, "text": ""}
    return await runner_call(node_of(task), "GET", f"/jobs/{task_id}/log?offset={offset}")


@router.get("/api/tasks/{task_id}/diff")
async def task_diff(task_id: str) -> dict:
    task = get_task(task_id)
    return await runner_call(node_of(task), "GET", f"/jobs/{task_id}/diff")


def moved_to(task: dict) -> str | None:
    """Why the task's work can't continue here: it was handed off (or is being handed off) to another attempt."""
    resolution = task.get("resolution") or {}
    if isinstance(resolution, str):
        try:
            resolution = json.loads(resolution)
        except ValueError:
            resolution = {}
    if resolution.get("kind") == "handed_off":
        return f"work moved to {resolution.get('task_id') or 'another task'}"
    if resolution.get("kind") == "handing_off":
        return "work moved to a handoff in progress"
    return None


def refuse_if_moved(task: dict) -> None:
    moved = moved_to(task)
    if moved:
        raise HTTPException(409, moved)


CONTINUE_RETRY = 60  # a continuation held by the node's policy (quiet hours, its concurrency) is retried after this
# eligible() reasons that pass on their own (the rest, e.g. mode ceiling or a missing agent, need the owner)
TRANSIENT_INELIGIBLE = ("runner offline", "on battery")


def node_hold(node: dict, agent: str, model: str | None, mode: str, gate: str | None, policy: dict,
              now: float | None = None, engine: str = "ollama") -> dict | None:
    """The node side of admission, shared by new tasks' starts, continuations and handoffs: a refusal when the
    node can't take this work at all (agent, model, mode ceiling, gate), a defer while its policy holds work back
    (quiet hours, its concurrency, its daily agent minutes). None when the node admits it."""
    now = time.time() if now is None else now
    why = eligible(node, agent, model, mode, gate, policy, engine=engine)
    if why and why.startswith(TRANSIENT_INELIGIBLE):
        # The node is briefly unreachable or on battery: retry shortly, as a queued new task would keep waiting.
        return {"outcome": "defer", "until": now + CONTINUE_RETRY, "reason": why, "wait": "node"}
    if why:
        return {"outcome": "refuse", "until": None, "reason": why, "wait": None}
    why = temporary_reason(node, policy)
    if why:
        return {"outcome": "defer", "until": now + CONTINUE_RETRY, "reason": why, "wait": "node"}
    return None


def continuation(task: dict, *, effort: str | None = None, now: float | None = None) -> dict:
    """The one admission for more work on an existing task (a message, an approval, an automatic tool approval,
    a scheduled resume, a pipeline fix round): the budget (kinds, the account's limit, concurrency, hard budget)
    and the node's policy. {"outcome": "admit"|"defer"|"refuse", "until", "reason", "wait"}."""
    now = time.time() if now is None else now
    verdict = budget.admission(task, now, effort=effort)
    if verdict["outcome"] != "admit":
        return verdict
    try:
        node = node_of(task)
    except HTTPException as exc:
        return {"outcome": "refuse", "until": None, "reason": str(exc.detail), "wait": None}
    return node_hold(node, task["agent"], task.get("model"), task["mode"], task.get("gate"),
                     task.get("policy") or {}, now, task.get("engine") or "ollama") or verdict


class NotAdmitted(HTTPException):
    """A continuation that admission deferred or refused (409); `verdict` says which, why and until when."""
    def __init__(self, verdict: dict):
        when = f" (try again after {clock(verdict['until'])})" if verdict.get("until") else ""
        super().__init__(409, f"{verdict['reason']}{when}")
        self.verdict = verdict


async def resume(task: dict, text: str, allowed_tools: list[str], *, reset_auto_approvals: bool = False,
                 effort: str | None = None, auto_approved: bool = False) -> None:
    """Caller holds the task lock. `effort` (pipelines' fix rounds) is sent only to runners that understand it.
    Every continuation passes admission here, before the runner hears of it, and holds its slot on the account
    until the task is working (raises NotAdmitted otherwise)."""
    refuse_if_moved(task)  # two writers never share one branch: the handoff attempt owns this work now
    verdict = continuation(task, effort=effort)
    if verdict["outcome"] != "admit":
        raise NotAdmitted(verdict)
    # Admission and the reservation happen with no await between them, so no other coroutine can be admitted to
    # the same last slot (the account's or the node's) in between.
    paid = task["agent"] in budget.PAID_AGENTS and task.get("budget_key")
    budget.RESERVED[task["id"]] = (task["budget_key"] if paid else None,
                                   budget.task_account(task) if paid else None, task.get("node_id"))
    try:
        await _send_resume(task, text, allowed_tools, reset_auto_approvals=reset_auto_approvals, effort=effort,
                           auto_approved=auto_approved)
    finally:
        budget.RESERVED.pop(task["id"], None)


async def _send_resume(task: dict, text: str, allowed_tools: list[str], *, reset_auto_approvals: bool,
                       effort: str | None, auto_approved: bool = False) -> None:
    tools = approvable_tools(task, [*(task.get("approved_tools") or []), *allowed_tools])
    if tools != task.get("approved_tools"):
        update(task["id"], approved_tools=json.dumps(tools))
    node = node_of(task)
    message_body = {"text": text, "allowed_tools": tools, "mode": None}
    if effort and budget_support(node):
        message_body["effort"] = effort
    await runner_call(node, "POST", f"/jobs/{task['id']}/message", message_body)
    fields = {"runs": (task["runs"] or 0) + 1, "ended_at": None, "denials": "[]", "error": None,
              "failure_reason": None, "resets_at": None, "resolution": None,
              "run_prompt_bytes": len(text.encode()), "usage": None, "num_turns": None}
    if effort and budget_support(node):
        fields["effort"] = effort
    if reset_auto_approvals:
        fields["auto_approvals"] = 0
    if transition(task, RESUMABLE, "working", **fields):
        # The earlier run's story lives in the event log: how it ended, shown without any later resolution.
        emit(task["id"], "task.resumed", {
            "title": task["title"], "run": fields["runs"], "from": task["state"], "ended_at": task.get("ended_at"),
            "failure_reason": task.get("failure_reason"), "display": display_status({**task, "resolution": None}),
            "auto_approved": auto_approved})


@router.post("/api/tasks/{task_id}/message")
async def message(task_id: str, m: MessageIn) -> dict:
    async with _locks[task_id]:
        task = get_task(task_id)
        refuse_if_moved(task)
        if task["state"] not in RESUMABLE:
            raise HTTPException(409, f"can't message a task that is {task['state']}")
        # resume() applies the continuation admission (docs/SPEC-limit-handoff.md 4.5): a defer or refusal is a 409.
        await resume(task, m.text, [], reset_auto_approvals=True)
    return get_task(task_id)


@router.post("/api/tasks/{task_id}/approve")
async def approve(task_id: str) -> dict:
    """User-only (dashboard): start an awaiting task, or grant a blocked task the tools it was denied."""
    async with _locks[task_id]:
        task = get_task(task_id)
        refuse_if_moved(task)
        if task["state"] == "awaiting_approval":
            if transition(task, {"awaiting_approval"}, "queued", ended_at=None, auto_approvals=0):
                emit(task_id, "task.approved", {"title": task["title"]})
                await try_start(get_task(task_id))
        elif task["state"] == "blocked":
            tools = denied_tool_names(task)
            # Approving tools never bypasses admission: a limited or busy account, or the node's policy, gives 409.
            await resume(task, "The user approved the tools you were denied: " + ", ".join(tools)
                         + ". Continue the task.", tools, reset_auto_approvals=True)
            emit(task_id, "task.approved", {"title": task["title"], "tools": tools})
        else:
            raise HTTPException(409, f"nothing to approve: task is {task['state']}")
    return get_task(task_id)


@router.post("/api/tasks/{task_id}/reject")
async def reject(task_id: str) -> dict:
    async with _locks[task_id]:
        task = get_task(task_id)
        if not transition(task, {"awaiting_approval", "blocked"}, "cancelled", error="rejected by user"):
            raise HTTPException(409, f"nothing to reject: task is {task['state']}")
    return get_task(task_id)


@router.post("/api/tasks/{task_id}/cancel")
async def cancel(task_id: str) -> dict:
    async with _locks[task_id]:
        task = get_task(task_id)
        if task["state"] in ("done", "cancelled"):
            return task
        fields = {}
        was_running = task["state"] in ("starting", "working")
        if was_running:
            try:
                job = await runner_call(node_of(task), "POST", f"/jobs/{task_id}/cancel")
                if isinstance(job.get("usage"), dict):
                    fields["usage"] = json.dumps(job["usage"])
            except HTTPException as e:
                if e.status_code != 404:
                    raise
        if transition(task, ACTIVE | RESUMABLE, "cancelled", **fields) and was_running:
            record_usage(task_id)
    return get_task(task_id)


@router.post("/api/tasks/{task_id}/complete")
async def complete(task_id: str) -> dict:
    async with _locks[task_id]:
        task = get_task(task_id)
        if not transition(task, RESUMABLE, "done"):
            raise HTTPException(409, f"can't complete a task that is {task['state']}")
    return get_task(task_id)


RESOLVABLE = {"failed", "interrupted", "blocked", "cancelled", "review"}


class ResolveIn(BaseModel):
    kind: str
    commit: str | None = None
    by: str | None = None
    note: str | None = None


class OutcomeIn(BaseModel):
    kind: str
    commit: str | None = None


@router.post("/api/tasks/{task_id}/resolve")
async def resolve(task_id: str, body: ResolveIn, request: Request) -> dict:
    """Admin only: record that a settled task no longer needs attention. Never changes the task's state,
    so a blocked task stays blocked (it only stops being listed as needing attention)."""
    async with _locks[task_id]:
        task = get_task(task_id)
        if body.kind not in ("dismissed", "done_elsewhere"):
            raise HTTPException(400, "kind must be dismissed or done_elsewhere")
        by = (body.by or "").strip()[:120]
        commit = (body.commit or "").strip()
        if commit and not valid_commit(commit):
            raise HTTPException(400, "commit must be 7 to 40 hex characters")
        if body.kind == "done_elsewhere" and not by:
            raise HTTPException(400, "done_elsewhere needs 'by' (who finished it)")
        if task["state"] not in RESOLVABLE:
            raise HTTPException(409, f"can't resolve a task that is {task['state']}")
        if (task.get("resolution") or {}).get("kind") in HANDOFF_KINDS:
            raise HTTPException(409, "a limit handoff owns this task's resolution")
        resolution: dict = {"kind": body.kind, "at": time.time()}
        if by:
            resolution["by"] = by
        if commit:
            resolution["commit"] = commit
        if body.note and body.note.strip():
            resolution["note"] = body.note.strip()[:500]
        # Conditional: a handoff claim written since the read above (it doesn't take this task lock) wins.
        marks = ",".join("?" * len(HANDOFF_KINDS))
        with db() as c:
            moved = c.execute(f"UPDATE tasks SET resolution=?, updated_at=? WHERE id=? AND (resolution IS NULL OR "
                              f"json_extract(resolution, '$.kind') IS NULL OR "
                              f"json_extract(resolution, '$.kind') NOT IN ({marks}))",
                              (json.dumps(resolution), time.time(), task_id, *HANDOFF_KINDS)).rowcount
        if moved != 1:
            raise HTTPException(409, "a limit handoff owns this task's resolution")
    emit(task_id, "task.resolved", {"title": task["title"], "kind": body.kind, "by": by or None,
                                    "commit": commit or None, "actor": getattr(request.state, "actor", None)})
    return with_root(get_task(task_id))


@router.post("/api/tasks/{task_id}/outcome")
async def set_outcome(task_id: str, body: OutcomeIn, request: Request) -> dict:
    """Admin only: record how a work item ended. Only on a root task. Marks a settled root done for merged/done,
    but never moves a blocked or awaiting-approval task."""
    async with _locks[task_id]:
        task = get_task(task_id)
        if task["parent_id"]:
            raise HTTPException(400, "an outcome belongs on the root task of the work item")
        if body.kind not in OUTCOME_KINDS:
            raise HTTPException(400, f"kind must be one of {list(OUTCOME_KINDS)}")
        commit = (body.commit or "").strip()
        if (body.kind == "merged" or commit) and not valid_commit(commit):
            raise HTTPException(400, "commit must be 7 to 40 hex characters" + (" (required for merged)" if body.kind == "merged" else ""))
        outcome_value = {"kind": body.kind, "at": time.time()}
        if commit:
            outcome_value["commit"] = commit
        update(task_id, outcome=json.dumps(outcome_value))
        if body.kind in ("merged", "done"):
            transition(task, {"review", "failed", "interrupted"}, "done")
    emit(task_id, "task.outcome", {"title": task["title"], "kind": body.kind, "commit": commit or None,
                                   "actor": getattr(request.state, "actor", None)})
    return with_root(get_task(task_id))


WORK_COLUMNS = ("id,parent_id,link,title,agent,model,state,failure_reason,resets_at,resolution,outcome,hidden,"
                "created_at,updated_at,started_at,ended_at,runs,gate,gate_result,exit_code,error,kind,account_label,"
                "launch_extra,node_id,account,budget_identity,"
                # The end of the output is where a usage-limit message sits (same slice derive_reason uses).
                "CASE WHEN failure_reason IS NULL AND state IN ('failed','blocked','interrupted','cancelled') "
                "THEN substr(result,-4000) END AS result")


def outcome_display(value: dict) -> dict:
    kind = value.get("kind")
    if kind == "merged":
        commit = f" · {value['commit'][:7]}" if value.get("commit") else ""
        return {"tone": "ok", "label": f"Merged{commit}"}
    if kind == "done":
        return {"tone": "ok", "label": "Done"}
    return {"tone": "muted", "label": "Abandoned"}


def attempt_entries(row: dict, resumed: list[dict], ctx: "DisplayContext | None" = None) -> list[dict]:
    """One entry per run of a task: earlier runs come from task.resumed events, the last from the row."""
    try:
        extra = json.loads(row.get("launch_extra") or "{}") or {}
    except (TypeError, ValueError):
        extra = {}
    handed_off = ((row.get("resolution") or {}) if isinstance(row.get("resolution"), dict) else {}).get("kind") == "handed_off"

    def entry(**kw) -> dict:
        return {"id": row["id"], "title": row["title"], "agent": row["agent"], "model": row["model"],
                "link": row["link"], "created_at": row["created_at"], "kind": row.get("kind"),
                "account_label": row.get("account_label"),
                # a limit handoff: this attempt continues another's checkpoint / this attempt's work moved on
                "handoff": bool(extra.get("source_job")), "handed_off": handed_off} | kw

    at = row["created_at"]
    out, started = [], row["started_at"] or at
    for number, event in enumerate(resumed, start=1):
        data = event["data"]
        # An earlier run is judged by what happened after it, not by how it stopped: it was resumed.
        merged = ctx.root_outcome(row["id"]) if ctx else None
        if merged and merged.get("kind") == "merged":
            shown = outcome_display(merged)
        elif data.get("auto_approved"):
            shown = {"tone": "muted", "label": "Auto-approved and resumed"}
        elif data.get("from") in ("blocked", "awaiting_approval"):
            shown = {"tone": "muted", "label": "Approved and resumed"}
        else:
            shown = {"tone": "muted", "label": "Resumed"}
        out.append(entry(state=data.get("from"), display=shown,
                         failure_reason=data.get("failure_reason"), started_at=started, ended_at=data.get("ended_at"),
                         run=number, at=at))
        at = started = event["ts"]
    out.append(entry(state=row["state"], display=live_display(row, ctx=ctx), failure_reason=row["failure_reason"],
                     started_at=started if resumed else row["started_at"], ended_at=row["ended_at"],
                     run=len(resumed) + 1, at=at))
    return out


@router.get("/api/work")
def work_items(limit: int = 50, include_hidden: int = 0) -> list[dict]:
    """One entry per root task (a piece of work), newest activity first, with every attempt in order."""
    limit = max(1, min(limit, 500))
    # Every task with the root of its work item. An orphan (parent missing) is its own root.
    tree = ("WITH RECURSIVE tree(id, root) AS ("
            "SELECT id, id FROM tasks WHERE parent_id IS NULL OR parent_id NOT IN (SELECT id FROM tasks) "
            "UNION ALL SELECT t.id, tree.root FROM tasks t JOIN tree ON t.parent_id = tree.id)")
    with db() as c:
        # Newest `limit` work items by latest activity across the whole item (an outcome bumps updated_at too).
        hidden = "" if include_hidden else "WHERE r.hidden = 0 "
        roots = [r[0] for r in c.execute(
            f"{tree} SELECT tree.root FROM tree JOIN tasks t ON t.id = tree.id JOIN tasks r ON r.id = tree.root "
            f"{hidden}GROUP BY tree.root ORDER BY MAX(t.updated_at) DESC, tree.root LIMIT ?", (limit,))]
        if not roots:
            return []
        marks = ",".join("?" * len(roots))
        scoped = (f"WITH RECURSIVE tree(id, root) AS (SELECT id, id FROM tasks WHERE id IN ({marks}) "
                  "UNION ALL SELECT t.id, tree.root FROM tasks t JOIN tree ON t.parent_id = tree.id)")
        rows = [dict(r) for r in c.execute(
            f"{scoped} SELECT {WORK_COLUMNS} FROM tasks WHERE id IN (SELECT id FROM tree) ORDER BY created_at, rowid",
            roots)]
        resumed: dict[str, list[dict]] = defaultdict(list)
        for r in c.execute(f"{scoped} SELECT task_id, ts, data FROM events WHERE type='task.resumed' "
                           "AND task_id IN (SELECT id FROM tree) ORDER BY id", roots):
            resumed[r["task_id"]].append({"ts": r["ts"], "data": json.loads(r["data"] or "{}")})
    ctx = DisplayContext()
    by_id = {r["id"]: r for r in rows}
    parents = {r["id"]: r["parent_id"] for r in rows}
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        r["resolution"] = json.loads(r["resolution"]) if r["resolution"] else None
        derive_reason(r)
        groups[root_in(parents, r["id"])].append(r)
    items = []
    for root_id, members in groups.items():
        root = by_id[root_id]
        if root["hidden"] and not include_hidden:
            continue
        attempts = []
        for index, member in enumerate(members):
            if member["hidden"] and not include_hidden:
                continue
            for part in attempt_entries(member, resumed.get(member["id"], []), ctx):
                attempts.append((part["at"], index, part["run"], part))
        attempts.sort(key=lambda item: item[:3])
        shown = [{k: v for k, v in part.items() if k not in ("at", "created_at")} for *_, part in attempts]
        outcome_value = json.loads(root["outcome"]) if root["outcome"] else None
        display = outcome_display(outcome_value) if outcome_value else (shown[-1]["display"] if shown else display_status(root))
        activity = max([m["updated_at"] or 0 for m in members] + [(outcome_value or {}).get("at") or 0])
        items.append({"root_id": root_id, "title": root["title"], "outcome": outcome_value, "display": display,
                      "updated_at": activity, "attempts": shown})
    items.sort(key=lambda item: item["updated_at"], reverse=True)
    return items


@router.post("/api/tasks/{task_id}/cleanup")
async def cleanup(task_id: str) -> dict:
    async with _locks[task_id]:
        task = get_task(task_id)
        if task["state"] in ACTIVE:
            raise HTTPException(409, "task is still active")
        out = await runner_call(node_of(task), "POST", f"/jobs/{task_id}/cleanup")
    emit(task_id, "task.cleaned_up", {"title": task["title"]})
    return out


@router.get("/api/inbox")
def inbox() -> list[dict]:
    """Everything waiting on a human: approvals to start, and blocked tasks with denied permissions."""
    return list_tasks(state="awaiting_approval,blocked")


@router.post("/api/nodes/{node}/ceiling")
def set_ceiling(node: str, m: ModeIn) -> dict:
    if m.mode not in MODES:
        raise HTTPException(400, f"mode must be one of {MODES}")
    with db() as c:
        if c.execute("UPDATE nodes SET ceiling=? WHERE id=? OR name=?", (m.mode, node, node)).rowcount != 1:
            raise HTTPException(404, f"unknown or ambiguous node {node}")
    emit(None, "node.ceiling", {"node": node, "mode": m.mode})
    return {"node": node, "ceiling": m.mode}


@router.get("/api/usage")
def usage(days: int = 1) -> dict:
    """Tasks and agent run time per node and agent (time waiting on humans isn't counted), for tasks created in the
    stated window [since, until] (`window_days` days back from now). `failed` counts unsettled failures only: a failed
    or interrupted task that is settled (resolution done_elsewhere, handed_off, dismissed, resolved_by; a retry; or a
    work item that merged) is not a failure. Settled is decided by display_status/live_display (docs/SPEC-dashboard-truth
    req 1), the same helpers every other page uses."""
    now = time.time()
    since = now - days * 86400
    with db() as c:
        rows = [dict(r) for r in c.execute("""SELECT node_id, agent, COUNT(*) AS tasks,
                                   SUM(state='working') AS running,
                                   0 AS failed,
                                   CAST(SUM(COALESCE(agent_seconds, 0)
                                            + CASE WHEN state='working' THEN ? - COALESCE(run_started_at, ?) ELSE 0 END)
                                        AS INTEGER) AS seconds
                            FROM tasks WHERE created_at >= ? GROUP BY node_id, agent ORDER BY tasks DESC""",
                         (now, now, since)).fetchall()]
        by_key = {(r["node_id"], r["agent"]): r for r in rows}
        candidates = c.execute("SELECT * FROM tasks WHERE created_at >= ? AND state IN ('failed','interrupted')",
                               (since,)).fetchall()
        merged_roots: dict[str, bool] = {}
        unsettled = []
        for row in candidates:
            root = task_root(c, row["id"])
            if root not in merged_roots:
                outcome = c.execute("SELECT outcome FROM tasks WHERE id=?", (root,)).fetchone()["outcome"]
                try:
                    merged_roots[root] = (json.loads(outcome) or {}).get("kind") == "merged" if outcome else False
                except (TypeError, ValueError):
                    merged_roots[root] = False
            if not merged_roots[root]:
                unsettled.append(row)
    for row in unsettled:
        if task_view(row)["display"]["tone"] not in ("resolved", "muted"):
            by_key[(row["node_id"], row["agent"])]["failed"] += 1
    return {"window_days": days, "since": since, "until": now, "rows": rows}


@router.get("/api/events")
async def event_stream(request: Request, after: int | None = None) -> StreamingResponse:
    """Server-sent events: the dashboard listens here instead of polling."""
    cursor = last_event_id() if after is None else after

    async def gen():
        nonlocal cursor
        yield "retry: 3000\n\n"
        while not await request.is_disconnected():
            since = snapshot()
            for e in events_after(cursor):
                cursor = e["id"]
                yield f"id: {e['id']}\ndata: {json.dumps(e)}\n\n"
            await changed(since, 15)
            yield ": keepalive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/api/events/list")
def event_list(after: int | None = None, before: int | None = None, limit: int = 200) -> list[dict]:
    """`after=<id>` alone: ascending, for incremental consumers (unchanged). Otherwise newest first; `before=<id>` pages."""
    limit = max(1, min(limit, 1000))
    if after is not None and before is None:
        return events_after(after, limit)
    return events_newest(limit, before, after or 0)

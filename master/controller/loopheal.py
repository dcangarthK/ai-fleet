"""Part G batch 2b (docs/SPEC-capacity-placement.md 3.5-3.7, 3a): loop-cut outcomes, gate bounce text, service self-heal.

Kept apart from tasks.py so the task code only calls three small hooks. No import of tasks/controller at module
level (tasks imports this module). Self-heal counters live in fleet.db (table `service_heal`), never in memory.
"""
import json
import re
import time

from events import emit
from store import db

RESTART_WINDOW = 15 * 60       # two failed restarts inside this window raise an alert instead of looping
MAX_FAILED_RESTARTS = 2
MISSED_BEATS = 3               # heartbeats missed before a restart
UNHEALTHY_SECONDS = 120        # health checks failing for this long before a restart
REVIEW_KINDS = {"review", "re_review", "final_review", "security_review"}

# ---------- 5. loop detection (the runner stops the session; these settle the task) ----------


def is_loop_cut(job: dict) -> bool:
    cap = job.get("cap")
    return job.get("state") == "capped" and isinstance(cap, dict) and cap.get("reason") == "loop"


def loop_reason(job: dict) -> str:
    cap = job.get("cap") or {}
    return str(cap.get("detail") or cap.get("kind") or "loop")[:200]


def has_verdict(result: str | None) -> bool:
    """True when `result` already holds a parseable review verdict (the grounded review contract)."""
    import grounding
    try:
        value = grounding._json_value(result or "")
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict) and value.get("verdict") in grounding.VERDICTS


def loop_outcome(task: dict, job: dict) -> str:
    """A review that already produced a verdict settles normally; anything else fails (last result is kept)."""
    if task.get("kind") in REVIEW_KINDS and has_verdict(job.get("result")):
        return "review"
    return "failed"


def loop_fields(new_state: str, job: dict) -> dict:
    note = f"loop cut off: {loop_reason(job)}"
    if new_state == "failed":
        return {"failure_reason": "loop", "error": note}
    return {"error": note}  # a review that already has its verdict: settles normally, with the note


# ---------- 6. gate bounce ----------

_PYTEST_FAIL = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$")
_UNITTEST_HEAD = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([^)]+)\)")
_ERROR_LINE = re.compile(r"^(?:[A-Za-z_][\w.]*(?:Error|Exception|Failure)\b.*|AssertionError.*)$")

_STALE_EMBED = re.compile(r"(runner|embed|stale|out of date|differs?)", re.I)


def _stale_embed(failures, output):
    """test_onboard fails because the embedded runner no longer matches nodes/runner.py."""
    return any("test_onboard" in name and _STALE_EMBED.search(err) for name, err in failures)


def _old_onboarding_kept(failures, output):
    """test_onboard fails on a missing needle (assertIn: "... not found in ..."): newer onboarding text is gone, which
    is what keeping an old branch's onboard.sh / onboard.ps1 in a merge looks like."""
    if _stale_embed(failures, output):  # the two traps are exclusive: a stale embed also fails with "not found in"
        return False
    return any("test_onboard" in name and "not found in" in err for name, err in failures)


TRAPS = (
    ("stale_runner_embed", _stale_embed,
     "Changed `nodes/runner.py`? Run `python3 nodes/build.py` to re-embed it in `onboard.sh` / `onboard.ps1`, "
     "and commit both. `test_onboard` fails otherwise."),
    ("old_onboarding_kept", _old_onboarding_kept,
     "Merging a branch that touched `nodes/onboard.sh` / `onboard.ps1`? Take main's copy "
     "(`git checkout main -- nodes/onboard.sh nodes/onboard.ps1`), then re-run `nodes/build.py`. "
     "Never keep an old branch's copy."),
)


def summarize_gate_failure(output: str | None, limit: int = 12) -> list[tuple[str, str]]:
    """(failing test name, first error) pairs from gate output (pytest and unittest shapes), not the raw tail."""
    found: list[tuple[str, str]] = []
    lines = (output or "").splitlines()
    for i, line in enumerate(lines):
        line = line.strip()
        m = _PYTEST_FAIL.match(line)
        if m:
            found.append((m.group(1), (m.group(2) or "").strip()))
            continue
        m = _UNITTEST_HEAD.match(line)
        if m:
            err = ""
            for follow in lines[i + 1:]:
                follow = follow.strip()
                if _UNITTEST_HEAD.match(follow):
                    break
                if _ERROR_LINE.match(follow):
                    err = follow
                    break
            found.append((f"{m.group(2)}.{m.group(1)}", err))
    seen, unique = set(), []
    for name, err in found:
        if name not in seen:
            seen.add(name)
            unique.append((name, err))
    return unique[:limit]


def gate_text(gate: dict | None, round_no: int = 1) -> str:
    """The message sent back to the builder: failing tests with the first error of each, plus matching trap lines."""
    output = (gate or {}).get("output_tail") or ""
    failures = summarize_gate_failure(output)
    lines = [f"The gate failed (bounce {round_no} of 3). You don't need to run the gate: the runner runs it after "
             "you finish. Fix these:"]
    if failures:
        for name, err in failures:
            lines.append(f"- {name}" + (f": {err[:300]}" if err else ""))
    else:
        tail = [ln for ln in output.splitlines() if ln.strip()][-3:]
        lines.append("- no failing test names found; last output: " + " | ".join(t.strip()[:200] for t in tail))
    # No parsed test names: match the traps against the raw output, as one pseudo failure.
    trap_input = failures or ([(output, output)] if output else [])
    for _name, matches, line in TRAPS:
        if matches(trap_input, output):
            lines.append("Checklist: " + line)
    return "\n".join(lines)


def gate_event(build: dict) -> dict:
    """Extra fields for the pipeline `build` event: set only when the build failed on its gate."""
    if build.get("state") != "failed" or build.get("failure_reason") != "gate_failed":
        return {}
    import tasks
    gate = build.get("gate_result")
    if isinstance(gate, str):
        try:
            gate = json.loads(gate)
        except ValueError:
            gate = None
    count = tasks.gate_failures(build["id"])
    text = gate_text(gate, min(count, 3))
    return {"failure_reason": "gate_failed", "gate_failures": count, "gate_text": text, "gate_task": build["id"],
            "gate_title": build.get("title")}


def emit_bounce(task_id: str, title: str | None, round_no: int) -> None:
    """Called by the controller once a bounce action has actually been applied (not on escalation)."""
    emit(task_id, "gate.bounced", {"title": title, "round": round_no,
                                   "reason": "gate failed; summary sent to the builder session"})


# ---------- 7. service self-heal ----------

def _ensure() -> None:
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS service_heal (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
                  "node TEXT NOT NULL, service TEXT NOT NULL, outcome TEXT NOT NULL)")


def _record(now: float, node: str, service: str, outcome: str) -> None:
    with db() as c:
        c.execute("INSERT INTO service_heal (ts, node, service, outcome) VALUES (?,?,?,?)", (now, node, service, outcome))


def _recent(now: float, node: str, service: str, outcome: str | tuple = ()) -> int:
    """Rows in the 15-minute window for (node, service); `outcome` is one outcome, a tuple of them, or () for all."""
    kinds = (outcome,) if isinstance(outcome, str) else tuple(outcome)
    marks = ",".join("?" * len(kinds))
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM service_heal WHERE node=? AND service=? AND ts>?"
                         + (f" AND outcome IN ({marks})" if kinds else ""),
                         (node, service, now - RESTART_WINDOW, *kinds)).fetchone()[0]


def _last_attempt(node: str, service: str) -> float | None:
    with db() as c:
        row = c.execute("SELECT MAX(ts) FROM service_heal WHERE node=? AND service=? AND outcome IN ('ok','failed')",
                        (node, service)).fetchone()
    return row[0]


def _pending(node: str, service: str) -> str | None:
    """The newest outcome for the service if it was a failed restart or an alert (not yet recovered), else None."""
    with db() as c:
        row = c.execute("SELECT outcome FROM service_heal WHERE node=? AND service=? ORDER BY id DESC LIMIT 1",
                        (node, service)).fetchone()
    return row[0] if row and row[0] in ("failed", "alert") else None


def cooldown_seconds() -> float:
    """No second restart of the same service sooner than a heartbeat TTL plus a node poll after the last one."""
    import tasks
    return tasks.HEARTBEAT_TTL + tasks.NODE_POLL_SECONDS


def needs_restart(svc: dict, now: float) -> str | None:
    """Why the service is down: 3 missed heartbeats, or failing health checks for 2 minutes. None if fine."""
    if (svc.get("missed_beats") or 0) >= MISSED_BEATS:
        return f"missed {svc['missed_beats']} heartbeats"
    since = svc.get("unhealthy_since")
    if since is not None and now - since >= UNHEALTHY_SECONDS:
        return f"health checks failing for {int(now - since)}s"
    return None


def _alerted(now: float, node: str, service: str) -> bool:
    """An alert in the window that no later `recovered` row has closed: the dedupe for service.alert."""
    with db() as c:
        alert = c.execute("SELECT MAX(id) FROM service_heal WHERE node=? AND service=? AND outcome='alert' AND ts>?",
                          (node, service, now - RESTART_WINDOW)).fetchone()[0]
        recovered = c.execute("SELECT MAX(id) FROM service_heal WHERE node=? AND service=? AND outcome='recovered'",
                              (node, service)).fetchone()[0]
    return alert is not None and (recovered is None or alert > recovered)


def _alert(now: float, node: str, name: str, why: str, detail: str) -> None:
    if not _alerted(now, node, name):  # once per window, and again if the service recovered and failed anew
        _record(now, node, name, "alert")
        emit(None, "service.alert", {"node": node, "service": name, "reason": why, "detail": detail})


def heal_tick(services: list[dict], restart, health, now: float | None = None, grace: bool | None = None) -> list[dict]:
    """One self-heal pass. `services`: [{node, service, missed_beats, unhealthy_since, unreachable?}]. `restart(node,
    service)` asks the service manager; `health(node, service)` verifies afterwards. Waits out the controller's
    startup grace. Every attempt (ok or failed) counts in the 15-minute window: two attempts raise `service.alert`
    instead of a third, and a cooldown spaces attempts. A healthy service that had failed or alerted gets
    `service.recovered`. A runner the controller can't reach alerts straight away (it can't take the request)."""
    now = time.time() if now is None else now
    if grace is None:
        import tasks
        grace = tasks.in_startup_grace(now)
    _ensure()
    results = []
    for svc in services:
        node, name = svc["node"], svc["service"]
        why = needs_restart(svc, now)
        if why is None:
            if not grace and _pending(node, name):
                _record(now, node, name, "recovered")
                emit(None, "service.recovered", {"node": node, "service": name, "reason": "healthy again"})
                results.append({"node": node, "service": name, "action": "recovered"})
            continue
        if grace:
            results.append({"node": node, "service": name, "action": "grace"})
            continue
        if svc.get("unreachable"):
            _alert(now, node, name, why, "runner unreachable; cannot restart it through itself")
            results.append({"node": node, "service": name, "action": "alert"})
            continue
        if _recent(now, node, name, ("ok", "failed")) >= MAX_FAILED_RESTARTS:
            _alert(now, node, name, why, f"{MAX_FAILED_RESTARTS} restarts in 15 minutes; not restarting again")
            results.append({"node": node, "service": name, "action": "alert"})
            continue
        last = _last_attempt(node, name)
        if last is not None and now - last < cooldown_seconds():
            results.append({"node": node, "service": name, "action": "cooldown"})
            continue
        try:
            restart(node, name)
            ok = bool(health(node, name))
        except Exception:  # a restart that raises is a failed restart, never a crash of the tick
            ok = False
        _record(now, node, name, "ok" if ok else "failed")
        emit(None, "service.restarted" if ok else "service.restart_failed", {"node": node, "service": name, "reason": why})
        results.append({"node": node, "service": name, "action": "restarted" if ok else "restart_failed"})
    return results


# ---------- 7. wiring: services from heartbeats, restart through the runner, the periodic tick ----------

# Only the start of a health failure lives in memory: a controller restart resets the 2-minute unhealthy timer, which
# errs safe (it delays a restart). Restart counters stay in fleet.db.
_DOWN_SINCE: dict[tuple[str, str], float] = {}
_RESTARTED_AT: dict[tuple[str, str], float] = {}  # when we last asked a runner to restart a service
RESTART_PAD = 2.0  # the runner restarts itself ~1 s after replying; a beat inside that gap is the old process


def collect_services(nodes: list[dict], now: float) -> list[dict]:
    """Service rows for heal_tick from node heartbeats. runner: freshness of the newest of heartbeat and poll
    (the rule runner_offline uses); a missed beat is one HEARTBEAT_TTL. ollama: the runner's metrics say it is down.
    claude-mem and the controller worker are NOT monitored: nothing reports them yet, and the runner has no
    restart for them. Add them here, with a runner report and a SERVICE_NAMES entry, when they are installed."""
    import tasks
    rows, live_keys = [], set()
    for node in nodes:
        if node.get("status") != "approved" or not node.get("runner"):
            continue
        seen = max([t for t in (node.get("heartbeat_at"), node.get("polled_at")) if t] or [0])
        missed = int(max(0.0, now - seen) // tasks.HEARTBEAT_TTL) if seen else 0
        unreachable = (node.get("runner_fails") or 0) >= 3
        rows.append({"node": node["id"], "service": "runner", "missed_beats": missed, "unhealthy_since": None,
                     "unreachable": unreachable})
        metrics = node["runner"].get("metrics")
        ollama = metrics.get("ollama") if isinstance(metrics, dict) else None
        key = (node["id"], "ollama")
        since = None
        if isinstance(ollama, dict) and ollama.get("up") is False and ollama.get("installed", True) is not False:
            since = _DOWN_SINCE.setdefault(key, now)
            live_keys.add(key)
        if since is not None or isinstance(ollama, dict):
            rows.append({"node": node["id"], "service": "ollama", "missed_beats": 0, "unhealthy_since": since})
    for key in [k for k in _DOWN_SINCE if k not in live_keys]:
        del _DOWN_SINCE[key]
    return rows


def _node(node_id: str) -> dict:
    import tasks
    return next(n for n in tasks.node_rows() if n["id"] == node_id)


def restart_via_runner(node_id: str, service: str) -> None:
    """Blocking (run in a thread): POST /services/restart on the node's runner."""
    import httpx
    import tasks
    node = _node(node_id)
    token = tasks.node_runner_token(node)
    _RESTARTED_AT[(node_id, service)] = time.time()
    r = httpx.post(tasks.runner_url(node) + "/services/restart", json={"service": service}, timeout=40,
                   headers={"X-Runner-Token": token} if token else {}, trust_env=False)
    r.raise_for_status()


def check_health(node_id: str, service: str, wait: float = 30.0, step: float = 1.5) -> bool:
    """Blocking: after a restart, wait until the service looks up. A runner counts as healthy only after a heartbeat
    or poll newer than the restart (the old process answering /health proves nothing). Ollama: runner metrics."""
    import httpx
    import tasks
    deadline = time.time() + wait
    since = _RESTARTED_AT.get((node_id, service), time.time()) + RESTART_PAD
    while True:
        node = _node(node_id)
        try:
            if service == "runner":
                if max([t for t in (node.get("heartbeat_at"), node.get("polled_at")) if t] or [0]) > since:
                    return True
            else:
                token = tasks.node_runner_token(node)
                body = httpx.get(tasks.runner_url(node) + "/health", timeout=3, trust_env=False,
                                 headers={"X-Runner-Token": token} if token else {}).json()
                if body.get("ok") and ((body.get("metrics") or {}).get("ollama") or {}).get("up") is True:
                    return True
        except (httpx.HTTPError, ValueError):
            pass
        if time.time() >= deadline:
            return False
        time.sleep(step)


def _pass(now: float, restart, health) -> list[dict]:
    """Blocking: read the nodes (sqlite), pick services, heal. Runs in a thread."""
    import tasks
    services = collect_services(tasks.node_rows(), now)
    return heal_tick(services, restart or restart_via_runner, health or check_health, now, tasks.in_startup_grace(now))


async def tick(now: float | None = None, restart=None, health=None) -> list[dict]:
    """One self-heal pass, entirely off the event loop. Never raises."""
    import asyncio
    now = time.time() if now is None else now
    try:
        return await asyncio.to_thread(_pass, now, restart, health)
    except Exception as exc:  # self-heal must never stop the poll loop
        print(f"service self-heal failed: {exc!r}")
        return []


_RUNNING = None


def kick() -> None:
    """Called by poll_loop each round: start a pass unless one is still running, and don't wait for it (a pass can
    take a minute on an unreachable node). One pass at a time, so ticks never overlap."""
    import asyncio
    global _RUNNING
    if _RUNNING is not None and not _RUNNING.done():
        return
    _RUNNING = asyncio.get_running_loop().create_task(tick())

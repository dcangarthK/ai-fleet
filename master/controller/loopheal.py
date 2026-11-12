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

TRAPS = (
    ("stale_runner_embed", re.compile(r"test_onboard|onboard\.(?:sh|ps1)[^\n]*(?:differ|stale|embed)", re.I),
     "Changed `nodes/runner.py`? Run `python3 nodes/build.py` to re-embed it in `onboard.sh` / `onboard.ps1`, "
     "and commit both. `test_onboard` fails otherwise."),
    ("old_onboarding_kept", re.compile(r"test_onboard[\s\S]*(?:merge|<<<<<<<|>>>>>>>)|(?:merge|<<<<<<<)[\s\S]*test_onboard", re.I),
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
    for _name, pattern, line in TRAPS:
        if pattern.search(output):
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
    emit(build["id"], "gate.bounced", {"title": build.get("title"), "round": count,
                                       "reason": "gate failed; summary sent to the builder session"})
    return {"failure_reason": "gate_failed", "gate_failures": count, "gate_text": text}


# ---------- 7. service self-heal ----------

def _ensure() -> None:
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS service_heal (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
                  "node TEXT NOT NULL, service TEXT NOT NULL, outcome TEXT NOT NULL)")


def _record(now: float, node: str, service: str, outcome: str) -> None:
    with db() as c:
        c.execute("INSERT INTO service_heal (ts, node, service, outcome) VALUES (?,?,?,?)", (now, node, service, outcome))


def _recent(now: float, node: str, service: str, outcome: str) -> int:
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM service_heal WHERE node=? AND service=? AND outcome=? AND ts>?",
                         (node, service, outcome, now - RESTART_WINDOW)).fetchone()[0]


def needs_restart(svc: dict, now: float) -> str | None:
    """Why the service is down: 3 missed heartbeats, or failing health checks for 2 minutes. None if fine."""
    if (svc.get("missed_beats") or 0) >= MISSED_BEATS:
        return f"missed {svc['missed_beats']} heartbeats"
    since = svc.get("unhealthy_since")
    if since is not None and now - since >= UNHEALTHY_SECONDS:
        return f"health checks failing for {int(now - since)}s"
    return None


def heal_tick(services: list[dict], restart, health, now: float | None = None, grace: bool | None = None) -> list[dict]:
    """One self-heal pass. `services`: [{node, service, missed_beats, unhealthy_since}]. `restart(node, service)`
    asks the service manager; `health(node, service)` verifies afterwards. Waits out the controller's startup
    grace; two failed restarts inside 15 minutes raise `service.alert` (once per window) instead of restarting."""
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
            continue
        if grace:
            results.append({"node": node, "service": name, "action": "grace"})
            continue
        if _recent(now, node, name, "failed") >= MAX_FAILED_RESTARTS:
            if not _recent(now, node, name, "alert"):
                _record(now, node, name, "alert")
                emit(None, "service.alert", {"node": node, "service": name, "reason": why,
                                             "detail": f"{MAX_FAILED_RESTARTS} failed restarts in 15 minutes; not restarting again"})
            results.append({"node": node, "service": name, "action": "alert"})
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

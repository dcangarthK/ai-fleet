"""Task lineage helpers: why a task ended badly, how a failed attempt was dealt with, how to show it.

Everything here is a pure function over plain dicts (task rows and runner jobs), so the API, the
notifier and the tests share one definition. See docs/SPEC-task-lineage.md.
"""
import datetime
import json
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LINKS = ("retry", "resume", "review", "fix", "merge", "check")
# "stalled": the controller stopped a dead or idle run (docs/SPEC-stall-reroute.md 12.5, 12.6)
REASONS = ("limit", "gate_failed", "denied", "error", "interrupted", "cancelled", "stalled", "loop")
RESOLUTION_KINDS = ("resolved_by", "dismissed", "done_elsewhere", "handed_off")
# Set by the controller during a limit handoff (docs/SPEC-limit-handoff.md 4.3): `handing_off` is the claim,
# `handed_off` the result. Neither can be overwritten by /resolve, and a handed-off task can't be resumed.
HANDOFF_KINDS = ("handing_off", "handed_off")
OUTCOME_KINDS = ("merged", "done", "abandoned")
FAILURE_STATES = {"failed", "blocked", "interrupted", "cancelled"}
RUNNING = {"queued": "Queued", "starting": "Starting", "working": "Working"}
# A successful attempt linked like this continues the work of the attempt it follows.
CHAIN_LINKS = {"retry", "resume", "fix"}
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def valid_commit(value) -> bool:
    return isinstance(value, str) and bool(COMMIT_RE.match(value))


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


TAIL = 4000  # runner results keep the end of the output, and a limit message is the last thing printed


def tail(text: str | None) -> str:
    return (text or "")[-TAIL:]


def is_limit_text(text: str | None) -> bool:
    """A usage-limit message from Claude ("You've hit your ... limit") or Codex, matched loosely."""
    low = (text or "").lower()
    return "limit" in low and bool(re.search(r"\breset|try again", low))


_PROVIDER_LIMIT_RE = re.compile(
    r"^\s*(?:error\s*[:\-]\s*)?(?:you(?:'|\u2019)ve hit your\b|you have hit your\b|claude(?: ai)? usage limit reached)",
    re.IGNORECASE)


def provider_limit(task: dict, job: dict | None = None) -> bool:
    """Whether a failed run hit the provider's own usage limit; this, not is_limit_text, drives enforcement.

    The run must have exited non-zero, and the provider's own message must open the text: Claude's result
    ("You've hit your 5-hour limit ... resets ...") or the Codex error line the runner captured (job.error).
    A report that merely talks about limits and reset times doesn't count."""
    job = job or {}
    code = job.get("exit_code") if job.get("exit_code") is not None else task.get("exit_code")
    if code in (0, None):
        return False
    for text in (job.get("result") or task.get("result"), job.get("error") or task.get("error")):
        head = (text or "").lstrip()
        if _PROVIDER_LIMIT_RE.match(head) and is_limit_text(tail(head)):
            return True
    return False


def failure_reason(task: dict, job: dict | None = None, state: str | None = None) -> str | None:
    """Why `task` ended badly (first match wins), or None when `state` is not a failure state.

    `task` is the stored row (gate_result may be JSON text); `job` is the runner job when we have one and
    wins over the row for result, exit code and gate.
    """
    state = state or task.get("state")
    if state not in FAILURE_STATES:
        return None
    job = job or {}
    if state == "cancelled":
        return "cancelled"
    if state == "interrupted":
        return "interrupted"
    if state == "blocked":
        return "denied"
    if task.get("failure_reason") == "loop":
        return "loop"  # set by the runner's loop cut (Part G 5)
    if task.get("failure_reason") == "stalled":
        # Only ever set explicitly by the stall stop (Part E 12.6); a resumed run clears it.
        return "stalled"
    if is_limit_text(tail(job.get("result") or task.get("result"))) or is_limit_text(tail(task.get("error"))):
        return "limit"
    gate = job.get("gate") if job.get("gate") is not None else _json(task.get("gate_result"))
    gate_state = gate.get("state") if isinstance(gate, dict) else None
    # A gate that never ran ("skipped" or no result: the agent itself failed) is an error, not a gate failure.
    if task.get("gate") and gate_state not in (None, "passed", "skipped"):
        return "gate_failed"
    return "error"


_RESET_RE = re.compile(
    r"(?:resets?|try again)(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b(?:\s*\(([^)\n]+)\))?"
    r"|(?:resets?|try again)(?:\s+at)?\s+(\d{1,2}):(\d{2})()()", re.IGNORECASE)


def parse_reset(text: str | None, after: float, local_tz: datetime.tzinfo | None = None) -> float | None:
    """The time a limit message says it resets today, as epoch seconds after `after`; None if it can't be read or
    that time has already passed and tomorrow's is more than LIKELY_RESET_SECONDS (5 h) away.

    "resets 2:30pm (Africa/Johannesburg)" uses the named zone; without one the machine's (or `local_tz`).
    """
    for m in _RESET_RE.finditer(text or ""):
        hour_text, minute_text, meridiem, zone = (m.group(1), m.group(2), m.group(3), m.group(4)) if m.group(1) \
            else (m.group(5), m.group(6), None, None)
        hour, minute = int(hour_text), int(minute_text or 0)
        if meridiem:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if meridiem.lower() == "p" else 0)
        elif hour > 23:
            continue
        if minute > 59:
            continue
        tz = local_tz or datetime.datetime.fromtimestamp(after).astimezone().tzinfo
        if zone:
            try:
                tz = ZoneInfo(zone.strip())
            except (ZoneInfoNotFoundError, ValueError, OSError):
                pass
        moment = datetime.datetime.fromtimestamp(after, tz).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if moment.timestamp() <= after:
            # Already passed today. It means tomorrow only when that is within a usage window ("resets 1am" seen at
            # 22:00); further off is unknown, never a false 22-hour limit ("1:47 PM" seen at 15:00). A task that
            # really is limited is found by the provider's own record (docs/SPEC-dashboard-truth.md 11).
            moment += datetime.timedelta(days=1)
            if moment.timestamp() - after > LIKELY_RESET_SECONDS:
                return None
        return moment.timestamp()
    return None


def clock(ts: float, tz: datetime.tzinfo | None = None) -> str:
    """12-hour clock time, e.g. "2:30 PM"."""
    moment = datetime.datetime.fromtimestamp(ts, tz) if tz else datetime.datetime.fromtimestamp(ts).astimezone()
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


LIKELY_RESET_SECONDS = 5 * 3600  # Claude's usage windows are 5 hours


def display_status(task: dict, tz: datetime.tzinfo | None = None, now: float | None = None) -> dict:
    """{"tone", "label"} for a task row: what to show a person.

    `tz` only affects the reset clock time; `now` (default: the current time) decides whether a limit pause
    is still in the future, so the function stays pure and testable.
    """
    now = time.time() if now is None else now
    state = task.get("state")
    if state in RUNNING:
        return {"tone": "running", "label": RUNNING[state]}
    if state == "awaiting_approval":
        return {"tone": "attention", "label": "Blocked: needs approval"}
    resolution = _json(task.get("resolution")) or {}
    kind = resolution.get("kind")
    # Rows 2-5 of the matrix (docs/SPEC-dashboard-truth.md 2.1): settled outcomes beat the raw state.
    if kind == "resolved_by":
        return {"tone": "muted", "label": f"Superseded by {resolution.get('task_id') or resolution.get('title') or 'a later attempt'}"}
    if kind == "done_elsewhere":
        commit = f" · {resolution['commit']}" if resolution.get("commit") else ""
        return {"tone": "resolved", "label": f"Done elsewhere{commit}"}
    if kind == "dismissed":
        return {"tone": "muted", "label": "Dismissed"}
    if kind == "handed_off":
        to = resolution.get("task_id") or resolution.get("model") or resolution.get("agent") or "another agent"
        return {"tone": "resolved", "label": f"Handed off → {to}"}
    if kind == "handing_off":
        return {"tone": "running", "label": "Handing off"}
    reason = task.get("failure_reason")
    if state == "blocked":
        return {"tone": "attention", "label": "Blocked: needs approval"}
    if state in ("failed", "interrupted"):
        if reason == "limit":
            resets = task.get("resets_at")
            if resets:
                if resets <= now:
                    return {"tone": "attention", "label": "Limit cleared: resume or hand off"}
                return {"tone": "paused", "label": f"Paused: limit, resets {clock(resets, tz)}"}
            ended = task.get("ended_at")
            if ended and now - ended > LIKELY_RESET_SECONDS:
                return {"tone": "attention", "label": "Limit cleared: resume or hand off"}
            return {"tone": "paused", "label": "Paused: usage limit"}
        if state == "interrupted" or reason == "interrupted":
            return {"tone": "attention", "label": "Interrupted"}
        label = {"gate_failed": "Failed: gate", "error": "Failed: error", "denied": "Failed: denied",
                 "stalled": "Failed: stalled", "loop": "Failed: loop"}.get(reason, "Failed")
        return {"tone": "attention", "label": label}
    if state == "review":
        return {"tone": "ok", "label": "Ready for review"}
    if state == "done":
        return {"tone": "ok", "label": "Done"}
    if state == "cancelled":
        return {"tone": "muted", "label": "Cancelled"}
    return {"tone": "muted", "label": str(state or "Unknown")}


def resolution_targets(success: dict, items: dict[str, dict]) -> list[str]:
    """Ids of earlier attempts in the same work item that `success` resolves.

    `items` is every task of the work item keyed by id. A failed/interrupted/blocked attempt with no
    resolution is resolved when it has the same non-empty link as `success` (the same kind of step), or when
    it sits on `success`'s retry/resume/fix chain (parent, grandparent, ... while the links stay in that set).
    A failed review is never resolved by a successful build, nor the other way round.
    """
    chain: set[str] = set()
    current = success
    while current.get("link") in CHAIN_LINKS and current.get("parent_id") in items and current["parent_id"] not in chain:
        current = items[current["parent_id"]]
        chain.add(current["id"])
    out = []
    for item in items.values():
        if item["id"] == success["id"] or item.get("state") not in ("failed", "interrupted", "blocked"):
            continue
        if _json(item.get("resolution")):
            continue
        if item["id"] in chain or (success.get("link") and item.get("link") == success["link"]):
            out.append(item["id"])
    return out


def now() -> float:
    return time.time()

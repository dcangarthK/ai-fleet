"""Orchestrator presence, derived from bridge calls (docs/SPEC-live-monitor.md 2.4). In memory only: nothing here is
persisted, and nothing here emits SSE events.

SESSIONS: session id -> {session, model, account_label, last_seen}, at most MAX_SESSIONS (oldest evicted first).
WAITS: session id -> the task ids of its open GET /api/tasks/wait; LAST_WAIT: session id -> {ids, ended_at}.
"""
import re
import time

VALUE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
MAX_SESSIONS = 20
STALE_SECONDS = 15 * 60

SESSIONS: dict[str, dict] = {}
WAITS: dict[str, list[str]] = {}
LAST_WAIT: dict[str, dict] = {}


def reset() -> None:
    SESSIONS.clear()
    WAITS.clear()
    LAST_WAIT.clear()


def _valid(value: str | None) -> str | None:
    value = (value or "").strip()
    return value if VALUE_RE.fullmatch(value) else None


def session_of(headers) -> str | None:
    return _valid(headers.get("x-fleet-session"))


def note(headers, scope: str, known_labels) -> None:
    """Record a bridge call. Only local or operate+ callers count (never view scope); invalid values are ignored,
    and an unknown account label is dropped, never an error. `known_labels` is called only when needed."""
    if scope not in ("operate", "admin"):
        return
    session = session_of(headers)
    if not session:
        return
    account = _valid(headers.get("x-fleet-account"))
    if account and account not in known_labels():
        account = None
    entry = SESSIONS.pop(session, None) or {"session": session}
    entry.update(model=_valid(headers.get("x-fleet-model")) or entry.get("model"),
                 account_label=account or entry.get("account_label"), last_seen=time.time())
    SESSIONS[session] = entry  # re-inserted: dict order is least recently seen first
    while len(SESSIONS) > MAX_SESSIONS:
        oldest = next(iter(SESSIONS))
        SESSIONS.pop(oldest)
        WAITS.pop(oldest, None)
        LAST_WAIT.pop(oldest, None)


def wait_started(session: str | None, ids: list[str]) -> None:
    if session:
        WAITS[session] = list(ids)


def wait_ended(session: str | None, ids: list[str]) -> None:
    if session:
        WAITS.pop(session, None)
        LAST_WAIT[session] = {"ids": list(ids), "ended_at": time.time()}


def sessions(now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    out = []
    for entry in reversed(list(SESSIONS.values())):  # most recently seen first
        last = LAST_WAIT.get(entry["session"])
        out.append({**entry, "stale": now - entry["last_seen"] > STALE_SECONDS,
                    "waiting_on": WAITS.get(entry["session"]),
                    "last_wait": ({"ids": last["ids"], "ago_s": round(now - last["ended_at"])} if last else None)})
    return out

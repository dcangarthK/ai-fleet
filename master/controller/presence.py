"""Orchestrator presence, derived from bridge calls (docs/SPEC-live-monitor.md 2.4). Nothing here emits SSE events.

SESSIONS: session id -> {session, model, account_label, last_seen}, at most MAX_SESSIONS (oldest evicted first).
Each session's last known presence is also kept in fleet.db (table `presence`, docs/SPEC-dashboard-truth.md req 8).
After a controller restart it is loaded back on first use with `restored` set: it shows as stale since its last_seen
until a new request carrying X-Fleet-Session refreshes it. Waits are not persisted.
WAITS: session id -> the task ids of its open GET /api/tasks/wait; LAST_WAIT: session id -> {ids, ended_at}.
"""
import re
import time

from store import db

VALUE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
MAX_SESSIONS = 20
STALE_SECONDS = 15 * 60
WRITE_EVERY = 60.0  # seconds: a plain refresh within this of the last saved one is not written again

SESSIONS: dict[str, dict] = {}
WAITS: dict[str, list[str]] = {}
LAST_WAIT: dict[str, dict] = {}
_loaded = False
_written: dict[str, float] = {}  # session id -> last_seen as last saved


def reset() -> None:
    """Forget everything, including what is saved (tests)."""
    global _loaded
    SESSIONS.clear()
    WAITS.clear()
    LAST_WAIT.clear()
    _written.clear()
    _loaded = True
    try:
        with db() as c:
            c.execute("DELETE FROM presence")
    except Exception as exc:  # no database yet: nothing was saved
        print(f"presence reset: {exc!r}")


def forget_loaded() -> None:
    """Make the next use load the saved presence again (what a restart does; tests)."""
    global _loaded
    _loaded = False
    _written.clear()


def _load() -> None:
    """Once per process: bring back the saved sessions, marked restored (stale until refreshed)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with db() as c:
            rows = c.execute("SELECT session, model, account_label, last_seen FROM presence "
                             "ORDER BY last_seen DESC LIMIT ?", (MAX_SESSIONS,)).fetchall()
    except Exception as exc:  # presence is informational: start empty
        print(f"presence load failed: {exc!r}")
        return
    for r in reversed(rows):  # oldest first, so dict order stays least recently seen first
        if r["session"] not in SESSIONS:
            SESSIONS[r["session"]] = {"session": r["session"], "model": r["model"],
                                      "account_label": r["account_label"], "last_seen": r["last_seen"],
                                      "restored": True}
            _written[r["session"]] = r["last_seen"]


def _save(entry: dict, changed: bool, evicted: list[str]) -> None:
    """Write the entry when it is new or changed or the saved copy is a minute old; drop evicted sessions."""
    try:
        with db() as c:
            session = entry["session"]
            if changed or entry["last_seen"] - _written.get(session, 0) >= WRITE_EVERY:
                c.execute("INSERT INTO presence (session, model, account_label, last_seen) VALUES (?,?,?,?) "
                          "ON CONFLICT(session) DO UPDATE SET model=excluded.model, "
                          "account_label=excluded.account_label, last_seen=excluded.last_seen",
                          (session, entry.get("model"), entry.get("account_label"), entry["last_seen"]))
                _written[session] = entry["last_seen"]
            for gone in evicted:
                c.execute("DELETE FROM presence WHERE session=?", (gone,))
                _written.pop(gone, None)
    except Exception as exc:  # presence is informational: it never fails a request
        print(f"presence save failed: {exc!r}")


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
    _load()
    old = SESSIONS.pop(session, None)
    entry = old or {"session": session}
    model = _valid(headers.get("x-fleet-model")) or entry.get("model")
    label = account or entry.get("account_label")
    changed = old is None or old.get("restored", False) or (old.get("model"), old.get("account_label")) != (model, label)
    entry.pop("restored", None)
    entry.update(model=model, account_label=label, last_seen=time.time())
    SESSIONS[session] = entry  # re-inserted: dict order is least recently seen first
    evicted = []
    while len(SESSIONS) > MAX_SESSIONS:
        oldest = next(iter(SESSIONS))
        SESSIONS.pop(oldest)
        WAITS.pop(oldest, None)
        LAST_WAIT.pop(oldest, None)
        evicted.append(oldest)
    _save(entry, changed, evicted)


def wait_started(session: str | None, ids: list[str]) -> None:
    if session:
        WAITS[session] = list(ids)


def wait_ended(session: str | None, ids: list[str]) -> None:
    if session:
        WAITS.pop(session, None)
        LAST_WAIT[session] = {"ids": list(ids), "ended_at": time.time()}


def sessions(now: float | None = None) -> list[dict]:
    """Most recently seen first. `stale` is true for a restored session (not seen since the restart) or one not
    seen for STALE_SECONDS; `stale_since` is then its last_seen, else null."""
    _load()
    now = time.time() if now is None else now
    out = []
    for entry in reversed(list(SESSIONS.values())):
        last = LAST_WAIT.get(entry["session"])
        stale = bool(entry.get("restored")) or now - entry["last_seen"] > STALE_SECONDS
        out.append({**{k: v for k, v in entry.items() if k != "restored"}, "stale": stale,
                    "stale_since": entry["last_seen"] if stale else None,
                    "waiting_on": WAITS.get(entry["session"]),
                    "last_wait": ({"ids": last["ids"], "ago_s": round(now - last["ended_at"])} if last else None)})
    return out

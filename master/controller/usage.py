"""Part G 1-2 (docs/SPEC-capacity-placement.md): live capacity from runner heartbeats, and what it says about a limit.

Pure helpers, no database: the runner reports `capacity` (per account: free slots and live usage windows),
`clean_capacity` keeps only what the contract allows and turns each record's age into an `observed_at` on the
controller's clock, and `verdict` reads one account's windows. Records older than STALE_S are stale: they never
clear a limit. `mode()` is the shadow/live switch for requirements 1-3 (env FLEET_CAPACITY_MODE, default shadow).
"""
import os
import re
import time

STALE_S = 30 * 60  # spec 3 req 1: older usage is labelled stale and never clears a limit
WINDOWS = ("5h", "weekly")
SOURCES = ("stream-json", "rollout", "inferred")
AGENTS = ("claude", "codex")
PROFILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MODES = ("off", "shadow", "live")

clock = time.time  # tests replace this with a fake clock


def now() -> float:
    return clock()


def mode() -> str:
    """"shadow" (default): record what would be decided, change nothing. "live": act. "off": do nothing."""
    value = os.environ.get("FLEET_CAPACITY_MODE", "shadow").strip().lower()
    return value if value in MODES else "shadow"


def is_live() -> bool:
    return mode() == "live"


def _number(value, high: float = 1e12):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0:
        return None
    return float(min(value, high))


def clean_capacity(value, at: float | None = None) -> list[dict] | None:
    """The runner's `capacity`: [{agent, profile, slots_free, usage: [{window, used_percent, resets_at, source,
    age_s}], rejected_until}], at most 16 entries, only these fields, wrong types dropped. Each usage record gets
    `observed_at` = `at` (the controller's receive time) minus the runner's own `age_s`, so clock skew between the
    machines never makes old data look fresh. None when nothing usable is left (the field is dropped)."""
    at = time.time() if at is None else at
    if not isinstance(value, list):
        return None
    out = []
    for item in value[:16]:
        if not isinstance(item, dict) or item.get("agent") not in AGENTS or not isinstance(item.get("profile"), str) \
                or not PROFILE_RE.fullmatch(item["profile"]):
            continue
        entry: dict = {"agent": item["agent"], "profile": item["profile"], "usage": []}
        free = _number(item.get("slots_free"), 1000)
        entry["slots_free"] = int(free) if free is not None else None
        for w in (item.get("usage") if isinstance(item.get("usage"), list) else [])[:4]:
            if not isinstance(w, dict) or w.get("window") not in WINDOWS:
                continue
            used, reset = _number(w.get("used_percent"), 1000), _number(w.get("resets_at"))
            age = _number(w.get("age_s"), 10 * 365 * 86400)
            if used is None or reset is None or age is None:
                continue
            entry["usage"].append({"window": w["window"], "used_percent": used, "resets_at": reset,
                                   "source": w["source"] if w.get("source") in SOURCES else "inferred",
                                   "observed_at": at - age})
        entry["rejected_until"] = _number(item.get("rejected_until"))
        out.append(entry)
    return out or None


def verdict(record: dict | None, now_s: float) -> dict:
    """What one account's live usage says: {"state": "limited", "until"} (a fresh window is used up with its reset
    still ahead, or a rejected event names an exact reset still ahead), {"state": "headroom", "observed_at"} (every
    window of the record is fresh and under 100%: older limit failures are cleared), or {"state": "unknown"} (no
    record, or it is stale: it never clears anything). `record` is {"windows": {name: {...}}, "rejected_until"}."""
    if not record:
        return {"state": "unknown"}
    windows = [w for w in (record.get("windows") or {}).values() if w]
    ends = [w["resets_at"] for w in windows if now_s - w["observed_at"] <= STALE_S and w["used_percent"] >= 100
            and w["resets_at"] > now_s]
    rejected = record.get("rejected_until")
    if rejected and rejected > now_s:
        ends.append(rejected)  # exact, and rejects never clear: it holds until its reset whatever its age
    if ends:
        return {"state": "limited", "until": max(ends)}
    if windows and all(now_s - w["observed_at"] <= STALE_S and w["used_percent"] < 100 for w in windows):
        return {"state": "headroom", "observed_at": min(w["observed_at"] for w in windows)}
    return {"state": "unknown"}

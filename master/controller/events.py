"""Task events: stored, broadcast to waiters (long-poll/SSE), and handed to plugins."""
import asyncio
import json
import subprocess
import threading
import time
import fnmatch
from pathlib import Path

from store import db

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"
_changed = asyncio.Event()


def emit(task_id: str | None, type_: str, data: dict | None = None) -> int:
    with db() as c:
        cur = c.execute("INSERT INTO events (ts, task_id, type, data) VALUES (?,?,?,?)",
                        (time.time(), task_id, type_, json.dumps(data or {})))
        event_id = cur.lastrowid
    event = {"id": event_id, "ts": time.time(), "task_id": task_id, "type": type_, "data": data or {}}
    _wake()
    run_plugins(event)
    return event_id


_loop: asyncio.AbstractEventLoop | None = None


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def _wake() -> None:
    # asyncio.Event isn't thread-safe; sync endpoints run in a threadpool, so hop onto the loop.
    if _loop is None:
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is _loop:
        _swap()
    else:
        _loop.call_soon_threadsafe(_swap)


def _swap() -> None:
    # Wake every waiter, then arm a fresh Event for the next round.
    global _changed
    old, _changed = _changed, asyncio.Event()
    old.set()


def snapshot() -> asyncio.Event:
    """Take this BEFORE reading state; anything emitted afterwards sets it, so no wakeup is lost."""
    return _changed


async def changed(since: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(since.wait(), timeout)
    except asyncio.TimeoutError:
        pass


def events_after(after: int, limit: int = 200) -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after, limit)).fetchall()
    return [dict(r) | {"data": json.loads(r["data"])} for r in rows]


def last_event_id() -> int:
    with db() as c:
        return c.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]


# ---------- plugins ----------
# A plugin is master/plugins/<name>.json: {"events": ["task.blocked", ...], "command": ["sh", "x.sh"]}.
# The command runs from the plugins directory with the event JSON on stdin. Local files only.

def load_plugins() -> list[dict]:
    plugins = []
    for manifest in sorted(PLUGIN_DIR.glob("*.json")):
        try:
            spec = json.loads(manifest.read_text())
            if spec.get("enabled", True) and spec.get("command"):
                plugins.append({"name": manifest.stem, "events": set(spec.get("events") or []),
                                "command": [str(x) for x in spec["command"]]})
        except (OSError, ValueError) as e:
            print(f"plugin {manifest.name} ignored: {e}")
    return plugins


PLUGINS = load_plugins()


def run_plugins(event: dict) -> None:
    for p in PLUGINS:
        if _plugin_wants(p, event):
            threading.Thread(target=_run_plugin, args=(p, event), daemon=True).start()


def _plugin_wants(plugin: dict, event: dict) -> bool:
    """Database rules override a manifest's legacy event list for that plugin."""
    try:
        with db() as c:
            rules = c.execute("SELECT event_glob,enabled FROM notify_rules WHERE plugin=?", (plugin["name"],)).fetchall()
    except Exception:  # startup migrations may not have run yet
        rules = []
    if not rules:
        return event["type"] in plugin["events"] or "*" in plugin["events"]
    for row in rules:
        if not row["enabled"]:
            continue
        pattern = row["event_glob"]
        if pattern in ("model.state", "model.state:blocked|failed"):
            if event["type"] == "model.state" and event.get("data", {}).get("state") in ("blocked", "failed"):
                return True
        elif fnmatch.fnmatchcase(event["type"], pattern):
            return True
    return False


def send_test(plugin_name: str, rule_id: str) -> bool:
    plugin = next((item for item in PLUGINS if item["name"] == plugin_name), None)
    if not plugin:
        return False
    event = {"id": 0, "ts": time.time(), "task_id": None, "type": "notify.test",
             "data": {"rule_id": rule_id}}
    threading.Thread(target=_run_plugin, args=(plugin, event), daemon=True).start()
    return True


def _run_plugin(plugin: dict, event: dict) -> None:
    try:
        subprocess.run(plugin["command"], input=json.dumps(event), text=True, cwd=PLUGIN_DIR,
                       capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"plugin {plugin['name']} failed: {e}")

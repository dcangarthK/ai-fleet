"""Per-task token telemetry from streamed job event logs."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query


router = APIRouter()

DEFAULT_CAPS = {
    "build": {"turns": 120, "context": 150_000},
    "fix": {"turns": 120, "context": 150_000},
    "review": {"turns": 30, "context": 100_000},
    "re_review": {"turns": 15},
    "test": {"turns": 20},
}
LEAN_TURN1_TARGET = 8_000
TASK_ID_RE = re.compile(r"^t-[0-9a-f]{6,}$")
_cache: dict[str, tuple[str, int, int, dict[str, Any]]] = {}


def _empty(job_id: str) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "turns": 0,
        "peak_context": 0,
        "turn1_context": 0,
        "input": 0,
        "cache_read": 0,
        "cache_write": 0,
        "output": 0,
        "tool_counts": {},
        "init": {"tools": 0, "mcp": 0, "skills": 0, "plugins": 0},
    }


def _count(value: object) -> int:
    return len(value) if isinstance(value, (list, dict, tuple, set)) else 0


def _number(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _cap(value: object) -> float:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    return value if ok else 0


def parse_events(path: Path, job_id: str | None = None) -> dict[str, Any]:
    """Stream a Claude JSONL event log into a conservative telemetry summary."""
    job_id = job_id or path.parent.name
    try:
        stat = path.stat()
    except FileNotFoundError:
        return _empty(job_id)
    cached = _cache.get(job_id)
    marker = (str(path), stat.st_mtime_ns, stat.st_size)
    if cached and cached[:3] == marker:
        return dict(cached[3])

    result = _empty(job_id)
    usages: dict[object, dict] = {}
    seen_tools: set[object] = set()
    with path.open(encoding="utf-8", errors="replace") as events:
        for lineno, line in enumerate(events):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                result["init"] = {
                    "tools": _count(event.get("tools")),
                    "mcp": _count(event.get("mcp_servers")),
                    "skills": _count(event.get("skills")),
                    "plugins": _count(event.get("plugins")),
                }
                continue
            if event.get("type") != "assistant" or event.get("parent_tool_use_id"):
                continue
            message = event.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(usage, dict):
                continue
            # One API message is streamed as several events (one per content block) sharing id and usage.
            msg_id = message.get("id")
            key = msg_id if isinstance(msg_id, str) and msg_id else ("line", lineno)
            usages[key] = usage  # dict keeps first-seen order; last usage wins
            content = message.get("content")
            if isinstance(content, list):
                counts = result["tool_counts"]
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name")
                        block_id = block.get("id")
                        if block_id is not None:
                            if block_id in seen_tools:
                                continue
                            seen_tools.add(block_id)
                        if isinstance(name, str):
                            counts[name] = counts.get(name, 0) + 1
    for usage in usages.values():
        input_tokens = _number(usage.get("input_tokens"))
        cache_read = _number(usage.get("cache_read_input_tokens"))
        cache_write = _number(usage.get("cache_creation_input_tokens"))
        context = input_tokens + cache_read + cache_write
        result["turns"] += 1
        result["peak_context"] = max(result["peak_context"], context)
        if result["turns"] == 1:
            result["turn1_context"] = context
        result["input"] += input_tokens
        result["cache_read"] += cache_read
        result["cache_write"] += cache_write
        result["output"] += _number(usage.get("output_tokens"))
    _cache[job_id] = (*marker, dict(result))
    return result


def _caps() -> dict[str, dict[str, int]]:
    caps = {kind: dict(values) for kind, values in DEFAULT_CAPS.items()}
    raw = os.environ.get("FLEET_TOKEN_CAPS")
    if not raw:
        return caps
    try:
        supplied = json.loads(raw)
    except json.JSONDecodeError:
        return caps
    if not isinstance(supplied, dict):
        return caps
    for kind, values in supplied.items():
        if isinstance(kind, str) and isinstance(values, dict):
            caps[kind] = {key: _cap(value) for key, value in values.items()}
    return caps


def _kind(task_id: str) -> str | None:
    """Task kind from the tasks table; None when unknown (never guessed from the id)."""
    try:
        from store import db
        with db() as c:
            row = c.execute("SELECT kind FROM tasks WHERE id=?", (task_id,)).fetchone()
    except Exception:
        return None
    kind = row["kind"] if row else None
    return kind if isinstance(kind, str) and kind else None


def _model(task_id: str) -> str | None:
    try:
        from store import db
        with db() as c:
            row = c.execute("SELECT model FROM tasks WHERE id=?", (task_id,)).fetchone()
    except Exception:
        return None
    model = row["model"] if row else None
    return model if isinstance(model, str) and model else None


def _row(path: Path) -> dict[str, Any]:
    task_id = path.parent.name
    row = parse_events(path, task_id)
    kind = _kind(task_id)
    row["task_id"] = task_id
    if kind is not None:
        row["kind"] = kind
    model = _model(task_id)
    if model is not None:
        row["model"] = model
    alerts: list[str] = []
    caps = _caps().get(kind, {}) if kind else {}
    # What Live draws the bars against: the task's caps and its share of each (None when the kind has no such cap).
    row["caps"] = {"turns": caps.get("turns") or None, "context": caps.get("context") or None,
                   "turn1": LEAN_TURN1_TARGET}
    row["usage"] = {
        "turns": round(row["turns"] / caps["turns"], 3) if caps.get("turns") else None,
        "context": round(row["peak_context"] / caps["context"], 3) if caps.get("context") else None,
        "turn1": round(row["turn1_context"] / LEAN_TURN1_TARGET, 3),
    }
    for field, cap in caps.items():
        value = row["peak_context"] if field == "context" else row.get(field, 0)
        if cap and value >= cap * 0.8:
            alerts.append(f"{field}_80_percent")
    if row["turn1_context"] > LEAN_TURN1_TARGET:
        alerts.append("turn1_context")
    row["alerts"] = alerts
    return row


def _jobs_dir() -> Path:
    return Path(os.environ.get("FLEET_JOBS_DIR", "~/.ai-fleet/jobs")).expanduser()


def _event_paths() -> list[Path]:
    jobs_dir = _jobs_dir()
    if not jobs_dir.is_dir():
        return []
    return sorted(jobs_dir.glob("*/events.jsonl"))


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Spend report: tokens per kind and per model (input = fresh + cache read + cache write)."""
    def blank() -> dict[str, int]:
        return {"tasks": 0, "turns": 0, "input": 0, "cache_read": 0, "cache_write": 0, "output": 0}

    by_kind: dict[str, dict[str, int]] = {}
    by_model: dict[str, dict[str, int]] = {}
    for row in rows:
        fresh_and_cache = row.get("input", 0) + row.get("cache_read", 0) + row.get("cache_write", 0)
        for table, key in ((by_kind, row.get("kind") or "unknown"), (by_model, row.get("model") or "unknown")):
            entry = table.setdefault(key, blank())
            entry["tasks"] += 1
            entry["turns"] += row.get("turns", 0)
            entry["input"] += fresh_and_cache
            entry["cache_read"] += row.get("cache_read", 0)
            entry["cache_write"] += row.get("cache_write", 0)
            entry["output"] += row.get("output", 0)
    return {"by_kind": by_kind, "by_model": by_model}


@router.get("/api/tokens/summary")
def tokens_summary(since: float | None = Query(default=None)) -> dict[str, Any]:
    return summarise(tokens_since(since)["tasks"])


@router.get("/api/tokens/{task_id}")
def task_tokens(task_id: str) -> dict[str, Any]:
    if not TASK_ID_RE.match(task_id):
        raise HTTPException(status_code=404, detail="task token log not found")
    root = _jobs_dir().resolve()
    path = (root / task_id / "events.jsonl").resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="task token log not found")
    try:
        return _row(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="task token log not found")


@router.get("/api/tokens")
def tokens_since(since: float | None = Query(default=None)) -> dict[str, list[dict[str, Any]]]:
    rows = []
    for path in _event_paths():
        try:
            if since is not None and path.stat().st_mtime < since:
                continue
            rows.append(_row(path))
        except FileNotFoundError:
            continue
    return {"tasks": rows}

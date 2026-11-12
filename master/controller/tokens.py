"""Per-task token telemetry from streamed job event logs."""
from __future__ import annotations

import json
import os
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
_cache: dict[str, tuple[str, int, int, dict[str, Any]]] = {}


def _empty(job_id: str) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "turns": 0,
        "peak_context": 0,
        "turn1_context": 0,
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
    with path.open(encoding="utf-8") as events:
        for line in events:
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
            if event.get("type") != "assistant":
                continue
            message = event.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(usage, dict):
                continue
            input_tokens = _number(usage.get("input_tokens"))
            cache_read = _number(usage.get("cache_read_input_tokens"))
            cache_write = _number(usage.get("cache_creation_input_tokens"))
            output = _number(usage.get("output_tokens"))
            context = input_tokens + cache_read + cache_write
            result["turns"] += 1
            result["peak_context"] = max(result["peak_context"], context)
            if result["turns"] == 1:
                result["turn1_context"] = context
            result["cache_read"] += cache_read
            result["cache_write"] += cache_write
            result["output"] += output
            content = message.get("content")
            if isinstance(content, list):
                counts = result["tool_counts"]
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name")
                        if isinstance(name, str):
                            counts[name] = counts.get(name, 0) + 1
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
            caps[kind] = {key: _number(value) for key, value in values.items()}
    return caps


def _kind(task_id: str) -> str | None:
    for kind in _caps():
        if task_id == kind or task_id.startswith(f"{kind}-") or task_id.startswith(f"{kind}_"):
            return kind
    return None


def _row(path: Path) -> dict[str, Any]:
    task_id = path.parent.name
    row = parse_events(path, task_id)
    kind = _kind(task_id)
    row["task_id"] = task_id
    if kind is not None:
        row["kind"] = kind
    alerts: list[str] = []
    caps = _caps().get(kind, {}) if kind else {}
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


@router.get("/api/tokens/{task_id}")
async def task_tokens(task_id: str) -> dict[str, Any]:
    path = _jobs_dir() / task_id / "events.jsonl"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="task token log not found")
    return _row(path)


@router.get("/api/tokens")
async def tokens_since(since: float | None = Query(default=None)) -> dict[str, list[dict[str, Any]]]:
    rows = []
    for path in _event_paths():
        if since is not None and path.stat().st_mtime < since:
            continue
        rows.append(_row(path))
    return {"tasks": rows}

"""Versioned prompt-only skills, repository import, bindings, and task snapshots."""
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from store import db


ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
AGENTS = {"claude", "codex", "ollama"}
SCOPES = {"fleet", "node", "role", "agent", "task"}
DEFAULT_DIR = Path(__file__).resolve().parents[2] / "skills"


def digest(description: str, agents: list[str], risk: str, body: str) -> str:
    encoded = json.dumps({"description": description, "agents": sorted(agents), "risk": risk, "body": body},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate(identifier: str, description, agents, risk, body) -> tuple[str, list[str], str, str]:
    if not ID_RE.fullmatch(identifier):
        raise ValueError("skill id must match ^[a-z][a-z0-9-]{1,40}$")
    if not isinstance(description, str) or not description.strip() or len(description) > 200:
        raise ValueError("description must be 1 to 200 characters")
    if not isinstance(agents, list) or not agents or any(a not in AGENTS for a in agents):
        raise ValueError(f"agents must be a non-empty list drawn from {sorted(AGENTS)}")
    if risk != "prompt-only":
        raise ValueError(f"risk {risk!r} is not supported yet")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("skill body is empty")
    if len(body.encode("utf-8")) > 24 * 1024:
        raise ValueError("skill body exceeds 24 KB")
    return description.strip(), list(dict.fromkeys(agents)), risk, body


def put(identifier: str, description: str, agents: list[str], risk: str, body: str,
        author: str, *, skip_digest: bool = False, requested_version: int | None = None,
        first_bindings: list[tuple[str, str]] | None = None) -> dict | None:
    """Add a version. ``first_bindings`` are inserted in the same transaction, and only when no
    version by ``author`` exists yet, so a skill never commits without its initial bindings."""
    description, agents, risk, body = validate(identifier, description, agents, risk, body)
    checksum = digest(description, agents, risk, body)
    now = time.time()
    added = []
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        if skip_digest and c.execute("SELECT 1 FROM skills WHERE id=? AND digest=?", (identifier, checksum)).fetchone():
            return None
        first = not c.execute("SELECT 1 FROM skills WHERE id=? AND author=?", (identifier, author)).fetchone()
        next_version = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM skills WHERE id=?", (identifier,)).fetchone()[0]
        version = max(next_version, requested_version or 1)
        c.execute("INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?)",
                  (identifier, version, description, json.dumps(agents), risk, body, checksum, author, now))
        for scope, scope_id in (first_bindings or []) if first else []:
            c.execute("INSERT OR IGNORE INTO skill_bindings VALUES (?,?,?)", (identifier, scope, scope_id))
            added.append({"skill_id": identifier, "scope": scope, "scope_id": scope_id})
    return {"id": identifier, "version": version, "description": description, "agents": agents,
            "risk": risk, "body": body, "digest": checksum, "author": author, "created_at": now,
            "bindings": added}


def _view(row, bindings=None) -> dict:
    value = dict(row)
    value["agents"] = json.loads(value["agents"])
    value["bindings"] = bindings or []
    return value


def current() -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT * FROM skills s WHERE version=(SELECT MAX(version) FROM skills v WHERE v.id=s.id) ORDER BY id"
        ).fetchall()
        bindings = {}
        for row in c.execute("SELECT skill_id,scope,scope_id FROM skill_bindings ORDER BY scope,scope_id"):
            bindings.setdefault(row["skill_id"], []).append({"scope": row["scope"], "scope_id": row["scope_id"]})
    return [_view(row, bindings.get(row["id"], [])) for row in rows]


def history(identifier: str) -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM skills WHERE id=? ORDER BY version DESC", (identifier,)).fetchall()
    return [_view(row) for row in rows]


def bind(identifier: str, scope: str, scope_id: str) -> dict:
    if scope not in SCOPES:
        raise ValueError("scope must be fleet, node, role, agent or task")
    if (scope == "fleet") != (scope_id == "*") or (scope != "fleet" and not scope_id):
        raise ValueError("invalid scope_id")
    with db() as c:
        if not c.execute("SELECT 1 FROM skills WHERE id=?", (identifier,)).fetchone():
            raise KeyError(identifier)
        c.execute("INSERT OR IGNORE INTO skill_bindings VALUES (?,?,?)", (identifier, scope, scope_id))
    return {"skill_id": identifier, "scope": scope, "scope_id": scope_id}


def unbind(identifier: str, scope: str, scope_id: str) -> dict:
    with db() as c:
        if c.execute("DELETE FROM skill_bindings WHERE skill_id=? AND scope=? AND scope_id=?",
                     (identifier, scope, scope_id)).rowcount == 0:
            raise KeyError(identifier)
    return {"skill_id": identifier, "scope": scope, "scope_id": scope_id, "deleted": True}


def snapshot(node: str, role: str | None, agent: str, explicit: list[str] | None) -> list[dict]:
    wanted = {("fleet", "*"), ("node", node), ("agent", agent)}
    if role:
        wanted.add(("role", role))
    explicit_ids = list(dict.fromkeys(explicit or []))
    with db() as c:
        for identifier in explicit_ids:
            if not c.execute("SELECT 1 FROM skills WHERE id=?", (identifier,)).fetchone():
                raise ValueError(f"unknown skill {identifier}")
        bound = {row[0] for scope, scope_id in wanted for row in
                 c.execute("SELECT skill_id FROM skill_bindings WHERE scope=? AND scope_id=?", (scope, scope_id))}
        identifiers = sorted(bound | set(explicit_ids))
        rows = []
        for identifier in identifiers:
            row = c.execute("SELECT * FROM skills WHERE id=? ORDER BY version DESC LIMIT 1", (identifier,)).fetchone()
            if row and agent in json.loads(row["agents"]):
                rows.append(dict(row))
    return rows


def add_to_effective(effective: dict, selected: list[dict]) -> dict:
    if not selected:
        return effective | {"skills": []}
    # Preserve Phase 6's already-rendered layer headings byte for byte.
    skill_text = "\n\n".join(f"## skill: {s['id']} v{s['version']}\n{s['body']}" for s in selected)
    instructions = "\n\n".join(x for x in (effective.get("instructions", ""), skill_text) if x)
    payload = json.dumps({"instructions": instructions, "policy": effective["policy"]}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False).encode()
    return effective | {"instructions": instructions, "hash": hashlib.sha256(payload).hexdigest(),
                        "skills": [{"id": s["id"], "version": s["version"], "digest": s["digest"]}
                                   for s in selected]}


def _frontmatter(text: str) -> tuple[dict, str]:
    match = re.match(r"^---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$", text)
    if not match:
        raise ValueError("missing YAML front matter")
    meta = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise ValueError("invalid front matter")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key.strip()] = [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
        elif value.isdigit():
            meta[key.strip()] = int(value)
        else:
            meta[key.strip()] = value.strip("'\"")
    return meta, match.group(2)


def _bindings(value) -> list[tuple[str, str]]:
    """Parse front-matter ``bind:`` ("scope:id" or a list of them) before anything is written."""
    if value is None:
        return []
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list):
        raise ValueError("bind must be a list of scope:id")
    parsed = []
    for item in items:
        scope, separator, scope_id = str(item).partition(":")
        if not separator or scope not in SCOPES - {"task"} or not scope_id or (scope == "fleet") != (scope_id == "*"):
            raise ValueError(f"invalid bind {item!r}")
        parsed.append((scope, scope_id))
    return parsed


def import_repo(directory: str | Path | None = None) -> list[dict]:
    root = Path(directory or os.environ.get("FLEET_SKILLS_DIR", DEFAULT_DIR)).expanduser()
    imported = []
    if not root.is_dir():
        return imported
    for path in sorted(root.glob("*/SKILL.md")):
        try:
            meta, body = _frontmatter(path.read_text(encoding="utf-8"))
            identifier = meta.get("id")
            if identifier != path.parent.name:
                raise ValueError("front-matter id must match its directory")
            # Bindings apply only with the first repo version (same transaction), so an operator's unbind sticks.
            result = put(identifier, meta.get("description"), meta.get("agents"), meta.get("risk"), body,
                         "repo", skip_digest=True,
                         requested_version=meta.get("version") if isinstance(meta.get("version"), int) else None,
                         first_bindings=_bindings(meta.get("bind")))
            if result:
                imported.append(result)
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            print(f"skill {path} ignored: {exc}")
    return imported

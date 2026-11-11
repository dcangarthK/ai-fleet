"""Seed instructions from the repository: master/instructions/fleet.md and <scope>/<id>.md.

A file is imported only when its scope has no instruction yet (as version 1, author "repo"); an
existing version, typically an operator's edit, is never overwritten.
"""
import os
import time
from pathlib import Path

import tasks
from store import db

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "instructions"
SCOPES = ("node", "role", "agent")
MAX_BYTES = 16 * 1024


def _targets(root: Path) -> list[tuple[str, str, Path]]:
    found = []
    if (root / "fleet.md").is_file():
        found.append(("fleet", "*", root / "fleet.md"))
    for scope in SCOPES:
        found += [(scope, path.stem, path) for path in sorted((root / scope).glob("*.md"))]
    return found


def _check_scope_id(scope: str, scope_id: str) -> None:
    """The same scope_id rules as the PUT /api/instructions API."""
    if scope == "role" and not tasks.ROLE_RE.fullmatch(scope_id):
        raise ValueError("role scope_id must be a role name")
    if scope == "agent" and scope_id not in tasks.AGENTS:
        raise ValueError(f"agent scope_id must be one of {sorted(tasks.AGENTS)}")
    if scope == "node":
        with db() as c:
            if not c.execute("SELECT 1 FROM nodes WHERE id=?", (scope_id,)).fetchone():
                raise ValueError(f"unknown node {scope_id}")


def import_repo(directory: str | Path | None = None) -> list[dict]:
    root = Path(directory or os.environ.get("FLEET_INSTRUCTIONS_DIR", DEFAULT_DIR)).expanduser()
    imported = []
    if not root.is_dir():
        return imported
    for scope, scope_id, path in _targets(root):
        try:
            _check_scope_id(scope, scope_id)
            text = path.read_text(encoding="utf-8").strip()
            if not text or len(text.encode("utf-8")) > MAX_BYTES:
                raise ValueError("instruction must be 1 byte to 16 KB")
        except (OSError, ValueError) as exc:
            print(f"instruction {path} ignored: {exc}")
            continue
        now = time.time()
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM instructions WHERE scope=? AND scope_id=?", (scope, scope_id)).fetchone():
                continue
            c.execute("INSERT INTO instructions VALUES (?,?,?,?,?,?)", (scope, scope_id, 1, text, "repo", now))
        imported.append({"scope": scope, "scope_id": scope_id, "version": 1, "text": text,
                         "author": "repo", "created_at": now})
    return imported

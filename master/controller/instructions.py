"""Seed instructions from the repository: master/instructions/fleet.md and <scope>/<id>.md.

A file is imported only when its scope has no instruction yet (as version 1, author "repo"); an
existing version, typically an operator's edit, is never overwritten.
"""
import os
import time
from pathlib import Path

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


def import_repo(directory: str | Path | None = None) -> list[dict]:
    root = Path(directory or os.environ.get("FLEET_INSTRUCTIONS_DIR", DEFAULT_DIR)).expanduser()
    imported = []
    if not root.is_dir():
        return imported
    for scope, scope_id, path in _targets(root):
        try:
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

"""SQLite storage shared by the controller modules."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("FLEET_DB", Path(__file__).with_name("fleet.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, name TEXT, ip TEXT, os TEXT, engine TEXT, port INTEGER,
    models TEXT DEFAULT '[]', status TEXT DEFAULT 'pending',
    fails INTEGER DEFAULT 0, last_seen REAL, inventory TEXT);
CREATE TABLE IF NOT EXISTS assignments (
    role TEXT, node TEXT, model TEXT, state TEXT DEFAULT 'active', catalog_id TEXT,
    params TEXT DEFAULT '{}', PRIMARY KEY (role, node, model));
CREATE TABLE IF NOT EXISTS model_catalog (
    id TEXT PRIMARY KEY, reference TEXT, engine TEXT DEFAULT 'ollama', display_name TEXT,
    download_gb REAL, digest TEXT, min_engine TEXT, defaults TEXT DEFAULT '{}',
    suggested_roles TEXT DEFAULT '[]', evals TEXT DEFAULT '{}', source TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS node_models (
    node TEXT, catalog_id TEXT, desired TEXT, state TEXT, reason TEXT, op_id TEXT,
    progress TEXT DEFAULT '{}', ownership TEXT, params TEXT DEFAULT '{}', override_fit INTEGER DEFAULT 0,
    confirm_preexisting INTEGER DEFAULT 0, updated_at REAL, PRIMARY KEY (node, catalog_id));
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, agent TEXT, model TEXT, prompt TEXT,
    mode TEXT, repo TEXT, worktree INTEGER DEFAULT 1, gate TEXT, node_id TEXT,
    state TEXT, created_at REAL, updated_at REAL, started_at REAL, ended_at REAL,
    session_id TEXT, result TEXT, denials TEXT DEFAULT '[]', gate_result TEXT,
    exit_code INTEGER, branch TEXT, cwd TEXT, runs INTEGER DEFAULT 0, error TEXT,
    pending_message TEXT, agent_seconds REAL DEFAULT 0, run_started_at REAL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, task_id TEXT, type TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS tokens (
    id TEXT PRIMARY KEY, hash TEXT UNIQUE, scope TEXT, label TEXT, created_at REAL, last_used REAL);
"""

# Columns added after the first release; ALTERed in on startup for existing databases.
MIGRATIONS = {
    "nodes": {"inventory": "TEXT", "ceiling": "TEXT DEFAULT 'auto-edit'", "runner": "TEXT",
              "runner_fails": "INTEGER DEFAULT 0", "runner_token": "TEXT"},
    "tasks": {"agent_seconds": "REAL DEFAULT 0", "run_started_at": "REAL"},
    "assignments": {"state": "TEXT DEFAULT 'active'", "catalog_id": "TEXT", "params": "TEXT DEFAULT '{}'"},
}


@contextmanager
def db():
    """Connection that commits on success, rolls back on error, and always closes."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with db() as c:
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        _seed_models(c)


def _seed_models(c: sqlite3.Connection) -> None:
    """Seed evaluated models without doing network I/O; custom catalog data wins on later starts."""
    import json
    import time

    results = Path(__file__).resolve().parents[2] / "evals" / "results" / "results.json"
    try:
        rows = json.loads(results.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("model"), str):
            continue
        reference = row["model"] if ":" in row["model"].rsplit("/", 1)[-1] else row["model"] + ":latest"
        model_id = reference.replace("/", "-").replace(":", "-")
        roles = row.get("roles") if isinstance(row.get("roles"), dict) else {}
        scores = {name: result.get("score") for name, result in roles.items()
                  if isinstance(result, dict) and result.get("score") is not None}
        c.execute(
            "INSERT OR IGNORE INTO model_catalog "
            "(id,reference,engine,display_name,download_gb,digest,min_engine,defaults,suggested_roles,evals,source,created_at) "
            "VALUES (?,?, 'ollama', ?,NULL,NULL,NULL,'{}',?,?, 'evals',?)",
            (model_id, reference, reference, json.dumps(sorted(scores)), json.dumps(scores), time.time()),
        )

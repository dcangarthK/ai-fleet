"""Durable, serial eval queue and leaderboard/recommendation views."""
import asyncio
import json
import os
import secrets
import shlex
import sys
import time
from pathlib import Path

import events
from store import db


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS = ROOT / "evals" / "results" / "results.json"
DEFAULT_COMMAND = [sys.executable, str(ROOT / "evals" / "run_evals.py")]
KNOWN_ROLES = {path.stem for path in (ROOT / "evals" / "cases").glob("*.json")}


def results_path() -> Path:
    configured = Path(os.environ.get("FLEET_EVAL_RESULTS", DEFAULT_RESULTS)).expanduser()
    return configured / "results.json" if configured.is_dir() else configured


def read_results() -> list[dict]:
    try:
        value = json.loads(results_path().read_text(encoding="utf-8"))
        return [row for row in value if isinstance(row, dict) and isinstance(row.get("model"), str)]
    except (OSError, ValueError, TypeError):
        return []


def run_view(row) -> dict:
    value = dict(row)
    for field, fallback in (("roles", []), ("scores", None), ("errors", None)):
        try:
            value[field] = json.loads(value[field]) if value[field] is not None else fallback
        except (ValueError, TypeError):
            value[field] = fallback
    return value


def list_runs() -> list[dict]:
    with db() as c:
        return [run_view(row) for row in c.execute("SELECT * FROM eval_runs ORDER BY COALESCE(started_at,0) DESC, rowid DESC")]


def leaderboard() -> list[dict]:
    fields = ("model", "roles", "size_gb", "fits_16gb", "median_s", "tokens_per_s", "errors")
    return [{field: row.get(field) for field in fields} for row in read_results()]


def enqueue(model: str, node: str, roles: list[str]) -> dict:
    identifier = "e-" + secrets.token_hex(4)
    with db() as c:
        c.execute("INSERT INTO eval_runs VALUES (?,?,?,?, 'queued', NULL,NULL,NULL,NULL,NULL)",
                  (identifier, model, node, json.dumps(roles)))
        row = c.execute("SELECT * FROM eval_runs WHERE id=?", (identifier,)).fetchone()
    events.emit(None, "eval.queued", {"id": identifier, "model": model, "node": node})
    return run_view(row)


def _claim():
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT 1 FROM eval_runs WHERE state='running'").fetchone():
            return None
        row = c.execute("SELECT * FROM eval_runs WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
        if not row:
            return None
        started = time.time()
        c.execute("UPDATE eval_runs SET state='running',started_at=? WHERE id=? AND state='queued'",
                  (started, row["id"]))
        return run_view(c.execute("SELECT * FROM eval_runs WHERE id=?", (row["id"],)).fetchone())


def _matching_result(model: str, started: float) -> dict | None:
    candidates = [row for row in read_results() if row.get("model") == model
                  and isinstance(row.get("evaluated_at"), (int, float)) and row["evaluated_at"] >= started]
    return max(candidates, key=lambda row: row["evaluated_at"], default=None)


def _refresh_catalog(row: dict) -> None:
    scores = {role: value.get("score") for role, value in (row.get("roles") or {}).items()
              if isinstance(value, dict) and isinstance(value.get("score"), (int, float))}
    reference = row["model"] if ":" in row["model"].rsplit("/", 1)[-1] else row["model"] + ":latest"
    identifier = reference.replace("/", "-").replace(":", "-")
    with db() as c:
        existing = c.execute("SELECT * FROM model_catalog WHERE reference IN (?,?) OR id=? LIMIT 1",
                             (row["model"], reference, identifier)).fetchone()
        if existing:
            c.execute("UPDATE model_catalog SET evals=? WHERE id=?", (json.dumps(scores), existing["id"]))
        else:
            c.execute(
                "INSERT INTO model_catalog (id,reference,engine,display_name,download_gb,digest,min_engine,defaults,"
                "suggested_roles,evals,source,created_at) VALUES (?,?, 'ollama', ?,?,NULL,NULL,'{}',?,?, 'evals',?)",
                (identifier, reference, reference, row.get("size_gb"), json.dumps(sorted(scores)),
                 json.dumps(scores), time.time()),
            )


async def _run(run: dict) -> None:
    command = shlex.split(os.environ["FLEET_EVAL_CMD"]) if os.environ.get("FLEET_EVAL_CMD") else DEFAULT_COMMAND
    node_ip = "127.0.0.1"
    with db() as c:
        node = c.execute("SELECT ip FROM nodes WHERE id=?", (run["node"],)).fetchone()
        if node and run["node"] != "master":
            node_ip = node["ip"]
    out = results_path().parent
    args = [*command, "--models", run["model"], "--roles", ",".join(run["roles"]),
            "--ollama", f"http://{node_ip}:11434", "--out", str(out)]
    try:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.STDOUT)
        output, _ = await process.communicate()
        log = output.decode("utf-8", "replace")[-16_384:]
        result = _matching_result(run["model"], run["started_at"])
        if process.returncode == 0 and result:
            scores = {role: value["score"] for role, value in (result.get("roles") or {}).items()
                      if isinstance(value, dict) and isinstance(value.get("score"), (int, float))}
            errors = result.get("errors") if isinstance(result.get("errors"), list) else []
            state = "done"
            _refresh_catalog(result)
        else:
            scores = None
            errors = ([f"eval command exited {process.returncode}"] if process.returncode else
                      ["eval command produced no matching results row"])
            state = "failed"
    except (OSError, ValueError) as exc:
        state, scores, errors, log = "failed", None, [f"eval command failed: {exc.__class__.__name__}"], ""
    with db() as c:
        c.execute("UPDATE eval_runs SET state=?,ended_at=?,scores=?,errors=?,log_tail=? WHERE id=?",
                  (state, time.time(), json.dumps(scores) if scores is not None else None,
                   json.dumps(errors), log, run["id"]))
    events.emit(None, f"eval.{state}", {"id": run["id"], "model": run["model"], "node": run["node"]})


async def worker() -> None:
    # A running subprocess cannot survive a controller restart; retry its durable row.
    with db() as c:
        c.execute("UPDATE eval_runs SET state='queued',started_at=NULL WHERE state='running'")
    while True:
        run = _claim()
        if run:
            events.emit(None, "eval.running", {"id": run["id"], "model": run["model"], "node": run["node"]})
            await _run(run)
        else:
            await asyncio.sleep(0.2)


def recommendations() -> list[dict]:
    rows = [row for row in read_results() if not (row.get("errors") or [])]
    with db() as c:
        nodes = [dict(row) for row in c.execute("SELECT * FROM nodes WHERE status='approved' ORDER BY name")]
        catalog = [dict(row) for row in c.execute("SELECT reference,download_gb FROM model_catalog")]
    catalog_by_ref = {row["reference"]: row for row in catalog}
    output = []
    for node in nodes:
        installed = set(json.loads(node.get("models") or "[]"))
        try:
            inventory = json.loads(node.get("inventory") or "{}")
            ram = (inventory.get("hardware") or {}).get("ram_gb")
            runner = json.loads(node.get("runner") or "{}")
            reserve = (runner.get("models") or {}).get("ram_reserve_gb", 4)
        except (ValueError, TypeError):
            ram, reserve = None, 4
        candidates = []
        for row in rows:
            model = row["model"]
            is_installed = model in installed or (model + ":latest") in installed
            reference = model if ":" in model.rsplit("/", 1)[-1] else model + ":latest"
            item = catalog_by_ref.get(model) or catalog_by_ref.get(reference)
            size = row.get("size_gb") or (item or {}).get("download_gb")
            fitting = item is not None and (ram is None or size is None or size * 1.2 <= ram - reserve)
            if is_installed or fitting:
                candidates.append((row, is_installed, size if isinstance(size, (int, float)) else float("inf")))
        roles = sorted({role for row, _, _ in candidates for role, score in (row.get("roles") or {}).items()
                        if isinstance(score, dict) and isinstance(score.get("score"), (int, float))})
        for role in roles:
            ranked = [(row["roles"][role]["score"], size, row, present) for row, present, size in candidates
                      if isinstance((row.get("roles") or {}).get(role), dict)]
            if ranked:
                score, _, best, present = max(ranked, key=lambda item: (item[0], -item[1]))
                output.append({"node": node["id"], "role": role, "model": best["model"],
                               "score": score, "installed": present})
    return output

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


TRUSTED_ROLE = "reviewer_real"
TRUSTED_SPLIT = "holdout"
TRUSTED_RECALL = 0.5


def trusted_precision() -> float:
    try:
        return float(os.environ.get("FLEET_TRUSTED_PRECISION", "0.9"))
    except ValueError:
        return 0.9


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _exact_ratio(metrics: dict, numerator: str, denominator: str, stored: str) -> float | None:
    """The ratio from raw counts when the row has them (exact), else the stored value (older rows)."""
    top, bottom = _count(metrics.get(numerator)), _count(metrics.get(denominator))
    if top is not None and bottom is not None:
        return top / bottom if bottom and top <= bottom else None
    value = metrics.get(stored)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def is_trusted_reviewer(result: dict, precision_min: float | None = None,
                        recall_min: float = TRUSTED_RECALL) -> bool:
    """A results row earns trust when its reviewer_real holdout precision and recall both meet the bar.

    Judged on matched/serious and found/bug_cases counts, so a ratio just under the bar can't pass by rounding."""
    precision_min = trusted_precision() if precision_min is None else precision_min
    role = (result.get("roles") or {}).get(TRUSTED_ROLE)
    metrics = ((role or {}).get("metrics") or {}).get(TRUSTED_SPLIT) if isinstance(role, dict) else None
    if not isinstance(metrics, dict):
        return False
    precision = _exact_ratio(metrics, "matched", "serious", "precision")
    recall = _exact_ratio(metrics, "found", "bug_cases", "recall")
    return precision is not None and recall is not None and precision >= precision_min and recall >= recall_min


def canonical_reference(model: str) -> str:
    """name:tag form (an untagged "qwen3" is "qwen3:latest"); compare model references only in this form."""
    return model if ":" in model.rsplit("/", 1)[-1] else model + ":latest"


def parse_params(value) -> dict | None:
    """Model settings as a dict ({} = the base model as served); None when they can't be read."""
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def derived_model(catalog_id: str) -> str:
    """The model Ollama serves for a catalog model with settings (same name the controller installs and routes)."""
    return "fleet-" + catalog_id


def is_trusted_reference(reference: str, trusted_refs: set[str]) -> bool:
    return bool(reference) and canonical_reference(reference) in trusted_refs


def _role_time(row: dict, role: str) -> float:
    value = (row.get("roles") or {}).get(role) or {}
    stamp = value.get("evaluated_at") if isinstance(value, dict) else None
    stamp = stamp if isinstance(stamp, (int, float)) else row.get("evaluated_at")
    return float(stamp) if isinstance(stamp, (int, float)) else float("-inf")


def latest_role_results(rows: list[dict], role: str) -> dict[str, dict]:
    """Per canonical model reference, the row holding the newest evaluation of `role` (by that role's own timestamp,
    else the row's). Rows for "qwen3" and "qwen3:latest" are the same model; other roles' runs don't count."""
    latest: dict[str, tuple[float, dict]] = {}
    for row in rows:
        if not isinstance(row.get("model"), str) or not isinstance((row.get("roles") or {}).get(role), dict):
            continue
        key, stamp = canonical_reference(row["model"]), _role_time(row, role)
        if key not in latest or stamp >= latest[key][0]:
            latest[key] = (stamp, row)
    return {key: row for key, (_, row) in latest.items()}


def trusted_reviewer_references(rows: list[dict] | None = None) -> set[str]:
    """Canonical references (name:tag) of the concrete models whose newest reviewer_real evaluation makes them
    trusted reviewers; a derived model counts as itself (fleet-<catalog_id>:latest), not as its base."""
    rows = read_results() if rows is None else rows
    return {key for key, row in latest_role_results(rows, TRUSTED_ROLE).items() if is_trusted_reviewer(row)}


def routed_model(assignment: dict, reference_by_id: dict[str, str]) -> str | None:
    """The concrete model an assignment serves the role with, or None when that can't be determined safely.

    With settings, the controller routes the derived fleet-<catalog_id> model, built from the catalog row's
    reference; so the catalog row must exist and name the assignment's own model (never trust the catalog's say-so
    alone). Settings without a catalog model aren't a configuration anyone benchmarked: None. Without settings, the
    assignment's own model is what's routed; a catalog_id, if given, must still match it."""
    model = assignment.get("model")
    params = parse_params(assignment.get("params"))
    if not isinstance(model, str) or not model or params is None:
        return None
    catalog_id = assignment.get("catalog_id")
    if catalog_id:
        reference = reference_by_id.get(catalog_id)
        if not reference or canonical_reference(reference) != canonical_reference(model):
            return None
        return derived_model(catalog_id) if params else model
    return None if params else model


def assignment_trusted(assignment: dict, reference_by_id: dict[str, str], trusted_refs: set[str]) -> bool:
    """Whether the concrete model this assignment routes was benchmarked as a trusted reviewer itself: the base
    model's results never stand in for a derived model's, or the other way round."""
    routed = routed_model(assignment, reference_by_id)
    return routed is not None and is_trusted_reference(routed, trusted_refs)


def trusted_roles(assignments: list[dict], catalog: list[dict], trusted_refs: set[str]) -> list[str]:
    """Roles whose every assigned model (on any node) is a trusted reviewer. Pure; the controller supplies rows."""
    reference_by_id = {item["id"]: item["reference"] for item in catalog if isinstance(item.get("reference"), str)}
    by_role: dict[str, list[bool]] = {}
    for row in assignments:
        by_role.setdefault(row["role"], []).append(assignment_trusted(row, reference_by_id, trusted_refs))
    return sorted(role for role, verdicts in by_role.items() if verdicts and all(verdicts))


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
            # The results row also keeps roles from earlier runs; this run's scores are only the roles it ran.
            scores = {role: value["score"] for role, value in (result.get("roles") or {}).items()
                      if role in run["roles"] and isinstance(value, dict)
                      and isinstance(value.get("score"), (int, float))}
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

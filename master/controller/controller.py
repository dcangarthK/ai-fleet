# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.110", "uvicorn>=0.29", "httpx>=0.27"]
# ///
"""Fleet controller for the master node.

Finds nodes via `tailscale status` (devices tagged tag:llm-node), probes their model servers,
inventory and agent runner over the tailnet, keeps LiteLLM's role groups (coder/reviewer/fast/...)
in sync with role assignments and node health, and runs agent tasks (tasks.py).

Run:    uv run --script controller.py              (dashboard on http://127.0.0.1:8700)
Tokens: uv run --script controller.py token create operate phone
        uv run --script controller.py token list | token revoke <id>
"""
import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402
import events  # noqa: E402
import policy as fleet_policy  # noqa: E402
import tasks  # noqa: E402
from store import db, init_db  # noqa: E402

LITELLM_URL = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000").rstrip("/")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
NODE_TAG = "tag:llm-node"
POLL_SECONDS = float(os.environ.get("FLEET_POLL_SECONDS", "30"))
MAX_FAILS = 3
ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
MODEL_REF_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}(/[a-z0-9][a-z0-9._-]{0,63})?(:[A-Za-z0-9][A-Za-z0-9._-]{0,63})?$")
KEEP_ALIVE_RE = re.compile(r"^[0-9]+(?:ms|s|m|h)$")
FLEET_PREFIX = "fleet--"
MASTER_MODEL_HOST = os.environ.get("FLEET_MASTER_MODEL_HOST", "")  # e.g. host.docker.internal
INFO_PORT = 11435  # onboard script publishes /fleet.json here (hardware, agents, model files)

# (engine, port, path): first that answers wins.
PROBES = [("ollama", 11434, "/api/tags"), ("openai", 8000, "/v1/models"), ("openai", 1234, "/v1/models"),
          ("openai", 8080, "/v1/models")]


def healthy(row: sqlite3.Row) -> bool:
    return row["status"] == "approved" and row["fails"] < MAX_FAILS and row["engine"] is not None


# ---------- discovery ----------

def tailscale_bin() -> str:
    return (os.environ.get("TAILSCALE_BIN") or shutil.which("tailscale")
            or "/Applications/Tailscale.app/Contents/MacOS/Tailscale")


def tailnet_nodes() -> list[dict]:
    out = subprocess.run([tailscale_bin(), "status", "--json"],
                         capture_output=True, text=True, check=True, timeout=10).stdout
    peers = (json.loads(out).get("Peer") or {}).values()
    return [
        {"id": p["ID"], "name": p["HostName"].lower(), "ip": p["TailscaleIPs"][0], "os": p.get("OS", ""),
         "online": bool(p.get("Online"))}
        for p in peers if NODE_TAG in (p.get("Tags") or []) and p.get("TailscaleIPs") and p.get("ID")
    ]


async def probe(client: httpx.AsyncClient, ip: str) -> tuple[str, int, list[str]] | None:
    # First server with models wins; an empty one only counts if nothing else answers.
    empty = None
    for engine, port, path in PROBES:
        try:
            r = await client.get(f"http://{ip}:{port}{path}", timeout=3)
            r.raise_for_status()
            body = r.json()
            if engine == "ollama":
                models = [str(m["name"]) for m in body["models"]]
            else:
                models = [str(m["id"]) for m in body["data"]]
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            continue  # not this engine, or an unexpected response
        if models:
            return engine, port, sorted(models)
        empty = empty or (engine, port, [])
    return empty


async def fetch_inventory(client: httpx.AsyncClient, ip: str) -> dict | None:
    try:
        r = await client.get(f"http://{ip}:{INFO_PORT}/fleet.json", timeout=3)
        r.raise_for_status()
        inv = r.json()
        return inv if isinstance(inv, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


async def refresh_master(client: httpx.AsyncClient) -> None:
    """The master's own runner is a node too (id 'master'), so its Claude/Codex/Ollama can take tasks."""
    health = await tasks.runner_health(client, tasks.LOCAL_RUNNER, tasks.MASTER_ID)
    found = await probe(client, "127.0.0.1")
    report = await runner_models({"id": tasks.MASTER_ID, "ip": "127.0.0.1"}) if health and health.get("models") else None
    found = merge_probe_models(found, report)
    with db() as c:
        c.execute("INSERT OR IGNORE INTO nodes (id, name, ip, os, status) VALUES (?, 'master', '127.0.0.1', ?, 'approved')",
                  (tasks.MASTER_ID, sys.platform))
        if health:
            c.execute("UPDATE nodes SET runner=?, runner_fails=0, last_seen=? WHERE id=?",
                      (json.dumps(health), time.time(), tasks.MASTER_ID))
        else:
            c.execute("UPDATE nodes SET runner_fails=runner_fails+1 WHERE id=?", (tasks.MASTER_ID,))
        # Same health bookkeeping as other nodes, so a stopped master Ollama drops out of its roles.
        if found:
            c.execute("UPDATE nodes SET engine=?, port=?, models=?, fails=0 WHERE id=?",
                      (found[0], found[1], json.dumps(found[2]), tasks.MASTER_ID))
        else:
            c.execute("UPDATE nodes SET fails=fails+1 WHERE id=?", (tasks.MASTER_ID,))
        merge_model_inventory(c, tasks.MASTER_ID, report)


async def runner_models(node: dict) -> dict | None:
    try:
        result = await tasks.runner_call(node, "GET", "/models", timeout=5)
        return result if isinstance(result, dict) else None
    except tasks.RunnerError:
        return None


def merge_probe_models(found, report):
    if not report:
        return found
    reported = [item.get("name") for item in report.get("models", [])
                if isinstance(item, dict) and isinstance(item.get("name"), str)]
    if found:
        return found[0], found[1], sorted(set(found[2]) | set(reported))
    return ("ollama", 11434, sorted(set(reported))) if reported else found


def merge_model_inventory(c: sqlite3.Connection, node_id: str, report: dict | None) -> None:
    if not report or not isinstance(report.get("ram_gb"), (int, float)):
        return
    row = c.execute("SELECT inventory FROM nodes WHERE id=?", (node_id,)).fetchone()
    try:
        inventory = json.loads(row["inventory"]) if row and row["inventory"] else {}
    except (ValueError, TypeError):
        inventory = {}
    if not isinstance(inventory, dict):
        inventory = {}
    hardware = inventory.get("hardware") if isinstance(inventory.get("hardware"), dict) else {}
    hardware["ram_gb"] = report["ram_gb"]
    inventory["hardware"] = hardware
    c.execute("UPDATE nodes SET inventory=? WHERE id=?", (json.dumps(inventory), node_id))


async def refresh_nodes() -> None:
    async with httpx.AsyncClient(trust_env=False) as client:
        await refresh_master(client)
    try:
        seen = await asyncio.to_thread(tailnet_nodes)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as e:
        print(f"tailscale status failed: {e}")
        return
    online = [n for n in seen if n["online"]]
    async with httpx.AsyncClient(trust_env=False) as client:  # tailnet only: never via a proxy
        results = await asyncio.gather(*(probe(client, n["ip"]) for n in online))
        inventories = await asyncio.gather(*(fetch_inventory(client, n["ip"]) for n in online))
        runners = await asyncio.gather(*(tasks.runner_health(client, f"http://{n['ip']}:{tasks.RUNNER_PORT}", n["id"])
                                         for n in online))
    model_reports = await asyncio.gather(*(runner_models(n) if runners[index] and runners[index].get("models") else
                                           asyncio.sleep(0, result=None) for index, n in enumerate(online)))
    probed = dict(zip([n["id"] for n in online], results))
    inventory = dict(zip([n["id"] for n in online], inventories))
    runner = dict(zip([n["id"] for n in online], runners))
    model_report = dict(zip([n["id"] for n in online], model_reports))
    with db() as c:
        for n in seen:
            c.execute("INSERT OR IGNORE INTO nodes (id) VALUES (?)", (n["id"],))
            c.execute("UPDATE nodes SET name=?, ip=?, os=? WHERE id=?", (n["name"], n["ip"], n["os"], n["id"]))
            if inventory.get(n["id"]):
                c.execute("UPDATE nodes SET inventory=? WHERE id=?", (json.dumps(inventory[n["id"]]), n["id"]))
            if runner.get(n["id"]):
                c.execute("UPDATE nodes SET runner=?, runner_fails=0 WHERE id=?", (json.dumps(runner[n["id"]]), n["id"]))
            else:
                c.execute("UPDATE nodes SET runner_fails=runner_fails+1 WHERE id=?", (n["id"],))
            found = merge_probe_models(probed.get(n["id"]), model_report.get(n["id"]))
            if found:
                engine, port, models = found
                c.execute("UPDATE nodes SET engine=?, port=?, models=?, fails=0, last_seen=? WHERE id=?",
                          (engine, port, json.dumps(models), time.time(), n["id"]))
            else:
                c.execute("UPDATE nodes SET fails=fails+1 WHERE id=?", (n["id"],))
            merge_model_inventory(c, n["id"], model_report.get(n["id"]))
        ids = [n["id"] for n in seen] + [tasks.MASTER_ID]
        # Nodes that left the tailnet (or lost the tag) count as failing too.
        c.execute(f"UPDATE nodes SET fails=fails+1, runner_fails=runner_fails+1 "
                  f"WHERE id NOT IN ({','.join('?' * len(ids))})", ids)


# ---------- LiteLLM reconciliation ----------

def deployment_id(role: str, node: str, model: str) -> str:
    return FLEET_PREFIX + re.sub(r"[^a-zA-Z0-9.-]", "_", f"{role}--{node}--{model}")


def desired_deployments() -> dict[str, dict]:
    with db() as c:
        nodes = {r["id"]: r for r in c.execute("SELECT * FROM nodes")}
        rows = c.execute("SELECT * FROM assignments").fetchall()
    desired = {}
    for a in rows:
        n = nodes.get(a["node"])
        # The master's own Ollama listens on 127.0.0.1, which LiteLLM's container can't reach; it can only
        # serve roles when FLEET_MASTER_MODEL_HOST names the host as seen from LiteLLM (host.docker.internal).
        if (not n or a["state"] != "active" or (n["id"] == tasks.MASTER_ID and not MASTER_MODEL_HOST)
                or not healthy(n) or a["model"] not in json.loads(n["models"])):
            continue
        host = MASTER_MODEL_HOST if n["id"] == tasks.MASTER_ID else n["ip"]
        assignment_params = json.loads(a["params"] or "{}")
        routed_model = "fleet-" + a["catalog_id"] if a["catalog_id"] and assignment_params else a["model"]
        if n["engine"] == "ollama":
            params = {"model": f"ollama_chat/{routed_model}", "api_base": f"http://{host}:{n['port']}"}
        else:
            params = {"model": f"openai/{a['model']}", "api_base": f"http://{host}:{n['port']}/v1",
                      "api_key": "none"}
        params["timeout"] = 600
        params.update(assignment_params)
        desired[deployment_id(a["role"], a["node"], a["model"])] = {
            "model_name": a["role"], "litellm_params": params,
            "model_info": {"id": deployment_id(a["role"], a["node"], a["model"])},
        }
    return desired


def _model_event(type_: str, row: dict, reference: str) -> None:
    events.emit(None, type_, {"node": row["node"], "model": reference,
                              "state": row.get("state"), "reason": row.get("reason")})


def _set_node_model(node: str, catalog_id: str, **fields) -> None:
    fields["updated_at"] = time.time()
    with db() as c:
        before = c.execute("SELECT state,reason FROM node_models WHERE node=? AND catalog_id=?",
                           (node, catalog_id)).fetchone()
        c.execute("UPDATE node_models SET %s WHERE node=? AND catalog_id=?" %
                  ",".join(name + "=?" for name in fields), (*fields.values(), node, catalog_id))
        catalog = c.execute("SELECT reference FROM model_catalog WHERE id=?", (catalog_id,)).fetchone()
    old_state = before["state"] if before else None
    old_reason = before["reason"] if before else None
    new_state = fields.get("state", old_state)
    new_reason = fields.get("reason", old_reason)
    if catalog and (not before or old_state != new_state or old_reason != new_reason):
        current = {"node": node, "catalog_id": catalog_id,
                   "state": new_state, "reason": new_reason}
        _model_event("model.state", current, catalog["reference"])


async def reconcile_models() -> None:
    """Advance desired model state and assignments once; runner operations remain durable."""
    with db() as c:
        rows = [dict(row) for row in c.execute(
            "SELECT nm.*, mc.reference FROM node_models nm JOIN model_catalog mc ON mc.id=nm.catalog_id")]
        nodes = {row["id"]: dict(row) for row in c.execute("SELECT * FROM nodes")}
    refresh_after = False
    for row in rows:
        node = nodes.get(row["node"])
        if not node:
            continue
        try:
            present = set(json.loads(node["models"] or "[]"))
            params = json.loads(row["params"] or "{}")
        except (ValueError, TypeError):
            present, params = set(), {}
        derived = "fleet-" + row["catalog_id"] if params else None
        target_present = row["reference"] in present and (not derived or derived in present)
        if row["desired"] == "present" and target_present:
            ownership = row.get("ownership") or "pre-existing"
            _set_node_model(row["node"], row["catalog_id"], state="ready", reason=None,
                            ownership=ownership, progress=json.dumps({"status": "ready"}))
            continue
        if row["desired"] == "absent" and row["reference"] not in present and (not derived or derived not in present):
            _set_node_model(row["node"], row["catalog_id"], state="removed", reason=None,
                            progress=json.dumps({"status": "removed"}))
            continue
        if row.get("op_id"):
            try:
                operation = await tasks.runner_call(node, "GET", "/operations/" + row["op_id"], timeout=10)
            except tasks.RunnerError as exc:
                _set_node_model(row["node"], row["catalog_id"], reason=str(exc.detail))
                continue
            state = operation.get("state", "failed")
            # A model that was on the node before stays pre-existing even after the fleet adds params to it.
            ownership = row.get("ownership") or ("fleet-managed" if state == "ready" else None)
            _set_node_model(row["node"], row["catalog_id"], state=state, reason=operation.get("reason"),
                            progress=json.dumps(operation.get("progress") or {}), ownership=ownership)
            if state in ("ready", "removed"):
                refresh_after = True
            continue
        body = {"ref": row["reference"]}
        path = "/models/ensure" if row["desired"] == "present" else "/models/remove"
        if derived:
            body["derived"] = derived
        if row["desired"] == "present":
            if params:
                body["params"] = params
            body["override_fit"] = bool(row["override_fit"])
            event_type = "model.install"
        else:
            body["confirm_preexisting"] = bool(row.get("confirm_preexisting"))
            event_type = "model.remove"
        try:
            operation = await tasks.runner_call(node, "POST", path, body, timeout=30)
        except tasks.RunnerError as exc:
            state = "blocked" if exc.status_code in (403, 409) else "failed"
            _set_node_model(row["node"], row["catalog_id"], state=state, reason=str(exc.detail))
            continue
        _set_node_model(row["node"], row["catalog_id"], op_id=operation["id"],
                        state=operation.get("state", "checking"), reason=operation.get("reason"),
                        progress=json.dumps(operation.get("progress") or {}))
        _model_event(event_type, dict(row) | {"state": operation.get("state"), "reason": operation.get("reason")},
                     row["reference"])
    if refresh_after:
        await refresh_nodes()
    with db() as c:
        waiting = c.execute(
            "SELECT a.role,a.node,a.model,a.catalog_id,nm.state AS install_state,n.models "
            "FROM assignments a LEFT JOIN node_models nm ON nm.node=a.node AND nm.catalog_id=a.catalog_id "
            "LEFT JOIN nodes n ON n.id=a.node WHERE a.state!='active'").fetchall()
        for assignment in waiting:
            state = assignment["install_state"]
            if state == "ready" and assignment["model"] in json.loads(assignment["models"] or "[]"):
                state = "active"
            elif state not in ("blocked", "failed"):
                state = "pending_install"
            c.execute("UPDATE assignments SET state=? WHERE role=? AND node=? AND model=?",
                      (state, assignment["role"], assignment["node"], assignment["model"]))


async def reconcile() -> dict:
    headers = {"Authorization": f"Bearer {MASTER_KEY}"}
    async with httpx.AsyncClient(base_url=LITELLM_URL, headers=headers, timeout=15, trust_env=False) as client:
        r = await client.get("/model/info")
        r.raise_for_status()
        existing = {}
        for m in r.json().get("data", []):
            mid = (m.get("model_info") or {}).get("id", "")
            if mid.startswith(FLEET_PREFIX):
                existing[mid] = m
        desired = desired_deployments()
        added, removed = [], []
        for mid, spec in desired.items():
            cur = existing.get(mid)
            if cur and cur.get("litellm_params", {}).get("api_base") == spec["litellm_params"]["api_base"]:
                continue
            if cur:
                (await client.post("/model/delete", json={"id": mid})).raise_for_status()
            (await client.post("/model/new", json=spec)).raise_for_status()
            added.append(mid)
        for mid in existing.keys() - desired.keys():
            (await client.post("/model/delete", json={"id": mid})).raise_for_status()
            removed.append(mid)
    return {"added": added, "removed": removed}


_tick_lock = asyncio.Lock()


async def tick() -> dict:
    async with _tick_lock:  # periodic and API-triggered ticks must not interleave LiteLLM edits
        await refresh_nodes()
        await reconcile_models()
        if not MASTER_KEY:
            return {"skipped": "no LITELLM_MASTER_KEY: model roles are not synced"}
        try:
            return await reconcile()
        except httpx.HTTPError as e:
            print(f"reconcile failed: {e}")
            return {"error": str(e)}


async def loop() -> None:
    while True:
        try:
            await tick()
        except Exception as e:  # one bad poll must not stop the controller
            print(f"tick failed: {e!r}")
        await asyncio.sleep(POLL_SECONDS)


# ---------- API ----------

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    events.bind_loop(asyncio.get_running_loop())
    background = [asyncio.create_task(loop()), asyncio.create_task(tasks.poll_loop())]
    yield
    for t in background:
        t.cancel()


app = FastAPI(title="fleet", lifespan=lifespan)
app.middleware("http")(auth.access_control)
app.include_router(tasks.router)


class Assignment(BaseModel):
    role: str
    node: str
    model: str
    ensure_model: bool = False
    catalog_id: str | None = None
    params: dict = Field(default_factory=dict)


class CatalogModel(BaseModel):
    reference: str
    display_name: str | None = None
    defaults: dict = Field(default_factory=dict)
    suggested_roles: list[str] = Field(default_factory=list)


class NodeModelRequest(BaseModel):
    desired: str
    params: dict | None = None
    override_fit: bool = False
    confirm_preexisting: bool = False


class InstructionBody(BaseModel):
    text: str


class PolicyBody(BaseModel):
    body: dict = Field(default_factory=dict)


class PolicyPreview(BaseModel):
    node: str
    agent: str
    role: str | None = None
    policy: dict | None = None


def validate_policy_scope(scope: str, scope_id: str) -> None:
    if scope not in ("fleet", "node", "role", "agent"):
        raise HTTPException(400, "scope must be fleet, node, role or agent")
    if scope == "fleet" and scope_id != "*":
        raise HTTPException(400, "fleet scope_id must be *")
    if scope != "fleet" and (not scope_id or scope_id == "*"):
        raise HTTPException(400, f"{scope} scope_id is invalid")
    if scope == "node":
        with db() as c:
            if not c.execute("SELECT 1 FROM nodes WHERE id=?", (scope_id,)).fetchone():
                raise HTTPException(404, f"unknown node {scope_id}")
    if scope == "role" and not ROLE_RE.fullmatch(scope_id):
        raise HTTPException(400, "role scope_id must be a role name")
    if scope == "agent" and scope_id not in tasks.AGENTS:
        raise HTTPException(400, f"agent scope_id must be one of {sorted(tasks.AGENTS)}")


def _version_rows(table: str, scope: str | None = None, scope_id: str | None = None) -> list[dict]:
    value = "text" if table == "instructions" else "body"
    where, args = "", []
    if scope is not None:
        where = "WHERE scope=? AND scope_id=?"
        args = [scope, scope_id]
    else:
        where = (f"WHERE version=(SELECT MAX(v.version) FROM {table} v "
                 f"WHERE v.scope={table}.scope AND v.scope_id={table}.scope_id)")
    with db() as c:
        rows = [dict(row) for row in c.execute(
            f"SELECT scope,scope_id,version,{value},author,created_at FROM {table} "
            f"{where} ORDER BY " + ("version DESC" if scope is not None else "scope,scope_id"), args)]
    if table == "policies":
        for row in rows:
            row["body"] = json.loads(row["body"])
    return rows


@app.get("/api/instructions")
def list_instructions() -> list[dict]:
    return _version_rows("instructions")


@app.get("/api/instructions/{scope}/{scope_id}/history")
def instruction_history(scope: str, scope_id: str) -> list[dict]:
    validate_policy_scope(scope, scope_id)
    return _version_rows("instructions", scope, scope_id)


@app.put("/api/instructions/{scope}/{scope_id}")
def put_instructions(scope: str, scope_id: str, body: InstructionBody, request: Request) -> dict:
    validate_policy_scope(scope, scope_id)
    if len(body.text.encode("utf-8")) > 16 * 1024:
        raise HTTPException(400, "instructions exceed 16 KB")
    now = time.time()
    author = getattr(request.state, "actor", "local")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        version = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM instructions WHERE scope=? AND scope_id=?",
                            (scope, scope_id)).fetchone()[0]
        c.execute("INSERT INTO instructions VALUES (?,?,?,?,?,?)",
                  (scope, scope_id, version, body.text, author, now))
    data = {"scope": scope, "scope_id": scope_id, "version": version, "text": body.text,
            "author": author, "created_at": now}
    events.emit(None, "instructions.changed", {k: data[k] for k in ("scope", "scope_id", "version", "author")})
    return data


@app.get("/api/policies")
def list_policies() -> list[dict]:
    return _version_rows("policies")


@app.get("/api/policies/{scope}/{scope_id}/history")
def policy_history(scope: str, scope_id: str) -> list[dict]:
    validate_policy_scope(scope, scope_id)
    return _version_rows("policies", scope, scope_id)


@app.put("/api/policies/{scope}/{scope_id}")
def put_policy(scope: str, scope_id: str, body: PolicyBody, request: Request) -> dict:
    validate_policy_scope(scope, scope_id)
    try:
        fleet_policy.validate(body.body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    now = time.time()
    author = getattr(request.state, "actor", "local")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        version = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM policies WHERE scope=? AND scope_id=?",
                            (scope, scope_id)).fetchone()[0]
        c.execute("INSERT INTO policies VALUES (?,?,?,?,?,?)",
                  (scope, scope_id, version, json.dumps(body.body), author, now))
    data = {"scope": scope, "scope_id": scope_id, "version": version, "body": body.body,
            "author": author, "created_at": now}
    events.emit(None, "policy.changed", {k: data[k] for k in ("scope", "scope_id", "version", "author")})
    return data


@app.post("/api/policies/preview")
def preview_policy(spec: PolicyPreview) -> dict:
    if spec.agent not in tasks.AGENTS:
        raise HTTPException(400, f"agent must be one of {sorted(tasks.AGENTS)}")
    with db() as c:
        node = resolve(c, spec.node)
    try:
        return tasks.effective_policy(node["id"], spec.role, spec.agent, spec.policy)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def model_id(reference: str) -> str:
    return reference.replace("/", "-").replace(":", "-")


def validate_model_params(params: dict) -> dict:
    allowed = {"num_ctx", "temperature", "top_p", "seed", "think", "keep_alive"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise HTTPException(400, "unknown model parameter: " + unknown[0])
    if "num_ctx" in params and (not isinstance(params["num_ctx"], int) or isinstance(params["num_ctx"], bool)
                                or not 512 <= params["num_ctx"] <= 131072):
        raise HTTPException(400, "invalid num_ctx")
    for key, low, high in (("temperature", 0, 2), ("top_p", 0, 1)):
        value = params.get(key)
        if key in params and (not isinstance(value, (int, float)) or isinstance(value, bool)
                              or value < low or value > high):
            raise HTTPException(400, "invalid " + key)
    if "seed" in params and (not isinstance(params["seed"], int) or isinstance(params["seed"], bool)):
        raise HTTPException(400, "invalid seed")
    if "think" in params and not isinstance(params["think"], bool):
        raise HTTPException(400, "invalid think")
    if "keep_alive" in params and (not isinstance(params["keep_alive"], str)
                                   or not KEEP_ALIVE_RE.fullmatch(params["keep_alive"])):
        raise HTTPException(400, "invalid keep_alive")
    return dict(params)


def registry_manifest(reference: str) -> tuple[float, str | None]:
    repo, separator, tag = reference.rpartition(":")
    if not separator:
        repo, tag = reference, "latest"
    if "/" not in repo:
        repo = "library/" + repo
    base = os.environ.get("FLEET_OLLAMA_REGISTRY", "https://registry.ollama.ai/v2").rstrip("/")
    try:
        with httpx.Client(trust_env=False, timeout=20) as client:
            response = client.get(f"{base}/{repo}/manifests/{tag}",
                                  headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
        if response.status_code == 404:
            raise HTTPException(400, f"{reference} isn't in the registry")
        response.raise_for_status()
        manifest = response.json()
        size = (manifest.get("config") or {}).get("size", 0)
        size += sum(layer.get("size", 0) for layer in manifest.get("layers", []) if isinstance(layer, dict))
        digest = response.headers.get("docker-content-digest") or (manifest.get("config") or {}).get("digest")
        if not size:
            raise HTTPException(400, f"{reference} has no known size in the registry")
        return size / 1e9, digest
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(502, f"registry lookup failed: {exc.__class__.__name__}")


def _json(value, fallback):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return result if isinstance(result, type(fallback)) else fallback
    except (ValueError, TypeError):
        return fallback


def model_fit(catalog: dict, node: dict) -> str:
    inventory = _json(node.get("inventory"), {})
    ram_gb = (inventory.get("hardware") or {}).get("ram_gb")
    size = catalog.get("download_gb")
    if not isinstance(ram_gb, (int, float)) or not isinstance(size, (int, float)):
        return "unknown"
    runner = _json(node.get("runner"), {})
    reserve = (runner.get("models") or {}).get("ram_reserve_gb", 4)
    context = _json(catalog.get("defaults"), {}).get("num_ctx", 0)
    need = size * 1.2 + context / 8192.0
    remaining = ram_gb - reserve - need
    return "wont_fit" if remaining < 0 else ("tight" if remaining <= 2 else "fits")


def catalog_view() -> list[dict]:
    with db() as c:
        catalog = [dict(row) for row in c.execute("SELECT * FROM model_catalog ORDER BY display_name,id")]
        nodes = [dict(row) for row in c.execute("SELECT * FROM nodes ORDER BY name")]
        states = {(row["node"], row["catalog_id"]): dict(row) for row in c.execute("SELECT * FROM node_models")}
    output = []
    for item in catalog:
        for key, fallback in (("defaults", {}), ("suggested_roles", []), ("evals", {})):
            item[key] = _json(item.get(key), fallback)
        cells = {}
        for node in nodes:
            installed = item["reference"] in _json(node.get("models"), [])
            row = states.get((node["id"], item["id"]), {})
            cells[node["id"]] = {
                "installed": installed,
                "state": row.get("state") or ("ready" if installed else "missing"),
                "reason": row.get("reason"),
                "progress": _json(row.get("progress"), {}),
                "ownership": row.get("ownership") or ("pre-existing" if installed else None),
                "fit": model_fit(item, node),
            }
        item["nodes"] = cells
        output.append(item)
    return output


@app.get("/api/models")
def list_catalog() -> list[dict]:
    return catalog_view()


@app.post("/api/models")
def add_catalog(spec: CatalogModel) -> dict:
    if not MODEL_REF_RE.fullmatch(spec.reference):
        raise HTTPException(400, "invalid model reference")
    defaults = validate_model_params(spec.defaults)
    size, digest = registry_manifest(spec.reference)
    identifier = model_id(spec.reference)
    with db() as c:
        existing = c.execute("SELECT evals,source,created_at FROM model_catalog WHERE id=?", (identifier,)).fetchone()
        evals = existing["evals"] if existing else "{}"
        source = existing["source"] if existing and existing["source"] == "evals" else "custom"
        created = existing["created_at"] if existing else time.time()
        c.execute(
            "INSERT OR REPLACE INTO model_catalog "
            "(id,reference,engine,display_name,download_gb,digest,min_engine,defaults,suggested_roles,evals,source,created_at) "
            "VALUES (?,?, 'ollama', ?,?,?,NULL,?,?,?,?,?)",
            (identifier, spec.reference, spec.display_name or spec.reference, size, digest,
             json.dumps(defaults), json.dumps(spec.suggested_roles), evals, source, created),
        )
    return next(item for item in catalog_view() if item["id"] == identifier)


@app.delete("/api/models/{catalog_id}")
def delete_catalog(catalog_id: str) -> dict:
    with db() as c:
        row = c.execute("SELECT id FROM model_catalog WHERE id=?", (catalog_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"unknown model {catalog_id}")
        wanted = c.execute("SELECT 1 FROM node_models WHERE catalog_id=? AND desired='present' LIMIT 1",
                           (catalog_id,)).fetchone()
        if wanted:
            raise HTTPException(409, "model is still desired on a node")
        c.execute("DELETE FROM node_models WHERE catalog_id=?", (catalog_id,))
        c.execute("DELETE FROM model_catalog WHERE id=?", (catalog_id,))
    return {"deleted": catalog_id}


@app.put("/api/nodes/{name}/models/{catalog_id}")
def set_node_model(name: str, catalog_id: str, request: NodeModelRequest) -> dict:
    if request.desired not in ("present", "absent"):
        raise HTTPException(400, "desired must be present or absent")
    with db() as c:
        node = resolve(c, name)
        model = c.execute("SELECT * FROM model_catalog WHERE id=?", (catalog_id,)).fetchone()
        if not model:
            raise HTTPException(404, f"unknown model {catalog_id}")
        old = c.execute("SELECT * FROM node_models WHERE node=? AND catalog_id=?",
                        (node["id"], catalog_id)).fetchone()
        installed = model["reference"] in _json(node["models"], [])
        if old and old["op_id"] and old["state"] in ("checking", "pulling", "configuring", "verifying", "removing"):
            raise HTTPException(409, f"{model['reference']} has an operation in progress on {node['name']}; wait for it to finish")
        params = (validate_model_params(request.params) if request.params is not None else
                  _json(old["params"], {}) if old else {})
        ownership = old["ownership"] if old and old["ownership"] else (
            "pre-existing" if installed else "fleet-managed")
        state = "ready" if request.desired == "present" and installed else (
            "removed" if request.desired == "absent" and not installed else "checking")
        c.execute(
            "INSERT OR REPLACE INTO node_models "
            "(node,catalog_id,desired,state,reason,op_id,progress,ownership,params,override_fit,confirm_preexisting,updated_at) "
            "VALUES (?,?,?,?,NULL,NULL,'{}',?,?,?,?,?)",
            (node["id"], catalog_id, request.desired, state, ownership, json.dumps(params),
             int(request.override_fit), int(request.confirm_preexisting), time.time()),
        )
        result = dict(c.execute("SELECT * FROM node_models WHERE node=? AND catalog_id=?",
                                (node["id"], catalog_id)).fetchone())
    result["params"] = _json(result["params"], {})
    result["progress"] = _json(result["progress"], {})
    return result


def fleet_state() -> dict:
    with db() as c:
        nodes = [dict(r) | {"models": json.loads(r["models"]), "healthy": healthy(r),
                            "inventory": json.loads(r["inventory"]) if r["inventory"] else None}
                 for r in c.execute("SELECT * FROM nodes ORDER BY name")]
        roles = [dict(r) for r in c.execute(
            "SELECT a.role, a.model, a.node AS node_id, n.name AS node, a.state, a.catalog_id, a.params "
            "FROM assignments a "
            "LEFT JOIN nodes n ON n.id = a.node ORDER BY a.role, n.name")]
    for n in nodes:
        n.pop("runner_token", None)
        n["runner"] = json.loads(n["runner"]) if n.get("runner") else None
        n["runner_ok"] = bool(n["runner"]) and (n.get("runner_fails") or 0) < MAX_FAILS
        n["effective_ceiling"] = tasks.ceiling(n) if n["runner"] else None
    for role in roles:
        role["params"] = _json(role.get("params"), {})
    return {"nodes": nodes, "assignments": roles, "max_fails": MAX_FAILS, "modes": tasks.MODES}


@app.get("/api/fleet")
def get_fleet() -> dict:
    return fleet_state()


def resolve(c: sqlite3.Connection, name: str) -> sqlite3.Row:
    rows = c.execute("SELECT * FROM nodes WHERE name=? OR id=?", (name, name)).fetchall()
    if not rows:
        raise HTTPException(404, f"unknown node {name}")
    if len(rows) > 1:
        raise HTTPException(409, f"several nodes are named {name}; use the node id: {[r['id'] for r in rows]}")
    return rows[0]


def set_status(name: str, status: str) -> dict:
    with db() as c:
        c.execute("UPDATE nodes SET status=? WHERE id=?", (status, resolve(c, name)["id"]))
    return fleet_state()


@app.post("/api/nodes/{name}/approve")
async def approve(name: str) -> dict:
    state = set_status(name, "approved")
    await tick()
    return state


@app.post("/api/nodes/{name}/drain")
async def drain(name: str) -> dict:
    state = set_status(name, "drained")
    await tick()
    return state


@app.post("/api/roles")
async def assign(a: Assignment) -> dict:
    if not ROLE_RE.match(a.role):
        raise HTTPException(400, "role must be lowercase letters, digits, dashes")
    with db() as c:
        node = resolve(c, a.node)
        catalog = (c.execute("SELECT * FROM model_catalog WHERE id=?", (a.catalog_id,)).fetchone()
                   if a.catalog_id else c.execute("SELECT * FROM model_catalog WHERE reference=?", (a.model,)).fetchone())
        model = catalog["reference"] if catalog else a.model
        installed = model in json.loads(node["models"])
        if not installed and not a.ensure_model:
            raise HTTPException(400, f"{a.node} does not serve {model}")
        if not installed and not catalog:
            raise HTTPException(400, f"{model} is not in the model catalog")
        params = validate_model_params((_json(catalog["defaults"], {}) if catalog else {}) | a.params)
        state = "active" if installed else "pending_install"
        catalog_id = catalog["id"] if catalog else None
        if not installed:
            c.execute(
                "INSERT INTO node_models "
                "(node,catalog_id,desired,state,reason,op_id,progress,ownership,params,override_fit,confirm_preexisting,updated_at) "
                "VALUES (?,?, 'present','checking',NULL,NULL,'{}','fleet-managed',?,0,0,?) "
                "ON CONFLICT(node,catalog_id) DO UPDATE SET desired='present',state='checking',reason=NULL,op_id=NULL,"
                "params=excluded.params,updated_at=excluded.updated_at",
                (node["id"], catalog_id, json.dumps(params), time.time()),
            )
        c.execute(
            "INSERT INTO assignments (role,node,model,state,catalog_id,params) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(role,node,model) DO UPDATE SET state=excluded.state,catalog_id=excluded.catalog_id,params=excluded.params",
            (a.role, node["id"], model, state, catalog_id, json.dumps(params)),
        )
    await tick()
    return fleet_state()


@app.delete("/api/roles")
async def unassign(a: Assignment) -> dict:
    with db() as c:
        c.execute("DELETE FROM assignments WHERE role=? AND node=? AND model=?",
                  (a.role, resolve(c, a.node)["id"], a.model))
    await tick()
    return fleet_state()


@app.post("/api/refresh")
async def refresh() -> dict:
    return {"reconcile": await tick()} | fleet_state()


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return Path(__file__).with_name("dashboard.html").read_text()


UI_DIST = Path(__file__).with_name("ui") / "dist"


@app.get("/app", include_in_schema=False)
@app.get("/app/{path:path}", include_in_schema=False)
def console(path: str = "") -> FileResponse:
    """The React console, built into ui/dist. Other paths fall back to index.html for its client-side routes."""
    root = UI_DIST.resolve()
    if path:
        f = (root / path).resolve()
        if f.is_file() and root in f.parents:
            return FileResponse(f, headers={"Cache-Control": "public, max-age=31536000, immutable"}
                                if path.startswith("assets/") else None)
        if path.startswith("assets/") or "." in path.rsplit("/", 1)[-1]:
            raise HTTPException(404, "not found")
    index = root / "index.html"
    if not index.is_file():
        raise HTTPException(503, "console not built: cd master/controller/ui && npm ci && npm run build")
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


@app.get("/login")
def login(token: str) -> RedirectResponse:
    """Remote sign-in: open https://<master>.ts.net/login?token=... once per device."""
    if not auth.token_scope(token):
        raise HTTPException(403, "unknown token")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.COOKIE, token, httponly=True, secure=True, samesite="strict", max_age=90 * 86400)
    return resp


def token_cli(args: list[str]) -> None:
    init_db()
    if args[:1] == ["create"] and len(args) >= 2:
        token = auth.create_token(args[1], " ".join(args[2:]) or "unnamed")
        host = auth.PUBLIC_HOST or "<FLEET_PUBLIC_HOST>"
        print(f"{args[1]} token (shown once): {token}\nSign in on a device: https://{host}/login?token={token}")
    elif args[:1] == ["list"]:
        with db() as c:
            for r in c.execute("SELECT id, scope, label, created_at, last_used FROM tokens ORDER BY created_at"):
                used = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_used"])) if r["last_used"] else "never"
                print(f"{r['id']}  {r['scope']:<8} {r['label']:<20} last used {used}")
    elif args[:1] == ["revoke"] and len(args) == 2:
        with db() as c:
            print("revoked" if c.execute("DELETE FROM tokens WHERE id=?", (args[1],)).rowcount else "no such token")
    else:
        raise SystemExit("usage: controller.py token create <view|operate|admin> [label] | token list | token revoke <id>")


def runner_token_cli(args: list[str]) -> None:
    init_db()
    if len(args) != 2 or args[0] not in ("set", "clear"):
        raise SystemExit("usage: controller.py runner-token set <node> | runner-token clear <node>")
    action, name = args
    with db() as c:
        node = resolve(c, name)
        if action == "set":
            if sys.stdin.isatty():
                import getpass
                token = getpass.getpass("runner token (input hidden): ").strip()
            else:
                token = sys.stdin.readline().strip()
            if not token:
                raise SystemExit("runner token is empty")
            c.execute("UPDATE nodes SET runner_token=? WHERE id=?", (token, node["id"]))
        else:
            c.execute("UPDATE nodes SET runner_token=NULL WHERE id=?", (node["id"],))
    print("runner token %s for %s" % ("stored" if action == "set" else "cleared", name))


def serve() -> None:
    """One server, two sockets on 127.0.0.1: the local port (full access) and the public port for
    `tailscale serve` (tokens always required). auth.access_control tells them apart by port."""
    import socket
    import uvicorn
    sockets = []
    for port in (int(os.environ.get("FLEET_PORT", "8700")), auth.PUBLIC_PORT):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        sockets.append(s)
    uvicorn.Server(uvicorn.Config(app)).run(sockets=sockets)


if __name__ == "__main__":
    if sys.argv[1:2] == ["token"]:
        token_cli(sys.argv[2:])
        raise SystemExit(0)
    if sys.argv[1:2] == ["runner-token"]:
        runner_token_cli(sys.argv[2:])
        raise SystemExit(0)
    if not MASTER_KEY:
        print("LITELLM_MASTER_KEY not set: tasks work, but model roles won't sync to LiteLLM")
    serve()

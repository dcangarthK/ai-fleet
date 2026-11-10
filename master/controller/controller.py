# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.110", "uvicorn>=0.29", "httpx>=0.27"]
# ///
"""Fleet controller for the master node.

Agentless: finds nodes via `tailscale status` (devices tagged tag:llm-node),
probes their model servers over the tailnet, and keeps LiteLLM's role groups
(coder/reviewer/fast/...) in sync with the role assignments and node health.

Run: uv run --script controller.py   (dashboard on http://127.0.0.1:8700)
"""
import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

DB_PATH = Path(os.environ.get("FLEET_DB", Path(__file__).with_name("fleet.db")))
LITELLM_URL = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000").rstrip("/")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
NODE_TAG = "tag:llm-node"
POLL_SECONDS = int(os.environ.get("FLEET_POLL_SECONDS", "30"))
MAX_FAILS = 3
ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
FLEET_PREFIX = "fleet--"
INFO_PORT = 11435  # onboard script publishes /fleet.json here (hardware, agents, model files)

# (engine, port, path): first that answers wins.
PROBES = [("ollama", 11434, "/api/tags"), ("openai", 8000, "/v1/models"), ("openai", 1234, "/v1/models"),
          ("openai", 8080, "/v1/models")]


# ---------- storage ----------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            id TEXT PRIMARY KEY, name TEXT, ip TEXT, os TEXT, engine TEXT, port INTEGER,
            models TEXT DEFAULT '[]', status TEXT DEFAULT 'pending',
            fails INTEGER DEFAULT 0, last_seen REAL, inventory TEXT);
        CREATE TABLE IF NOT EXISTS assignments (
            role TEXT, node TEXT, model TEXT, PRIMARY KEY (role, node, model));
        """)
        if "inventory" not in [r[1] for r in c.execute("PRAGMA table_info(nodes)")]:
            c.execute("ALTER TABLE nodes ADD COLUMN inventory TEXT")


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


async def refresh_nodes() -> None:
    try:
        seen = await asyncio.to_thread(tailnet_nodes)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as e:
        print(f"tailscale status failed: {e}")
        return
    online = [n for n in seen if n["online"]]
    async with httpx.AsyncClient(trust_env=False) as client:  # tailnet only: never via a proxy
        results = await asyncio.gather(*(probe(client, n["ip"]) for n in online))
        inventories = await asyncio.gather(*(fetch_inventory(client, n["ip"]) for n in online))
    probed = dict(zip([n["id"] for n in online], results))
    inventory = dict(zip([n["id"] for n in online], inventories))
    with db() as c:
        for n in seen:
            c.execute("INSERT OR IGNORE INTO nodes (id) VALUES (?)", (n["id"],))
            c.execute("UPDATE nodes SET name=?, ip=?, os=? WHERE id=?", (n["name"], n["ip"], n["os"], n["id"]))
            if inventory.get(n["id"]):
                c.execute("UPDATE nodes SET inventory=? WHERE id=?", (json.dumps(inventory[n["id"]]), n["id"]))
            found = probed.get(n["id"])
            if found:
                engine, port, models = found
                c.execute("UPDATE nodes SET engine=?, port=?, models=?, fails=0, last_seen=? WHERE id=?",
                          (engine, port, json.dumps(models), time.time(), n["id"]))
            else:
                c.execute("UPDATE nodes SET fails=fails+1 WHERE id=?", (n["id"],))
        ids = [n["id"] for n in seen]
        # Nodes that left the tailnet (or lost the tag) count as failing too.
        c.execute(f"UPDATE nodes SET fails=fails+1 WHERE id NOT IN ({','.join('?' * len(ids))})", ids)


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
        if not n or not healthy(n) or a["model"] not in json.loads(n["models"]):
            continue
        if n["engine"] == "ollama":
            params = {"model": f"ollama_chat/{a['model']}", "api_base": f"http://{n['ip']}:{n['port']}"}
        else:
            params = {"model": f"openai/{a['model']}", "api_base": f"http://{n['ip']}:{n['port']}/v1",
                      "api_key": "none"}
        params["timeout"] = 600
        desired[deployment_id(a["role"], a["node"], a["model"])] = {
            "model_name": a["role"], "litellm_params": params,
            "model_info": {"id": deployment_id(a["role"], a["node"], a["model"])},
        }
    return desired


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
    task = asyncio.create_task(loop())
    yield
    task.cancel()


app = FastAPI(title="fleet", lifespan=lifespan)
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


@app.middleware("http")
async def local_only(request: Request, call_next):
    # Block DNS rebinding (foreign Host) and cross-site writes: browsers can't add a
    # custom header cross-origin without a CORS preflight, which this server never grants.
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if host not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    if request.method != "GET" and request.headers.get("x-fleet") != "1":
        return JSONResponse({"detail": "missing X-Fleet header"}, status_code=403)
    return await call_next(request)


class Assignment(BaseModel):
    role: str
    node: str
    model: str


def fleet_state() -> dict:
    with db() as c:
        nodes = [dict(r) | {"models": json.loads(r["models"]), "healthy": healthy(r),
                            "inventory": json.loads(r["inventory"]) if r["inventory"] else None}
                 for r in c.execute("SELECT * FROM nodes ORDER BY name")]
        roles = [dict(r) for r in c.execute(
            "SELECT a.role, a.model, a.node AS node_id, n.name AS node FROM assignments a "
            "LEFT JOIN nodes n ON n.id = a.node ORDER BY a.role, n.name")]
    return {"nodes": nodes, "assignments": roles, "max_fails": MAX_FAILS}


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
        if a.model not in json.loads(node["models"]):
            raise HTTPException(400, f"{a.node} does not serve {a.model}")
        c.execute("INSERT OR IGNORE INTO assignments VALUES (?,?,?)", (a.role, node["id"], a.model))
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


if __name__ == "__main__":
    import uvicorn
    if not MASTER_KEY:
        raise SystemExit("LITELLM_MASTER_KEY not set (source master/litellm/.env)")
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("FLEET_PORT", "8700")))

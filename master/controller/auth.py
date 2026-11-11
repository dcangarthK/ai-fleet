"""Access control. Which port a request arrived on decides the rules, never a client header.

Local port (8700): only this machine can connect. Full access; the Host must be localhost (blocks DNS
rebinding) and writes need the X-Fleet header (blocks cross-site forms).
Public port (FLEET_PUBLIC_PORT, 8701, the target of `tailscale serve`): every request needs a scoped token,
as a Bearer header or the cookie set by /login?token=... Scopes: view (read) < operate (create/steer tasks)
< admin (approvals, nodes, roles, ceilings).
"""
import hashlib
import os
import secrets
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from store import db

LOCAL_HOSTS = {"127.0.0.1", "localhost"}
PUBLIC_HOST = os.environ.get("FLEET_PUBLIC_HOST", "").lower()  # only used to print sign-in links
PUBLIC_PORT = int(os.environ.get("FLEET_PUBLIC_PORT", "8701"))
SCOPES = ["view", "operate", "admin"]
COOKIE = "fleet_token"

# Writes that change who/what is trusted. Everything else that writes is "operate".
ADMIN_PATHS = ("/api/roles", "/api/models", "/api/instructions", "/api/policies",
               "/api/skills", "/api/enroll", "/api/notify-rules", "/api/export", "/api/import",
               "/api/tasks/approve", "/api/tasks/reject", "/api/budget/rules",
               # clearing a "not signed in" slot (docs/SPEC-limit-handoff.md 3.1); reading resources is view scope
               "/api/resources")
PUBLIC_PATHS = {"/enroll/onboard.sh", "/api/enroll/complete"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(scope: str, label: str) -> str:
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    token = "fl_" + secrets.token_urlsafe(32)
    with db() as c:
        c.execute("INSERT INTO tokens VALUES (?,?,?,?,?,NULL)",
                  (secrets.token_hex(4), _hash(token), scope, label, time.time()))
    return token


def token_identity(token: str | None) -> tuple[str, str] | None:
    if not token:
        return None
    with db() as c:
        row = c.execute("SELECT id, scope, label FROM tokens WHERE hash=?", (_hash(token),)).fetchone()
        if row:
            c.execute("UPDATE tokens SET last_used=? WHERE id=?", (time.time(), row["id"]))
    return (row["scope"], row["label"]) if row else None


def token_scope(token: str | None) -> str | None:
    identity = token_identity(token)
    return identity[0] if identity else None


def required_scope(request: Request) -> str:
    if request.method in ("GET", "HEAD"):
        return "admin" if request.url.path == "/api/export" else "view"
    path = request.url.path
    if path == "/api/policies/preview":
        return "view"
    # Installing/removing a catalog model is an ordinary requested operation; other node mutations are admin.
    if path.startswith("/api/nodes/") and "/models/" in path:
        return "operate"
    if path.startswith("/api/nodes/"):
        return "admin"
    # "/handoff" is "Hand off now" (docs/SPEC-limit-handoff.md 5): admin only, like approvals.
    if path.startswith(ADMIN_PATHS) or (path.startswith("/api/tasks/") and path.endswith(
            ("/approve", "/reject", "/resolve", "/outcome", "/handoff"))):
        return "admin"
    return "operate"


def _deny(msg: str) -> JSONResponse:
    return JSONResponse({"detail": msg}, status_code=403)


async def access_control(request: Request, call_next):
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
    public_enrollment = request.url.path in PUBLIC_PATHS
    if request.method not in ("GET", "HEAD") and not public_enrollment and request.headers.get("x-fleet") != "1":
        return _deny("missing X-Fleet header")
    port = (request.scope.get("server") or ("", 0))[1]
    if port != PUBLIC_PORT:
        if host not in LOCAL_HOSTS:
            return _deny("forbidden host")
        scope = "admin"
        # A local client may ask for *less* access (the orchestrator's MCP bridge sends operate), never more.
        lowered = (request.headers.get("x-fleet-scope") or "").strip().lower()
        if lowered in SCOPES and SCOPES.index(lowered) < SCOPES.index(scope):
            scope = lowered
            if SCOPES.index(scope) < SCOPES.index(required_scope(request)):
                return _deny(f"needs {required_scope(request)} access; this request asked for {scope}")
        request.state.scope = scope
        request.state.actor = "local"
        return await call_next(request)
    if request.url.path == "/login" or public_enrollment:
        request.state.scope = "view"
        request.state.actor = "enrollment"
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.cookies.get(COOKIE)
    identity = token_identity(token)
    if identity is None:
        return _deny("sign in: open /login?token=<token>")
    scope, label = identity
    if SCOPES.index(scope) < SCOPES.index(required_scope(request)):
        return _deny(f"needs {required_scope(request)} access; this token is {scope}")
    request.state.scope = scope
    request.state.actor = label
    return await call_next(request)

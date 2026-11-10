"""Access control.

localhost (127.0.0.1/localhost): full access; writes need the X-Fleet header (blocks cross-site forms).
FLEET_PUBLIC_HOST (your `tailscale serve` name): needs a scoped token, as a Bearer header or the cookie set
by /login?token=...  Scopes: view (read) < operate (create/steer tasks) < admin (approvals, nodes, roles).
Any other Host header is refused (DNS rebinding).
"""
import hashlib
import os
import secrets
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from store import db

LOCAL_HOSTS = {"127.0.0.1", "localhost"}
PUBLIC_HOST = os.environ.get("FLEET_PUBLIC_HOST", "").lower()
SCOPES = ["view", "operate", "admin"]
COOKIE = "fleet_token"

# Writes that change who/what is trusted. Everything else that writes is "operate".
ADMIN_PATHS = ("/api/nodes/", "/api/roles", "/api/tasks/approve", "/api/tasks/reject")


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


def token_scope(token: str | None) -> str | None:
    if not token:
        return None
    with db() as c:
        row = c.execute("SELECT id, scope FROM tokens WHERE hash=?", (_hash(token),)).fetchone()
        if row:
            c.execute("UPDATE tokens SET last_used=? WHERE id=?", (time.time(), row["id"]))
    return row["scope"] if row else None


def required_scope(request: Request) -> str:
    if request.method in ("GET", "HEAD"):
        return "view"
    path = request.url.path
    if path.startswith(ADMIN_PATHS) or (path.startswith("/api/tasks/") and path.endswith(("/approve", "/reject"))):
        return "admin"
    return "operate"


def _deny(msg: str) -> JSONResponse:
    return JSONResponse({"detail": msg}, status_code=403)


async def access_control(request: Request, call_next):
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
    if request.method not in ("GET", "HEAD") and request.headers.get("x-fleet") != "1":
        return _deny("missing X-Fleet header")
    if host in LOCAL_HOSTS:
        request.state.scope = "admin"
        return await call_next(request)
    if not PUBLIC_HOST or host != PUBLIC_HOST:
        return _deny("forbidden host")
    if request.url.path == "/login":
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.cookies.get(COOKIE)
    scope = token_scope(token)
    if scope is None:
        return _deny("sign in: open /login?token=<token>")
    if SCOPES.index(scope) < SCOPES.index(required_scope(request)):
        return _deny(f"needs {required_scope(request)} access; this token is {scope}")
    request.state.scope = scope
    return await call_next(request)

"""Single-admin-token auth gate for librarian (/admin) routes
(architecture-v2.md §14, §15: "librarian areas behind auth"). Fail-closed —
an unset ADMIN_TOKEN refuses every admin route rather than defaulting open;
a shipped fallback that left control routes unauthenticated would be a de
facto backdoor.
"""
"""Two ways to present the token: the `X-Admin-Token` header (JS fetch()
calls to JSON action endpoints) and the `admin_token` cookie (set once by
/admin/login so plain page navigation and htmx GET polling — which can't
attach custom headers per-request — stay authenticated too). Both compare
against the same CLAUDE.md invariant-8 filesystem/env-only secret; neither
is weaker than the other.
"""
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request

from . import config


def deposit_caller(authorization: str | None) -> str | None:
    """Who a LocalAGI request is (07 §7.4): "deposit" for the deposit
    token, "admin" for the admin token, None for anything else. A bearer
    token, as LocalAGI's client sends it. With no deposit token configured
    the routes are closed — never open."""
    if not config.DEPOSIT_TOKEN:
        return None
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return None
    if secrets.compare_digest(supplied, config.DEPOSIT_TOKEN):
        return "deposit"
    if config.ADMIN_TOKEN and secrets.compare_digest(supplied, config.ADMIN_TOKEN):
        return "admin"
    return None


@dataclass(frozen=True)
class Viewer:
    """Who is asking (07 §7.4, amended 2026-10-22): the admin token
    (`master`), a user of users.toml, or a guest while no user exists."""
    name: str | None
    role: str                       # "admin" | "user" | "guest"
    master: bool = False


GUEST = Viewer(None, "guest")
USER_COOKIE = "louise_token"


def tokens_of(headers, cookies) -> list[str]:
    """Every token the request presents, in this order: a bearer token (an
    OpenAI client, an MCP client, LocalAGI), `X-Admin-Token`, the user
    cookie, the admin cookie."""
    out = []
    scheme, _, supplied = (headers.get("authorization") or "").partition(" ")
    if scheme.lower() == "bearer" and supplied.strip():
        out.append(supplied.strip())
    for t in (headers.get("x-admin-token"), cookies.get(USER_COOKIE), cookies.get("admin_token")):
        if t:
            out.append(t)
    return out


def viewer_of(tokens: list[str]) -> Viewer | None:
    """The first token that names someone; else a guest while no user is
    active (Ask stays LAN-open until the first user exists); else None."""
    from app import users
    for t in tokens:
        if config.ADMIN_TOKEN and secrets.compare_digest(t, config.ADMIN_TOKEN):
            return Viewer("admin", "admin", master=True)
    try:
        for t in tokens:
            u = users.resolve(t)
            if u is not None:
                users.touch(u.name)
                return Viewer(u.name, u.role)
        return None if users.any_active() else GUEST
    except Exception:   # noqa: BLE001 — a users.toml that does not read: nobody but the admin token, never open
        return None


def is_admin(tokens: list[str]) -> bool:
    v = viewer_of(tokens)
    return v is not None and v.role == "admin"


def require_admin(request: Request) -> None:
    """The admin token, or a user whose role is `admin`."""
    from app import users
    tokens = tokens_of(request.headers, request.cookies)
    try:
        admins = any(u.role == "admin" for u in users.load())
    except Exception:   # noqa: BLE001 — unreadable: the admin token alone
        admins = False
    if not config.ADMIN_TOKEN and not admins:
        raise HTTPException(401, "ADMIN_TOKEN not configured — admin routes are disabled")
    if not is_admin(tokens):
        raise HTTPException(401, "invalid or missing admin token "
                                 "(X-Admin-Token header, a bearer token or the admin_token cookie)")


#: what a user may reach once users exist: Ask and the reader, their
#: pictures and translation, the machine interfaces (05 §5.4), Connect.
#: Login, Help, the peers' routes and the well-known documents stay open.
READER_PATHS = ("/ask", "/read/", "/connect", "/api/ask/", "/api/search", "/api/verify",
                "/api/books", "/api/status", "/api/stats", "/api/events", "/v1/")


def gated(path: str) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p) for p in READER_PATHS)


class ViewerGate:
    """ASGI middleware: puts the viewer on `request.state.viewer` for every
    request, and answers for a READER path nobody may see — a page asked
    by a browser goes to the login page, anything else gets 401 with
    `WWW-Authenticate: Bearer`. Pure ASGI, not BaseHTTPMiddleware, so a
    caller's disconnect still reaches the search (app/cancel.py)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from urllib.parse import quote
        from starlette.requests import Request as _Request
        from starlette.responses import JSONResponse, RedirectResponse
        req = _Request(scope)
        v = viewer_of(tokens_of(req.headers, req.cookies))
        scope.setdefault("state", {})["viewer"] = v
        if v is None and gated(scope["path"]):
            if req.method == "GET" and "text/html" in req.headers.get("accept", "") \
                    and "hx-request" not in req.headers:
                resp = RedirectResponse(f"/login?next={quote(scope['path'])}", status_code=303)
            else:
                resp = JSONResponse({"detail": "a token is needed: `Authorization: Bearer <token>` "
                                               "(louise user add <name> gives one)"},
                                    status_code=401, headers={"WWW-Authenticate": "Bearer"})
            return await resp(scope, receive, send)
        return await self.app(scope, receive, send)

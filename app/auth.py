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

from fastapi import Cookie, Header, HTTPException

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


def require_admin(x_admin_token: str | None = Header(default=None),
                   admin_token: str | None = Cookie(default=None)) -> None:
    if not config.ADMIN_TOKEN:
        raise HTTPException(401, "ADMIN_TOKEN not configured — admin routes are disabled")
    supplied = x_admin_token or admin_token
    if not supplied or not secrets.compare_digest(supplied, config.ADMIN_TOKEN):
        raise HTTPException(401, "invalid or missing admin token "
                                  "(X-Admin-Token header or admin_token cookie)")

"""What the front says on the wire, defined once: the run header, the
headers that describe one hop, the refusal, the admin token and its check.

Kept apart from front_localai.py since the llama-swap front shared it; that
front retired at migration step 15g (docs/notes/legacy-to-delete.md §8).
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

RUN_HEADER = "Louise-Run"
#: not passed on in either direction: they describe one hop, not the message
HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                 "te", "trailers", "transfer-encoding", "upgrade", "host",
                 "content-length"})


class Refused(Exception):
    """The front will not do it. `status` and `body` are the HTTP answer;
    503 is a deferral to Louise's worker, never an error on a document."""

    def __init__(self, status: int, reason: str, **extra: Any) -> None:
        super().__init__(reason)
        self.status = status
        self.body = {"error": reason, **extra}


def read_admin_token(environ: dict[str, str] | os._Environ) -> str | None:
    """The token, from the file `ADMIN_TOKEN_FILE` names. Unset: None, and
    the operator's routes answer 401. Set and unreadable: a boot error that
    names the path and never the material."""
    path = environ.get("ADMIN_TOKEN_FILE")
    if not path:
        return None
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise RuntimeError(f"ADMIN_TOKEN_FILE={path} cannot be read: "
                           f"{type(e).__name__}") from None
    if not token:
        raise RuntimeError(f"ADMIN_TOKEN_FILE={path} is empty")
    return token


def check_admin(request: Request, admin_token: str | None) -> None:
    given = request.headers.get("authorization", "")
    if (admin_token is None or not given.startswith("Bearer ")
            or not hmac.compare_digest(given[7:].encode(), admin_token.encode())):
        raise HTTPException(401, "this route is the operator's: send the admin token "
                                 "as a Bearer authorization")

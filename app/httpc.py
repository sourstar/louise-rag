"""One pooled HTTP client for the paths that call a model server.

`httpx.post(url, ...)` — the module-level function — builds a whole `Client`
for that one request and throws it away: a fresh TLS context, which parses
and loads the system CA bundle, and a fresh connection, which pays the TCP
and TLS handshake again. It is the right call for something that happens
once. It is the wrong call inside a loop that feeds a GPU.

MEASURED ON THE NODE, 2026-09-22: `ssl.create_default_context` under
`embeddings._request_vectors` was 185 of 1,122 samples — 16 % of the
worker's whole CPU — while the encoder it feeds sat at 99 % of one core
waiting for work. The endpoints are on the LAN and speak plain HTTP, so the
TLS context was not even used; it is built by the constructor regardless.

So: one `Client` per process, its connection pool kept warm, requests issued
through it from every thread (httpx's Client is safe to share; each request
takes a connection from the pool). Timeouts stay per request, because they
differ per caller and per batch.

Not a general HTTP layer: acquisition has its own transports with their own
budgets and retries (`app/acquire/`), and nothing here touches the transfer
manager's byte budget (invariant 5) — these calls carry model input and
output, which the budget has never covered.
"""
from __future__ import annotations

import threading
from typing import Any

import httpx

#: The pool. Sized for the callers that share it: a few model endpoints, each
#: with its own dispatch gate bounding how many requests can be in flight
#: (`app/dispatch.py`), plus the searches. Keepalive holds a connection per
#: endpoint per lane rather than re-handshaking each time.
LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=32,
                      keepalive_expiry=120.0)

_lock = threading.Lock()
_client: httpx.Client | None = None


def client() -> httpx.Client:
    """The process's client, built on first use."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = httpx.Client(limits=LIMITS, follow_redirects=False)
    return _client


def post(url: str, **kw: Any) -> httpx.Response:
    return client().post(url, **kw)


def get(url: str, **kw: Any) -> httpx.Response:
    return client().get(url, **kw)


def put(url: str, **kw: Any) -> httpx.Response:
    return client().put(url, **kw)


def reset() -> None:
    """Drop the client and its pool — for tests, and for a process that has
    just been told its endpoints moved."""
    global _client
    with _lock:
        old, _client = _client, None
    if old is not None:
        try:
            old.close()
        except Exception:                               # noqa: BLE001
            pass

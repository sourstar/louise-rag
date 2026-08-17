"""Rate limiting and body-size caps.

Every coordinator endpoint is reachable by anyone who can route to it, and
POST /bundles accepts a multi-megabyte archive. Without caps the service is a
free memory-exhaustion target, so these are not optional hardening — they are
part of the endpoint contract.

Deliberately in-process and dependency-free: a single coordinator instance is
what §7 describes ("~200 lines FastAPI, holds no bulk data"), and reaching for
Redis to rate-limit one process would be the opposite of "deliberately boring".
A multi-instance deployment needs a shared limiter, and that is called out here
rather than silently assumed away.
"""
import threading
import time

from fastapi import HTTPException, Request

MAX_BODY_BYTES = 256 << 20        # a bundle archive; ~256 MiB of f16 vectors
MAX_JSON_BYTES = 1 << 20


class RateLimiter:
    """Fixed-window counter per key. Coarse on purpose: it exists to stop
    hammering, not to meter fairly."""

    def __init__(self, limit: int, window_seconds: float):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.limit:
                retry = self.window - (now - hits[0])
                raise HTTPException(
                    429, f"rate limit exceeded; retry in {retry:.0f}s")
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 10_000:      # bound the key space
                cutoff = now - self.window
                self._hits = {k: v for k, v in self._hits.items()
                              if v and v[-1] > cutoff}


def client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def read_capped_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    """Read a request body, refusing anything oversized.

    Content-Length is checked first as a cheap early out, but it is
    client-supplied and can lie, so the streamed read is capped independently —
    otherwise a chunked request with no Content-Length bypasses the check
    entirely.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise HTTPException(413, f"body exceeds {limit} bytes")
        except ValueError:
            raise HTTPException(400, "malformed Content-Length") from None

    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, f"body exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)

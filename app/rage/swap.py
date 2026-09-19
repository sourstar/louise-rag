"""The HTTP client of the local llama-swap — the process runner.

docs/notes/rage.md §4. llama-swap listens on 127.0.0.1 inside the container
and nothing reaches it but this client. It has no load endpoint (verified
2026-09-19 against its README): a model starts on the first request to
`/upstream/<id>/…`, so a load IS a request to the entry's health path, made
by the front once admission has said yes. It has unload endpoints, and they
are the only other thing asked of it.

EVERY WAIT IS BOUNDED (CLAUDE.md). A load ends at `load_timeout_s`, a little
past llama-swap's own `healthCheckTimeout`; a proxied request at the read
timeout. A caller that gives up sooner is simply disconnected.
"""
from __future__ import annotations

import httpx


class SwapError(RuntimeError):
    """llama-swap could not do what was asked. `status` is what the front
    answers with: 502 when it failed, 504 when it timed out — both are an
    endpoint busy to Louise's worker, a deferral and never a document's error."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class Swap:
    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 load_timeout_s: float = 960.0, read_timeout_s: float = 3600.0) -> None:
        self.load_timeout_s = load_timeout_s
        self.client = httpx.AsyncClient(
            base_url=base_url, transport=transport,
            timeout=httpx.Timeout(connect=10.0, read=read_timeout_s, write=60.0, pool=10.0))

    async def aclose(self) -> None:
        await self.client.aclose()

    async def load(self, variant: str, health: str) -> None:
        """Start the variant and wait until it answers its health path."""
        try:
            r = await self.client.get(f"/upstream/{variant}{health}",
                                      timeout=self.load_timeout_s)
        except httpx.TimeoutException:
            raise SwapError(f"{variant} did not come up within "
                            f"{self.load_timeout_s:.0f} s", 504) from None
        except httpx.HTTPError as e:
            raise SwapError(f"llama-swap is unreachable: {type(e).__name__}") from None
        if r.status_code >= 400:
            raise SwapError(f"{variant} failed to start: llama-swap answered "
                            f"{r.status_code}: {r.text[:300]}")

    async def unload(self, variant: str) -> None:
        try:
            r = await self.client.post(f"/api/models/unload/{variant}", timeout=120.0)
        except httpx.HTTPError as e:
            raise SwapError(f"llama-swap is unreachable: {type(e).__name__}") from None
        if r.status_code >= 400 and r.status_code != 404:    # 404: it was not running
            raise SwapError(f"unloading {variant}: llama-swap answered {r.status_code}")

    async def send(self, method: str, variant: str, path: str, *, query: str,
                   headers: dict[str, str], content: bytes) -> httpx.Response:
        """One request, passed on as it came: never merged with another,
        never reordered (03 §3.3 — the gate measures batch invariance at the
        peer's own batching, and a front that coalesced would measure a path
        production does not take)."""
        url = f"/upstream/{variant}/{path}" + (f"?{query}" if query else "")
        request = self.client.build_request(method, url, headers=headers, content=content)
        try:
            return await self.client.send(request, stream=True)
        except httpx.TimeoutException:
            raise SwapError(f"{variant} did not answer in time", 504) from None
        except httpx.HTTPError as e:
            raise SwapError(f"{variant} is unreachable: {type(e).__name__}") from None

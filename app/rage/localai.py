"""The HTTP client of the local LocalAI — the model runner (migration step 15).

docs/notes/rage-over-localai.md: LocalAI launches, loads, keeps and evicts the
models; RAGE states the run of every answer. LocalAI listens on 127.0.0.1
inside the container with no key (§8.4: it takes keys only from an
environment value, which CLAUDE.md invariant 8 rules out), so nothing reaches
it but this client, and RAGE's front guards the operator's routes.

What RAGE asks of it, and nothing else:
- `GET /system`: each loaded model's backend process and the options its
  weights were loaded with (`load_options`, deploy/localai/patches P1);
- `GET /version`: the release, for the engine's readable `build`;
- `GET /v1/models`: which models it is configured with;
- `POST /backend/load`, `/backend/shutdown`: the operator's load, unload and
  restart; `POST /models/reload`: re-read the model YAML after a PUT or DELETE;
- the models' own routes, passed on as they came.

EVERY WAIT IS BOUNDED (CLAUDE.md), as in `swap.py`: a load at
`load_timeout_s`, a proxied request at the read timeout.
"""
from __future__ import annotations

from typing import Any

import httpx

from . import loadopts


class LocalAIError(RuntimeError):
    """LocalAI could not do what was asked. `status` is what the front
    answers with: 502 when it failed, 504 when it timed out — an endpoint
    busy to Louise's worker, a deferral and never a document's error."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class LocalAI:
    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 load_timeout_s: float = 960.0, read_timeout_s: float = 3600.0) -> None:
        self.load_timeout_s = load_timeout_s
        self.client = httpx.AsyncClient(
            base_url=base_url, transport=transport,
            timeout=httpx.Timeout(connect=10.0, read=read_timeout_s, write=60.0, pool=10.0))

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _call(self, method: str, path: str, *, json: Any = None,
                    timeout: float = 30.0, absent_ok: bool = False) -> httpx.Response:
        try:
            r = await self.client.request(method, path, json=json, timeout=timeout)
        except httpx.TimeoutException:
            raise LocalAIError(f"LocalAI did not answer {path} within {timeout:.0f} s",
                               504) from None
        except httpx.HTTPError as e:
            raise LocalAIError(f"LocalAI is unreachable: {type(e).__name__}") from None
        if r.status_code >= 400 and not (absent_ok and r.status_code == 404):
            raise LocalAIError(f"LocalAI answered {path} with {r.status_code}: {r.text[:300]}")
        return r

    async def system(self) -> list[dict[str, Any]]:
        """`loaded_models`, read so that `load_options` keeps LocalAI's digits
        (loadopts.parse)."""
        body = loadopts.parse((await self._call("GET", "/system")).content)
        models = body.get("loaded_models") if isinstance(body, dict) else None
        return [m for m in models or [] if isinstance(m, dict)]

    async def version(self) -> str:
        body = (await self._call("GET", "/version")).json()
        version = body.get("version") if isinstance(body, dict) else None
        if not isinstance(version, str) or not version:
            raise LocalAIError(f"LocalAI's /version names no version: {body!r}")
        return version

    async def models(self) -> set[str]:
        body = (await self._call("GET", "/v1/models")).json()
        return {m["id"] for m in (body.get("data") or []) if isinstance(m, dict) and "id" in m}

    async def load(self, model_id: str) -> None:
        await self._call("POST", "/backend/load", json={"model": model_id},
                         timeout=self.load_timeout_s)

    async def shutdown(self, model_id: str) -> None:
        """Stop the model's backend. A model that is not loaded is already
        stopped, which is what was asked: LocalAI answers it 404
        (`BackendShutdownEndpoint`, `model.ErrModelNotFound`)."""
        await self._call("POST", "/backend/shutdown", json={"model": model_id},
                         timeout=120.0, absent_ok=True)

    async def reload_configs(self) -> None:
        await self._call("POST", "/models/reload")

    async def send(self, method: str, path: str, *, query: str, headers: dict[str, str],
                   content: bytes) -> httpx.Response:
        """One request, passed on as it came: never merged with another,
        never reordered (03 §3.3 — the gate measures batch invariance at the
        peer's own batching)."""
        url = f"/{path}" + (f"?{query}" if query else "")
        request = self.client.build_request(method, url, headers=headers, content=content)
        try:
            return await self.client.send(request, stream=True)
        except httpx.TimeoutException:
            raise LocalAIError(f"LocalAI did not answer /{path} in time", 504) from None
        except httpx.HTTPError as e:
            raise LocalAIError(f"LocalAI is unreachable: {type(e).__name__}") from None

"""The HTTP surface of RAGE — docs/notes/rage.md §6.

    /upstream/<model>/…                 the model, with Louise-Run on the answer
    GET  /louise/models                 what is configured, what is loaded, which run
    GET  /louise/runs/<run_id>          docs/09-formats.md §9.12's object
    GET  /louise/gpu                    the clock
    POST /louise/models/<id>/load       {device?}      — operator
    POST /louise/models/<id>/unload                    — operator
    POST /louise/models/<id>/restart                   — operator
    PUT  /louise/models/<id>            the entry, as rage.d/<id>.toml  — operator
    DELETE /louise/models/<id>                                          — operator

A URL NAMES A MODEL, NEVER A DEVICE. The front routes to the variant that is
up; a first request for a dormant model loads it on its default device,
through admission like any load.

THE OPERATOR'S ROUTES TAKE THE ADMIN TOKEN, read from the file
`ADMIN_TOKEN_FILE` names (CLAUDE.md invariant 8: a secret is a path, never a
value in the environment, a log line or a message). Unset, they answer 401.
Inference and the read routes are open on the LAN, as the model ports were.

NOTHING IS MERGED OR REORDERED. One request in, one request on (swap.send).
"""
from __future__ import annotations

import asyncio
import hmac
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.background import BackgroundTask

from . import model
from .clock import HEADER as LEVEL_HEADER, Clock, is_interactive
from .placement import Placement, Refused
from .swap import Swap, SwapError

RUN_HEADER = "Louise-Run"
#: not passed on in either direction: they describe one hop, not the message
_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                  "te", "trailers", "transfer-encoding", "upgrade", "host",
                  "content-length"})
WATCH_S = 5.0
#: how often the cards are looked at (placement.relieve). A stranger filled 21
#: GiB of the card in under a minute on 2026-09-20; one ioctl and a dozen small
#: files per look.
PRESSURE_S = 3.0


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


def create_app(placement: Placement, swap: Swap, clock: Clock,
               admin_token: str | None, *, watch: bool = True) -> FastAPI:

    async def _watch() -> None:
        seen = placement.signature()
        while True:
            await asyncio.sleep(WATCH_S)
            try:
                now = placement.signature()
                if now != seen:
                    seen = now
                    if placement.reload():
                        await placement.apply()
            except Exception as e:              # noqa: BLE001 — a watcher that dies
                # silently stops applying the operator's edits for good
                placement.log(f"applying rage.toml failed, will try again: "
                              f"{type(e).__name__}: {e}")

    async def _pressure() -> None:
        while True:
            await asyncio.sleep(PRESSURE_S)
            try:
                await placement.relieve()
            except Exception as e:              # noqa: BLE001 — the same: a watcher
                # that dies silently is a card nobody looks at any more
                placement.log(f"looking at the cards failed, will look again: "
                              f"{type(e).__name__}: {e}")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tasks = [asyncio.create_task(placement.apply())]
        if watch:
            tasks.append(asyncio.create_task(_watch()))
            tasks.append(asyncio.create_task(_pressure()))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            clock.close()                       # the card gets its clock back
            await swap.aclose()

    app = FastAPI(title="RAGE", lifespan=lifespan, docs_url=None, redoc_url=None)

    def admin(request: Request) -> None:
        given = request.headers.get("authorization", "")
        if (admin_token is None or not given.startswith("Bearer ")
                or not hmac.compare_digest(given[7:].encode(), admin_token.encode())):
            raise HTTPException(401, "this route is the operator's: send the admin token "
                                     "as a Bearer authorization")

    @app.exception_handler(Refused)
    async def _refused(_request: Request, exc: Refused) -> JSONResponse:
        headers = {"Retry-After": "30"} if exc.status == 503 else None
        return JSONResponse(exc.body, status_code=exc.status, headers=headers)

    # ---- reading ---------------------------------------------------------------

    @app.get("/health")
    async def health() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/louise/models")
    async def models() -> list[dict[str, Any]]:
        return placement.describe()

    @app.get("/louise/runs/{run_id}")
    async def run(run_id: str) -> dict[str, Any]:
        found = placement.store.get(run_id)
        if found is None:
            raise HTTPException(404, f"this container has stated no run {run_id}")
        return found

    @app.get("/louise/gpu")
    async def gpu() -> dict[str, Any]:
        """The clock; each card's REAL memory and whose it is; what RAGE refused,
        gave up under pressure and restored (Louise's notification); and this
        container's name, so a reader of the host's processes can tell which
        of them are RAGE's."""
        cards = sorted({d for m in placement.cfg.models.values() for d in m.cmd} - {model.CPU})
        return {"clock": clock.status(),
                "container": os.uname().nodename,
                "memory": {card: placement.card_memory(card) for card in cards},
                "events": list(placement.events)}

    # ---- the operator's ----------------------------------------------------------

    def _row(model_id: str) -> dict[str, Any]:
        return next(r for r in placement.describe() if r["id"] == model_id)

    @app.post("/louise/models/{model_id}/load", dependencies=[Depends(admin)])
    async def load(model_id: str, request: Request) -> dict[str, Any]:
        raw = await request.body()
        body = await request.json() if raw.strip() else {}
        device = body.get("device") if isinstance(body, dict) else None
        if device is not None and not isinstance(device, str):
            raise HTTPException(400, "device must be a string")
        await placement.ensure(model_id, device)
        return _row(model_id)

    @app.post("/louise/models/{model_id}/unload", dependencies=[Depends(admin)])
    async def unload(model_id: str) -> dict[str, Any]:
        await placement.unload(model_id)
        return {"id": model_id, "state": "unloaded"}

    @app.post("/louise/models/{model_id}/restart", dependencies=[Depends(admin)])
    async def restart(model_id: str) -> dict[str, Any]:
        await placement.restart(model_id)
        return _row(model_id)

    def _dropin(model_id: str) -> Path:
        return model.dropin_dir(placement.config_path) / f"{model_id}.toml"

    def _hand_written(model_id: str) -> None:
        entry = placement.cfg.models.get(model_id)
        if entry is not None and entry.source == str(placement.config_path):
            raise Refused(409, f"{model_id} is defined in {placement.config_path.name}, the "
                               f"operator's file; edit it there. The API writes only rage.d/")

    @app.put("/louise/models/{model_id}", dependencies=[Depends(admin)])
    async def put(model_id: str, request: Request) -> dict[str, Any]:
        _hand_written(model_id)
        if model_id in placement.placed:
            raise Refused(409, f"{model_id} is loaded; unload it before changing it")
        try:
            entry = model.entry_from_table(model_id, await request.json(),
                                           source=str(_dropin(model_id)))
        except model.RageConfigError as e:
            raise Refused(400, str(e)) from None
        path = _dropin(model_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text(model.entry_to_toml(entry), encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        if not placement.reload():
            path.unlink(missing_ok=True)
            raise Refused(400, "the configuration does not load with this entry; see the log")
        return _row(model_id)

    @app.delete("/louise/models/{model_id}", dependencies=[Depends(admin)])
    async def delete(model_id: str) -> dict[str, Any]:
        if model_id not in placement.cfg.models:
            raise Refused(404, f"{model_id!r} is not a model of this container")
        _hand_written(model_id)
        if model_id in placement.placed:
            raise Refused(409, f"{model_id} is loaded; unload it before removing it")
        _dropin(model_id).unlink(missing_ok=True)
        placement.reload()
        return {"id": model_id, "state": "removed"}

    # ---- the models ----------------------------------------------------------------

    @app.api_route("/upstream/{model_id}/{path:path}",
                   methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def upstream(model_id: str, path: str, request: Request) -> StreamingResponse:
        placed = await placement.ensure(model_id)
        placed.in_flight += 1                   # no await since ensure(): never idle now
        placed.last_used = time.time()
        entry = placement.cfg.models[model_id]
        waiting = is_interactive(request.headers.get(LEVEL_HEADER))
        if waiting:
            clock.enter()
        try:
            answer = await swap.send(
                request.method, entry.variant_id(placed.device), path,
                query=request.url.query,
                headers={k: v for k, v in request.headers.items() if k.lower() not in _HOP},
                content=await request.body())
        except SwapError as e:
            placed.in_flight -= 1
            if waiting:
                clock.leave()
            raise Refused(e.status, str(e)) from None
        except BaseException:
            placed.in_flight -= 1
            if waiting:
                clock.leave()
            raise

        # A body already read is passed on decoded, so it no longer has the
        # encoding its header names; a body still streaming goes on raw, as sent.
        read = answer.is_stream_consumed
        headers = {k: v for k, v in answer.headers.items()
                   if k.lower() not in _HOP and not (read and k.lower() == "content-encoding")}
        if placed.run is not None and placement.still_the_run(placed):
            headers[RUN_HEADER] = placed.run

        finished = False

        async def finish() -> None:
            """Once, however the answer ends. A caller that hangs up before
            the body starts never runs the generator's `finally`, and a clock
            left held would stay at its maximum for good; so the same
            clean-up is also the response's background task."""
            nonlocal finished
            if finished:
                return
            finished = True
            placed.in_flight -= 1
            await answer.aclose()
            placed.last_used = time.time()
            if waiting:
                clock.leave()

        async def body():
            try:
                if read:
                    yield answer.content
                else:
                    async for chunk in answer.aiter_raw():
                        yield chunk
            finally:
                await finish()

        return StreamingResponse(body(), status_code=answer.status_code, headers=headers,
                                 background=BackgroundTask(finish))

    return app

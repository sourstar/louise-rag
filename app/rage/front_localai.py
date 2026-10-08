"""The HTTP surface of RAGE when LocalAI runs the models (`RAGE_RUNNER=localai`;
migration step 15d, docs/notes/rage-over-localai.md §7).

    /upstream/<model>/…                 the model, with Louise-Run on the answer
    GET  /louise/models                 what LocalAI is configured with, what is loaded
    GET  /louise/runs/<run_id>          docs/09-formats.md §9.12's object
    GET  /louise/gpu                    the clock, the card by model, the events
    POST /louise/models/<id>/load       — operator: LocalAI's /backend/load
    POST /louise/models/<id>/unload     — operator: LocalAI's /backend/shutdown
    POST /louise/models/<id>/restart    — operator: the fan's button (rage.md §11)
    PUT  /louise/models/<id>            LocalAI's model YAML  — operator
    DELETE /louise/models/<id>                                — operator

RAGE HAS ONE JOB HERE, and three it keeps beside it. The job: the run of
every answer, read from what LocalAI launched (observer.py). Beside it: the
card's clock while a person waits (clock.py); the only door to LocalAI's
admin routes, which listen on loopback with no key (§8.4); and the System
tab's panel. Loading, eviction, preload and caching are LocalAI's.

A URL NAMES A MODEL, AND NOTHING ELSE DOES. LocalAI picks the model from the
body, and RAGE states the run of the model the URL names; so the body's
`model` is set to the URL's, whatever it said. Nobody who asks — a person, an
agent, Louise's own search — chooses a model: the collection's file names the
URL, and the run on the answer is checked against the space before the vector
is used (03 §3.3). A refusal instead would have made every client spell the
model twice and keep the two in step; Louise's query side did not (it sent the
DOCUMENT encoder's name to the query encoder's URL, `embeddings._side`), and
under LocalAI that would have embedded a MedCPT query with the article
encoder while the header named the query encoder's run (operator, 2026-10-03:
"the user should not bother about choosing the right model").

A TEXT PAIR IS ASKED FOR, NEVER GUESSED (docs/notes/input-forms.md; pairs.py).
An embeddings body with `input_pairs` is passed on only to a model whose YAML
declares `pairs:true`, each pair as one marked string; any other model, and
any plain input carrying the marker, is a 400. An answer to a pair request
whose run was loaded without `pairs:true` is a 502, never a vector.

THE MODEL YAML IS THE DESIRED STATE (invariant 10; operator, 2026-10-03).
`PUT` writes `<id>.yaml` into LocalAI's models directory — JSON, which is
YAML, so no YAML library is added — with a first line that marks it as
RAGE's; a file without it is the operator's and is edited by hand (409).
`PUT` refuses a YAML that leaves unpinned an option LocalAI would fill in by
itself (§10), so the API cannot bring back the drift §10 removes.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect

from . import pairs
from .clock import HEADER as LEVEL_HEADER, Clock, is_interactive
from .localai import LocalAI, LocalAIError
from .model import _ID
from .observer import Observer
from .wire import HOP as _HOP, RUN_HEADER, Refused, check_admin

#: how often LocalAI is read: the panel's events, and a new process's run
#: computed before the first request that needs it
WATCH_S = 5.0

#: The first line of every YAML this front writes. A file without it is the
#: operator's.
MARKER = "# written by RAGE (PUT /louise/models/<id>): change it through the API"

#: The options LocalAI fills in by itself when a YAML leaves them unset
#: (rage-over-localai §10), each of which changes a run when it changes. A
#: YAML written through the API pins every one.
PINNED = ("context_size", "threads", "gpu_layers")
#: `seed` too: unset, or LocalAI's -1 (`RAND_SEED`), LocalAI draws a random
#: one at EVERY load (`core/backend/options.go`, `getSeed`) — found by
#: running it (2026-10-03): each reload was another run, "Seed 551695963 →
#: 1045691491", and each would have been gated again.
PINNED_PARAMETERS = ("model", "batch", "seed")
RAND_SEED = -1


def _ours(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8") as f:
            return f.readline().rstrip("\n") == MARKER
    except OSError:
        return False


def yaml_config(path: Path) -> dict[str, Any]:
    """A model YAML as this repo writes them — JSON after `#` comment lines,
    RAGE's marker among them. A file in YAML proper is not read (ValueError):
    no YAML library is added for it."""
    body = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))
    config = json.loads(body)
    if not isinstance(config, dict):
        raise ValueError("not an object")
    return config


def unpinned(body: dict[str, Any]) -> list[str]:
    """The fields of §10's list a model YAML leaves to LocalAI."""
    missing = [k for k in PINNED if body.get(k) is None]
    params = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
    missing += [f"parameters.{k}" for k in PINNED_PARAMETERS
                if params.get(k) is None or (k == "seed" and params[k] == RAND_SEED)]
    backend = str(body.get("backend") or "")
    if backend.startswith("llama"):
        options = body.get("options") if isinstance(body.get("options"), list) else []
        if not any(isinstance(o, str) and o.split(":", 1)[0] in ("parallel", "n_parallel")
                   for o in options):
            missing.append("options: parallel:<n>")
    return missing


def create_localai_app(observer: Observer, client: LocalAI, clock: Clock,
                       admin_token: str | None, *, models_dir: Path,
                       watch: bool = True) -> FastAPI:
    models_dir = Path(models_dir)

    async def _watch() -> None:
        while True:
            try:
                await observer.refresh()
            except Exception as e:              # noqa: BLE001 — a watcher that dies
                # silently is a panel that stops moving and runs nobody states
                observer.log(f"[rage] reading LocalAI failed, will read again: "
                             f"{type(e).__name__}: {e}")
            await asyncio.sleep(WATCH_S)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tasks = [asyncio.create_task(_watch())] if watch else []
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            clock.close()                       # the card gets its clock back
            await client.aclose()

    app = FastAPI(title="RAGE", lifespan=lifespan, docs_url=None, redoc_url=None)

    def admin(request: Request) -> None:
        check_admin(request, admin_token)

    @app.exception_handler(Refused)
    async def _refused(_request: Request, exc: Refused) -> JSONResponse:
        headers = {"Retry-After": "30"} if exc.status == 503 else None
        return JSONResponse(exc.body, status_code=exc.status, headers=headers)

    @app.exception_handler(LocalAIError)
    async def _runner(_request: Request, exc: LocalAIError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    # ---- reading ---------------------------------------------------------------

    @app.get("/health")
    async def health() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/louise/models")
    async def models() -> list[dict[str, Any]]:
        configured = await client.models()
        rows = []
        for model_id in sorted(configured | set(observer.instances)):
            row = observer.row(model_id)
            files = _files(model_id)
            # None: LocalAI lists a model no file defines any more. Its reload
            # never forgets one (localai-bugs.md B8); it goes at its next start.
            row["file"] = (None if not files else
                           "rage" if any(_ours(p) for p in files) else "operator")
            rows.append(row)
        return rows

    @app.get("/louise/runs/{run_id}")
    async def run(run_id: str) -> dict[str, Any]:
        found = observer.store.get(run_id)
        if found is None:
            raise HTTPException(404, f"this container has stated no run {run_id}")
        return found

    @app.get("/louise/gpu")
    async def gpu() -> dict[str, Any]:
        """The clock; each card's real memory, split by model, the rest a
        stranger's; which host process is which model; what RAGE saw happen."""
        return {"clock": clock.status(),
                "container": os.uname().nodename,
                "memory": {card: observer.card_memory(card)
                           for card in sorted(observer.cfg.pci)},
                "processes": observer.processes(),
                "events": list(observer.events)}

    # ---- the operator's ----------------------------------------------------------

    def _check_id(model_id: str) -> None:
        if not _ID.fullmatch(model_id):
            raise Refused(400, f"{model_id!r} is not a model id")

    @app.post("/louise/models/{model_id}/load", dependencies=[Depends(admin)])
    async def load(model_id: str) -> dict[str, Any]:
        _check_id(model_id)
        await client.load(model_id)
        await observer.refresh()
        await observer.settle(model_id)
        return observer.row(model_id)

    @app.post("/louise/models/{model_id}/unload", dependencies=[Depends(admin)])
    async def unload(model_id: str) -> dict[str, Any]:
        _check_id(model_id)
        await client.shutdown(model_id)
        await observer.refresh()
        return {"id": model_id, "state": "unloaded"}

    @app.post("/louise/models/{model_id}/restart", dependencies=[Depends(admin)])
    async def restart(model_id: str) -> dict[str, Any]:
        """That model and no other: never another model, never another app
        (operator, 2026-09-20). With its options pinned (§10) the reload
        states the same run."""
        _check_id(model_id)
        await client.shutdown(model_id)
        await client.load(model_id)
        await observer.refresh()
        await observer.settle(model_id)
        return observer.row(model_id)

    def _files(model_id: str) -> list[Path]:
        return [p for p in (models_dir / f"{model_id}.yaml", models_dir / f"{model_id}.yml")
                if p.exists()]

    async def _not_the_operators(model_id: str) -> list[Path]:
        files = _files(model_id)
        hand = [p.name for p in files if not _ours(p)]
        if hand:
            raise Refused(409, f"{model_id} is defined in {hand[0]}, the operator's file; "
                               f"edit it there. The API writes only the files it wrote")
        if not files and model_id in await client.models():
            raise Refused(409, f"LocalAI has a model {model_id!r} that no file of RAGE's "
                               f"defines: the operator's, under another name, or one whose "
                               f"file was removed, which LocalAI forgets only at its next "
                               f"start (localai-bugs.md B8)")
        return files

    @app.put("/louise/models/{model_id}", dependencies=[Depends(admin)])
    async def put(model_id: str, request: Request) -> dict[str, Any]:
        _check_id(model_id)
        try:
            body = await request.json()
        except ValueError:
            raise Refused(400, "the body is the model's LocalAI configuration, as JSON") from None
        if not isinstance(body, dict):
            raise Refused(400, "the body is the model's LocalAI configuration, as an object")
        if body.setdefault("name", model_id) != model_id:
            raise Refused(400, f"the body names the model {body['name']!r} and the URL "
                               f"{model_id!r}; they are one model")
        missing = unpinned(body)
        if missing:
            raise Refused(422, f"{model_id}: pin {', '.join(missing)} in the YAML. LocalAI "
                               f"fills an unset one in by itself, from settings and readings "
                               f"that change without anyone editing this model, and every "
                               f"change is a new run (rage-over-localai §10)")
        await _not_the_operators(model_id)
        path = models_dir / f"{model_id}.yaml"
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(MARKER + "\n" + json.dumps(body, indent=2, sort_keys=True,
                                                  ensure_ascii=False) + "\n",
                       encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        # A loaded model keeps the options it was loaded with until it is
        # loaded again: stopped now, its next load is the new run, named.
        if model_id in observer.instances:
            await client.shutdown(model_id)
        await client.reload_configs()
        await observer.refresh()
        return {"id": model_id, "state": "written", "file": path.name}

    @app.delete("/louise/models/{model_id}", dependencies=[Depends(admin)])
    async def delete(model_id: str) -> dict[str, Any]:
        _check_id(model_id)
        files = await _not_the_operators(model_id)
        if not files:
            raise Refused(404, f"{model_id!r} is not a model of this container")
        await client.shutdown(model_id)
        for p in files:
            p.unlink(missing_ok=True)
        await client.reload_configs()
        await observer.refresh()
        if model_id in await client.models():
            # LocalAI 58830f7: /models/reload adds and updates, never removes
            # (localai-bugs.md B8; seen on the real one, 2026-10-03)
            raise Refused(502, f"{model_id}: its file is removed, but LocalAI still lists it "
                               f"after /models/reload; it goes at LocalAI's next start "
                               f"(localai-bugs.md B8)")
        return {"id": model_id, "state": "removed"}

    # ---- the models ----------------------------------------------------------------

    def _declares_pairs(model_id: str) -> tuple[bool | None, str]:
        """Whether the model's YAML — what LocalAI sends as the load call's
        `Options` — declares `pairs:true`; None and why, when it cannot be read."""
        if not _ID.fullmatch(model_id):
            return None, f"{model_id!r} is not a model id"
        files = _files(model_id)
        if not files:
            return None, "no model YAML of this container defines it"
        try:
            config = yaml_config(files[0])
        except (OSError, ValueError) as e:
            return None, f"its YAML {files[0].name} cannot be read as JSON ({e})"
        return pairs.declares_pairs(config.get("options")), ""

    def _body(model_id: str, path: str, raw: bytes, content_type: str) -> tuple[bytes, bool]:
        """The body, naming the URL's model whatever it named, with a pair
        request made one LocalAI can carry (pairs.py); and whether it asks for
        pairs. Passed on as it came when nothing changes, so nothing is
        re-serialised for nothing."""
        if not raw.strip() or "json" not in content_type.lower():
            return raw, False
        try:
            body = json.loads(raw)
        except ValueError:
            return raw, False
        if not isinstance(body, dict):
            return raw, False
        paired = False
        if path.rstrip("/").endswith("embeddings"):
            if pairs.FIELD in body:
                declared, why = _declares_pairs(model_id)
                paired = pairs.translate(body, model_id, declared, why)
            else:
                pairs.translate(body, model_id, False)      # the marker check alone
        elif pairs.FIELD in body:
            raise Refused(400, f"{pairs.FIELD} is a field of an embeddings request")
        if not paired and body.get("model") == model_id:
            return raw, False
        body["model"] = model_id
        return json.dumps(body, ensure_ascii=False).encode("utf-8"), paired

    def _pairs_run(run_id: str) -> bool:
        run = observer.store.get(run_id) or {}
        options = (run.get("engine") or {}).get("options") or {}
        return pairs.declares_pairs(options.get("Options"))

    @app.api_route("/upstream/{model_id}/{path:path}",
                   methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def upstream(model_id: str, path: str, request: Request) -> Response:
        try:
            raw = await request.body()
        except ClientDisconnect:
            return Response(status_code=499)    # gone before it finished asking
        content, paired = _body(model_id, path, raw, request.headers.get("content-type", ""))
        before = observer.current(model_id)
        waiting = is_interactive(request.headers.get(LEVEL_HEADER))
        if waiting:
            clock.enter()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
        try:
            answer = await client.send(request.method, path, query=request.url.query,
                                       headers=headers, content=content)
        except BaseException:
            if waiting:
                clock.leave()
            raise
        headers_at = time.time()

        finished = False

        async def finish() -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            await answer.aclose()
            if waiting:
                clock.leave()

        try:
            run_id = await observer.answered_by(model_id, before, headers_at)
        except BaseException:
            await finish()
            raise
        if paired and run_id is not None and not _pairs_run(run_id):
            # the YAML said pairs and the process answering was loaded without
            # them (edited by hand, not reloaded): its vector is of a marked
            # string read as text, and is never passed on
            await finish()
            raise Refused(502, f"{model_id}: the process that answered was loaded without "
                               f"the option {pairs.OPTION} (run {run_id}); restart the model "
                               f"so its YAML is loaded")
        read = answer.is_stream_consumed
        out = {k: v for k, v in answer.headers.items()
               if k.lower() not in _HOP and not (read and k.lower() == "content-encoding")}
        if run_id is not None:
            out[RUN_HEADER] = run_id

        async def body():
            try:
                if read:
                    yield answer.content
                else:
                    async for chunk in answer.aiter_raw():
                        yield chunk
            finally:
                await finish()

        return StreamingResponse(body(), status_code=answer.status_code, headers=out,
                                 background=BackgroundTask(finish))

    return app

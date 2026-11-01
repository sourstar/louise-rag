"""VeGa's receipts service (docs/notes/vega.md §2): it takes receipts and run
descriptions from the wrappers, and Louise reads them.

    intake   a unix socket on a dataset both containers mount (LocalAI's,
             where the wrappers run, and louise-vega's, where this runs).
             One JSON object a line each way, canonical JSON (09 §9.1):
               → {"kind": "run", "run_id": …, "run": {driver, engine, weights}}
               → {"kind": "receipt", "id", "run_id", "input_digest", "vector_digest"}
               → {"kind": "tree", "image": …, "tree": {<path>: <entry>, …}}
               ← {"ok": true}  or  {"ok": false, "code": …, "error": …}
             A receipt is acknowledged only once it is stored: the wrapper
             returns its vector only after that answer.
    HTTP     read-only:
               GET /vega/receipts?ids=a,b,…   the receipts found; a missing
                                              one is simply absent
               GET /vega/runs/<run_id>        09 §9.12's run object
               GET /vega/trees/<image>        the per-file list that engine
                                              image is the digest of

A RUN IS ACCEPTED ONLY UNDER ITS OWN NAME: `run_id` must be `H` of the
description's canonical JSON (app/vega/runs.py, the one definition), so a run
read here is the run a receipt names. Runs are kept across restarts
(app/vega/store.py's table, VeGa's own file): a calibration statement may be
written long after the answer that named its run (09 §9.12). A receipt naming
a run this service has never been told is refused with `unknown-run`; the
wrapper tells it the run and sends the receipt again.

A TREE IS KEPT UNDER ITS OWN DIGEST, THE SAME WAY: `image` must be `H` of the
list's canonical JSON — the wrapper's `tree_manifest`, what a backend
engine's `image` hashes (09 §9.3) — so the list read back is the one the run
names, and two runs whose `image` differs can be diffed file by file
(vega.md §12: a tree digest once moved with no file changed, and the list it
was computed from no longer existed). One row per distinct tree, megabytes
each, told once per tree by a wrapper; `MAX_LINE` is sized for it.

RECEIPTS ARE KEPT A BOUNDED TIME IN A BOUNDED STORE (`RECEIPT_TTL_S`,
`MAX_RECEIPTS`), in memory: a receipt is read once, seconds after its answer.
One that is gone when Louise asks is a deferral on her side — she embeds
again — never an error on a document. An id is ONE-TIME: the first receipt
for it stands; the same receipt again is acknowledged (a wrapper resending
after a lost answer), a different one for the same id is refused.

Nothing here is a secret (invariant 8): receipts are digests and random ids,
runs are what a Codex publishes. The log names runs and refusals, not inputs.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from fastapi import APIRouter, FastAPI, HTTPException, Query

from ..hold.codex import CodexError, canonical_json, digest
from . import runs as runs_mod
from .store import RunStore
from .tag import DIGEST

#: How long a receipt is kept. Louise reads a batch's receipts right after
#: LocalAI answers it — milliseconds to seconds; ten minutes covers a worker
#: that stalls between the two requests, and is short enough that an unread
#: receipt does not linger.
RECEIPT_TTL_S = 600.0
#: How many receipts at most, oldest dropped first. One is about 650 bytes in
#: memory, so this is about 65 MB at worst. The node embeds tens of inputs a
#: second (bge-m3 on the card, 10,700 tokens/s, model-pool-over-localai §12), about
#: 21,000 in ten minutes: five times that headroom.
MAX_RECEIPTS = 100_000
#: Longest intake line: a run description is about 2 KB, a receipt 300 bytes,
#: a tree's per-file list megabytes (DAR-legal-it's backend: 44,710 entries,
#: 8.7 MB on 2026-10-12). The socket is the dataset's two containers' alone.
MAX_LINE = 64 << 20
#: What a tree's entry may be (deploy/vega/vega_wrapper.py engine_manifest):
#: a `setting` is the one of vega.json that changes what is embedded (`cuts`)
TREE_TYPES = ("file", "dir", "link", "setting")
#: Most ids one read may ask for: a batch is at most a few hundred inputs.
MAX_IDS = 1024
#: The socket's mode. The access control is the dataset: only LocalAI's
#: container and louise-vega's mount it. Open within it, so the wrappers
#: connect whatever user LocalAI's container runs them as.
SOCKET_MODE = 0o666

_ID = re.compile(r"[0-9a-f]{32}")
RECEIPT_KEYS = ("id", "run_id", "input_digest", "vector_digest")
RUN_KEYS = ("driver", "engine", "weights")
UNKNOWN_RUN, CONFLICT, INVALID = "unknown-run", "conflict", "invalid"


class Refused(ValueError):
    """The intake will not take this message. `code` is what the wrapper
    acts on; the message says which rule."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---- the receipts ----------------------------------------------------------------

class ReceiptStore:
    """Receipts by id, each for `ttl_s`, at most `max_receipts`. Every
    receipt lives the same time, so insertion order is expiry order: expiry
    and the bound both drop from the front, O(1) each."""

    def __init__(self, ttl_s: float = RECEIPT_TTL_S, max_receipts: int = MAX_RECEIPTS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s, self.max_receipts, self.clock = ttl_s, max_receipts, clock
        self._items: OrderedDict[str, tuple[float, dict[str, str]]] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            self._expire(self.clock())
            return len(self._items)

    def _expire(self, now: float) -> None:
        while self._items:
            expires, _ = next(iter(self._items.values()))
            if expires > now:
                return
            self._items.popitem(last=False)

    def put(self, receipt: Mapping[str, str]) -> bool:
        """True for a new receipt, False for the same one again; Refused
        (`conflict`) for another receipt under an id already taken."""
        r = {k: receipt[k] for k in RECEIPT_KEYS}
        with self._lock:
            now = self.clock()
            self._expire(now)
            held = self._items.get(r["id"])
            if held is not None:
                if held[1] == r:
                    return False
                raise Refused(CONFLICT, "another receipt holds this id: an id is one-time")
            while len(self._items) >= self.max_receipts:
                self._items.popitem(last=False)
            self._items[r["id"]] = (now + self.ttl_s, r)
            return True

    def get(self, ids: Iterable[str]) -> list[dict[str, str]]:
        with self._lock:
            self._expire(self.clock())
            found = (self._items.get(i) for i in ids)
            return [dict(item[1]) for item in found if item is not None]


# ---- the runs --------------------------------------------------------------------

class Runs:
    """The run descriptions, kept across restarts in VeGa's own file."""

    def __init__(self, store: RunStore) -> None:
        self.store = store
        self._known: set[str] = set()
        self._lock = threading.Lock()

    def put(self, run_id: Any, run: Any) -> None:
        if not isinstance(run, dict) or set(run) != set(RUN_KEYS):
            raise Refused(INVALID, f"a run is an object of exactly {', '.join(RUN_KEYS)}")
        if not isinstance(run["engine"], dict) or not isinstance(run["driver"], str):
            raise Refused(INVALID, "a run's engine is an object and its driver a string")
        if not isinstance(run["weights"], str) or not DIGEST.fullmatch(run["weights"]):
            raise Refused(INVALID, "a run's weights are a digest, sha256:<64 hex>")
        try:
            computed = runs_mod.run_id(run)
        except (CodexError, UnicodeEncodeError, TypeError) as e:
            raise Refused(INVALID, f"the run has no canonical form: {e}") from None
        if run_id != computed:
            raise Refused(INVALID, "run_id is not H of the run's canonical JSON (09 §9.12)")
        self.store.put(computed, run)
        with self._lock:
            self._known.add(computed)

    def known(self, run_id: str) -> bool:
        with self._lock:
            if run_id in self._known:
                return True
        if self.store.get(run_id) is None:
            return False
        with self._lock:
            self._known.add(run_id)
        return True

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self.store.get(run_id)

    def put_tree(self, image: Any, tree: Any) -> None:
        """The per-file list `image` is the digest of, taken only under its own
        digest: `image` must be `H` of the list's canonical JSON, the one
        definition the wrapper's `tree_digest` uses (09 §9.3)."""
        if not isinstance(image, str) or not DIGEST.fullmatch(image):
            raise Refused(INVALID, "a tree's image is a digest, sha256:<64 hex>")
        if not isinstance(tree, dict) or not tree:
            raise Refused(INVALID, "a tree is a non-empty object of path: entry")
        for path, entry in tree.items():
            if not isinstance(entry, dict) or entry.get("type") not in TREE_TYPES:
                raise Refused(INVALID, f"a tree entry is an object whose type is one of "
                                       f"{', '.join(TREE_TYPES)} ({path!r} is not)")
        try:
            computed = digest(canonical_json(tree))
        except (CodexError, UnicodeEncodeError, TypeError) as e:
            raise Refused(INVALID, f"the tree has no canonical form: {e}") from None
        if image != computed:
            raise Refused(INVALID, "image is not H of the tree's canonical JSON: this is not "
                                   "the list that digest names (09 §9.3)")
        self.store.put_tree(computed, canonical_json(tree))

    def get_tree(self, image: str) -> dict[str, Any] | None:
        raw = self.store.get_tree(image)
        return json.loads(raw) if raw is not None else None


# ---- the intake -----------------------------------------------------------------

def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise Refused(INVALID, f"the message repeats the key {k!r}")
        out[k] = v
    return out


def _no_float(text: str) -> Any:
    raise Refused(INVALID, "a number that is not an integer (09 §9.1)")


def _receipt(msg: dict[str, Any]) -> dict[str, str]:
    if set(msg) != {"kind", *RECEIPT_KEYS}:
        raise Refused(INVALID, f"a receipt is exactly kind, {', '.join(RECEIPT_KEYS)}")
    if not isinstance(msg["id"], str) or not _ID.fullmatch(msg["id"]):
        raise Refused(INVALID, "a receipt's id is 32 lowercase hexadecimal digits")
    for k in ("run_id", "input_digest", "vector_digest"):
        if not isinstance(msg[k], str) or not DIGEST.fullmatch(msg[k]):
            raise Refused(INVALID, f"a receipt's {k} is a digest, sha256:<64 hex>")
    return {k: msg[k] for k in RECEIPT_KEYS}


def handle(line: bytes, receipts: ReceiptStore, runs: Runs) -> dict[str, Any]:
    """One intake message, one answer. The answer to a receipt is `ok` only
    once it is stored."""
    try:
        try:
            msg = json.loads(line, object_pairs_hook=_no_duplicates, parse_float=_no_float,
                             parse_constant=_no_float)
        except UnicodeDecodeError:
            raise Refused(INVALID, "the message is not UTF-8") from None
        except Refused:
            raise
        except ValueError as e:
            raise Refused(INVALID, f"the message is not JSON: {e}") from None
        if not isinstance(msg, dict):
            raise Refused(INVALID, "a message is a JSON object")
        kind = msg.get("kind")
        if kind == "run":
            if set(msg) != {"kind", "run_id", "run"}:
                raise Refused(INVALID, "a run message is exactly kind, run_id, run")
            runs.put(msg["run_id"], msg["run"])
        elif kind == "receipt":
            r = _receipt(msg)
            if not runs.known(r["run_id"]):
                raise Refused(UNKNOWN_RUN, "the receipt names a run this service was never "
                                           "told: send the run first")
            receipts.put(r)
        elif kind == "tree":
            if set(msg) != {"kind", "image", "tree"}:
                raise Refused(INVALID, "a tree message is exactly kind, image, tree")
            runs.put_tree(msg["image"], msg["tree"])
        else:
            raise Refused(INVALID, "a message's kind is run, receipt or tree")
    except Refused as e:
        return {"ok": False, "code": e.code, "error": str(e)}
    return {"ok": True}


def answer_line(reply: Mapping[str, Any]) -> bytes:
    return canonical_json(dict(reply)) + b"\n"


class Intake:
    """The unix socket the wrappers write to. A stale socket file from a
    previous run is replaced; any other file at the path is refused."""

    def __init__(self, path: Path, receipts: ReceiptStore, runs: Runs, *,
                 log: Callable[[str], None] = print) -> None:
        self.path, self.receipts, self.runs, self.log = Path(path), receipts, runs, log
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            mode = self.path.lstat().st_mode
        except FileNotFoundError:
            mode = None
        if mode is not None:
            if not stat.S_ISSOCK(mode):
                raise RuntimeError(f"{self.path} exists and is not a socket; VeGa will not "
                                   f"replace it")
            self.path.unlink()
        self._server = await asyncio.start_unix_server(self._client, path=str(self.path),
                                                       limit=MAX_LINE)
        os.chmod(self.path, SOCKET_MODE)
        self.log(f"[vega] intake on {self.path}")

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            if stat.S_ISSOCK(self.path.lstat().st_mode):
                self.path.unlink()
        except OSError:
            pass

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    writer.write(answer_line({"ok": False, "code": INVALID,
                                              "error": f"a line is at most {MAX_LINE} bytes"}))
                    await writer.drain()
                    return
                if not line:
                    return
                reply = handle(line, self.receipts, self.runs)
                if not reply["ok"]:
                    self.log(f"[vega] intake refused a message ({reply['code']}): "
                             f"{reply['error']}")
                writer.write(answer_line(reply))
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            return
        finally:
            writer.close()


# ---- the read-only HTTP API ------------------------------------------------------

def router(receipts: ReceiptStore, runs: Runs) -> APIRouter:
    api = APIRouter()

    @api.get("/vega/receipts")
    def read_receipts(ids: str = Query("")) -> dict[str, Any]:
        wanted = [i for i in ids.split(",") if i]
        if not wanted or len(wanted) > MAX_IDS:
            raise HTTPException(400, f"ids is a comma-separated list of 1 to {MAX_IDS} ids")
        bad = sum(1 for i in wanted if not _ID.fullmatch(i))
        if bad:
            raise HTTPException(400, f"{bad} of the ids are not 32 lowercase hexadecimal "
                                     f"digits")
        return {"receipts": receipts.get(wanted)}

    @api.get("/vega/runs/{run_id}")
    def read_run(run_id: str) -> dict[str, Any]:
        run = runs.get(run_id) if DIGEST.fullmatch(run_id) else None
        if run is None:
            raise HTTPException(404, "VeGa was never told this run")
        return run

    @api.get("/vega/trees/{image}")
    def read_tree(image: str) -> dict[str, Any]:
        tree = runs.get_tree(image) if DIGEST.fullmatch(image) else None
        if tree is None:
            raise HTTPException(404, "VeGa was never told this tree's file list")
        return tree

    return api


def create_app(receipts: ReceiptStore, runs: Runs, intake: Intake | None) -> FastAPI:
    """The service on its own (`python -m app.vega`); louise-vega mounts
    `router` and starts an `Intake` in its own app instead."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if intake is not None:
            await intake.start()
        try:
            yield
        finally:
            if intake is not None:
                await intake.close()

    app = FastAPI(title="VeGa", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.include_router(router(receipts, runs))

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "receipts": len(receipts)}

    return app

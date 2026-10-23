"""A model is pinned in LocalAI while Louise processes with it, then restored
(operator, 2026-10-23: "model eviction during processing shall be
forbidden … reading the pinning status, in case pinning a model before
processing, then restoring the previous pinning status after processing.
It safeguards processing and it safeguards other models needing the GPU").

LocalAI's reclaimer never evicts a model inside a call, but it evicts one
BETWEEN the calls of a batch: in 72 h it evicted vega-qwen3-embedding-4b 129
times, and the worker's backend gate then paused embedding on the reload's
timeout (München, 2026-10-23; the defects behind it are
docs/notes/localai-bugs.md B3, B4, B7, B11). A pinned model is never evicted.
So:

- `hold(url, model, holder)` before processing: the first holder of a model
  READS its pinned state (`GET /api/models/config-json/<model>`); if it is
  unpinned, it PINS it (`PUT /models/toggle-pinned/<model>/pin`, which reaches
  the watchdog at once) and records `prior=false`. Later holders only join.
  Called again by the same holder, it renews.
- `release(holder)` after: when a model's last holder leaves, the state read
  before is restored — unpinned only if it was unpinned before; a model
  that was already pinned is never touched.
- The record is `<LOUISE_ROOT>/state/model-pins.json` under a lock, shared by
  every process of the node (the worker, a command beside it), so a holder
  never unpins a pin it did not make and two processes share one model.
- A holder renews while it works; one silent for HOLDER_TTL_S is dead (a
  killed command), and `reconcile` restores what it held. The worker's own
  holders from a previous boot are dropped at once (`reconcile(boot=True)`),
  and what that found and fixed is reported.

A FAILED CALL IS A DEFECT SIGNAL, NOT A DEFERRAL (operator, 2026-10-23:
"Failed pin calls: monitor such event, it shouldn't happen unless there is
some bug"). Every failed read, pin, unpin or restore is an error event
naming the model, the call, the status or exception and the holder; it is
counted in the record (`failures`), which the System page, `hold collection
show` and the worker's telemetry read. Processing still goes ahead unpinned
— every call has a timeout, never a hang (CLAUDE.md: no unbounded wait on an
optional subsystem). A failed restore leaves a model pinned that should not
be: it stays in the record without holders, is retried at every
`reconcile`, and is shown until it succeeds.

Queries are not pinned: a slowdown is acceptable for them.
"""
from __future__ import annotations

import atexit
import fcntl
import json
import os
import queue
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

#: one LocalAI call, read or write
CALL_TIMEOUT_S = 5.0
#: a holder silent this long is dead; longer than any batch, shorter than an hour
HOLDER_TTL_S = 900.0
#: a holder renewed more recently than this is not written again: an embed
#: call renews, and a slice is seconds
RENEW_S = 30.0
#: after a failed read or pin, this process asks again no sooner: one error
#: a minute, not one per embed call
RETRY_S = 60.0
#: Louise's embedders on the model runner are VeGa's, `vega-<model>`, on
#: LocalAI (worker.EMBEDDER_PREFIX): only they have LocalAI's pin API. An
#: encoder elsewhere (llama-server, an OpenAI-style service) is not pinned,
#: and not reported: nothing failed.
EMBEDDER_PREFIX = "vega-"


def _applies(model: str) -> bool:
    return model.startswith(EMBEDDER_PREFIX)


_retry_after: dict[str, float] = {}
#: the worker's holders are named so, and a new worker drops a previous one's
WORKER = "worker "
#: failures kept in the record for the pages; the count is kept whole
RECENT_FAILURES = 20
#: a restore that keeps failing is reported at its 1st attempt and every Nth
RESTORE_REPORT_EVERY = 15


def _publish(level: str, text: str) -> None:
    """The node's event log, as model_load's cold starts: stdout, and the
    first collection's `events` table under stage `models` (db.NODE_STAGES)."""
    from contextlib import closing
    from . import collections as collections_mod, db
    collections = collections_mod.load_all()
    if not collections:
        print(f"{time.strftime('%H:%M:%S')} [node {level}:models] {text}", flush=True)
        return
    with closing(db.connect(collections[0])) as con:
        db.log_event(con, level, "models", text)


_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
_writer: threading.Thread | None = None
_writer_lock = threading.Lock()


def _drain() -> None:
    """Writes until the queue is empty, then ends: a worker's stop leaves no
    thread of this module behind."""
    global _writer
    while True:
        try:
            level, text = _queue.get(timeout=0.5)
        except queue.Empty:
            with _writer_lock:
                if _queue.empty():
                    _writer = None
                    return
            continue
        try:
            _publish(level, text)
        except Exception:   # noqa: BLE001 — a log line never takes a thread down
            print(f"{time.strftime('%H:%M:%S')} [node {level}:models] {text}", flush=True)


def _queued(level: str, text: str) -> None:
    """Off the caller's thread: an embed batch never waits on a manifest's
    busy timeout to say what happened to its pin."""
    global _writer
    with _writer_lock:
        _queue.put((level, text))
        if _writer is None:
            _writer = threading.Thread(target=_drain, name="model-pins-log", daemon=True)
            _writer.start()


_say: Callable[[str, str], None] = _queued


def set_log(fn: Callable[[str, str], None]) -> None:
    """`fn(level, text)`; tests capture it."""
    global _say
    _say = fn


def _path() -> Path:
    from app import config
    return config.louise_root() / "state" / "model-pins.json"


def _key(server: str, model: str) -> str:
    return f"{server}|{model}"


@contextmanager
def _state() -> Iterator[dict]:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_name(".model-pins.lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                st = json.loads(p.read_bytes())
            except (OSError, ValueError):
                st = {}
            st.setdefault("pins", {})
            st.setdefault("failures", {"count": 0, "recent": []})
            before = json.dumps(st, sort_keys=True)
            yield st
            if json.dumps(st, sort_keys=True) != before:
                tmp = p.with_name(".model-pins.json.tmp")
                tmp.write_text(json.dumps(st, sort_keys=True), encoding="utf-8")
                os.replace(tmp, p)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _fail(st: dict, call: str, server: str, model: str, holder: str, detail: str,
          then: str, *, collection: str | None = None, report: bool = True,
          now: float | None = None) -> None:
    """One failed call: counted, kept among the recent, and an error event."""
    f = st["failures"]
    f["count"] = int(f.get("count", 0)) + 1
    f["recent"] = (f.get("recent") or [])[-(RECENT_FAILURES - 1):] + [{
        "at": time.time() if now is None else now, "call": call, "server": server,
        "model": model, "holder": holder, "collection": collection, "detail": detail}]
    if report:
        _say("error", f"model pin: {call} of {model} at {server} failed ({detail}) "
                      f"for {holder}; {then}")


# ---- LocalAI ---------------------------------------------------------------------

def _read(server: str, model: str) -> tuple[bool | None, str]:
    """(the model's pinned state, "") or (None, why it could not be read)."""
    from app import httpc
    try:
        r = httpc.get(f"{server}/api/models/config-json/{model}", timeout=CALL_TIMEOUT_S)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        return bool((r.json() or {}).get("pinned", False)), ""
    except Exception as e:   # noqa: BLE001 — reported by the caller
        return None, f"{type(e).__name__}: {e}"[:300]


def _set(server: str, model: str, on: bool) -> str:
    """"" when done, else why not."""
    from app import httpc
    try:
        r = httpc.put(f"{server}/models/toggle-pinned/{model}/{'pin' if on else 'unpin'}",
                      timeout=CALL_TIMEOUT_S)
        return "" if r.status_code == 200 else f"HTTP {r.status_code}"
    except Exception as e:   # noqa: BLE001 — reported by the caller
        return f"{type(e).__name__}: {e}"[:300]


def read_pinned(server: str, model: str) -> bool | None:
    return _read(server, model)[0]


# ---- holding --------------------------------------------------------------------

def hold(url: str | None, model: str | None, holder: str, *,
         collection: str | None = None, now: float | None = None) -> bool:
    """`holder` processes with `model` on `url`'s runner from now on: pinned
    if it was not (and the prior state recorded), or joined, or renewed.
    True when the model is pinned for it; False when it runs unpinned.
    `collection`, when the processing is one collection's, names it in a
    failure, for `hold collection show`."""
    if not url or not model or not _applies(model):
        return False
    from app import model_load
    server = model_load.server_of(url)
    now = time.time() if now is None else now
    k = _key(server, model)
    if now < _retry_after.get(k, 0.0):
        return False                 # failed under a minute ago, and said so
    try:
        with _state() as st:
            entry = st["pins"].get(k)
            if entry is not None:
                if now - entry["holders"].get(holder, 0.0) >= RENEW_S:
                    entry["holders"][holder] = now
                return True
            prior, why = _read(server, model)
            if prior is None:
                _retry_after[k] = now + RETRY_S
                _fail(st, "read", server, model, holder, why,
                      f"it processes with the model unpinned; asked again in "
                      f"{RETRY_S:.0f} s", collection=collection, now=now)
                return False
            if not prior:
                why = _set(server, model, True)
                if why:
                    _retry_after[k] = now + RETRY_S
                    _fail(st, "pin", server, model, holder, why,
                          f"it processes with the model unpinned; asked again in "
                          f"{RETRY_S:.0f} s", collection=collection, now=now)
                    return False
            st["pins"][k] = {"server": server, "model": model, "prior": prior,
                             "holders": {holder: now}, "collection": collection,
                             "restore_attempts": 0}
            if not prior:
                _say("info", f"model pin: {model} pinned for {holder} "
                             f"(it was unpinned; restored after)")
            return True
    except OSError as e:
        _say("error", f"model pin: the record {_path()} could not be written ({e}); "
                      f"{holder} processes with {model} unpinned")
        return False


def _restore(st: dict, entry: dict, holder: str, now: float) -> bool:
    """The state read before, back. False when the unpin failed: the entry
    stays, holderless, and the next reconcile retries."""
    if entry.get("prior") is not False:
        return True
    why = _set(entry["server"], entry["model"], False)
    if not why:
        _say("info", f"model pin: {entry['model']} unpinned again, its state before"
                     + (f" (after {entry['restore_attempts']} failed attempt(s))"
                        if entry.get("restore_attempts") else ""))
        return True
    n = entry["restore_attempts"] = int(entry.get("restore_attempts", 0)) + 1
    _fail(st, "restore", entry["server"], entry["model"], holder, why,
          f"the model stays pinned though it was not before; retried at every "
          f"reconcile (attempt {n})",
          collection=entry.get("collection"),
          report=(n == 1 or n % RESTORE_REPORT_EVERY == 0), now=now)
    return False


def _drop(st: dict, gone: Callable[[str, float], bool], now: float) -> list[tuple[str, str]]:
    """Drops the holders `gone` names; restores every entry left without one.
    Returns (model, holder) per holder dropped."""
    dropped: list[tuple[str, str]] = []
    for k in list(st["pins"]):
        entry = st["pins"][k]
        last = ""
        for h, t in list(entry["holders"].items()):
            if gone(h, t):
                del entry["holders"][h]
                dropped.append((entry["model"], h))
                last = h
        if not entry["holders"] and _restore(st, entry, last or "reconcile", now):
            del st["pins"][k]
    return dropped


def release(holder: str) -> None:
    """`holder` is done with every model it held; a model whose last holder
    this was gets its prior state back."""
    try:
        with _state() as st:
            _drop(st, lambda h, t: h == holder, time.time())
    except OSError as e:
        _say("error", f"model pin: the record {_path()} could not be written ({e}); "
                      f"{holder}'s pins are restored when it goes stale")


def reconcile(*, now: float | None = None, boot: bool = False,
              live: set[str] | frozenset[str] = frozenset(),
              own_idle_s: float | None = None) -> list[tuple[str, str]]:
    """Restore what dead holders left pinned: any holder silent for
    HOLDER_TTL_S, and, at a worker's boot, every holder of the worker before
    it (it was killed with them); and retry every failed restore. `live`
    are the caller's own holders that are alive whatever their age (the
    worker's embed stages: one document can embed for hours); this
    process's own `hold_for_process` holders go once idle for `own_idle_s`.
    Returns the (model, holder) pairs dropped; at boot, reports what it found."""
    now = time.time() if now is None else now
    mine = f" pid {os.getpid()}"

    def gone(h: str, t: float) -> bool:
        if h in live:
            return False
        if own_idle_s is not None and h.endswith(mine) and now - t > own_idle_s:
            return True
        return now - t > HOLDER_TTL_S or (boot and h.startswith(WORKER))
    try:
        with _state() as st:
            dropped = _drop(st, gone, now)
            stuck = sorted(e["model"] for e in st["pins"].values() if not e["holders"])
    except OSError as e:
        _say("error", f"model pin: the record {_path()} could not be read ({e}); "
                      f"nothing reconciled")
        return []
    if boot:
        if dropped or stuck:
            _say("warn", "model pin at boot: "
                 + (f"{len(dropped)} holder(s) left by a process that ended "
                    f"({', '.join(sorted({f'{m} ({h})' for m, h in dropped}))}), dropped"
                    if dropped else "no holder left behind")
                 + (f"; still pinned, restore failing: {', '.join(stuck)}" if stuck
                    else "; every model they held is back to its state before"))
        else:
            _say("info", "model pin at boot: nothing left pinned by a previous process")
    elif dropped:
        _say("warn", f"model pin: {len(dropped)} holder(s) silent for over "
                     f"{HOLDER_TTL_S:.0f} s dropped "
                     f"({', '.join(sorted({f'{m} ({h})' for m, h in dropped}))})")
    return dropped


def status() -> dict:
    """What the System page and the telemetry show: what is pinned now and
    for whom, what is left pinned with its restore failing, and the failures."""
    try:
        with _state() as st:
            snap = json.loads(json.dumps(st))
    except OSError:
        return {"pins": [], "stuck": [], "failures": {"count": 0, "recent": []}}
    pins = list(snap["pins"].values())
    return {"pins": [p for p in pins if p["holders"]],
            "stuck": [p for p in pins if not p["holders"]],
            "failures": snap["failures"]}


def counters() -> dict:
    s = status()
    return {"pinned": len(s["pins"]), "stuck": len(s["stuck"]),
            "failures": int(s["failures"].get("count", 0))}


def failures_for(collection: str) -> list[dict]:
    """The recent failures of processing for `collection`."""
    return [f for f in status()["failures"].get("recent", [])
            if f.get("collection") == collection]


_process_holders: set[str] = set()
#: "worker " in the worker (`set_worker`), so its process holders are a
#: worker's and a new worker drops a killed one's at boot
_process_prefix = ""


def set_worker() -> None:
    global _process_prefix
    _process_prefix = WORKER


def process_holder(kind: str) -> str:
    return f"{_process_prefix}{kind} pid {os.getpid()}"


def hold_for_process(url: str | None, model: str | None, kind: str, *,
                     collection: str | None = None) -> bool:
    """For processing with no scope of its own (every document-side embed
    call reaches here through embeddings): `hold` as `<kind> pid <pid>`,
    renewed by each call, released when the process exits, or once idle
    (`reconcile`: own_idle_s in the worker, HOLDER_TTL_S for the rest)."""
    holder = process_holder(kind)
    if holder not in _process_holders:
        _process_holders.add(holder)
        atexit.register(release, holder)
    return hold(url, model, holder, collection=collection)


@contextmanager
def holding(url: str | None, model: str | None, holder: str, *,
            collection: str | None = None):
    """`with holding(url, model, holder):` around one processing that ends
    with the block (a calibration gate, a vectors-sample)."""
    hold(url, model, holder, collection=collection)
    try:
        yield
    finally:
        release(holder)

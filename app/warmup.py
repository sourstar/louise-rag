"""The question path's models, loaded before the first question needs them.

Operator, 2026-10-27 (option A of four, in München's session): "is there a
way to warm up the models at startup or even between startups?" The first
question after the day's restarts got no answer: the translator's cold load
took 23 s, the reranker loaded twice, and the browser had gone when the list
was ready ~65 s in.

So the web, once at its start and again whenever a model server it asks
comes back after being unreachable (a LocalAI restart), warms in the
background, one after the other: the translator, each distinct reranker the
held collections name, and the chat model. Each goes through the existing
warm of its caller (`translate.warm`, `retrieval._warm_reranker`,
`llm.warm`), so app/model_load.py's rules hold: one warm per model at a
time, bounded by the model's load bound, a model already listed as loaded
is not touched. Nothing here blocks a request; a question asked meanwhile
joins the warm in flight.

NOT a keep-alive (option C, not chosen): a model evicted later, under
embedding load, is loaded again by the question that needs it, as before.
NOT the query encoders: 4-8 s cold, and the one gutenberg embeds with is
usually loaded anyway.

A restart is seen as the server going unreachable and coming back
(`/system` answering None, then ids): LocalAI's `/system` carries no start
time. A restart faster than one `POLL_S` is missed and costs the first
question its load, as before. Standard alternative: a readiness hook in the
runner's orchestrator (a Kubernetes postStart / an init job) — the node runs
TrueNAS apps, which offer none.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

#: how often each model server is asked whether it is there (one cached GET)
POLL_S = 10.0

_started = False
_lock = threading.Lock()


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} [web info:warmup] {msg}", flush=True)


def steps() -> list[tuple[str, str, Callable[[], object]]]:
    """(name, server, warm) for every model the question path always or
    slowly needs, in the order a question meets them."""
    from app import collections as collections_mod, config, llm, model_load, retrieval, translate
    out: list[tuple[str, str, Callable[[], object]]] = []
    if config.TRANSLATE_MODEL and config.TRANSLATE_URL:
        out.append((config.TRANSLATE_MODEL, model_load.server_of(config.TRANSLATE_URL), translate.warm))
    seen: set[tuple[str, str]] = set()
    try:
        held = collections_mod.load_all()
    except Exception:   # noqa: BLE001 — no collections read: no rerankers to warm
        held = []
    for c in held:
        r = collections_mod.reranker_for(c)
        if r is None or not r.model or (r.url, r.model) in seen:
            continue
        seen.add((r.url, r.model))
        out.append((r.model, model_load.server_of(r.url), lambda r=r: retrieval._warm_reranker(r)))
    if config.CHAT_API == "openai" and config.CHAT_URL and config.CHAT_MODEL:
        out.append((config.CHAT_MODEL, model_load.server_of(config.CHAT_URL), llm.warm))
    return out


def warm_all(why: str, only: set[str] | None = None) -> None:
    """Warm each step, one at a time; `only` limits it to these servers.
    Every outcome is said; none is raised."""
    for name, server, warm in steps():
        if only is not None and server not in only:
            continue
        t0 = time.monotonic()
        try:
            warm()
            _log(f"{name}: ready after {time.monotonic() - t0:.1f} s ({why})")
        except Exception as e:   # noqa: BLE001 — a warm never fails anything
            _log(f"{name}: not warmed ({why}): {type(e).__name__}: {str(e)[:200]}")


def _back(up: dict[str, bool]) -> set[str]:
    """The model servers that answer now after not answering at the last
    look (`up`, updated in place): a restart, as far as one can be seen."""
    from app import model_load
    back = set()
    for s in {server for _, server, _ in steps()}:
        now = model_load.loaded_models(s, model_load.SYSTEM_TTL_S) is not None
        if now and up.get(s) is False:
            back.add(s)
        up[s] = now
    return back


def _watch() -> None:
    warm_all("web start")
    up: dict[str, bool] = {}
    while True:
        time.sleep(POLL_S)
        back = _back(up)
        if back:
            warm_all("model server back: " + ", ".join(sorted(back)), only=back)


def start() -> bool:
    """Start the watcher once per process; False when switched off
    (`LOUISE_WARMUP=0`, the test suite's) or already started."""
    global _started
    if os.getenv("LOUISE_WARMUP", "1") == "0":
        return False
    with _lock:
        if _started:
            return False
        _started = True
    threading.Thread(target=_watch, name="warmup", daemon=True).start()
    return True

"""Which recommended engine setting THIS node takes for a collection
(the format and its signing: app/hold/engine_settings.py, 09 §9.19).

`collections.recipe_for` calls `applied`: the keys of `batch` and
`concurrency` the collection file leaves unset take the entry recommended
for one of this node's card types — this node's own measurement first, else
the most recent. The cards come from the node's GPU probe (GET /gpu), once
per process; a probe that is unset or silent means no recommendation, never
a wait (the client's timeout bounds the one call, and a failure is retried
after CARDS_RETRY_S, not on every call). Reading is cached on the files'
names, times and sizes, so a call with no file costs one directory listing.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from .hold.engine_settings import DIR, Card, Recommendation, read_all, recommend

log = logging.getLogger(__name__)

#: a probe that did not answer is asked again after this
CARDS_RETRY_S = 300.0

_lock = threading.Lock()
_cards: tuple[Card, ...] | None = None
_cards_failed_at: float | None = None
_chosen: dict[tuple, tuple[Any, Recommendation | None]] = {}
_told: dict[str, tuple] = {}


def node_cards(*, fetch: Callable[[str], dict] | None = None,
               clock: Callable[[], float] = time.monotonic) -> tuple[Card, ...]:
    """This node's card types, from its GPU probe (GET /gpu), once per
    process. Bounded by the probe client's timeout (gpu.TIMEOUT_S); a probe
    that is unset or does not answer gives none — and no recommendation —
    and is asked again after CARDS_RETRY_S, never on every call."""
    global _cards, _cards_failed_at
    from . import config, gpu
    if _cards is not None:
        return _cards
    if not config.GPU_PROBE_URL:
        return ()
    with _lock:
        if _cards is not None:
            return _cards
        if _cards_failed_at is not None and clock() - _cards_failed_at < CARDS_RETRY_S:
            return ()
        try:
            data = (fetch or gpu._get_json)(config.GPU_PROBE_URL.rstrip("/") + "/gpu")
            devices = gpu.from_json(data).devices
            _cards = tuple(dict.fromkeys(Card(d.driver, d.vendor_id, d.device_id)
                                         for d in devices))
        except Exception as e:   # noqa: BLE001 — no card known: no recommendation
            _cards_failed_at = clock()
            log.info("engine settings: the GPU probe did not answer (%s); no "
                     "recommendation applies until it does", type(e).__name__)
            return ()
        return _cards


def _stamp(d: Path) -> tuple:
    try:
        return tuple(sorted((f.name, f.stat().st_mtime_ns, f.stat().st_size)
                            for f in d.glob("*.json")))
    except OSError:
        return ()


def for_collection(collection, *, cards: Iterable[Card] | None = None,
                   own: str | None = None, shard_dir: Path | None = None
                   ) -> Recommendation | None:
    """The recommendation this node takes for `collection`, or None: no
    Codex, no space, no known card, no file, or no entry for this card.
    Cached on the files' names, times and sizes — one directory listing a
    call."""
    codex = getattr(collection, "codex", None)
    if codex is None or getattr(codex, "space", None) is None:
        return None
    cards = tuple(node_cards() if cards is None else cards)
    if not cards:
        return None
    if shard_dir is None:
        from . import collections as collections_mod
        shard_dir = collections_mod.louise_root() / "collections" / collection.name / "shard"
    d = Path(shard_dir) / DIR
    stamp = _stamp(d)
    if not stamp:
        return None                     # the common case: no file, one listing
    if own is None:
        from . import run_check
        own = run_check.signer()
    key = (str(d), codex.collection_id, codex.space_id, cards, own)
    hit = _chosen.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    bodies, refused = read_all(shard_dir, collection_id=codex.collection_id,
                               space_id=codex.space_id)
    for why in refused:
        log.warning("%s: engine settings set aside: %s", collection.name, why)
    rec = recommend(bodies, cards, own=own)
    _chosen[key] = (stamp, rec)
    return rec


def applied(collection, recipe):
    """`recipe` with this node's recommendation in the keys its collection
    file leaves unset (`EmbedRecipe.stated`); `recipe` itself when nothing
    applies. `collections.recipe_for` calls it, so the gate measures what
    the worker sends (`run_check.contributing`)."""
    stated = getattr(recipe, "stated", frozenset())
    if {"batch", "concurrency"} <= stated:
        return recipe
    try:
        rec = for_collection(collection)
    except Exception as e:   # noqa: BLE001 — a recommendation is never a reason not to run
        log.warning("%s: engine settings not read (%s: %s); the file's settings apply",
                    getattr(collection, "name", "?"), type(e).__name__, e)
        return recipe
    if rec is None:
        return recipe
    change: dict[str, Any] = {}
    if rec.batch is not None and "batch" not in stated:
        change["batch"] = rec.batch
    if rec.concurrency is not None and "concurrency" not in stated:
        change["concurrency"] = rec.concurrency
    if not change:
        return recipe
    if recipe.device is None:
        # `spends` is inferred from the FILE's concurrency (gpu when serial):
        # a recommended concurrency must not turn a GPU encoder into a CPU one
        change["device"] = recipe.spends
    out = dataclasses.replace(recipe, **change)
    said = (rec.signer, tuple(sorted(change.items())))
    if _told.get(collection.name) != said:
        _told[collection.name] = said
        log.info("%s: engine settings recommended by %s… for %s:%s applied: %s "
                 "(measured %s); the collection file's own keys are kept",
                 collection.name, rec.signer[:20], rec.card.vendor_id, rec.card.device_id,
                 ", ".join(f"{k} {v}" for k, v in sorted(change.items()) if k != "device"),
                 rec.measured)
    return out


def reset() -> None:
    """Forget the cards and the choices (tests; a probe moved)."""
    global _cards, _cards_failed_at
    with _lock:
        _cards = None
        _cards_failed_at = None
        _chosen.clear()
        _told.clear()

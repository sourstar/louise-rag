"""Which model on this node's model server runs a collection's encoder —
decided by the node, never asked (CLAUDE.md RULE 2, amended 2026-10-25), and
decided by WEIGHTS only.

A collection's Codex names its encoder by the digest of its weights
(`space.encoder.weights`, and `space.query.encoder.weights` for an asymmetric
space; 02 §2.4, 09 §9.3). A model server names its models by whatever its
operator typed (`vega-medcpt-article`). The one thing that ties the two is
the RUN a model states when it answers: VeGa's receipt (an `api = "vega"`
encoder) or the `Louise-Run` header names a run id, and the run's
description — `{driver, engine, weights}`, verified to hash to that id
(app/hold/runs.py `describe`) — says which weights were loaded. This module
asks that, one tiny embedding per candidate model, through the node's one
embedding client (`run_check` / `embeddings`), and a model COUNTS ONLY IF
the weights its run states are the Codex's, byte for byte. A name, a label
or a list position never decides: they only order which models are asked
first, so the probes stop early. A model whose run states nothing, or that
does not answer, does not count — the collection is then held without an
encoder and the caller says in plain words what is missing.

The standard this leans on is the platform's own: 09 §9.12's run object and
`hold embedder run`, the command that states a run for a recipe. An
alternative would be a model registry on the server keyed by weights digest
(an OCI-style content-addressed model store, or VeGa answering
`GET /vega/models` with each model's last stated weights): no request would
load a model to learn its weights. It pays once a node serves many models
whose names do not resemble their labels; until then the probe — bounded,
cached, and stopping at the first match — is the simplest thing that cannot
pick a wrong model.

COST. A probe may make the server load a model. So: only models whose name
shares a word with the encoder's label are asked (all of them, up to the
bound, when the Codex has no label), at most `PROBE_MAX` of them, each
bounded by `PROBE_TIMEOUT_S`, and every answer is cached per (server, wire,
model) — a hit for `HIT_TTL_S`, a miss for `MISS_TTL_S` — so the Library's
dialog and the route that follows it ask once.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Iterable, Sequence

#: how many models one resolution may ask at most
PROBE_MAX = 6
#: one probe's bound: a model the server has to load first answers late
PROBE_TIMEOUT_S = 120.0
HIT_TTL_S = 1800.0
MISS_TTL_S = 300.0
#: words a label and a model name share without saying anything about which
#: weights they are (quantisations, wire names, the obvious)
_GENERIC = frozenset({"vega", "localai", "gguf", "f16", "f32", "bf16", "q8", "q4", "fp16", "fp32",
                      "model", "models", "the", "and", "with", "on", "embed", "embedding",
                      "embeddings", "encoder", "llama", "cpp", "sycl", "rocm", "cuda", "vulkan",
                      "cpu", "gpu"})

_lock = threading.Lock()
_cache: dict[tuple[str, str, str], tuple[str | None, float]] = {}


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def _get(obj: Any, *path: str) -> Any:
    """`obj.a.b` or `obj["a"]["b"]`: a Codex's space or a recipe's table."""
    for key in path:
        if obj is None:
            return None
        obj = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
    return obj


def space_weights(space: Any) -> tuple[str | None, str | None, str]:
    """(document weights, query weights or None, label) of a space — a
    Codex's `space` or a recipe's `[space]` table alike. The query side is
    None in a symmetric space (09 §9.3)."""
    return (_get(space, "encoder", "weights") or None,
            _get(space, "query", "encoder", "weights") or None,
            str(_get(space, "encoder", "label") or ""))


def has_query_encoder(space: Any) -> bool:
    """Does the space name a query encoder of its own (09 §9.3)? Then a
    `create` embeds the query side's reference too and needs its endpoint."""
    return _get(space, "query", "encoder") is not None


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1 and w not in _GENERIC}


def candidates(models: Iterable[str], label: str, limit: int = PROBE_MAX) -> list[str]:
    """The models worth asking, most likely first: those whose name shares a
    word with the label, by how many — an ORDER, never a verdict. Without a
    label every model is a candidate, by name."""
    models = sorted({str(m) for m in models if m})
    want = _words(label)
    if not want:
        return models[:limit]
    scored = [(-len(_words(m) & want), m) for m in models]
    return [m for s, m in sorted(scored) if s < 0][:limit]


def served_weights(url: str, api: str, model: str, *, dtype: str = "f32", ctx: int = 8192,
                   dim: int | None = None) -> str | None:
    """The weights digest the run answering `model` at `url` states, VERIFIED
    against its run id (`run_check.describe_run`); None when it states none,
    does not answer, or answers wrongly. Cached (module docstring)."""
    key = (url.rstrip("/"), api or "openai", model)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[1] < (HIT_TTL_S if hit[0] else MISS_TTL_S):
            return hit[0]
    weights = _probe(key[0], key[1], model, dtype=dtype, ctx=ctx, dim=dim)
    with _lock:
        _cache[key] = (weights, time.monotonic())
    return weights


def _probe(url: str, api: str, model: str, *, dtype: str, ctx: int, dim: int | None) -> str | None:
    from . import collections as collections_mod, embeddings, run_check
    recipe = collections_mod.EmbedRecipe(url=url, api=api, model=model, dtype=dtype, ctx=ctx,
                                         dim=dim, query_model=model)
    try:
        seen: set = set()
        embeddings.embed_calibration(recipe, [run_check.PROBE], runs=seen,
                                     timeout=PROBE_TIMEOUT_S)
        if len(seen) != 1 or None in seen:
            return None                 # no run stated: nothing to hold the model to
        return run_check.describe_run(recipe, next(iter(seen))).weights or None
    except Exception:   # noqa: BLE001 — a model that cannot say its weights does not count
        return None


def on_server(url: str, api: str, models: Sequence[str], space: Any, *,
              probe: Callable[..., str | None] | None = None) -> tuple[str, str] | None:
    """(embed_model, query_model) on the server at `url` whose stated runs
    load the space's weights — each side its own — or None when no candidate
    does. The query side of a symmetric space is the document side's model."""
    weights, qweights, label = space_weights(space)
    if not url or not weights or not models:
        return None
    probe = probe or served_weights
    kw = {"dtype": str(_get(space, "dtype") or "f32"), "ctx": int(_get(space, "ctx") or 8192),
          "dim": _get(space, "dim")}
    asked = candidates(models, label)

    def find(want: str) -> str | None:
        for m in asked:
            if probe(url, api, m, **kw) == want:
                return m
        return None

    embed = find(weights)
    if embed is None:
        return None
    if not qweights or qweights == weights:
        return embed, embed
    query = find(qweights)
    return (embed, query) if query is not None else None

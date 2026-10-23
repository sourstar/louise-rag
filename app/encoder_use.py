"""Using a collection's encoder from OUTSIDE the worker's embed stage — the
`share contribute` and `hold vectors build` commands, and the worker's own
publisher (app/worker.py `_publisher`), which embeds a bundle's sixteen
challenge rows (09 §9.7).

Two things every such user needs, in one place:

- THE CALLABLES (03 §3.3): the collection's own encoder through its recipe
  — THE resolution the worker embeds with (`collections.recipe_for`,
  invariant 10) — and, for a collection gated per answer (app/hold/runs.py),
  the guard that stands between a batch's answers and its write.
- THE ENCODER LEASE (Perugia, 2026-10-19; scheduling.md pitfall 17): the
  worker's sweep of idle embedders judged a GPU-bound backend idle by its
  CPU share and stopped it under a running contribute seven times, a 19 s
  cold start each. A process embedding with a model says so in
  `state/node.sqlite` (app/node_state.py `encoder_leases`), renewed before
  every batch; the sweep leaves a leased model alone while the lease lives,
  and a lease a dead process left expires on its own.
"""
from __future__ import annotations

import os
import sys
from typing import Callable


def holder(command: str) -> str:
    """Who holds a lease: the command and its process, so a second run of
    the same command is another holder and the end of one releases only
    its own."""
    return f"{command} pid {os.getpid()}"


def leasing(embed, recipe, *, holder: str, collection: str | None = None):
    """`embed`, renewing the encoder lease before each batch. One row
    replaced per batch — cheaper than the batch by orders of magnitude; a
    store that cannot be written never stops the embed."""
    from app import model_load, model_pins, node_state
    server = model_load.server_of(recipe.url)
    model = getattr(recipe, "model", None)
    if not model:
        return embed

    def leased(chunks):
        try:
            node_state.lease_encoder(server, model, holder)
        except Exception as e:   # noqa: BLE001 — the lease is a courtesy to the sweep, not a gate
            print(f"louise: encoder lease not written ({type(e).__name__}: {e}); the worker's "
                  f"sweep may unload {model} between batches", file=sys.stderr)
        # pinned while this command processes, its prior state restored by
        # `release` (app/model_pins.py; operator, 2026-10-23)
        model_pins.hold(recipe.url, model, holder, collection=collection)
        return embed(chunks)
    return leased


def release(holder: str) -> None:
    """Every lease `holder` holds goes: the command ended."""
    from app import model_pins, node_state
    try:
        node_state.release_encoder(holder)
    except Exception:   # noqa: BLE001 — it expires on its own
        pass
    model_pins.release(holder)


def callables(codex, recipe, *, root, name: str, stop, holder: str,
              log: Callable[[str], None] | None = None):
    """`(embed, before_write)` for `codex` through `recipe`: `embed(chunks)`
    gives each chunk's vector in its layer's form (input forms, 09 §9.3),
    under the lease; `before_write` is the per-answer run guard where the
    Codex asks one (None otherwise). `embed` raises `embedder.GateDeferred`
    when the encoder is not there to ask."""
    from app import embeddings
    from app.hold import embedder as emb, runs as hold_runs
    by_run = hold_runs.applies(codex)
    seen: set = set()
    say = log or (lambda m: print(m, file=sys.stderr))

    def embed(chunks):
        seen.clear()
        try:
            if by_run:
                return embeddings.embed_chunks(codex, chunks, recipe=recipe, runs=seen)
            return embeddings.embed_chunks(codex, chunks, recipe=recipe)
        except Exception as e:   # noqa: BLE001
            raise emb.GateDeferred(f"the encoder at {recipe.url} could not embed: {e}") from e

    before_write = None
    if by_run:
        from app import run_check

        def before_write():
            run_check.guard(root, name, codex, recipe, set(seen), stop=stop.is_set, log=say)
    return leasing(embed, recipe, holder=holder, collection=name), before_write

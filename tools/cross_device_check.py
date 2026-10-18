"""`vectors-sample` against the same model on ANOTHER device
(docs/notes/gpu-silent-wrong-vectors.md).

The card can return a finite, normalised, wrong vector for some inputs, the
same one every time. A check that re-embeds on the same card agrees with it,
and so does the calibration gate. This runs the collection's own
`vectors-sample` (its sampling, its inputs re-cut from the records through the
layers, its tolerance) with exactly one thing changed: the encoder's model is
another LocalAI model of the same weights and settings on another device (a
`-cpu` twin behind VeGa, so every answer still comes with its receipt).

It is a diagnostic: the run is not the space's validated run, so nothing is
attested and nothing is written. Its report is the check's, plus the chunk
ids that failed.

    python -m tools.cross_device_check normattiva --model vega-dar-legal-it-cpu [--sample 0.02]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path


def cpu_recipe(recipe, model: str, api: str | None = None):
    """The collection's encoder with its document-side model replaced (and its
    api, for a twin served without VeGa: a diagnostic needs no receipt)."""
    if not model or model == recipe.model:
        raise ValueError("--model names the twin on another device, not the collection's own")
    return dataclasses.replace(recipe, model=model, api=api or recipe.api)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cross_device_check")
    p.add_argument("collection")
    p.add_argument("--model", required=True, help="the same model on another device")
    p.add_argument("--api", default=None, help="the twin's api when it is not behind VeGa (openai)")
    p.add_argument("--sample", default=None, help="raise the plan's part fraction")
    p.add_argument("--seed", default=None, help="32 bytes as hex")
    p.add_argument("--root", type=Path, default=None, help="where collections live (the CLI's default)")
    a = p.parse_args(argv)

    from app import embeddings, louise
    from app.hold import check as chk, collection as col, records as rec

    a.root = a.root or louise._default_root()
    name, codex = col.resolve(a.root, a.collection)
    local = rec.local_collection(a.root, name, codex)
    snapshot = louise._latest_snapshot(a.root, name, None)
    seed = bytes.fromhex(a.seed) if a.seed else os.urandom(32)
    recipe = cpu_recipe(louise._recipe_for(codex, name), a.model, a.api)
    report = chk.run(
        a.root, name, codex, snapshot, seed=seed, checks=["vectors-sample"], sample=a.sample,
        embed=lambda texts: embeddings.embed_chunks(codex, texts, recipe=recipe),
        reference=None, reparse=None, origin_unit=None, origin_bytes=None, against=None,
        only_shards=None, live=None,
        vectors_for=lambda ids: embeddings.vectors_by_chunk_id(ids, collection=local),
        log=lambda m: print(m, file=sys.stderr))
    for o in report.outcomes:
        print(json.dumps({"collection": name, "model": a.model, "check": o.check,
                          "result": o.result, "detail": o.detail, "why": o.why,
                          "seed": seed.hex()}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

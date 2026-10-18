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

`--explain` then embeds the failing chunks once more with the collection's own
model and prints, per chunk, the three cosines that tell the cases apart:
stored against the twin, stored against the card now, the card now against the
twin. A precision gap between devices reads high everywhere; a wrong vector
reads low against the twin and high against the card.

`--export FILE` embeds nothing: it writes the sample's inputs (as the encoder is
fed them) and stored vectors, for a machine without the node's limits to embed
and compare (the dev PC's CPU twin, operator 2026-10-18: the NAS takes no
sustained load).
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


class Recorder:
    """What the check read and embedded, by chunk id. `vectors_sample` asks
    for the stored vectors of the ids it picked, then embeds the texts of those
    it holds, in that order (app/hold/vectors_check.py); a call whose sizes do
    not line up is not recorded rather than guessed."""

    def __init__(self, vectors_for, embed):
        self._vectors_for, self._embed = vectors_for, embed
        self._ids: list[str] = []
        self.stored: dict[str, list[float]] = {}
        self.text: dict[str, str] = {}
        self.twin: dict[str, list[float]] = {}

    def vectors_for(self, ids):
        held = self._vectors_for(ids)
        self._ids = [cid for cid in ids if cid in held]
        self.stored.update(held)
        return held

    def embed(self, texts):
        out = self._embed(texts)
        if len(texts) == len(self._ids) == len(out):
            for cid, t, v in zip(self._ids, texts, out):
                self.text[cid], self.twin[cid] = t, list(v)
        return out

    def export(self) -> list[dict]:
        """Every chunk the check read, in the order it read them: its id, its
        input as the encoder is fed it (a text, or a pair as two strings) and
        its stored vector — what another machine needs to embed it and compare
        (`--export`)."""
        return [{"chunk": cid, "input": list(t) if isinstance(t, tuple) else t,
                 "stored": [float(x) for x in self.stored[cid]]}
                for cid, t in self.text.items()]

    def explain(self, failing, own_embed) -> list[dict]:
        """One row per failing chunk the recorder saw, its text embedded again
        by `own_embed` (the collection's own model)."""
        import numpy as np
        ids = [cid for cid in failing if cid in self.text]
        if not ids:
            return []
        now = own_embed([self.text[cid] for cid in ids])

        def cos(a, b) -> float:
            a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
            return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
        return [{"chunk": cid, "chars": len(self.text[cid]),
                 "stored_twin": round(cos(self.stored[cid], self.twin[cid]), 6),
                 "stored_card_now": round(cos(self.stored[cid], v), 6),
                 "card_now_twin": round(cos(v, self.twin[cid]), 6)}
                for cid, v in zip(ids, now)]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cross_device_check")
    p.add_argument("collection")
    p.add_argument("--model", default=None, help="the same model on another device")
    p.add_argument("--api", default=None, help="the twin's api when it is not behind VeGa (openai)")
    p.add_argument("--sample", default=None, help="raise the plan's part fraction")
    p.add_argument("--seed", default=None, help="32 bytes as hex")
    p.add_argument("--root", type=Path, default=None, help="where collections live (the CLI's default)")
    p.add_argument("--explain", action="store_true",
                   help="embed the failing chunks again with the collection's own model and "
                        "print the three cosines per chunk")
    p.add_argument("--export", type=Path, default=None, metavar="FILE",
                   help="embed nothing: write the sample's inputs and stored vectors to FILE, "
                        "for another machine to embed and compare")
    a = p.parse_args(argv)
    if not a.model and not a.export:
        p.error("--model names the twin, or --export writes the sample for another machine")

    from app import embeddings, louise
    from app.hold import check as chk, collection as col, records as rec

    a.root = a.root or louise._default_root()
    name, codex = col.resolve(a.root, a.collection)
    local = rec.local_collection(a.root, name, codex)
    snapshot = louise._latest_snapshot(a.root, name, None)
    seed = bytes.fromhex(a.seed) if a.seed else os.urandom(32)
    own = louise._recipe_for(codex, name)
    if a.export:
        # nothing is embedded here: a zero vector per input, so the check
        # records what it read and fails every chunk, which is not reported
        twin = lambda texts: [[0.0] * codex.space.dim for _ in texts]   # noqa: E731
    else:
        recipe = cpu_recipe(own, a.model, a.api)
        twin = lambda texts: embeddings.embed_chunks(codex, texts, recipe=recipe)   # noqa: E731
    rec_ = Recorder(lambda ids: embeddings.vectors_by_chunk_id(ids, collection=local), twin)
    report = chk.run(
        a.root, name, codex, snapshot, seed=seed, checks=["vectors-sample"], sample=a.sample,
        embed=rec_.embed,
        reference=None, reparse=None, origin_unit=None, origin_bytes=None, against=None,
        only_shards=None, live=None,
        vectors_for=rec_.vectors_for,
        log=lambda m: print(m, file=sys.stderr))
    if a.export:
        rows = rec_.export()
        a.export.write_text(json.dumps({"collection": name, "collection_id": codex.collection_id,
                                        "space_id": codex.space_id, "ctx": codex.space.ctx,
                                        "seed": seed.hex(), "chunks": rows}), encoding="utf-8")
        print(json.dumps({"collection": name, "exported": len(rows), "to": str(a.export),
                          "seed": seed.hex()}))
        return 0
    for o in report.outcomes:
        print(json.dumps({"collection": name, "model": a.model, "check": o.check,
                          "result": o.result, "detail": o.detail, "why": o.why,
                          "seed": seed.hex()}, default=str))
        if a.explain and o.detail and o.detail.get("failed"):
            for row in rec_.explain(o.detail["failed"],
                                    lambda texts: embeddings.embed_chunks(codex, texts, recipe=own)):
                print(json.dumps(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

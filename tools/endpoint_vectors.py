"""What does each encoder answer TODAY — recorded, so that whatever answers
tomorrow can be held to it.

    python -m tools.endpoint_vectors record  --out /data/state/endpoint-vectors
    python -m tools.endpoint_vectors compare --against /data/state/endpoint-vectors
    python -m tools.endpoint_vectors compare --against DIR \
        --url gutenberg=http://nas:30437/upstream/qwen3-embedding-4b \
        --url pubmed.query=http://nas:30437/upstream/medcpt-query

WHY THIS EXISTS. RAGE replaces the per-model apps in one cut-over: the
operator's rule is that they are shut off BEFORE RAGE loads, because together
they would hold every model twice on the card (docs/notes/rage.md §16). The
first proof of RAGE is that its vectors match today's endpoints — and once
those are off there is nothing left to compare with. So `record` runs FIRST,
while they still answer; `compare` runs after, against RAGE.

WHAT IS RECORDED, per collection and per side (a bi-encoder pair such as
MedCPT's has a document side and a query side):
- the sixteen fingerprint texts of docs/09-formats.md §9.12 — the shortest,
  the longest and fourteen between, four of them at or past a full context
  window, because a build that is right on a sentence and wrong on a full
  window is exactly what happened here;
- an even spread of the calibration set beside them (`--texts`, 64);
each ALONE in its request, in order, as the endpoint returned it
(embeddings.embed_calibration, one_at_a_time). One safetensors file and one JSON beside it.

WHAT COMPARE SAYS. For each side: whether the fingerprint is the same BIT FOR
BIT — the same engine on the same card reproduces it, and that is stronger
than any tolerance — and the worst and median cosine over everything
recorded. The tolerance is the collection's own (its Codex's
`space.tolerance`) or `--tolerance`; there is no constant here, because 03
§3.1 allows none. Exit 0: every side within tolerance (or none given); 1: a
side below it; 3: an endpoint did not answer — a deferral, never a verdict.

A DIFFERENT llama.cpp BUILD IS EXPECTED NOT TO BE BIT-EQUAL. gutenberg's
encoder runs b10603 today and RAGE's image holds b10630: another engine,
which the cosines judge and the gate will test (03 §3.3).

DNA collections are skipped: the calibration set is text, and they are a
non-binding work in progress (operator, 2026-09-19).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import sys
import time
from pathlib import Path

from app import collections as collections_mod, embeddings
from app.hold import calibration as cal, fingerprint
from app.hold.embedder import cosine

MODALITY = "text"
SIDES = ("document", "query")


def chosen(texts: list[str], spread: int) -> list[int]:
    """The fingerprint's sixteen first, in rank order, then the spread."""
    sixteen = fingerprint.indices(texts)
    step = max(1, len(texts) // max(1, spread))
    rest = [i for i in range(0, len(texts), step) if i not in set(sixteen)]
    return sixteen + rest


def _is_dna(collection) -> bool:
    codex = getattr(collection, "codex", None)
    if codex is not None:
        return codex.modality == "dna"
    return collection.plugin.startswith("genome")


def _sides(recipe) -> list[str]:
    return list(SIDES) if recipe.query_url else ["document"]


def _paths(out: Path, name: str, side: str) -> tuple[Path, Path]:
    # by hand, not with_suffix(): that REPLACES ".document", and the query
    # side would then overwrite the document side's file
    return out / f"{name}.{side}.safetensors", out / f"{name}.{side}.json"


def _selected(names: list[str]):
    found = [c for c in collections_mod.load_all() if not names or c.name in names]
    missing = set(names) - {c.name for c in found}
    if missing:
        raise SystemExit(f"endpoint_vectors: no such collection: {sorted(missing)}")
    return [c for c in found if not _is_dna(c)]


def record(out: Path, names: list[str], spread: int) -> int:
    import numpy as np
    from safetensors.numpy import save_file

    set_id, texts = cal.load(MODALITY)
    picked = chosen(texts, spread)
    out.mkdir(parents=True, exist_ok=True)
    deferred = False
    for collection in _selected(names):
        recipe = collections_mod.recipe_for(collection)
        for side in _sides(recipe):
            url = recipe.query_url if side == "query" else recipe.url
            t0 = time.monotonic()
            try:
                vectors = embeddings.embed_calibration(
                    recipe, [texts[i] for i in picked], query=side == "query",
                    one_at_a_time=True)
            except Exception as e:                          # noqa: BLE001
                print(f"{collection.name}.{side}: deferred — {url} did not answer: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                deferred = True
                continue
            tensor, sidecar = _paths(out, collection.name, side)
            save_file({"vectors": np.asarray(vectors, dtype="<f4")}, str(tensor))
            meta = {"collection": collection.name, "side": side, "url": url,
                    "api": recipe.api, "model": recipe.model, "ctx": recipe.ctx,
                    "set": set_id, "indices": picked,
                    "fingerprint": fingerprint.of(vectors[:fingerprint.COUNT]),
                    "dim": len(vectors[0]), "seconds": round(time.monotonic() - t0, 1),
                    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            sidecar.write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
            print(f"{collection.name}.{side}: {len(picked)} texts from {url} in "
                  f"{meta['seconds']} s — fingerprint {meta['fingerprint'][:23]}…")
    return 3 if deferred else 0


def _tolerance(collection, given: float | None) -> float | None:
    codex = getattr(collection, "codex", None)
    if codex is not None:
        return float(codex.space.tolerance)
    return given


def compare(against: Path, names: list[str], urls: dict[str, str],
            tolerance: float | None) -> int:
    from safetensors.numpy import load_file

    set_id, texts = cal.load(MODALITY)
    worst_exit = 0
    for collection in _selected(names):
        recipe = collections_mod.recipe_for(collection)
        for side in _sides(recipe):
            tensor, sidecar = _paths(against, collection.name, side)
            label = f"{collection.name}.{side}"
            if not sidecar.is_file():
                print(f"{label}: nothing recorded in {against}")
                continue
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            if meta["set"] != set_id:
                raise SystemExit(f"{label}: recorded over calibration set {meta['set'][:23]}…, "
                                 f"this platform ships {set_id[:23]}…; not comparable")
            key = collection.name if side == "document" else label
            now = recipe
            if key in urls:
                field = "query_url" if side == "query" else "url"
                now = dataclasses.replace(recipe, **{field: urls[key].rstrip("/")})
            url = now.query_url if side == "query" else now.url
            try:
                vectors = embeddings.embed_calibration(
                    now, [texts[i] for i in meta["indices"]], query=side == "query",
                    one_at_a_time=True)
            except Exception as e:                          # noqa: BLE001
                print(f"{label}: deferred — {url} did not answer: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                worst_exit = max(worst_exit, 3)
                continue
            before = load_file(str(tensor))["vectors"]
            cosines = [cosine(a, b) for a, b in zip(before.tolist(), vectors)]
            same = fingerprint.of(vectors[:fingerprint.COUNT]) == meta["fingerprint"]
            limit = _tolerance(collection, tolerance)
            low = [meta["indices"][i] for i, c in enumerate(cosines)
                   if limit is not None and c < limit]
            print(f"{label}: {meta['url']}  ->  {url}")
            print(f"  fingerprint  {'SAME, bit for bit' if same else 'different'}")
            print(f"  cosine       worst {min(cosines):.6f}, median "
                  f"{statistics.median(cosines):.6f}, over {len(cosines)} texts")
            if limit is None:
                print("  tolerance    none: no Codex and no --tolerance; no verdict")
            else:
                print(f"  tolerance    {limit}: "
                      + ("every text within it" if not low
                         else f"{len(low)} below it — set indices {low[:12]}"))
                if low:
                    worst_exit = max(worst_exit, 1)
    return worst_exit


def _url_map(items: list[str]) -> dict[str, str]:
    out = {}
    for item in items:
        key, sep, url = item.partition("=")
        if not sep or not url.startswith("http"):
            raise SystemExit(f"endpoint_vectors: --url wants COLLECTION[.query]=URL, "
                             f"got {item!r}")
        out[key] = url
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = ap.add_subparsers(dest="verb", required=True)
    r = verbs.add_parser("record", help="what each encoder answers now")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--collection", action="append", default=[])
    r.add_argument("--texts", type=int, default=64,
                   help="how many of the calibration set beside the sixteen (64)")
    c = verbs.add_parser("compare", help="what answers now, against a recording")
    c.add_argument("--against", type=Path, required=True)
    c.add_argument("--collection", action="append", default=[])
    c.add_argument("--url", action="append", default=[], metavar="COLLECTION[.query]=URL",
                   help="answer from this endpoint instead of the collection file's")
    c.add_argument("--tolerance", type=float, default=None,
                   help="for a collection with no Codex; a Codex's own tolerance wins")
    a = ap.parse_args(argv)
    if a.verb == "record":
        return record(a.out, a.collection, a.texts)
    return compare(a.against, a.collection, _url_map(a.url), a.tolerance)


if __name__ == "__main__":
    sys.exit(main())

"""15f step 0 and after: every live collection's encoder (and query encoder)
asked its 512 calibration texts through the collection's own recipe; the
vectors and the runs that answered are kept in <out>/<label>-<collection>-<side>.npy.

    python record15f.py <label> <out dir>             record
    python record15f.py compare <a> <b> <out dir>     cosines between two records
"""
import json
import sys
from pathlib import Path

import numpy as np

from app import collections as C, embeddings
from app.hold import calibration as cal, collection as col

ROOT = Path("/data/collections")


def sides(c):
    name, codex = col.resolve(ROOT, c.name)
    texts = cal.verify(codex.modality, codex.space.calibration.set)
    recipe = C.recipe_for(c)
    yield "document", recipe, texts, False
    if recipe.query_url:
        inst = codex.space.query.instruction or ""
        yield "query", recipe, [f"{inst}{t}" for t in texts], True


def record(label, out):
    for c in C.load_all():
        if getattr(c, "embed", None) is None:
            continue
        try:
            for side, recipe, texts, q in sides(c):
                runs: set = set()
                v = embeddings.embed_texts(texts, recipe=recipe, query=q, runs=runs)
                np.save(out / f"{label}-{c.name}-{side}.npy", np.asarray(v, dtype=np.float32))
                print(json.dumps({"collection": c.name, "side": side, "n": len(v),
                                  "dim": len(v[0]), "runs": sorted(runs)}), flush=True)
        except Exception as e:  # noqa: BLE001 — one collection's failure is reported, the rest go on
            print(json.dumps({"collection": c.name, "error": f"{type(e).__name__}: {e}"[:300]}),
                  flush=True)


def compare(a, b, out):
    for fa in sorted(out.glob(f"{a}-*.npy")):
        fb = out / fa.name.replace(f"{a}-", f"{b}-", 1)
        if not fb.exists():
            print(json.dumps({"file": fa.name, "missing": fb.name}))
            continue
        x, y = np.load(fa), np.load(fb)
        x /= np.linalg.norm(x, axis=1, keepdims=True)
        y /= np.linalg.norm(y, axis=1, keepdims=True)
        cos = (x * y).sum(axis=1)
        print(json.dumps({"pair": fa.name[len(a) + 1:-4], "min": round(float(cos.min()), 6),
                          "median": round(float(np.median(cos)), 6),
                          "below_0.99": int((cos < 0.99).sum())}))


if __name__ == "__main__":
    if sys.argv[1] == "compare":
        compare(sys.argv[2], sys.argv[3], Path(sys.argv[4]))
    else:
        out = Path(sys.argv[2])
        out.mkdir(parents=True, exist_ok=True)
        record(sys.argv[1], out)

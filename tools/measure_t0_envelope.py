"""tools/measure_t0_envelope.py — check T0's envelope literals against real data.

03 §3.5's T0 admits a bundle when its mean vector has cosine ≥ 0.5 to the
envelope's mean and the 2nd and 98th percentiles of the per-dimension
standard-deviation ratios lie within [0.4, 2.0]. Those numbers came from
this program, and `space.envelope` is a Codex field — so they freeze at
`hold collection create` and every peer inherits them. A reader who wants
to know whether they still hold on a new encoder, a new corpus or a new
dimension should be able to re-run the measurement rather than trust the
table, which is why this ships beside the constants.

    python tools/measure_t0_envelope.py --embed-url URL --corpus DIR \
        [--model NAME] [--ctx 8192] [--chunks 300] [--lang en] [--json OUT]

WHAT IT DOES, in three stages:

  1. embeds the 512 calibration texts and reduces them to 09 §9.12's
     envelope — row 0 the per-dimension mean, row 1 the per-dimension
     standard deviation;
  2. cuts a slice of a real corpus into chunks with the collection's own
     chunker and embeds them, which is a plausible bundle;
  3. computes T0's two statistics, and then computes them again for seven
     deliberately broken bundles derived from the same vectors.

The third stage is the part that matters. A threshold loosened until the
honest case passes is worthless; what makes these constants defensible is
that random, foreign, un-normalised, half-substituted and degenerate
bundles still fail them. Those controls cost no GPU — they are numpy over
the matrix stage 2 already produced.

WHAT IT FOUND, 2026-09-09, and why the rule is a percentile:

    run  encoder              dim   corpus    space              cosine  outside
    A    Qwen3-Embedding-8B  4096   english   gutenberg-pack     0.752   16
    B    Qwen3-Embedding-8B  4096   chinese   (that encoder)     0.658   33
    C    Qwen3-Embedding-4B  2560   english   gutenberg          0.747    0
    D    MedCPT article       768   english   (that encoder)     0.933    0
    E    MedCPT article       768   pubmed    pubmed             0.882    0

The deviation test PASSED at 2560 dimensions and FAILED at 4096 on the
same honest vectors — because `min` over 4096 samples is smaller than
`min` over 2560 for no reason but the count. A rule written as *every
dimension* is a multiple-comparison problem and widening it only
postpones the failure to a larger space; a percentile does not drift with
sample count, so the constants barely had to move.

The cosine moves for three separate reasons, and they are worth keeping
apart: model SIZE 0.005 (8B to 4B), CORPUS 0.095 (English to Chinese),
model CLASS 0.18 (decoder to BERT). The old bound of 0.9 refused three of
the four live spaces above; the old deviation band refused only the
4096-dimensional one.

Not measured: `bge-m3` at dim 1024 and `ctx` 2048, which carries
`wikipedia` and `wikivec` — the largest collections here and the only
ones on the `cdc` and `verbatim` chunkers. And another modality, which
cannot be measured at all until a DNA calibration set exists (09 §9.12
ships one set per modality; this platform ships only `text`).

RUN IT AGAINST THE COLLECTION'S OWN ENCODER, from
`config/collections/<name>.toml` and not from a deployment note. The
first four runs above were attributed to the wrong collection for a day
because a stale note said the 8B served `gutenberg`; the TOML says 4B,
and the TOML is what runs.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                    # noqa: E402

from app.hold import calibration as cal               # noqa: E402

#: 03 §3.5's literals. Named here so the report shows what it is judging
#: against rather than burying them in a comparison.
COS_MIN = 0.5
BAND = (0.4, 2.0)
QUANTILE = 2            # the 2nd and 98th percentiles


def embed_one(url: str, model: str, text: str, ctx: int,
              budget: int | None = None):
    """One text, truncated to `budget` characters when given.

    Returns `(vector, None)` or `(None, token_count)` when the server
    refuses it for length — which is not an error here but the signal the
    caller shrinks on. The platform's own client does the same thing
    (`app/embeddings.py`), because no character rule bounds a token count:
    measured over these 512 texts, chars per token ran 1.02 to 5.70.
    """
    body = json.dumps({"model": model,
                       "input": [text[:budget] if budget else text]}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=1800).read())
    except urllib.error.HTTPError as e:
        try:
            return None, json.loads(e.read())["error"].get("n_prompt_tokens", -1)
        except Exception:                              # noqa: BLE001
            return None, -1
    return np.asarray(d["data"][0]["embedding"], dtype="float32"), None


def fit(url: str, model: str, text: str, ctx: int):
    """The longest prefix the encoder accepts, found by scaling on what it
    reports rather than by halving — a proportional step converges in one
    or two attempts where halving throws away half a text for a 5 %
    overshoot."""
    budget = None
    for _ in range(12):
        v, over = embed_one(url, model, text, ctx, budget)
        if v is not None:
            return v
        budget = int((budget or len(text)) * (ctx / max(over, ctx + 1)) * 0.92)
    raise SystemExit("a text could not be embedded at any length")


def embed_all(url: str, model: str, items, ctx: int, label: str):
    out, t0 = [], time.time()
    for i, s in enumerate(items):
        out.append(fit(url, model, s, ctx))
        if (i + 1) % 64 == 0:
            print(f"  {label} {i+1}/{len(items)}  {time.time()-t0:.0f}s",
                  file=sys.stderr, flush=True)
    return np.vstack(out)


def corpus_chunks(root: Path, want: int, lang: str | None):
    """A slice of Project Gutenberg, cut by the collection's own chunker.

    The chunker matters: a bundle is chunks as this collection produces
    them, and a measurement over paragraphs or whole books would describe
    a bundle nobody ships.
    """
    import re

    from app import chunking
    from app.plugins.gutenberg import pg_text

    lang_re = re.compile(r"<dcterms:language>.*?>([a-z]{2,3})<", re.S)
    chunks: list[str] = []
    for d in sorted((p for p in root.iterdir() if p.is_dir() and p.name.isdigit()),
                    key=lambda p: int(p.name)):
        if lang:
            rdf = d / f"pg{d.name}.rdf"
            if not rdf.is_file():
                continue
            try:
                m = lang_re.search(rdf.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if not m or m.group(1) != lang:
                continue
        txt = next((p for p in sorted(d.iterdir())
                    if ".txt" in p.name and "nroff" not in p.name), None)
        if txt is None:
            continue
        try:
            body = pg_text.read_clean(txt)
        except OSError:
            continue
        if len(body) < 20_000:
            continue
        for c in chunking.chunk_book(f"gutenberg:{d.name}", int(d.name),
                                     [("", body)]):
            chunks.append(c["text"])
            if len(chunks) >= want:
                return chunks
    return chunks


def statistics(reference: np.ndarray, bundle: np.ndarray) -> dict:
    """T0's two numbers, plus what a reader needs to judge them."""
    mean, sd = reference.mean(axis=0), reference.std(axis=0)
    m = bundle.mean(axis=0)
    cos = float(m @ mean / (np.linalg.norm(m) * np.linalg.norm(mean) + 1e-30))
    r = bundle.std(axis=0) / np.where(sd > 0, sd, np.inf)
    lo, hi = float(np.percentile(r, QUANTILE)), float(np.percentile(r, 100 - QUANTILE))
    return {
        "cosine": cos,
        "p_lo": lo, "p_hi": hi,
        "min": float(r.min()), "max": float(r.max()),
        "outside_every_dim": int(((r < BAND[0]) | (r > BAND[1])).sum()),
        "passes": bool(cos >= COS_MIN and BAND[0] <= lo and hi <= BAND[1]),
    }


def controls(reference: np.ndarray, bundle: np.ndarray, seed: int) -> dict:
    """The seven broken bundles, derived from the honest one so that the
    only thing that differs is the defect being modelled."""
    rng = np.random.default_rng(seed)
    n, dim = bundle.shape
    R = rng.standard_normal((n, dim)).astype("float32")
    R /= np.linalg.norm(R, axis=1, keepdims=True)
    return {
        "random unit vectors": R,
        "dimension-permuted (a foreign model)": bundle[:, rng.permutation(dim)],
        "un-normalised": bundle * rng.uniform(0.2, 5.0, (n, 1)).astype("float32"),
        "half real, half random": np.vstack([bundle[:n // 2], R[:n - n // 2]]),
        "degenerate (one repeated)": np.repeat(bundle[:1], n, axis=0),
        "10 % random": np.vstack([bundle[:int(n * 0.9)], R[:n - int(n * 0.9)]]),
        "25 % random": np.vstack([bundle[:int(n * 0.75)], R[:n - int(n * 0.75)]]),
    }


def line(label: str, s: dict) -> str:
    return (f"{label:<38} cos {s['cosine']:+.4f}  "
            f"p{QUANTILE} {s['p_lo']:6.3f}  p{100-QUANTILE} {s['p_hi']:8.3f}  "
            f"min {s['min']:6.3f} max {s['max']:10.3f}   "
            f"{'PASS' if s['passes'] else 'fail'}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--embed-url", required=True,
                    help="an OpenAI-compatible /v1/embeddings endpoint")
    ap.add_argument("--corpus", type=Path, required=True,
                    help="the Gutenberg corpus root")
    ap.add_argument("--model", default="embed")
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--chunks", type=int, default=300)
    ap.add_argument("--lang", default=None,
                    help="restrict the slice to one language, e.g. zh")
    ap.add_argument("--cache", type=Path, default=None,
                    help="directory for the two vector matrices, so a "
                         "second run costs no GPU")
    ap.add_argument("--json", type=Path, default=None)
    a = ap.parse_args(argv)

    cache = a.cache
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    ref_path = cache / "reference.npy" if cache else None
    bun_path = cache / "bundle.npy" if cache else None

    print("1. the calibration set -> the envelope", file=sys.stderr)
    _, texts = cal.load("text")
    if ref_path and ref_path.exists():
        V = np.load(ref_path)
        print(f"  reusing cached {V.shape}", file=sys.stderr)
    else:
        V = embed_all(a.embed_url, a.model, texts, a.ctx, "cal")
        if ref_path:
            np.save(ref_path, V)
    sd = V.std(axis=0)
    print(f"  {V.shape}; envelope sd min {sd.min():.3e} median "
          f"{np.median(sd):.5f} max {sd.max():.5f}; "
          f"{int((sd == 0).sum())} dead dimensions", file=sys.stderr)

    print("\n2. a real corpus slice -> a plausible bundle", file=sys.stderr)
    if bun_path and bun_path.exists():
        B = np.load(bun_path)
        print(f"  reusing cached {B.shape}", file=sys.stderr)
    else:
        chunks = corpus_chunks(a.corpus, a.chunks, a.lang)
        if not chunks:
            raise SystemExit(f"{a.corpus}: no books matched")
        print(f"  {len(chunks)} chunks", file=sys.stderr)
        B = embed_all(a.embed_url, a.model, chunks, a.ctx, "chunk")
        if bun_path:
            np.save(bun_path, B)

    print(f"\n3. T0 at cos >= {COS_MIN}, p{QUANTILE}/p{100-QUANTILE} "
          f"within {list(BAND)}\n")
    honest = statistics(V, B)
    print(line(f"  HONEST ({a.lang or 'default'} corpus)", honest))
    print()
    results = {"honest": honest, "controls": {}}
    for name, X in controls(V, B, seed=20260909).items():
        s = statistics(V, X)
        results["controls"][name] = s
        print(line(f"  {name}", s))

    missed = [n for n, s in results["controls"].items() if s["passes"]]
    print(f"\n  honest bundle: {'PASSES' if honest['passes'] else 'FAILS'}")
    print(f"  controls caught: {7 - len(missed)}/7")
    if missed:
        print(f"  NOT caught: {', '.join(missed)}")
        print("  (partial contamination is not admission's to catch — "
              "03 §3.5 leaves it to T3 and T4)")
    if a.json:
        a.json.write_text(json.dumps(results, indent=1), encoding="utf-8")
        print(f"\n  written: {a.json}")
    return 0 if honest["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

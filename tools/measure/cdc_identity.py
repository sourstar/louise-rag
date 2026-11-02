"""DIAGNOSTIC (read-only): does the COMPILED `cdc` boundary loop cut exactly
where the Python loop cuts, on this node's own records?

app/chunking.py's content-defined chunker (wikipedia's `default` layer) runs
its per-character loop in the extension native/louise_cdc when the image
carries it (docs/notes/python-hot-spots.md, "D"), and the Python loop
`_cdc_boundaries`, unchanged, otherwise. One moved boundary is a different
chunk under the same space id: wikipedia's tens of millions of chunks
re-embedded, or worse, two cuts mixed in one space (CLAUDE.md invariant 3).
The suite compares the two on every chunking test, the test suite's strings
and fixtures, and 1,000 seeded texts (tests/test_cdc_native.py); this
compares them on REAL records, IN THE IMAGE that will run them.

BEFORE ANY DEPLOY OF AN IMAGE CARRYING THE EXTENSION, München runs it in
that image on at least 100,000 real records whose layer is `cdc`, and
deploys only on IDENTICAL:

  python -m tools.measure.cdc_identity --collection wikipedia-v8 --records 100000
  python -m tools.measure.cdc_identity --collection wikipedia-v8 --part FILE [--part FILE …]
  python -m tools.measure.cdc_identity --generated 20000      # seeded texts, no collection

THE VERDICT. IDENTICAL needs every compared record identical AND at least
`--records` records compared whose layer cuts by `cdc` (`--generated`: that
many texts): zero, or fewer than asked, is INCOMPLETE (exit 2) — never a
success — and so is a run where the extension does not import, since then
there is nothing to compare. Any difference is DIFFERENT (exit 1); the first
are named (part, uid, what differs), and `--diff-out FILE` writes each one
with its sections as a JSON line, ready to become a test. SIGINT/SIGTERM
prints what was compared so far as INCOMPLETE, exit 130.

WHAT IS COMPARED, per record:
- its layer `cdc`: every non-empty section (stripped, as `chunk_cdc` cuts
  it) at the layer's sizes — the extension's boundary list
  (`chunking._NATIVE.boundaries`) against the Python loop's, every
  boundary. The extension declines (None) exactly a text it cannot answer
  — a `str` holding a lone surrogate, which has no UTF-8 form — and
  answering such a text, or declining any other, is a difference too;
- whatever its layer: its whole cut through `layers.chunks_for_document`
  (`hold.drift.record_cut`, the path every stage cuts by), every chunk dict,
  with the extension and with the Python loop forced (`python_loop`).

WHICH RECORDS. The record parts of the collection's latest snapshot (or
`--snapshot`), or the files `--part` names (an unfinished build's staged
parts), shuffled with `--seed`; the first `--per-part` records of each.
All the cores the parse pool allows (`parallel.each_part`: PARSE_PROCESSES,
or `--jobs`), each child at nice 10. It writes nothing but `--diff-out`.
Beside the verdict it prints each path's µs a record (measured in the
children, under whatever else the node runs — a speed figure, not a
benchmark).
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import random
import signal
import sys
import time
from contextlib import contextmanager
from itertools import islice
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

#: differing records named on the console (all of them go to --diff-out)
SHOWN = 20
#: generated texts a pool task compares
TEXTS_PER_TASK = 100
#: a u64: the extension answers sizes below it
U64 = 1 << 64

_niced = False


def _nice() -> None:
    global _niced
    if not _niced and multiprocessing.parent_process() is not None:   # a pool child
        _niced = True
        try:
            os.nice(10)
        except (AttributeError, OSError):
            pass


@contextmanager
def python_loop():
    """The Python loop for the block, explicitly: the extension set aside."""
    from app import chunking
    saved, chunking._NATIVE = chunking._NATIVE, None
    try:
        yield
    finally:
        chunking._NATIVE = saved


def answerable(text, sizes) -> bool:
    """Whether the extension must answer: an exact `str` with a UTF-8 form,
    sizes in 0..2**64, and not the Python loop's endless case (`max` 0
    with text past `min`)."""
    if type(text) is not str or not all(type(s) is int and 0 <= s < U64 for s in sizes):
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    mn, _, mx = sizes
    return mx > 0 or len(text) <= mn


def compare(text: str, sizes: tuple[int, int, int]) -> dict:
    """The extension's boundaries against `_cdc_boundaries`' for one text:
    {"boundaries": n, "declined": 0|1, "differ": None | what differs}."""
    from app import chunking
    want = list(chunking._cdc_boundaries(text, *sizes))
    got = chunking._NATIVE.boundaries(text, *sizes)
    must = answerable(text, sizes)
    out = {"boundaries": len(want), "declined": int(got is None), "differ": None}
    if got is None:
        if must:
            out["differ"] = {"sizes": list(sizes), "why": "the extension declined a text it can answer"}
            return out
        got = list(chunking._cdc_ends(text, *sizes))         # what cdc_split then cuts by
    elif not must:
        out["differ"] = {"sizes": list(sizes), "why": "the extension answered a text it cannot"}
        return out
    if got != want:
        k = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want)))
        out["differ"] = {"sizes": list(sizes), "why": "the boundaries differ", "at": k,
                         "native": got[k:k + 3], "python": want[k:k + 3],
                         "native_n": len(got), "python_n": len(want)}
    return out


# ---------------------------------------------------------------- generated texts

#: what the generated texts are written in: code point ranges, inclusive.
#: No range enters U+D800–U+DFFF but "surrogates" (texts with no UTF-8 form).
ALPHABETS: dict[str, tuple[tuple[int, int], ...]] = {
    "ascii": ((0x20, 0x7E),),
    "controls": ((0x00, 0x1F), (0x7F, 0x9F)),
    "latin-1": ((0xA0, 0xFF),),
    "latin-extended": ((0x100, 0x24F), (0x1E00, 0x1EFF)),
    "greek-cyrillic": ((0x370, 0x3FF), (0x400, 0x52F)),
    "hebrew-arabic": ((0x590, 0x5FF), (0x600, 0x6FF)),
    "devanagari-thai": ((0x900, 0x97F), (0xE00, 0xE7F)),
    "cjk": ((0x3040, 0x30FF), (0x4E00, 0x9FFF), (0xAC00, 0xD7A3)),
    "combining": ((0x300, 0x36F), (0x1AB0, 0x1AFF), (0x20D0, 0x20FF), (0xFE20, 0xFE2F)),
    "emoji": ((0x1F300, 0x1F64F), (0x1F680, 0x1F6FF), (0x1F900, 0x1FAFF)),
    "astral": ((0x10000, 0x10FFFF),),
    "specials": ((0x200B, 0x200F), (0x2028, 0x202E), (0xE000, 0xE0FF), (0xFE00, 0xFE0F),
                 (0xFEFF, 0xFEFF), (0xFFF9, 0xFFFF), (0x10FFFE, 0x10FFFF)),
}
#: sequences that are one glyph and several code points
EMOJI_SEQUENCES = tuple("".join(map(chr, cps)) for cps in (
    (0x1F468, 0x200D, 0x1F469, 0x200D, 0x1F467, 0x200D, 0x1F466),   # a family, ZWJ-joined
    (0x1F1EE, 0x1F1F9),                                             # a flag
    (0x1F44D, 0x1F3FD),                                             # a skin tone
    (0x2764, 0xFE0F),                                               # VS16
    (0x1F3F3, 0xFE0F, 0x200D, 0x1F308),
    (0x1F9D1, 0x1F3FF, 0x200D, 0x1F4BB),
    (0x65, 0x301),                                                  # e + combining acute
    (0x1100, 0x1161, 0x11A8),                                       # conjoining jamo
    (0x1F3F4, 0xE0067, 0xE0062, 0xE0065, 0xE006E, 0xE0067, 0xE007F),  # tag sequence
))
#: the lengths around the sizes: where an off-by-one would show
EDGE_LENGTHS = (0, 1, 2, 3, 999, 1000, 1001, 1002, 3999, 4000, 4001, 7999, 8000, 8001,
                8002, 9000, 9001, 15999, 16000, 16001, 24001)
#: sizes beside wikipedia's: 1 <= min < target <= max, the extremes included
#: (TOML's largest integer, 2**63 - 1)
FIXED_SIZES = ((200, 600, 900), (1, 2, 2), (1, 2, 3), (1, 8000, 8000), (7999, 8000, 8000),
               (1000, 1001, 8000), (1000, 4000, 4000), (1, 2**62, 2**63 - 1),
               (2**62, 2**62 + 1, 2**63 - 1), (16, 64, 128), (5, 6, 100_000))
KINDS = ("words", "random", "uniform", "periodic", "stacks", "sequences", "surrogates")
KIND_WEIGHTS = (40, 20, 6, 8, 8, 8, 4)


def _pool(rng: random.Random, names) -> list[str]:
    out = []
    for name in names:
        for lo, hi in ALPHABETS[name]:
            out += [chr(rng.randint(lo, hi)) for _ in range(min(200, hi - lo + 1))]
    return out


def _length(rng: random.Random, long: int) -> int:
    k = rng.random()
    if k < 0.10:
        return rng.choice(EDGE_LENGTHS)
    if k < 0.45:
        return rng.randint(0, 2_000)
    if k < 0.85:
        return rng.randint(2_000, 20_000)
    if k < 0.97:
        return rng.randint(20_000, 60_000)
    return rng.randint(60_000, max(60_000, long))


def _words(rng: random.Random, pool: list[str], n: int) -> str:
    vocab = ["".join(rng.choices(pool, k=rng.randint(1, 12))) for _ in range(rng.randint(5, 400))]
    seps = rng.choice(((" ",), (" ", " ", " ", "\n", "\n\n", ", ", ". "), ("\n",), ("",)))
    parts: list[str] = []
    size = 0
    while size < n:
        w = rng.choice(vocab) + rng.choice(seps)
        parts.append(w)
        size += len(w)
    return "".join(parts)[:n]


def generated_text(rng: random.Random, long: int = 400_000) -> str:
    """A seeded text: one of KINDS, in one to four of ALPHABETS, of a length
    from 0 to `long` code points (EDGE_LENGTHS one time in ten)."""
    kind = rng.choices(KINDS, KIND_WEIGHTS)[0]
    n = _length(rng, long)
    pool = _pool(rng, rng.sample(sorted(ALPHABETS), rng.randint(1, 4)))
    if kind == "words":
        return _words(rng, pool, n)
    if kind == "random":
        return "".join(rng.choices(pool, k=n))
    if kind == "uniform":                     # a constant hash: no cut, every piece max long
        return rng.choice(pool) * n
    if kind == "periodic":
        pattern = "".join(rng.choices(pool, k=rng.randint(1, 50)))
        return (pattern * (n // len(pattern) + 1))[:n]
    if kind == "stacks":                      # a base letter under up to 30 combining marks
        marks = _pool(rng, ["combining"])
        out, size = [], 0
        while size < n:
            out.append(rng.choice(pool) + "".join(rng.choices(marks, k=rng.randint(0, 30))))
            size += len(out[-1])
        return "".join(out)[:n]
    if kind == "sequences":
        out, size = [], 0
        while size < n:
            out.append(rng.choice(EMOJI_SEQUENCES) if rng.random() < 0.7 else rng.choice(pool))
            size += len(out[-1])
        return "".join(out)[:n]
    # "surrogates": no UTF-8 form — lone ones, and a pair as two code points
    text = list(_words(rng, pool, max(n, 1)))
    for _ in range(rng.randint(1, 5)):
        lone = chr(rng.randint(0xD800, 0xDFFF))
        text[rng.randrange(len(text))] = rng.choice((lone, chr(0xD83D) + chr(0xDE00)))
    return "".join(text)


def generated_sizes(rng: random.Random, length: int) -> list[tuple[int, int, int]]:
    """The sizes a generated text is cut at: wikipedia's (the version-1
    constants), one of FIXED_SIZES, and one at the text's own scale, so that
    a short text is cut many times too."""
    from app import chunking
    mx = rng.randint(2, max(2, length // rng.choice((1, 2, 4, 16, 64)) + 2))
    tg = rng.randint(2, mx)
    return [(chunking.CDC_MIN_CHARS, chunking.CDC_TARGET_CHARS, chunking.CDC_MAX_CHARS),
            rng.choice(FIXED_SIZES), (rng.randint(1, tg - 1), tg, mx)]


def generated(seed: int, i: int, long: int = 400_000) -> tuple[str, list[tuple[int, int, int]]]:
    """Text `i` of seed `seed` and its sizes — the same however the texts are
    split between tasks."""
    rng = random.Random(f"louise-cdc-identity:{seed}:{i}")
    text = generated_text(rng, long)
    return text, generated_sizes(rng, len(text))


def _empty() -> dict:
    return {"compared": 0, "cdc_records": 0, "sections": 0, "chars": 0, "boundaries": 0,
            "declined": 0, "native_s": 0.0, "python_s": 0.0, "no_extension": 0, "differ": []}


def check_generated(seed: int, start: int, count: int, long: int, keep_text: bool) -> dict:
    """One pool task: texts start … start+count-1, every size of each."""
    _nice()
    from app import chunking
    out = _empty()
    if chunking._NATIVE is None:
        out["no_extension"] = 1
        return out
    for i in range(start, start + count):
        text, sizes = generated(seed, i, long)
        bad = []
        for s in sizes:
            got = compare(text, s)
            out["boundaries"] += got["boundaries"]
            out["declined"] += got["declined"]
            if got["differ"]:
                bad.append(got["differ"])
        out["compared"] += 1
        out["cdc_records"] += 1
        out["chars"] += len(text)
        if bad:
            out["differ"].append({"text": i, "seed": seed, "differs": bad,
                                  **({"value": text} if keep_text else {})})
    return out


# ---------------------------------------------------------------- real records

def check_part(root: str, name: str, part: str, take: int, keep_text: bool) -> dict:
    """One pool task: the first `take` records of one part, both ways."""
    _nice()
    from app import chunking, layers
    from app.hold import collection as col
    from app.hold import drift
    from app.pack import records as pack_records
    out = _empty()
    if chunking._NATIVE is None:
        out["no_extension"] = 1
        return out
    _, codex = col.resolve(Path(root), name)
    for rec in islice(pack_records.iter_records(Path(part)), take):
        row = {"uid": rec["uid"], "native_id": rec.get("native_id", 0)}
        sections = [tuple(x) for x in rec.get("sections") or []]
        layer = layers.layer_of(codex, int(row["native_id"]), row)
        bad = []
        if layer.chunker == chunking.CDC:
            out["cdc_records"] += 1
            sizes = chunking._cdc_sizes(layer.params)
            for _label, body in sections:
                text = (body or "").strip()
                if not text:
                    continue
                got = compare(text, sizes)
                out["sections"] += 1
                out["chars"] += len(text)
                out["boundaries"] += got["boundaries"]
                out["declined"] += got["declined"]
                if got["differ"]:
                    bad.append(got["differ"])
        t0 = time.perf_counter()
        native = drift.record_cut(codex, rec)
        t1 = time.perf_counter()
        with python_loop():
            python = drift.record_cut(codex, rec)
        t2 = time.perf_counter()
        out["native_s"] += t1 - t0
        out["python_s"] += t2 - t1
        if native != python:
            bad.append({"why": "the record's cut differs", "native_chunks": len(native),
                        "python_chunks": len(python)})
        out["compared"] += 1
        if bad:
            out["differ"].append({"part": Path(part).name, "uid": rec["uid"], "layer": layer.chunker,
                                  "differs": bad, **({"sections": sections} if keep_text else {})})
    return out


def parts_for(a) -> tuple[str, str, list[str]]:
    """(root, collection name, part files) for the run."""
    from app import collections as collections_mod
    from app.hold import collection as col
    from app.hold import seed
    root = Path(a.root) if a.root else collections_mod.louise_root() / "collections"
    name, _codex = col.resolve(root, a.collection)
    if a.part:
        return str(root), name, [str(Path(p)) for p in a.part]
    snapshot = seed.latest_snapshot(root, name, a.snapshot)
    tree = col.shard_dir_of(root, name)
    return str(root), name, [str(tree / s.path) for s in snapshot.shards]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--collection", default="wikipedia-v8", help="whose records and layers")
    p.add_argument("--snapshot", default=None, help="a snapshot id (default: the latest)")
    p.add_argument("--part", action="append", help="a record part file, instead of the snapshot's")
    p.add_argument("--root", default=None, help="the collections root (default: the node's)")
    p.add_argument("--records", type=int, default=100_000,
                   help="records cut by cdc to compare (default 100000)")
    p.add_argument("--per-part", type=int, default=1_000, help="records read from each part")
    p.add_argument("--generated", type=int, default=0, help="compare this many seeded texts instead")
    p.add_argument("--long", type=int, default=400_000, help="the longest generated text, code points")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--jobs", type=int, default=None, help="processes (default PARSE_PROCESSES)")
    p.add_argument("--diff-out", type=Path, default=None, help="differing records, JSON lines")
    a = p.parse_args(argv)
    from app import chunking, config, parallel
    if a.jobs:
        config.PARSE_PROCESSES = a.jobs
    keep = a.diff_out is not None
    want = a.generated or a.records
    if chunking._NATIVE is None:
        print("INCOMPLETE: the extension louise_cdc does not import here — nothing to compare, "
              "nothing proven (run it in the image that carries it)", file=sys.stderr)
        return 2
    if a.generated:
        fn, where = check_generated, {"generated": a.generated, "long": a.long}
        tasks = [(a.seed, i, min(TEXTS_PER_TASK, a.generated - i), a.long, keep)
                 for i in range(0, a.generated, TEXTS_PER_TASK)]
    else:
        root, name, parts = parts_for(a)
        if not parts:
            print("INCOMPLETE: no record part found — nothing compared, nothing proven", file=sys.stderr)
            return 2
        random.Random(a.seed).shuffle(parts)
        fn, where = check_part, {"collection": name, "parts": len(parts)}
        tasks = [(root, name, part, a.per_part, keep) for part in parts]
    total = _empty()
    del total["differ"]
    differ: list[dict] = []
    stopped = False

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    t0 = last = time.monotonic()
    try:
        for got in parallel.each_part(fn, tasks):
            for k in total:
                total[k] += got[k]
            differ.extend(got["differ"])
            if time.monotonic() - last > 30:
                last = time.monotonic()
                print(f"{total['cdc_records']:,} compared, {len(differ)} differ, "
                      f"{time.monotonic() - t0:.0f} s", file=sys.stderr, flush=True)
            if total["cdc_records"] >= want and not a.generated:
                break
    except KeyboardInterrupt:
        stopped = True
    n = total["cdc_records"]
    if total["no_extension"]:
        verdict, code = "INCOMPLETE", 2
    elif differ:
        verdict, code = "DIFFERENT", 1
    elif n >= max(1, want) and not stopped:
        verdict, code = "IDENTICAL", 0
    else:
        verdict, code = "INCOMPLETE", 130 if stopped else 2
    if keep and differ:
        with a.diff_out.open("w", encoding="utf-8") as f:
            for d in differ:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
    m = total["compared"]
    summary = {
        "verdict": verdict, "asked": want, **where, "extension": getattr(chunking._louise_cdc, "VERSION", None),
        **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in total.items()},
        "differ": len(differ),
        "first_differing": [{k: v for k, v in d.items() if k not in ("sections", "value")}
                            for d in differ[:SHOWN]],
        "python_us_per_record": round(total["python_s"] / m * 1e6, 1) if m and total["python_s"] else None,
        "native_us_per_record": round(total["native_s"] / m * 1e6, 1) if m and total["native_s"] else None,
        "seed": a.seed, "wall_s": round(time.monotonic() - t0, 1)}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    unit = "texts" if a.generated else "records cut by cdc"
    why = {"IDENTICAL": f"all {n:,} {unit} compared cut at the same boundaries both ways",
           "DIFFERENT": f"{len(differ):,} of {m:,} differ — do not deploy",
           "INCOMPLETE": (f"{n:,} of {want:,} {unit} compared — not a proof"
                          + ("; a child could not import the extension" if total["no_extension"] else ""))}[verdict]
    print(f"{verdict}: {why}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

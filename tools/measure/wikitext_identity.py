"""DIAGNOSTIC (read-only): does the live wikitext parse give the SAME BYTES
as the code before its rewrite, on this node's own wikipedia pages?

app/plugins/wikipedia/wikitext.py was rewritten for speed on 2026-11-02
(docs/notes/python-hot-spots.md, "A": no per-node tree search, one template
walk). Its output is the prose the chunker cuts and the vectors are made
of, so one changed byte is a new cleaning version and 52.9 M pages
re-embedded (CLAUDE.md invariant 3). The code before is kept verbatim in
tools/measure/wikitext_reference.py, and the test suite compares the two on
every wikitext literal of tests/ and thousands of seeded adversarial pages
(tests/test_wikitext_identity.py). This compares them on REAL pages: it
reads pages from the held multistream dumps with the plugin's own reader
(`source._stream_pages`), runs the plugin's own path on each —
`redirect_info` for a redirect, `parse_all` for every other page — through
both, and compares `json.dumps(sort_keys=True)` of the two results (or the
two exceptions: the same failure is the same answer).

BEFORE ANY DEPLOY OF THE REWRITE, München runs it on at least 100,000 real
pages, and deploys only on IDENTICAL:

  python -m tools.measure.wikitext_identity --collection wikipedia-v8 --pages 100000
  python -m tools.measure.wikitext_identity --dump PATH [--dump PATH …] --pages 100000

THE VERDICT. IDENTICAL needs every compared page identical AND at least
`--pages` pages compared: zero pages, or fewer than asked, is INCOMPLETE —
never a success (exit 2). Any difference is DIFFERENT (exit 1); the first
differing pages are named (dump, page id, title, function), and `--diff-out
FILE` writes each one's wikitext as a JSON line, ready to become a test.
SIGINT/SIGTERM prints what was compared so far as INCOMPLETE, exit 130.

WHICH PAGES. Every page stream of every dump (the collection's pinned
snapshot, or `--dump`), shuffled with `--seed`, so the sample spans the
languages and the whole of each dump; namespace 0 only, unless
`--any-namespace`. All the cores the parse pool allows (`parallel.each_part`:
PARSE_PROCESSES, or `--jobs`), each child at nice 10. It reads the dumps and
writes nothing but `--diff-out`. Beside the verdict it prints both
implementations' µs a page on these pages (measured, in the children, under
whatever else the node runs — a speed figure, not a benchmark).
"""
from __future__ import annotations

import argparse
import bz2
import json
import multiprocessing
import os
import random
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

#: streams a pool task reads (~100 pages each, both implementations)
STREAMS_PER_TASK = 4
#: differing pages named on the console (all of them go to --diff-out)
SHOWN = 20

_niced = False


def _outcome(fn, text: str) -> str:
    try:
        return json.dumps(fn(text), sort_keys=True, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 — the same failure is the same answer
        return f"raised {type(e).__name__}: {e}"


def check(spans: list[tuple[str, int, int]], namespaces: tuple[str, ...] | None,
          keep_text: bool) -> dict:
    """One pool task: every page of these streams, old against new. Module
    level, its arguments and result pickle (`parallel.each_part`)."""
    global _niced
    if not _niced and multiprocessing.parent_process() is not None:   # a pool child
        _niced = True
        try:
            os.nice(10)
        except (AttributeError, OSError):
            pass
    from app.plugins.wikipedia import source as wsrc
    from app.plugins.wikipedia import wikitext as live
    from tools.measure import wikitext_reference as ref
    out = {"read": 0, "compared": 0, "parse_all": 0, "redirect_info": 0, "both_raised": 0,
           "chars": 0, "ref_s": 0.0, "live_s": 0.0, "differ": []}
    for k, (dump, start, end) in enumerate(spans):
        for pid, page in wsrc._stream_pages(Path(dump), start, end).items():
            out["read"] += 1
            if namespaces is not None and (page.findtext("ns") or "").strip() not in namespaces:
                continue
            text = page.findtext("revision/text") or ""
            name = ("redirect_info" if ref.is_redirect(text) or page.find("redirect") is not None
                    else "parse_all")
            # which runs first alternates: neither is always the warm one
            first, second = (ref, live) if (out["compared"] + k) % 2 else (live, ref)
            t0 = time.perf_counter()
            a = _outcome(getattr(first, name), text)
            t1 = time.perf_counter()
            b = _outcome(getattr(second, name), text)
            t2 = time.perf_counter()
            out["ref_s" if first is ref else "live_s"] += t1 - t0
            out["live_s" if first is ref else "ref_s"] += t2 - t1
            out["compared"] += 1
            out[name] += 1
            out["chars"] += len(text)
            if a.startswith("raised ") and b.startswith("raised "):
                out["both_raised"] += 1
            if a != b:
                out["differ"].append({"dump": Path(dump).name, "page_id": pid,
                                      "title": page.findtext("title") or "", "function": name,
                                      **({"wikitext": text} if keep_text else {})})
    return out


def streams_of(dump: str) -> list[tuple[str, int, int]]:
    """Every page stream of one dump as (dump, start, end), from its index;
    the last runs to the end of the file (end 0), as the plugin reads it."""
    from app.plugins.wikipedia import source as wsrc
    path = Path(dump)
    index = path.with_name(path.name[: -len(wsrc._DUMP_SUFFIX)] + wsrc._INDEX_SUFFIX)
    starts: list[int] = []
    with bz2.open(index, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            off = line.split(":", 1)[0]
            if off.isdigit() and (not starts or int(off) != starts[-1]):
                starts.append(int(off))
    return [(dump, s, starts[i + 1] if i + 1 < len(starts) else 0) for i, s in enumerate(starts)]


def dumps_for(a) -> list[Path]:
    from app.plugins.wikipedia import source as wsrc
    if a.dump:
        return [Path(d) for d in a.dump]
    from app import collections as collections_mod
    from app.plugins import walk
    c = collections_mod.by_name(a.collection)
    if c is None:
        raise SystemExit(f"no collection named {a.collection!r} on this node (--dump names files)")
    found = walk.files(c.corpus_dir, f"*{wsrc._DUMP_SUFFIX}")
    if c.snapshot:
        found = [d for d in found if d.parent.name == c.snapshot]
    return found


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--collection", default="wikipedia-v8", help="whose dumps (its pinned snapshot)")
    p.add_argument("--dump", action="append", help="a pages-articles-multistream dump, instead")
    p.add_argument("--pages", type=int, default=100_000, help="pages to compare (default 100000)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--any-namespace", action="store_true", help="not namespace 0 only")
    p.add_argument("--jobs", type=int, default=None, help="processes (default PARSE_PROCESSES)")
    p.add_argument("--diff-out", type=Path, default=None, help="differing pages' wikitext, JSON lines")
    a = p.parse_args(argv)
    from app import config, parallel
    if a.jobs:
        config.PARSE_PROCESSES = a.jobs
    dumps = dumps_for(a)
    if not dumps:
        print("INCOMPLETE: no dump found — nothing compared, nothing proven", file=sys.stderr)
        return 2
    print(f"{len(dumps)} dump(s); reading their indexes…", file=sys.stderr, flush=True)
    streams = [s for got in parallel.each_part(streams_of, [(str(d),) for d in dumps]) for s in got]
    random.Random(a.seed).shuffle(streams)
    tasks = [(streams[i:i + STREAMS_PER_TASK], None if a.any_namespace else ("0",), a.diff_out is not None)
             for i in range(0, len(streams), STREAMS_PER_TASK)]
    total = {"read": 0, "compared": 0, "parse_all": 0, "redirect_info": 0, "both_raised": 0,
             "chars": 0, "ref_s": 0.0, "live_s": 0.0}
    differ: list[dict] = []
    stopped = False

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    t0 = time.monotonic()
    last = t0
    try:
        for got in parallel.each_part(check, tasks):
            for k in total:
                total[k] += got[k]
            differ.extend(got["differ"])
            if time.monotonic() - last > 30:
                last = time.monotonic()
                print(f"{total['compared']:,} compared, {len(differ)} differ, "
                      f"{time.monotonic() - t0:.0f} s", file=sys.stderr, flush=True)
            if total["compared"] >= a.pages:
                break
    except KeyboardInterrupt:
        stopped = True
    n = total["compared"]
    if differ:
        verdict, code = "DIFFERENT", 1
    elif n >= max(1, a.pages) and not stopped:
        verdict, code = "IDENTICAL", 0
    else:
        verdict, code = "INCOMPLETE", 130 if stopped else 2
    if a.diff_out is not None and differ:
        with a.diff_out.open("w", encoding="utf-8") as f:
            for d in differ:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
    summary = {
        "verdict": verdict, "asked": a.pages, **{k: v for k, v in total.items() if not k.endswith("_s")},
        "differ": len(differ),
        "first_differing": [{k: v for k, v in d.items() if k != "wikitext"} for d in differ[:SHOWN]],
        "reference_us_per_page": round(total["ref_s"] / n * 1e6, 1) if n else None,
        "live_us_per_page": round(total["live_s"] / n * 1e6, 1) if n else None,
        "speedup": round(total["ref_s"] / total["live_s"], 2) if total["live_s"] else None,
        "dumps": [str(d) for d in dumps], "seed": a.seed, "wall_s": round(time.monotonic() - t0, 1)}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    why = {"IDENTICAL": f"all {n:,} pages compared give the same bytes",
           "DIFFERENT": f"{len(differ):,} of {n:,} pages differ — do not deploy",
           "INCOMPLETE": f"{n:,} of {a.pages:,} pages compared — not a proof"}[verdict]
    print(f"{verdict}: {why}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

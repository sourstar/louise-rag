"""Are the vectors we already STORED still what this endpoint produces?

    python -m tools.vector_compliance gutenberg              # last 3 books
    python -m tools.vector_compliance gutenberg --books 10 --chunks 40
    python -m tools.vector_compliance gutenberg --repeat 20  # endpoint only
    python -m tools.vector_compliance gutenberg --json

WHY THIS EXISTS. On 2026-08-27 gutenberg's endpoint (llama.cpp + SYCL on an
Intel Arc card, Qwen3-Embedding-8B) was found not to be bit-exact: the same
text embedded twice returns different vectors. That alone is unremarkable —
`backend_gate` already allows for it, noting that same-backend jitter across
restarts and concurrency is <= 1e-4 (cos >= 0.9998), and TAU is 0.995.

What is NOT allowed is the excursion: some long inputs came back at cosine
0.78-0.81 against a fresh embedding of the identical text. Short inputs did
not. And the excursions are INTERMITTENT — `backend_gate`'s own `long-en`
probe (~3800 chars) passed twice on the same day, so this is a rate, not a
constant, and a rate is a thing to measure rather than assume.

TWO QUESTIONS, TWO MODES, because they are genuinely different:

  --stored (default)  Do the vectors on disk still verify? This is the one
                      that says whether the corpus is tainted. It compares
                      each stored vector against a fresh embedding of the
                      chunk text as it is chunked TODAY, which is also
                      exactly what a peer would do to verify our work.

  --repeat N          Is the endpoint a function at all? Embeds the same
                      texts N times and reports the spread. Use this to
                      measure the excursion RATE, and after any change to
                      the server's flags to see whether it helped.

READ THE FAILURE DIRECTION. A low cosine here has three possible causes and
they are not equally likely: the endpoint drifted (what this hunts), the
document was re-chunked so the stored vector belongs to different text (the
tool reports chunk-id overlap so you can see this), or the model was
swapped behind the endpoint (backend_gate exists to catch that first).
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys

from app import collections as c_mod, db, embeddings, worker
from app.verify import backend_gate


def cosine(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return num / (na * nb)


def _summary(sims: list[float], tau: float) -> dict:
    if not sims:
        return {"n": 0}
    below = [s for s in sims if s < tau]
    return {
        "n": len(sims),
        "min": round(min(sims), 6),
        "median": round(statistics.median(sims), 6),
        "max": round(max(sims), 6),
        "below_tau": len(below),
        "below_tau_pct": round(100.0 * len(below) / len(sims), 1),
    }


def check_stored(collection, *, books: int, chunks: int, tau: float) -> dict:
    """Compare stored vectors against fresh embeddings, newest books first."""
    con = db.connect(collection)
    store = embeddings._conn(collection)
    if embeddings.TABLE not in store.table_names():
        return {"error": "no vector table for this collection"}
    tbl = store.open_table(embeddings.TABLE)
    recipe = c_mod.recipe_for(collection)

    rows = con.execute(
        "SELECT uid, native_id, updated_at FROM documents "
        "WHERE in_scope=1 AND embed_done=1 AND error IS NULL "
        "ORDER BY updated_at DESC LIMIT ?", (books,)).fetchall()

    out: list[dict] = []
    every: list[float] = []
    for r in rows:
        doc = con.execute("SELECT * FROM documents WHERE uid=?",
                          (r["uid"],)).fetchone()
        local = worker._load_chunks(doc, collection)
        stored = {x["id"]: x["vector"] for x in
                  tbl.search().where(embeddings._book_filter(int(r["native_id"])))
                     .select(["id", "vector"]).limit(200_000).to_list()}
        entry = {"uid": r["uid"], "embedded_at": r["updated_at"],
                 "chunks_now": len(local), "vectors_stored": len(stored),
                 # a document re-chunked since it was embedded is a DIFFERENT
                 # explanation for a low score, so make it visible
                 "id_overlap": len({c["id"] for c in local} & set(stored))}
        sims, worst = [], []
        for ch in local[:chunks]:
            sv = stored.get(ch["id"])
            if sv is None:
                continue
            fresh = embeddings._embed_batch([ch["text"]], 180, recipe)[0]
            s = cosine(list(sv), fresh)
            sims.append(s)
            if s < tau:
                worst.append({"id": ch["id"], "chars": len(ch["text"]),
                              "cosine": round(s, 6)})
        entry.update(_summary(sims, tau))
        entry["failures"] = worst[:10]
        every.extend(sims)
        out.append(entry)
    con.close()
    return {"tau": tau, "books": out, "overall": _summary(every, tau)}


def check_repeat(collection, *, repeats: int, tau: float,
                 concurrency: int = 1) -> dict:
    """Is the endpoint a function? Same text, many times.

    `concurrency` is the variable that has to be controlled, not assumed.
    docs/notes/approved-encoders.md records this Arc build measuring cosine
    min/mean 1.000000 over 16 texts in its approval trial, while the same
    endpoint measured 0.78 on long texts during a live embed workload. A
    server run with `-np 4` is a different machine when four slots are busy
    than when one is, so a reproducibility number without a stated
    concurrency is not a number — run this at 1 AND at the worker's setting
    before concluding anything about the endpoint.
    """
    from concurrent.futures import ThreadPoolExecutor

    recipe = c_mod.recipe_for(collection)
    cases = {
        "short": backend_gate.PROBES["short-en"],
        "long": backend_gate.PROBES["long-en"],
        "non-ascii": backend_gate.PROBES["non-ascii"],
    }
    out = {}
    for label, text in cases.items():
        if concurrency > 1:
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                vecs = list(ex.map(
                    lambda _: embeddings._embed_batch([text], 180, recipe)[0],
                    range(repeats)))
        else:
            vecs = [embeddings._embed_batch([text], 180, recipe)[0]
                    for _ in range(repeats)]
        # every pair, not just against the first: one bad draw among many
        # should not be hidden by which draw happened to be the reference
        sims = [cosine(vecs[i], vecs[j])
                for i in range(len(vecs)) for j in range(i + 1, len(vecs))]
        entry = _summary(sims, tau)
        entry["chars"] = len(text)
        entry["byte_identical"] = all(v == vecs[0] for v in vecs[1:])
        out[label] = entry
    return {"tau": tau, "repeats": repeats, "concurrency": concurrency,
            "cases": out}


def _print_stored(report: dict) -> None:
    if "error" in report:
        print(report["error"])
        return
    tau = report["tau"]
    print(f"stored vectors vs a fresh embedding — TAU={tau}\n")
    for b in report["books"]:
        print(f"== {b['uid']}  chunks_now={b['chunks_now']} "
              f"stored={b['vectors_stored']} id_overlap={b['id_overlap']}")
        if not b.get("n"):
            print("   no comparable chunks (re-chunked since it was embedded?)\n")
            continue
        print(f"   n={b['n']}  min={b['min']:.6f}  median={b['median']:.6f}  "
              f"max={b['max']:.6f}")
        print(f"   below TAU: {b['below_tau']}/{b['n']} ({b['below_tau_pct']}%)")
        for f in b["failures"]:
            print(f"      {f['id']}  chars={f['chars']}  cos={f['cosine']:.6f}")
        print()
    o = report["overall"]
    if o.get("n"):
        print(f"OVERALL n={o['n']} min={o['min']:.6f} median={o['median']:.6f} "
              f"below TAU {o['below_tau']}/{o['n']} ({o['below_tau_pct']}%)")
        print("VERDICT:", "COMPLIANT" if not o["below_tau"] else
              "NOT COMPLIANT — stored vectors do not reproduce")


def _print_repeat(report: dict) -> None:
    print(f"endpoint self-consistency — {report['repeats']} repeats at "
          f"concurrency {report['concurrency']}, TAU={report['tau']}\n")
    for label, e in report["cases"].items():
        print(f"{label:>10} (chars {e['chars']:>5}): "
              f"byte-identical={str(e['byte_identical']):<5} "
              f"min={e['min']:.6f} median={e['median']:.6f} "
              f"below TAU {e['below_tau']}/{e['n']}")
    worst = min(e["min"] for e in report["cases"].values())
    print("\nVERDICT:", "endpoint reproduces within TAU" if worst >= report["tau"]
          else f"endpoint does NOT reproduce (worst {worst:.6f})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("collection")
    ap.add_argument("--books", type=int, default=3,
                    help="how many recently embedded documents to check")
    ap.add_argument("--chunks", type=int, default=25,
                    help="chunks sampled per document")
    ap.add_argument("--repeat", type=int, metavar="N",
                    help="endpoint mode: embed the same probes N times instead")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="repeat mode: requests in flight. A server run with "
                         "-np 4 is a different machine at 1 than at 4 — always "
                         "state the concurrency a reproducibility number came "
                         "from (see docs/notes/approved-encoders.md)")
    ap.add_argument("--tau", type=float, default=backend_gate.TAU,
                    help=f"pass bar (default backend_gate.TAU={backend_gate.TAU}; "
                         f"the same-function bar for encoder approval is 0.999)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    collection = c_mod.by_name(args.collection)
    if collection is None:
        print(f"no such collection: {args.collection}", file=sys.stderr)
        return 2
    from app import licensing        # invariant 9: stored text is embedded (audit)
    licensing.require_ack_or_exit(collection, use="its stored text is embedded")

    if args.repeat:
        report = check_repeat(collection, repeats=args.repeat, tau=args.tau,
                              concurrency=args.concurrency)
        printer, failed = _print_repeat, min(
            e["min"] for e in report["cases"].values()) < args.tau
    else:
        report = check_stored(collection, books=args.books, chunks=args.chunks,
                              tau=args.tau)
        printer = _print_stored
        failed = bool(report.get("overall", {}).get("below_tau"))

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        printer(report)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

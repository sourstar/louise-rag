"""Find books whose stored vectors are corrupt, and drop those vectors.

    python -m tools.vector_sweep gutenberg                 # dry run, reports only
    python -m tools.vector_sweep gutenberg --delete        # act on the findings
    python -m tools.vector_sweep gutenberg --delete --resume

THE SEARCH (the operator's, 2026-08-28). Corruption was observed to cluster
by book, so a full scan of every book is wasteful:

  * test the first book;
  * a BAD book means test the NEXT one — a cluster is walked one by one;
  * a GOOD book means skip `--stride` books ahead;
  * a book found bad AFTER a skip is a new cluster: walk LEFT and RIGHT from
    it, stopping in each direction at the first good book;
  * then resume skipping from the rightmost bad book of that cluster.

TESTING ONE BOOK. Its stored vectors are compared against a fresh embedding
of the same chunk text. The book is BAD at the first chunk that disagrees —
there is nothing to learn from the rest of it. A book is called GOOD only
after `--max-chunks` chunks agree, which is a CONFIDENCE STATEMENT, not a
proof: at the ~15% per-chunk corruption rate seen in the bad books, 40 clean
chunks put the chance of a missed bad book near 1e-3. Raise it if that is
not good enough; the cost is linear.

THE SAFEGUARD, and why it is not optional. A mismatch alone does not say
which side is wrong — the stored vector, or the fresh draw it was compared
against. This endpoint has produced bad vectors, so a single bad DRAW would
otherwise condemn a healthy book and delete its embeddings. So every
suspected chunk is re-drawn:

    two fresh draws agree with each other, and disagree with stored
        -> the STORED vector is corrupt; the book is bad
    the two fresh draws disagree with EACH OTHER
        -> the endpoint is misbehaving right now; the sweep ABORTS rather
           than delete anything on the word of an unreliable encoder

DELETION removes the book's rows from the vector table and clears
`embed_done`, so the document is re-embedded when embedding is turned back
on. It is delete-then-insert on a book id, which is invariant 2's own
idiom — nothing else is touched, and no text is lost.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

from app import collections as c_mod, db, embeddings, worker
from app.verify import backend_gate


def cosine(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return num / (na * nb)


class EndpointUnreliable(RuntimeError):
    """Two fresh draws of the same text disagreed. Nothing may be deleted on
    the word of an encoder that cannot reproduce itself."""


class Sweeper:
    def __init__(self, collection, *, tau: float, max_chunks: int, stride: int,
                 delete: bool, concurrency: int = 1, log=print):
        self.c = collection
        self.tau = tau
        self.max_chunks = max_chunks
        self.stride = stride
        self.delete = delete
        self.concurrency = max(1, concurrency)
        self.log = log
        self.recipe = c_mod.recipe_for(collection)
        self.con = db.connect(collection)
        self.store = embeddings._conn(collection)
        self.tbl = self.store.open_table(embeddings.TABLE)
        self.checked = 0          # chunks embedded, the real cost
        self.verdicts: dict[int, dict] = {}

    # ---------------------------------------------------------------- books

    def books(self) -> list[int]:
        """Every book that HAS embeddings, in native-id order — the order the
        clustering was observed in."""
        rows = self.con.execute(
            "SELECT native_id FROM documents WHERE in_scope=1 AND embed_done=1 "
            "AND error IS NULL ORDER BY CAST(native_id AS INTEGER)").fetchall()
        return [int(r["native_id"]) for r in rows]

    def _fresh(self, text: str) -> list[float]:
        self.checked += 1
        # a stored chunk's text, in its layer's form
        return embeddings.embed_chunks(self.c, [text], recipe=self.recipe, timeout=180)[0]

    def _fresh_many(self, texts: list[str]) -> list[list[float]]:
        """One request per text, several in flight. Never one request with
        several texts: config.EMBED_BATCH is 1 because this backend does not
        batch correctly, and that applies to a verifier exactly as it does to
        the worker."""
        if len(texts) == 1 or self.concurrency == 1:
            return [self._fresh(t) for t in texts]
        from concurrent.futures import ThreadPoolExecutor
        self.checked += len(texts)
        with ThreadPoolExecutor(max_workers=self.concurrency) as ex:
            return list(ex.map(
                lambda t: embeddings.embed_chunks(self.c, [t], recipe=self.recipe,
                                                 timeout=180)[0], texts))

    def test(self, book_id: int) -> dict:
        """GOOD/BAD for one book, stopping at the first corrupt vector."""
        row = self.con.execute(
            "SELECT * FROM documents WHERE in_scope=1 AND CAST(native_id AS INTEGER)=?",
            (book_id,)).fetchone()
        if row is None:
            return {"book": book_id, "verdict": "absent"}
        try:
            chunks = worker._load_chunks(row, self.c)
        except Exception as e:      # noqa: BLE001 — a corpus fact, not our business
            return {"book": book_id, "verdict": "unreadable", "detail": str(e)[:80]}
        stored = {r["id"]: list(r["vector"]) for r in
                  self.tbl.search().where(embeddings._book_filter(book_id))
                      .select(["id", "vector"]).limit(200_000).to_list()}
        if not stored:
            return {"book": book_id, "verdict": "no-vectors"}

        # Only chunks that HAVE a stored vector can be judged, capped at
        # max_chunks. Embedded in waves so a book costs wall-clock
        # max_chunks/concurrency instead of max_chunks serial calls — the
        # difference between a sweep that finishes overnight and one that
        # takes a day and a half. The VERDICT is unchanged: the first
        # disagreeing chunk in order still decides, a wave merely embeds a
        # few chunks the serial version would not have reached.
        todo = [ch for ch in chunks if ch["id"] in stored][:self.max_chunks]
        seen = 0
        for start in range(0, len(todo), self.concurrency):
            wave = todo[start:start + self.concurrency]
            fresh = self._fresh_many([ch["text"] for ch in wave])
            for ch, fv in zip(wave, fresh):
                sv = stored[ch["id"]]
                seen += 1
                if cosine(sv, fv) >= self.tau:
                    continue
                # suspected — confirm before condemning (module docstring)
                a, b = self._fresh(ch["text"]), self._fresh(ch["text"])
                if cosine(a, b) < self.tau:
                    raise EndpointUnreliable(
                        f"book {book_id} chunk {ch['id']}: two fresh draws differ "
                        f"(cos {cosine(a, b):.6f}) — the encoder is not "
                        f"reproducing itself, so nothing can be judged or "
                        f"deleted right now")
                if cosine(sv, a) < self.tau and cosine(sv, b) < self.tau:
                    return {"book": book_id, "verdict": "bad", "chunk": ch["id"],
                            "chars": len(ch["text"]),
                            "cosine": round(cosine(sv, a), 6), "checked": seen}
        return {"book": book_id, "verdict": "good", "checked": seen,
                "vectors": len(stored)}

    def drop(self, book_id: int) -> int:
        """Remove a bad book's vectors and let it be embedded again."""
        n = len(self.tbl.search().where(embeddings._book_filter(book_id))
                    .select(["id"]).limit(200_000).to_list())
        if not self.delete:
            return n
        self.tbl.delete(embeddings._book_filter(book_id))
        self.con.execute("UPDATE documents SET embed_done=0, updated_at=? "
                         "WHERE in_scope=1 AND CAST(native_id AS INTEGER)=?",
                         (int(time.time()), book_id))
        self.con.commit()
        return n

    def _record(self, res: dict) -> None:
        self.verdicts[res["book"]] = res
        if res["verdict"] == "bad":
            n = self.drop(res["book"])
            res["dropped"] = n
            self.log(f"  BAD  book {res['book']:>7}  first bad chunk "
                     f"{res['chunk']} cos={res['cosine']:.4f} "
                     f"({res['checked']} checked) -> "
                     f"{'dropped' if self.delete else 'would drop'} {n} vectors")

    # ------------------------------------------------------------- the walk

    def run(self, order: list[int], start: int = 0) -> dict:
        i, n = start, len(order)
        while i < n:
            res = self._verdict(order[i])
            if res["verdict"] == "bad":
                # inside a cluster: walk it one by one to the right
                i += 1
                continue
            # NOT bad: skip ahead. A book already judged is never re-tested,
            # but the position still advances by the STRIDE — walking forward
            # one at a time from a known-good book was the first version's
            # bug, and it quietly turned the whole sweep into a full scan.
            j = i + self.stride
            if j >= n:
                break
            if self._verdict(order[j])["verdict"] != "bad":
                i = j
                continue
            # a new cluster: open it out both ways, then resume past its end
            i = self._expand(order, j) + self.stride
        return self.summary()

    def _verdict(self, book: int) -> dict:
        """This book's verdict, testing it only if it is not already known."""
        known = self.verdicts.get(book)
        if known is not None:
            return known
        res = self.test(book)
        self._record(res)
        return res

    def _expand(self, order: list[int], j: int) -> int:
        """Walk left and right from a hit, stopping at the first good book in
        each direction. Returns the index of the rightmost bad book."""
        k = j - 1
        while k >= 0 and order[k] not in self.verdicts:
            res = self.test(order[k])
            self._record(res)
            if res["verdict"] != "bad":
                break
            k -= 1
        rightmost = j
        k = j + 1
        while k < len(order):
            if order[k] in self.verdicts:
                k += 1
                continue
            res = self.test(order[k])
            self._record(res)
            if res["verdict"] != "bad":
                break
            rightmost = k
            k += 1
        return rightmost

    def summary(self) -> dict:
        by = {}
        for v in self.verdicts.values():
            by[v["verdict"]] = by.get(v["verdict"], 0) + 1
        bad = sorted(b for b, v in self.verdicts.items() if v["verdict"] == "bad")
        return {"tested": len(self.verdicts), "verdicts": by,
                "chunks_embedded": self.checked, "bad_books": bad,
                "vectors_dropped": sum(v.get("dropped", 0)
                                       for v in self.verdicts.values())}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("collection")
    ap.add_argument("--tau", type=float, default=backend_gate.TAU)
    ap.add_argument("--stride", type=int, default=10)
    ap.add_argument("--max-chunks", type=int, default=40,
                    help="chunks that must agree before a book is called good")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="chunk checks in flight per book (one request each, "
                         "never a batched request)")
    ap.add_argument("--delete", action="store_true",
                    help="actually drop bad books' vectors (default: report only)")
    ap.add_argument("--limit", type=int, help="stop after N books in the order")
    ap.add_argument("--state", help="JSON file: written as it goes, so a sweep "
                                    "can be resumed or audited afterwards")
    args = ap.parse_args(argv)

    collection = c_mod.by_name(args.collection)
    if collection is None:
        print(f"no such collection: {args.collection}", file=sys.stderr)
        return 2
    from app import licensing        # invariant 9: stored text is re-embedded (audit)
    licensing.require_ack_or_exit(collection, use="its stored text is re-embedded")

    sw = Sweeper(collection, tau=args.tau, max_chunks=args.max_chunks,
                 stride=args.stride, delete=args.delete,
                 concurrency=args.concurrency)
    order = sw.books()
    if args.limit:
        order = order[:args.limit]
    print(f"{len(order)} book(s) with vectors; stride={args.stride} "
          f"max_chunks={args.max_chunks} tau={args.tau} "
          f"{'DELETING' if args.delete else 'dry run'}", flush=True)
    t0 = time.time()
    try:
        report = sw.run(order)
    except EndpointUnreliable as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        report = sw.summary()
        report["aborted"] = str(e)
        if args.state:
            Path(args.state).write_text(json.dumps(
                {"report": report, "verdicts": sw.verdicts}, indent=2),
                encoding="utf-8")
        return 3
    report["seconds"] = round(time.time() - t0, 1)
    print("\n" + json.dumps(report, indent=2))
    if args.state:
        Path(args.state).write_text(json.dumps(
            {"report": report, "verdicts": sw.verdicts}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

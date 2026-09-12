"""Re-clean `documents.title` in place, and the catalog index with it.

WHY. Gutenberg's parser let MARC subfield delimiters out of PG's RDF into
4,648 of 79,286 titles (5.86%, measured 2026-09-12) — the `… : $b
subtitle` form that 245$b marks — and 09 §9.5 says `title` is "cleaned".
Parser 6 fixes what it writes NEXT; the titles already in a manifest stay
as they are until something re-parses them, and they are what a reader is
looking at. This applies the same function to the stored value, so no
re-parse and no corpus read is needed.

WHAT IT TOUCHES. `documents.title` and `catalog_fts` — and nothing else.
Not `updated_at`: on this table that column is a PIPELINE signal, and
genesis's candidate scan reads it into `ShardDemand.oldest`
(app/bundles/genesis.py:389), the ordering that decides which archive
shard is fetched next. Not `content_hash` either — `title` IS inside it,
but that hash belongs to a BUILT record and this collection's records are
rebuilt for v1.0 anyway (docs/notes/v1-rebuild.md).

The catalog write mirrors `worker._rdf_one` exactly: INSERT OR REPLACE by
the document's rowid, never DELETE ... WHERE uid, because `uid` is
UNINDEXED and that scans the whole shadow table (it was 14.02 of rdf's
14.25 ms/doc at 36k rows, and grows linearly).

Never runs automatically; an operator invokes it (CLAUDE.md invariant 3,
"never silently").

Usage:
  python -m tools.clean_titles [--collection gutenberg] [--dry-run] [--limit N]

Idempotent: a second run finds nothing to do.
"""
import argparse
import sys
import time

from app import collections as collections_mod, db
from app.plugins.gutenberg.rdf import clean_title

#: The cleaning can only change a title that has a `$` or abnormal
#: whitespace, so the candidates come from four LIKE probes rather than a
#: scan of every row — pubmed's manifest has 40M of them, and a tool that
#: reads all of them to fix none is the per-item cost CLAUDE.md warns
#: about. Every candidate is then checked properly, so the probes may be
#: over-broad but never under-broad.
_CANDIDATES = ("SELECT rowid, uid, title, authors, subjects, bookshelves "
               "FROM documents WHERE title IS NOT NULL AND title != '' AND ("
               "title LIKE '%$%' OR title LIKE ' %' OR title LIKE '% ' "
               "OR title LIKE '%  %' OR title LIKE '%' || char(10) || '%' "
               "OR title LIKE '%' || char(9) || '%')")

BATCH = 500


def run(con, *, dry_run: bool = False, limit: int | None = None) -> tuple[int, int]:
    """Returns (titles changed, candidates examined)."""
    rows = con.execute(_CANDIDATES).fetchall()
    if limit is not None:
        rows = rows[:limit]
    changed = [(r, clean_title(r["title"])) for r in rows]
    changed = [(r, t) for r, t in changed if t and t != r["title"]]
    print(f"{len(rows)} candidate(s) examined, {len(changed)} title(s) to clean"
          f"{'  [DRY RUN]' if dry_run else ''}", flush=True)
    for r, new in changed[:5]:
        print(f"  {r['uid']}\n    - {r['title'][:96]!r}\n    + {new[:96]!r}")
    if len(changed) > 5:
        print(f"  … and {len(changed) - 5} more")
    if dry_run or not changed:
        return 0, len(rows)

    done = 0
    for i in range(0, len(changed), BATCH):
        batch = changed[i:i + BATCH]
        con.execute("BEGIN IMMEDIATE")
        try:
            con.executemany("UPDATE documents SET title=? WHERE uid=?",
                            [(t, r["uid"]) for r, t in batch])
            # by ROWID, as worker._rdf_one does
            con.executemany(
                "INSERT OR REPLACE INTO catalog_fts"
                "(rowid,uid,title,authors,subjects,bookshelves) VALUES(?,?,?,?,?,?)",
                [(r["rowid"], r["uid"], t, r["authors"], r["subjects"],
                  r["bookshelves"]) for r, t in batch])
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        done += len(batch)
        print(f"  {done}/{len(changed)}", flush=True)
    return done, len(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--collection", default="gutenberg")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    loaded = collections_mod.load_all(skip_unreadable=True)
    cols = loaded[0] if isinstance(loaded, tuple) else loaded
    col = next((c for c in cols if c.name == args.collection), None)
    if col is None:
        raise SystemExit(f"no such collection: {args.collection!r} "
                         f"(have {[c.name for c in cols]})")
    t0 = time.monotonic()
    done, seen = run(db.connect(collection=col), dry_run=args.dry_run,
                     limit=args.limit)
    print(f"\ndone: {done:,} title(s) cleaned of {seen:,} examined, "
          f"{time.monotonic() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

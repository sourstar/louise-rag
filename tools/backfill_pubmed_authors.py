"""Fill in `documents.authors` for an already-built pubmed collection.

WHY THIS EXISTS. `PubmedSource.parse()` has set `creators=[]` since the
first commit and `medline.parse_article` never parsed authors at all, so
`plugin_io.metadata`'s fallback computed `authors=""` and every pubmed
document displayed no author — 39,994,988 of them, measured 2026-09-12.
The origin XML carried the authors the whole time and `pack.authors_of`
has been extracting them into each container's Parquet pack since the
pack format existed, which is why this needs no re-fetch and no re-parse
of anything we did not already have.

WHAT IT DOES NOT TOUCH. Only `documents.authors` (and `updated_at`).
Records, `content_hash`, chunks, vectors, `space_id`, snapshots and
signatures are all untouched, so the collection stays verifiable exactly
as it is: `hold records build` reads `plugin_io.canonical_doc`, never
`plugin_io.metadata`. Correcting `creators` in the RECORDS is a separate
decision — it bumps PARSER_VERSION into `collection_id` and forks the
collection (operator, 2026-09-12: hotfix the display, leave the records).

Never runs automatically; an operator invokes it (CLAUDE.md invariant 3,
"never silently").

Usage:
  python -m tools.backfill_pubmed_authors [--collection pubmed] [--dry-run]
         [--start-at pubmed26n0500.xml.gz] [--limit N] [--force] [--quiet]

Idempotent and resumable. One container per transaction, so a container
is either wholly done or wholly untouched — which is what makes the
skip-probe below sound. Interrupt it whenever; re-run it to continue.
"""
import argparse
import sys
import time
from pathlib import Path

from app import collections as collections_mod, db
from app.plugins.pubmed import pack

PROBE = 5          # pmids sampled to decide a container is already done


def containers_of(corpus_root: Path) -> list[Path]:
    """Every container, in the order `PubmedSource.discover` walks them."""
    if not corpus_root.is_dir():
        raise SystemExit(f"not a directory: {corpus_root}")
    return sorted(p for p in corpus_root.iterdir()
                  if p.is_file() and (p.name.endswith(".xml")
                                      or p.name.endswith(".xml.gz")))


def already_done(con, source: str, pmids: list[str]) -> bool:
    """True when this container's rows already carry authors.

    Sound because each container commits in one transaction: there is no
    half-written container to be fooled by. Every pmid offered here is
    one the PACK has authors for, so a populated row can only have come
    from a previous run. A pmid with no row at all (out of scope, or
    never imported) is no evidence either way and is skipped.
    """
    for pmid in pmids[:PROBE]:
        row = con.execute("SELECT authors FROM documents "
                          "WHERE source=? AND native_id=?", (source, pmid)).fetchone()
        if row is None:
            continue
        return bool((row["authors"] or "").strip())
    return False


def run(con, col, *, dry_run: bool = False, start_at: str | None = None,
        limit: int | None = None, force: bool = False,
        quiet: bool = False) -> tuple[int, int, int]:
    """Do the work against an already-resolved collection and connection.

    Separate from `main` so it can be driven from a test without a real
    config tree, and so the only thing `main` owns is resolving which
    collection the operator meant.

    Returns (rows updated, containers skipped as already done, authored
    records read).
    """
    source = col.plugin
    todo = containers_of(col.corpus_dir)
    if start_at:
        todo = [p for p in todo if p.name >= start_at]
    if limit is not None:
        todo = todo[:limit]

    print(f"{col.name}: {len(todo)} containers under {col.corpus_dir}"
          f"{'  [DRY RUN]' if dry_run else ''}", flush=True)

    t0 = time.monotonic()
    updated = skipped_containers = seen = 0
    last = None
    for i, container in enumerate(todo, 1):
        try:
            pairs = pack.record_authors(container)      # builds the pack if absent
        except Exception as e:                           # noqa: BLE001
            # one bad container costs one line, never the run — the same
            # rule discover() follows for a truncated download
            print(f"  !! {container.name}: {type(e).__name__}: {e}", flush=True)
            continue
        seen += len(pairs)
        if not pairs:
            continue
        if not force and already_done(con, source, list(pairs)):
            skipped_containers += 1
            last = container.name
            continue
        if not dry_run:
            # `authors` AND NOTHING ELSE — deliberately not `updated_at`.
            # On this table updated_at tracks PIPELINE progress, and
            # genesis's candidate scan feeds it into
            # `ShardDemand.oldest`, which orders which archive shard is
            # fetched next (app/bundles/genesis.py:389). Bumping 40M rows
            # would perturb that ordering to fill a display column. It
            # would be harmless today — pubmed's 37 shards are all final
            # — but "harmless because nothing is running" is not a
            # property worth depending on.
            guard = "" if force else " AND (authors IS NULL OR authors='')"
            con.execute("BEGIN IMMEDIATE")
            try:
                cur = con.executemany(
                    f"UPDATE documents SET authors=? "
                    f"WHERE source=? AND native_id=?{guard}",
                    [(a, source, pmid) for pmid, a in pairs.items()])
                updated += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        last = container.name
        if not quiet or i % 50 == 0 or i == len(todo):
            el = time.monotonic() - t0
            rate = i / el if el else 0
            eta = (len(todo) - i) / rate if rate else 0
            print(f"  [{i}/{len(todo)}] {container.name}  "
                  f"+{len(pairs)} authored  total updated {updated:,}  "
                  f"{el/60:.1f}m elapsed, ETA {eta/60:.0f}m", flush=True)

    el = time.monotonic() - t0
    print(f"\ndone: {updated:,} rows updated, {seen:,} authored records read, "
          f"{skipped_containers} containers already done, {el/60:.1f} min")
    if last and not dry_run:
        print(f"resume after an interruption with --start-at {last}")
    return updated, skipped_containers, seen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--collection", default="pubmed")
    ap.add_argument("--dry-run", action="store_true",
                    help="read the packs and count, write nothing")
    ap.add_argument("--start-at", default=None, metavar="NAME",
                    help="skip containers sorting before this filename")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="stop after N containers (for a timed sample)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite authors already present")
    ap.add_argument("--quiet", action="store_true", help="one line per 50 containers")
    args = ap.parse_args(argv)

    loaded = collections_mod.load_all(skip_unreadable=True)
    cols = loaded[0] if isinstance(loaded, tuple) else loaded
    col = next((c for c in cols if c.name == args.collection), None)
    if col is None:
        raise SystemExit(f"no such collection: {args.collection!r} "
                         f"(have {[c.name for c in cols]})")
    from app import licensing        # invariant 9: the corpus is parsed (audit)
    licensing.require_ack_or_exit(col, use="its corpus is parsed")
    run(db.connect(collection=col), col, dry_run=args.dry_run,
        start_at=args.start_at, limit=args.limit, force=args.force,
        quiet=args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())

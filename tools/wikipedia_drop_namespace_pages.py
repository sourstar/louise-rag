"""Remove the wikipedia documents that are not articles, admitted before
discovery read each wiki's own namespace names.

WHY THIS EXISTS. Discovery skipped non-article pages by an English list of
namespace names (`File:`, `Category:`, `Template:`…), so on ten of the
eleven wikis `Kategorie:`, `Catégorie:`, `Категория:`, `رده:` and the rest
entered the collection as articles: 108 of 440 sampled dump-layer pages
(2026-09-15), about 9 M of its 38 M dump-layer documents. Discovery now
reads the names from each dump's <siteinfo> header
(`source.namespace_prefixes`); this removes what the old list let in.

WHAT IT REMOVES. A manifest row whose page is, by its dump's index and the
dump's own namespace table, not an article (eswiki's `Anexo` IS one:
`source._CONTENT_NAMESPACES`) — and only while nothing has
been done with it: rdf, fts and embed undone, no error. Such a row is a
pointer into the dump and nothing else; no catalog entry, text index or
vector refers to it. A row that HAS moved on is counted and kept: removing
it takes more than deleting the row.

Each batch is deleted in its own BEGIN IMMEDIATE transaction that re-checks
the untouched condition, so a row the running worker has begun is kept.
Deleting renumbers no other row (invariant 6: nothing VACUUMs a manifest).

Never runs automatically; an operator invokes it.

Usage:
  python -m tools.wikipedia_drop_namespace_pages [--collection wikipedia] [--apply]

A dry run unless --apply: it counts per wiki and namespace, writes nothing.
Idempotent: a second run finds nothing left to remove.
"""
import argparse
import bz2
import sys
import time
from collections import Counter
from pathlib import Path

from app import collections as collections_mod, db
from app.plugins import walk
from app.plugins.wikipedia import source as wsrc
from app.plugins.wikipedia.ids import LANG_ORDINAL, native_id_of

#: rows per transaction: short, so the running worker's writers wait on
#: this for a moment at a time, never for long
BATCH = 1000

_HELD = ("SELECT rdf_done, fts_done, embed_done, error FROM documents "
         "WHERE source=? AND native_id=?")
_DELETE = ("DELETE FROM documents WHERE source=? AND native_id=? AND rdf_done=0 "
           "AND fts_done=0 AND embed_done=0 AND error IS NULL")


def dumps_of(col) -> list[tuple[Path, Path, str]]:
    """(dump, index, language) for every dump discovery reads — the pinned
    snapshot's, of the collection's languages."""
    dumps = walk.files(col.corpus_dir, f"*{wsrc._DUMP_SUFFIX}")      # through its links
    if getattr(col, "snapshot", None):
        dumps = [d for d in dumps if d.parent.name == col.snapshot]
    out = []
    for d in dumps:
        lang = wsrc._lang_of(d.name)
        index = d.with_name(d.name.replace(wsrc._DUMP_SUFFIX, wsrc._INDEX_SUFFIX))
        if lang in LANG_ORDINAL and index.is_file():
            out.append((d, index, lang))
    return out


def non_articles(dump: Path, index: Path):
    """(page id, namespace name) for every page of the dump whose title
    carries one of the wiki's own non-article namespaces."""
    names = wsrc.namespace_prefixes(dump, index)
    if not names:
        raise SystemExit(f"{dump.name}: its header names no namespaces; "
                         f"nothing is removed on a guess")
    with bz2.open(index, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            _offset, _, rest = line.rstrip("\n").partition(":")
            page_id, _, title = rest.partition(":")
            if page_id.isdigit() and title.startswith(names):
                yield int(page_id), title.split(":", 1)[0]


def _delete(con, source: str, nids: list[str], apply: bool) -> int:
    if not apply:
        return len(nids)
    con.execute("BEGIN IMMEDIATE")
    try:
        cur = con.executemany(_DELETE, [(source, n) for n in nids])
        gone = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        con.executemany("DELETE FROM embed_queue WHERE uid=?",
                        [(f"{source}:{n}",) for n in nids])
        con.execute("COMMIT")
        return gone
    except BaseException:
        con.execute("ROLLBACK")
        raise


def run(con, col, *, apply: bool = False) -> dict:
    """Against an already-resolved collection and connection. Returns
    {"found", "removed", "kept"}: non-article rows held, rows removed (or,
    in a dry run, that would be), rows kept because they have moved on."""
    source = col.plugin
    found, kept, removed = Counter(), Counter(), Counter()
    t0 = time.monotonic()
    print(f"{col.name}: non-article pages{'' if apply else '  [DRY RUN]'}", flush=True)
    for dump, index, lang in dumps_of(col):
        pending: list[str] = []
        for page_id, ns in non_articles(dump, index):
            nid = str(native_id_of(lang, page_id))
            row = con.execute(_HELD, (source, nid)).fetchone()
            if row is None:
                continue                    # never admitted
            found[(lang, ns)] += 1
            if row["rdf_done"] or row["fts_done"] or row["embed_done"] or row["error"]:
                kept[(lang, ns)] += 1
                continue
            pending.append(nid)
            if len(pending) >= BATCH:
                removed[lang] += _delete(con, source, pending, apply)
                pending = []
        if pending:
            removed[lang] += _delete(con, source, pending, apply)
        by_ns = ", ".join(f"{ns} {n:,}" for (lg, ns), n in found.most_common() if lg == lang)
        print(f"  {lang}: {sum(n for (lg, _), n in found.items() if lg == lang):,} held "
              f"({by_ns or 'none'}); {'removed' if apply else 'would remove'} "
              f"{removed[lang]:,}; kept {sum(n for (lg, _), n in kept.items() if lg == lang):,} "
              f"that moved on  [{(time.monotonic() - t0) / 60:.1f} min]", flush=True)
    total = {"found": sum(found.values()), "removed": sum(removed.values()),
             "kept": sum(kept.values())}
    print(f"done: {total['found']:,} held, {total['removed']:,} "
          f"{'removed' if apply else 'would be removed'}, {total['kept']:,} kept")
    return total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--collection", default="wikipedia")
    ap.add_argument("--apply", action="store_true",
                    help="delete; without it, count and write nothing")
    args = ap.parse_args(argv)
    loaded = collections_mod.load_all(skip_unreadable=True)
    cols = loaded[0] if isinstance(loaded, tuple) else loaded
    col = next((c for c in cols if c.name == args.collection), None)
    if col is None:
        raise SystemExit(f"no such collection: {args.collection!r} "
                         f"(have {[c.name for c in cols]})")
    if col.plugin != "wikipedia":
        raise SystemExit(f"{col.name} is a {col.plugin!r} collection, not wikipedia")
    from app import licensing        # invariant 9: the corpus's index is read
    licensing.require_ack_or_exit(col, use="its corpus is read")
    run(db.connect(collection=col), col, apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""tests/test_discovery_lang.py — discovery records `lang`, so the
planning pass does not have to parse the corpus to find it.

**Measured 2026-09-10, running step 8's gutenberg build for real.**
`pack_build.plan` asks `slicing.slice_hint` for each document's slice and
falls back to a full `plugin_io.canonical_doc` parse when the hint cannot
answer. Gutenberg slices by `lang`; `slice_hint` reads it from the
manifest row; and nothing wrote it there until the rdf stage, long after
`records build` needs it.

So on a fresh manifest the hint never fired: pass 1 parsed all 77,866
books SINGLE-THREADED at 19.2 documents/s — 68 minutes — and pass 2 then
parsed the same corpus again through the parse pool.

Every other plugin's hint answers from the locator or a derived index
(`wikipedia` from the dump name, `embedded` from the encoded native id,
`pubmed` from its container's date index). Gutenberg's was the one that
could not, and this is why.
"""
import sqlite3

import pytest

from app import db, discovery
from app.pack import slicing


def test_the_gutenberg_scan_reports_a_language(corpus_dir):
    """Straight from the plugin: the DocRef carries `lang` in `extra`,
    beside `txt_name`, which is the channel discovery already reads."""
    from app.plugins.base import DiscoverState
    from app.plugins.gutenberg.source import GutenbergSource

    refs = [r for r in GutenbergSource().discover(
        DiscoverState(corpus_root=corpus_dir, state_dir=corpus_dir))
        if not r.error and r.scope != "container"]
    assert refs, "the mini corpus discovered nothing"
    assert all("lang" in r.extra for r in refs)
    assert all(r.extra["lang"] for r in refs), "a language is never empty"


def test_discovery_writes_it_to_the_manifest(isolated_config, corpus_dir,
                                             data_dir):
    from app import collections as collections_mod
    c = collections_mod.default_collection()
    con = db.connect(c)
    try:
        discovery.sync_collection(con, c)
        rows = con.execute(
            "SELECT uid, lang FROM documents WHERE error IS NULL").fetchall()
        assert rows, "nothing discovered"
        assert all(r["lang"] for r in rows), \
            "lang is NULL — slice_hint will fall back to a full parse"
    finally:
        con.close()


def test_slice_hint_now_answers_without_a_parse(isolated_config, corpus_dir):
    """The point of the whole change. A hint that returns None is a
    document the planning pass parses."""
    from app import collections as collections_mod
    c = collections_mod.default_collection()
    con = db.connect(c)
    try:
        discovery.sync_collection(con, c)
        rows = con.execute(
            "SELECT * FROM documents WHERE error IS NULL").fetchall()
        for row in rows:
            hint = slicing.slice_hint("gutenberg", row)
            assert hint is not None, f"{row['uid']}: no hint, so a full parse"
            assert hint.startswith("lang=")
    finally:
        con.close()


def test_a_rescan_does_not_lose_it(isolated_config, corpus_dir):
    """Discovery runs on every scan and the second one takes the
    `unchanged` path. A language written once must stay written."""
    from app import collections as collections_mod
    c = collections_mod.default_collection()
    con = db.connect(c)
    try:
        discovery.sync_collection(con, c)
        before = {r["uid"]: r["lang"] for r in
                  con.execute("SELECT uid, lang FROM documents")}
        discovery.sync_collection(con, c)
        after = {r["uid"]: r["lang"] for r in
                 con.execute("SELECT uid, lang FROM documents")}
        assert after == before
    finally:
        con.close()


def test_a_plugin_that_supplies_no_language_still_discovers(isolated_config,
                                                            corpus_dir,
                                                            monkeypatch):
    """`lang` is OPTIONAL in the ref: pubmed, wikipedia and embedded supply
    none and answer their hints another way. A missing one must write NULL,
    which is what every row held before this change, and not fail."""
    from app import collections as collections_mod
    from app.plugins.gutenberg.source import GutenbergSource

    real = GutenbergSource.discover

    def no_lang(self, state):
        for ref in real(self, state):
            ref.extra.pop("lang", None)
            yield ref

    monkeypatch.setattr(GutenbergSource, "discover", no_lang)
    c = collections_mod.default_collection()
    con = db.connect(c)
    try:
        discovery.sync_collection(con, c)
        rows = con.execute(
            "SELECT lang FROM documents WHERE error IS NULL").fetchall()
        assert rows and all(r["lang"] is None for r in rows)
    finally:
        con.close()

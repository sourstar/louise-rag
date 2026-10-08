"""The license gate for EVERY way data arrives (invariant 9, amendment
36): a yellow collection without its acknowledgement is not packed, not
scanned and not fed by an archive — however the data arrived.

The packed-collection half of this file (a pack's own terms, the create
dialog over a pack) is boxed with the `packed` plugin in
attic/tests/test_license_gate.py (2026-09-14).
"""
from dataclasses import replace
from pathlib import Path

import pytest

from app import collections as collections_mod, db, licensing, worker
from app.pack import build as pack_build

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _coll(tmp_path, name, plugin, corpus_dir, **kw):
    d = tmp_path / name
    return collections_mod.Collection(
        name=name, plugin=plugin, profile=None, embed_mode_default=None,
        corpus_dir=Path(corpus_dir), manifest_path=d / "manifest.db",
        fts_dir=d / "fts", lance_dir=d / "lancedb", **kw)


@pytest.fixture
def pubmed_origin(isolated_config, tmp_path):
    """A yellow collection with no acknowledgement recorded."""
    c = _coll(tmp_path, "pubmed", "pubmed", FIXTURES / "pubmed-mini")
    assert licensing.state_for(c) == licensing.MISSING
    return c


def test_pack_build_refuses_a_yellow_collection_without_its_ack(pubmed_origin, tmp_path):
    with pytest.raises(pack_build.BuildError, match="acknowledgement"):
        pack_build.build(pubmed_origin, tmp_path / "packs" / "refused", sign=False,
                         log=lambda *_: None)
    acked = replace(pubmed_origin, license_ack=licensing.make_ack(pubmed_origin))
    assert licensing.state_for(acked) == licensing.ACKNOWLEDGED
    with pytest.raises(Exception, match="requires an acknowledgement"):
        licensing.require_ack(pubmed_origin, use="use")
    # green needs nothing; the gate never blocks it
    green = _coll(tmp_path, "g", "gutenberg", tmp_path / "corpus")
    assert licensing.state_for(green) == licensing.NOT_REQUIRED


def test_the_worker_withholds_the_scan_and_the_archive_without_an_ack(pubmed_origin):
    con = db.connect(pubmed_origin)
    worker._license_nagged.clear()
    worker._discover(con, pubmed_origin, "test")
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0, "not scanned"
    events = [r["message"] for r in con.execute("SELECT message FROM events WHERE level='warn'")]
    assert any("corpus scan withheld" in m and "invariant 9" in m for m in events)
    assert worker._drain_remote_archive(
        con, replace(pubmed_origin, genesis_archive="medcpt-pubmed"), None) is False
    # acknowledged: the scan runs
    acked = replace(pubmed_origin, license_ack=licensing.make_ack(pubmed_origin))
    worker._discover(con, acked, "test")
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] > 0
    con.close()


def test_an_archive_shard_already_on_disk_is_not_a_bypass(pubmed_origin, tmp_path):
    """Invariant 9's ingestion half, on the path that had no gate.

    `genesis_fetch` refuses to FETCH an archive without an ack, which
    covers the bytes that move over the network. It does not cover a shard
    that is already on disk — a local archive whose vectors travel with
    the corpus, a hand-populated cache directory, a peer copy. The
    invariant names exactly those: "hand-populated folders and peer copies
    are not a bypass (observed live: a peer-copied shard was indexed and
    imported ack-free)". What it gates is USE, not transport.

    So the check belongs at `import_shard`, which every path reaches:
    `import_archive` walks through it and `louise hold vectors import
    --archive` is the only caller
    of either.
    """
    from app.bundles import genesis

    con = db.connect(pubmed_origin)
    archive = genesis.ARCHIVES["medcpt-pubmed"]

    def never(*a, **k):                       # nothing may be read
        raise AssertionError("the shard was read before the licence was checked")

    with pytest.raises(Exception, match="requires an acknowledgement"):
        genesis.import_shard(con, pubmed_origin, archive, "0", fetch=never)
    with pytest.raises(Exception, match="requires an acknowledgement"):
        genesis.import_archive(con, pubmed_origin, archive, shards=["0"], fetch=never)

    # Nothing was recorded either: a refused import leaves no cursor row
    # to short-circuit a later, acknowledged one.
    assert con.execute("SELECT COUNT(*) FROM genesis_imports").fetchone()[0] == 0

    # And a green collection is never blocked by it.
    green = _coll(tmp_path, "g-arch", "gutenberg", tmp_path / "corpus")
    assert licensing.state_for(green) == licensing.NOT_REQUIRED
    con.close()

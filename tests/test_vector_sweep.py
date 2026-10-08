"""The clustered sweep (tools/vector_sweep) — the search, and the safeguard.

The tool DELETES vectors, so the two things worth pinning are that it walks
the order the operator specified, and that it cannot be talked into deleting
a healthy book by one bad draw from an encoder that has a history of them.
"""
import hashlib

import numpy as np
import pytest

from app import collections as collections_mod, db, discovery, embeddings, worker
from tools import vector_sweep as vs


@pytest.fixture
def collection(tmp_path, isolated_config, corpus_dir):
    c = collections_mod.Collection(
        name="books", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus_dir, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance", embed=None)
    con = db.connect(c)
    discovery.sync_collection(con, c)
    con.execute("UPDATE documents SET in_scope=1, rdf_done=1, fts_done=1, embed_done=1")
    con.commit()
    con.close()
    return c


def good_vec(text):
    """hashlib, not hash(): str hashing is salted per process, so `hash()`
    makes a test of reproducibility itself irreproducible."""
    h = int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(),
                       "big")
    return [float((h >> (8 * i)) & 0xFF) + 1.0 for i in range(4)]


@pytest.fixture
def seeded(collection, monkeypatch):
    """Every book embedded with the honest function."""
    # at the client's lowest level, which `sweeper` patches again with its
    # draws: a patch above it would answer for the sweep too
    monkeypatch.setattr(embeddings, "_embed_batch",
                        lambda batch, timeout, recipe, **kw: [good_vec(t) for t in batch])
    con = db.connect(collection)
    for row in con.execute("SELECT * FROM documents").fetchall():
        embeddings.upsert_book(int(row["native_id"]), row["lang"],
                               worker._load_chunks(row, collection), collection)
    con.close()
    return collection


def corrupt(collection, book_id):
    """Rewrite one book's stored vectors with a different answer."""
    store = embeddings._conn(collection)
    tbl = store.open_table(embeddings.TABLE)
    rows = tbl.search().where(f"book_id = {book_id}").limit(10_000).to_list()
    tbl.delete(f"book_id = {book_id}")
    for r in rows:
        r["vector"] = np.asarray([0.0, 0.0, 0.0, 1.0], dtype=np.float32)
    tbl.add(rows)


def sweeper(collection, monkeypatch, *, delete=False, draws=None, **kw):
    """`draws` lets a test make the ENCODER misbehave instead of the store."""
    seq = list(draws or [])

    def fake(batch, timeout, recipe):
        if seq:
            return [seq.pop(0)]
        return [good_vec(batch[0])]

    monkeypatch.setattr(embeddings, "_embed_batch", fake)
    opts = dict(tau=0.995, max_chunks=40, stride=10, delete=delete)
    opts.update(kw)
    return vs.Sweeper(collection, **opts)


# ------------------------------------------------------------------ testing

def test_a_clean_book_reads_good(seeded, monkeypatch):
    sw = sweeper(seeded, monkeypatch)
    books = sw.books()
    assert sw.test(books[0])["verdict"] == "good"


def test_a_corrupt_book_reads_bad_and_stops_at_the_first_chunk(seeded, monkeypatch):
    books = [int(r["native_id"]) for r in
             db.connect(seeded).execute("SELECT native_id FROM documents")]
    corrupt(seeded, books[0])
    sw = sweeper(seeded, monkeypatch)
    res = sw.test(books[0])
    assert res["verdict"] == "bad"
    # it stopped early: one comparison, plus the two confirmation draws
    assert res["checked"] == 1
    assert sw.checked == 3


def test_deleting_drops_the_vectors_and_requeues_the_document(seeded, monkeypatch):
    con = db.connect(seeded)
    book = int(con.execute("SELECT native_id FROM documents").fetchone()["native_id"])
    corrupt(seeded, book)
    sw = sweeper(seeded, monkeypatch, delete=True)
    sw._record(sw.test(book))
    store = embeddings._conn(seeded)
    left = store.open_table(embeddings.TABLE).search().where(
        f"book_id = {book}").limit(100).to_list()
    assert left == [], "the corrupt vectors are still there"
    row = con.execute("SELECT embed_done FROM documents WHERE CAST(native_id AS INTEGER)=?",
                      (book,)).fetchone()
    assert row["embed_done"] == 0, "the document must be re-embedded later"
    con.close()


def test_a_dry_run_deletes_nothing(seeded, monkeypatch):
    con = db.connect(seeded)
    book = int(con.execute("SELECT native_id FROM documents").fetchone()["native_id"])
    corrupt(seeded, book)
    sw = sweeper(seeded, monkeypatch, delete=False)
    sw._record(sw.test(book))
    store = embeddings._conn(seeded)
    assert store.open_table(embeddings.TABLE).search().where(
        f"book_id = {book}").limit(100).to_list(), "dry run deleted vectors"
    assert con.execute("SELECT embed_done FROM documents WHERE CAST(native_id AS INTEGER)=?",
                       (book,)).fetchone()["embed_done"] == 1
    con.close()


# ---------------------------------------------------------- THE SAFEGUARD

def test_one_bad_DRAW_does_not_condemn_a_healthy_book(seeded, monkeypatch):
    """The failure this prevents: the encoder returns one wrong vector, the
    book is called bad, and its perfectly good embeddings are deleted."""
    books = [int(r["native_id"]) for r in
             db.connect(seeded).execute("SELECT native_id FROM documents")]
    # the FIRST draw is wrong; the confirming draws are honest
    sw = sweeper(seeded, monkeypatch, delete=True,
                 draws=[[0.0, 0.0, 0.0, 1.0]])
    res = sw.test(books[0])
    assert res["verdict"] == "good", "a single bad draw condemned a healthy book"
    store = embeddings._conn(seeded)
    assert store.open_table(embeddings.TABLE).search().where(
        f"book_id = {books[0]}").limit(100).to_list()


def test_an_unreliable_endpoint_aborts_instead_of_deleting(seeded, monkeypatch):
    """If two fresh draws of one text disagree, the encoder cannot be trusted
    to judge anything — deleting on its word would be the worst outcome."""
    books = [int(r["native_id"]) for r in
             db.connect(seeded).execute("SELECT native_id FROM documents")]
    corrupt(seeded, books[0])
    sw = sweeper(seeded, monkeypatch, delete=True,
                 draws=[[0.0, 0.0, 0.0, 1.0],      # detection
                        [1.0, 0.0, 0.0, 0.0],      # confirm A
                        [0.0, 1.0, 0.0, 0.0]])     # confirm B — disagrees with A
    with pytest.raises(vs.EndpointUnreliable):
        sw.test(books[0])


# --------------------------------------------------------------- the walk

class FakeSweeper(vs.Sweeper):
    """Walk-order only: verdicts come from a set, no embedding at all."""

    def __init__(self, bad: set):
        self.bad = bad
        self.verdicts = {}
        self.stride = 10
        self.delete = False
        self.checked = 0
        self.order_tested: list[int] = []
        self.log = lambda *a, **k: None

    def test(self, book_id):
        self.order_tested.append(book_id)
        return {"book": book_id,
                "verdict": "bad" if book_id in self.bad else "good",
                "checked": 1, "chunk": "x", "cosine": 0.1, "chars": 10}

    def drop(self, book_id):
        return 0


def test_a_clean_run_only_samples_every_stride():
    sw = FakeSweeper(bad=set())
    order = list(range(100))
    sw.run(order)
    assert sw.order_tested == [0, 10, 20, 30, 40, 50, 60, 70, 80, 90]


def test_a_cluster_is_opened_out_in_both_directions():
    """Landing on 30 must find 28..33 by walking left and right, and then
    resume from the RIGHTMOST bad book plus the stride."""
    sw = FakeSweeper(bad={28, 29, 30, 31, 32, 33})
    sw.run(list(range(100)))
    assert {28, 29, 30, 31, 32, 33} <= set(sw.order_tested)
    assert 27 in sw.order_tested and 34 in sw.order_tested   # the stops
    assert 43 in sw.order_tested, "should resume at rightmost bad (33) + 10"


def test_a_bad_first_book_is_walked_one_by_one():
    sw = FakeSweeper(bad={0, 1, 2})
    sw.run(list(range(50)))
    assert sw.order_tested[:4] == [0, 1, 2, 3]


def test_every_bad_book_found_is_recorded():
    sw = FakeSweeper(bad={40, 41})
    report = sw.run(list(range(100)))
    assert report["bad_books"] == [40, 41]
    assert report["verdicts"]["bad"] == 2

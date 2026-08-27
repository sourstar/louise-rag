"""tools/vector_compliance — does a stored vector still reproduce?

The tool decides whether a corpus is tainted, so its own arithmetic and its
failure direction are worth pinning: a clean endpoint must read COMPLIANT,
a drifting one must read NOT COMPLIANT and exit non-zero, and a document
that was simply re-chunked must be distinguishable from a bad endpoint.
"""
import numpy as np
import pytest

from app import collections as collections_mod, db, discovery, embeddings, worker
from tools import vector_compliance as vc


@pytest.fixture
def collection(tmp_path, isolated_config, corpus_dir):
    c = collections_mod.Collection(
        name="books", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus_dir, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance", embed=None)
    con = db.connect(c)
    discovery.sync_collection(con, c)
    con.execute("UPDATE documents SET in_scope=1, rdf_done=1, fts_done=1")
    con.commit()
    con.close()
    return c


def embed_as(fn, monkeypatch):
    monkeypatch.setattr(embeddings, "_embed_batch",
                        lambda texts, timeout, recipe: [fn(t) for t in texts])


def stable(text):
    """A deterministic 'encoder': the same text always gives the same vector."""
    h = abs(hash(text))
    return [float((h >> (8 * i)) & 0xFF) + 1.0 for i in range(4)]


def embed_everything(collection, monkeypatch):
    con = db.connect(collection)
    rows = con.execute("SELECT * FROM documents").fetchall()
    for row in rows:
        chunks = worker._load_chunks(row, collection)
        embeddings.upsert_book(int(row["native_id"]), row["lang"], chunks,
                               collection)
        con.execute("UPDATE documents SET embed_done=1, updated_at=? WHERE uid=?",
                    (1, row["uid"]))
    con.commit()
    con.close()
    return rows


def test_a_reproducing_endpoint_reads_compliant(collection, monkeypatch):
    embed_as(stable, monkeypatch)
    embed_everything(collection, monkeypatch)
    report = vc.check_stored(collection, books=3, chunks=10, tau=0.995)
    assert report["overall"]["n"] > 0
    assert report["overall"]["below_tau"] == 0
    assert report["overall"]["min"] == pytest.approx(1.0)


def test_a_drifting_endpoint_reads_not_compliant(collection, monkeypatch):
    """The whole point: vectors written by yesterday's endpoint no longer
    verify against today's."""
    embed_as(stable, monkeypatch)
    embed_everything(collection, monkeypatch)

    def drifted(text):
        v = stable(text)
        return [v[1], v[0], v[3], v[2]]     # same magnitudes, different answer

    embed_as(drifted, monkeypatch)
    report = vc.check_stored(collection, books=3, chunks=10, tau=0.995)
    assert report["overall"]["below_tau"] == report["overall"]["n"]
    assert report["overall"]["min"] < 0.995


def test_the_exit_code_carries_the_verdict(collection, monkeypatch, capsys):
    """It has to be usable as a gate, not just as a report."""
    embed_as(stable, monkeypatch)
    embed_everything(collection, monkeypatch)
    # the fixture builds a Collection directly rather than through a TOML,
    # so main()'s name lookup has to be pointed at it
    monkeypatch.setattr(vc.c_mod, "by_name",
                        lambda name: collection if name == collection.name else None)
    assert vc.main([collection.name, "--books", "3", "--chunks", "5"]) == 0
    assert "COMPLIANT" in capsys.readouterr().out

    embed_as(lambda t: [1.0, 0.0, 0.0, 0.0], monkeypatch)
    assert vc.main([collection.name, "--books", "3", "--chunks", "5"]) == 1
    assert "NOT COMPLIANT" in capsys.readouterr().out


def test_a_rechunked_document_is_visible_as_such(collection, monkeypatch):
    """A low score has more than one explanation. If the document was
    re-chunked, its stored ids no longer match and THAT is the finding —
    reporting it as endpoint drift would send someone hunting a ghost."""
    embed_as(stable, monkeypatch)
    embed_everything(collection, monkeypatch)
    monkeypatch.setattr(worker, "_load_chunks", lambda row, c: [
        {"id": f"{row['uid']}:{i}:99", "seq": i, "chapter": "",
         "text": f"rechunked {i}"} for i in range(4)])
    report = vc.check_stored(collection, books=1, chunks=10, tau=0.995)
    book = report["books"][0]
    assert book["id_overlap"] == 0
    assert book.get("n", 0) == 0, "nothing comparable — not a drift verdict"


def test_repeat_mode_measures_the_endpoint_not_the_corpus(collection, monkeypatch):
    calls = {"n": 0}

    def jittery(text):
        calls["n"] += 1
        base = stable(text)
        return [base[0] + calls["n"] * 1e-6, *base[1:]]

    embed_as(jittery, monkeypatch)
    report = vc.check_repeat(collection, repeats=4, tau=0.995)
    assert set(report["cases"]) == {"short", "long", "non-ascii"}
    for case in report["cases"].values():
        assert case["n"] == 6                 # every PAIR of 4 draws
        assert case["byte_identical"] is False
        assert case["below_tau"] == 0         # jitter, not drift


def test_repeat_mode_records_the_concurrency_it_measured_at(collection, monkeypatch):
    """A reproducibility number without a concurrency is not a number: the
    approval trial measured 1.000000 on this build while a live workload
    measured 0.78, and `-np 4` is the difference between those machines."""
    embed_as(stable, monkeypatch)
    report = vc.check_repeat(collection, repeats=3, tau=0.995, concurrency=4)
    assert report["concurrency"] == 4
    assert all(c["below_tau"] == 0 for c in report["cases"].values())


def test_one_bad_draw_is_not_hidden_by_the_reference(collection, monkeypatch):
    """Comparing everything against draw 0 hides a bad draw whenever draw 0
    is itself the bad one. Every pair is compared."""
    seen = {"n": 0}

    def one_bad(text):
        seen["n"] += 1
        return [0.0, 1.0, 0.0, 0.0] if seen["n"] == 1 else [1.0, 0.0, 0.0, 0.0]

    embed_as(one_bad, monkeypatch)
    report = vc.check_repeat(collection, repeats=4, tau=0.995)
    assert report["cases"]["short"]["min"] == pytest.approx(0.0), \
        "the outlier was the reference draw and went unnoticed"

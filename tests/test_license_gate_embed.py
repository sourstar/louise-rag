"""Invariant 9 gates EMBEDDING, on every selector the worker uses.

`_next_book` gated its round — rdf, fts, and the embeds it selects — but
the main loop embeds through `_next_embed` and `_embed_candidates` whenever
pipeline threads own rdf/fts, which is the deployed configuration, and
neither asked. So a yellow or red collection without an acknowledgement
still had its documents embedded once they were past fts: use of the
corpus, which invariant 9 forbids "however the data arrived". Found by the
review of `louise health`, 2026-09-13.
"""
from app import collections as collections_mod, db, licensing, worker


def _collection(tmp_path):
    return collections_mod.Collection(
        name="pm", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")


def test_the_embed_selectors_withhold_an_unacknowledged_collection(isolated_config,
                                                                   tmp_path, monkeypatch):
    c = _collection(tmp_path)
    con = db.connect(c)
    con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,rdf_done,"
                "fts_done,embed_done) VALUES('g:1','g','1','p',1,1,1,0)")
    con.execute("INSERT INTO embed_queue(uid,priority,ts) VALUES('g:1',100,0)")
    scope = {"embed_mode": "lazy"}

    # the control: with nothing to acknowledge, the queued document IS
    # selected — so what withholds it below is the gate, not an ineligible row
    monkeypatch.setattr(licensing, "state_for", lambda col: licensing.NOT_REQUIRED)
    stage, row = worker._next_embed(con, scope, c)
    assert stage == "embed" and row["uid"] == "g:1"
    assert [r["uid"] for r in worker._embed_candidates(con, scope, 4, c)] == ["g:1"]

    monkeypatch.setattr(licensing, "state_for", lambda col: licensing.MISSING)
    monkeypatch.setattr(licensing, "license_class_for", lambda col: "yellow")
    assert worker._next_embed(con, scope, c) == (None, None)
    assert worker._embed_candidates(con, scope, 4, c) == []

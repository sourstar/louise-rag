"""tests/deposit/test_deposit_erasure.py — an erase leaves no trace (02 §2.9;
migration step 15c).

A deposit carries a marker in its text and one in its key. Its text is
indexed, its vectors written; the markers are shown to be on disk; it is
erased; then no file anywhere under the node's root holds either marker —
not the text index (tantivy keeps deleted documents in shared segments),
not the vector table (Lance keeps deleted rows in deletion files and old
versions), not the manifest or its write-ahead log. The other deposits stay
searchable, text and vectors.
"""
import secrets

import numpy as np
import pytest

from app import collections as collections_mod, embeddings, fts, layers
from app.hold import deposit
from app.plugins.base import DocRef, RawDoc
from tests.deposit.test_deposit_identity import make, node  # noqa: F401


def _marker(tag: str) -> str:
    # letters only, no substring shared with any other text here, so a
    # compressor cannot hide it behind a back-reference
    return tag + "".join(secrets.choice("bcdfghjkmnpqrstvwxz") for _ in range(24))


def _embed_all(name: str) -> None:
    """Vectors for every deposit, as the worker's embed stage would write
    them — text included, which is exactly why Lance must be purged."""
    c = collections_mod.by_name(name)
    plugin = c.plugin_instance()
    from app.hold.deposit import _connect
    from contextlib import closing
    with closing(_connect(c)) as con:
        rows = con.execute("SELECT * FROM documents").fetchall()
        out = []
        for row in rows:
            ref = DocRef(source_id="deposit", native_id=row["native_id"], locator=row["path"])
            doc = plugin.parse(plugin.fetch(ref))
            for ch in layers.chunks_for_document(c, row, doc.sections):
                v = np.random.default_rng(ch["seq"]).standard_normal(768).astype("float16")
                out.append({"id": ch["id"], "book_id": int(row["native_id"]), "lang": "und",
                            "seq": ch["seq"], "chapter": ch["chapter"] or "",
                            "text": ch["text"], "vector": v})
        embeddings.upsert_chunks(out, collection=c, lock_con=con, dtype="f16")


def _files_holding(root, *markers) -> list[str]:
    hits = []
    for p in root.rglob("*"):
        if p.is_file():
            data = p.read_bytes()
            if any(m.encode("utf-8") in data for m in markers):
                hits.append(str(p.relative_to(root)))
    return hits


@pytest.fixture
def three(node):  # noqa: F811
    make(node, "alice")
    text_marker, key_marker = _marker("t"), _marker("k")
    deposit.add("alice", "keep-one.txt", b"Kept alpha document about gardening.\n")
    deposit.add("alice", f"{key_marker}.txt",
                f"Secret line {text_marker} to be forgotten.\n".encode())
    deposit.add("alice", "keep-two.md", b"# Kept\n\nKept beta note about cooking.\n")
    _embed_all("alice")
    root = node.parent                       # LOUISE_ROOT: every collection, node state
    return root, text_marker, key_marker


def test_the_markers_are_on_disk_before_the_erase(three):
    """The check below is only worth something if it would have caught them."""
    root, t, k = three
    hits = _files_holding(root, t)
    assert any(h.startswith("collections/alice/lance/") for h in hits), hits
    assert any(h.startswith("collections/alice/index/") for h in hits), hits
    assert any(h.startswith("collections/alice/origin/") for h in hits), hits
    assert _files_holding(root, k)


def test_an_erase_leaves_no_trace_anywhere_under_the_root(three):
    root, t, k = three
    key = f"{k}.txt"
    deposit.erase("alice", key)
    assert _files_holding(root, t, k) == []


def test_the_other_deposits_survive_whole(three):
    root, t, k = three
    key = f"{k}.txt"
    deposit.erase("alice", key)
    c = collections_mod.by_name("alice")
    assert deposit.keys("alice") == ["keep-one.txt", "keep-two.md"]
    assert fts.search("other", "gardening", collection=c)
    assert fts.search("other", "cooking", collection=c)
    tbl = embeddings._conn(c).open_table(embeddings.TABLE)
    texts = tbl.to_arrow().column("text").to_pylist()
    assert any("gardening" in x for x in texts) and any("cooking" in x for x in texts)
    assert not any(t in x for x in texts)


def test_a_replaced_deposit_leaves_no_trace_of_its_old_version(node):  # noqa: F811
    make(node, "alice")
    old = _marker("o")
    deposit.add("alice", "memo.txt", f"first draft {old}\n".encode())
    deposit.add("alice", "other.txt", b"a neighbour sharing the index segment\n")
    _embed_all("alice")
    deposit.add("alice", "memo.txt", b"second draft\n")
    assert _files_holding(node.parent, old) == []


def test_reset_erases_everything_with_one_purge(three, monkeypatch):
    root, t, k = three
    calls = []
    real = deposit.purge
    monkeypatch.setattr(deposit, "purge", lambda c, con: calls.append(1) or real(c, con))
    assert deposit.reset("alice") == 3
    assert calls == [1]
    assert deposit.keys("alice") == []
    assert _files_holding(root, t, k) == []
    for word in ("gardening", "cooking"):
        assert _files_holding(root, word) == [] or all(
            not h.startswith("collections/alice/") for h in _files_holding(root, word))


def test_every_table_that_names_a_document_is_erased_from(three):
    """`_keyed_tables` finds them by their columns, so a table added later
    that keys on the uid is erased from without anyone extending a list."""
    from contextlib import closing
    c = collections_mod.by_name("alice")
    with closing(deposit._connect(c)) as con:
        found = dict(deposit._keyed_tables(con))
    assert found.get("documents") == "uid"
    assert found.get("events") == "doc_uid"


def _deleted_docs(index_dir) -> int:
    """Documents tantivy still holds in a segment although they are deleted
    — its own count, from `meta.json`. Zero is what "gone from the disk"
    means for a tantivy index; a byte search cannot say it, because the
    store is compressed."""
    import json
    meta = json.loads((index_dir / "meta.json").read_text())
    return sum((seg.get("deletes") or {}).get("num_deleted_docs", 0)
               for seg in meta["segments"])


def _has_term(index, field, word) -> bool:
    index.reload()
    return bool(index.searcher().terms_with_prefix(field, word))


def test_a_document_sharing_a_segment_is_gone_only_after_the_rewrite(node):  # noqa: F811
    """The case the rewrite exists for: two documents in ONE tantivy segment
    (what a merge makes of many). Deleting one leaves it in the segment —
    counted as deleted, its terms still in the dictionary; rewriting from
    the live documents removes both."""
    make(node, "alice")
    c = collections_mod.by_name("alice")
    gone, kept = _marker("g"), _marker("s")
    chunk = lambda i, text: {"id": f"x{i}", "chapter": "", "text": text,  # noqa: E731
                             "tokens": 3, "seq": 0}
    # ONE indexing thread, so both documents land in one segment — the state
    # a merge produces, which tantivy's multi-threaded writer does not on a
    # two-document commit (it wrote them into a segment each)
    import threading
    idx = fts.get_index("other", c)
    fts._writers[(str(c.fts_dir), "other")] = (idx.writer(15_000_000, 1),
                                               threading.Lock())
    fts.index_books([("und", 901, [chunk(1, f"erased {gone}")]),
                     ("und", 902, [chunk(2, f"survivor {kept}")])], c)
    fts.delete_book("und", 901, c)
    index = c.fts_dir / "other"
    assert _deleted_docs(index) == 1 and _has_term(idx, "body", gone)
    assert fts.rewrite_without("other", c) == 1
    assert _deleted_docs(index) == 0 and not _has_term(idx, "body", gone)
    assert [h["book_id"] for h in fts.search("other", kept, collection=c)] == [902]


def test_the_catalog_rewrite_keeps_only_the_rows_it_is_given(node):  # noqa: F811
    from app import catalog
    make(node, "alice")
    c = collections_mod.by_name("alice")
    gone, kept = _marker("g"), _marker("s")
    catalog.write(c, [(1, gone, "", "", "und"), (2, kept, "", "", "und")])
    idx = catalog._open(catalog.path_for(c))
    assert _has_term(idx, "title", gone)
    assert catalog.rewrite(c, [(2, kept, "", "", "und")]) == 1
    idx = catalog._open(catalog.path_for(c))
    assert not _has_term(idx, "title", gone) and _has_term(idx, "title", kept)
    assert _deleted_docs(catalog.path_for(c)) == 0


def test_after_an_erase_no_index_holds_a_deleted_document(three):
    root, t, k = three
    deposit.erase("alice", f"{k}.txt")
    for meta in (root / "collections" / "alice" / "index").rglob("meta.json"):
        assert _deleted_docs(meta.parent) == 0, meta


def test_a_crash_before_the_purge_is_finished_on_the_next_opening(three, monkeypatch):
    """The erase marks the purge pending before it touches anything; a crash
    before the purge leaves the mark, and the next opening completes it."""
    root, t, k = three
    real = deposit.purge

    def crash(c, con):
        raise KeyboardInterrupt("the process died here")
    monkeypatch.setattr(deposit, "purge", crash)
    with pytest.raises(KeyboardInterrupt):
        deposit.erase("alice", f"{k}.txt")
    assert _files_holding(root, t)            # the vector table still has it
    monkeypatch.setattr(deposit, "purge", real)
    deposit.keys("alice")                     # a READ writes nothing, purges nothing
    assert _files_holding(root, t)
    assert deposit.finish_pending() == ["alice"]   # the worker's deposit thread, starting
    assert _files_holding(root, t, k) == []
    assert deposit.keys("alice") == ["keep-one.txt", "keep-two.md"]

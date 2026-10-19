"""tests/test_embed_marks.py — one place marks a document embedded
(app/embed_marks.py): the manifest row, the imported-provenance rows, the
queue entry; and a document is complete when every chunk the layers derive
from its record has a vector — asked of the table, never of
`documents.chunks`."""
import time

from app import config, db, embed_marks
from app.bundles import ledger


def test_mark_embedded_marks_the_row_and_clears_provenance_and_queue(isolated_config):
    con = db.connect()
    con.execute("INSERT INTO documents(uid, source, native_id, path, updated_at) VALUES(?,?,?,?,?)",
                ("gutenberg:1", "gutenberg", 1, "1.txt", 1))
    con.execute("INSERT INTO documents(uid, source, native_id, path, updated_at) VALUES(?,?,?,?,?)",
                ("gutenberg:2", "gutenberg", 2, "2.txt", 1))
    con.execute("INSERT INTO embed_queue(uid, priority, ts) VALUES(?,?,?)", ("gutenberg:1", 0, 1))
    ledger.set_verified(con, "gutenberg:1:0:1", 0.99, True)
    n = embed_marks.mark_embedded(con, None, ["gutenberg:1", "gutenberg:9"])
    assert n == 1, "a uid the manifest does not know touches no row"
    r1, r2 = [con.execute("SELECT embed_done, embed_version, updated_at FROM documents WHERE uid=?", (u,)).fetchone()
              for u in ("gutenberg:1", "gutenberg:2")]
    assert (r1["embed_done"], r1["embed_version"]) == (1, config.EMBED_VERSION) and r1["updated_at"] >= int(time.time()) - 5
    assert (r2["embed_done"], r2["updated_at"]) == (0, 1), "the other row untouched"
    assert con.execute("SELECT COUNT(*) FROM embed_queue").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM chunk_verification WHERE uid=?", ("gutenberg:1",)).fetchone()[0] == 0
    con.close()


def test_complete_uids_asks_the_table_for_every_chunk_of_the_record(codex):
    """A record is complete when each chunk the layers derive from its
    sections has a vector; one chunk missing is not; no chunk is."""
    from app import layers as layers_mod
    records = [{"uid": "gutenberg:1", "native_id": 1, "content_hash": "sha256:" + "a" * 64,
                "sections": [["Chapter I", "one two three four five six seven eight nine ten " * 40]]},
               {"uid": "gutenberg:2", "native_id": 2, "content_hash": "sha256:" + "b" * 64,
                "sections": [["Chapter I", "alpha beta gamma delta " * 60]]},
               {"uid": "gutenberg:3", "native_id": 3, "content_hash": "sha256:" + "c" * 64, "sections": []}]
    ids = {r["uid"]: [c["id"] for c in layers_mod.chunks_for_document(
        codex, {"uid": r["uid"], "native_id": r["native_id"]}, [tuple(s) for s in r["sections"]])] for r in records}
    assert ids["gutenberg:1"] and ids["gutenberg:2"] and ids["gutenberg:3"] == []
    held = set(ids["gutenberg:1"]) | set(ids["gutenberg:2"][:-1])
    asked: list[list[str]] = []

    def vectors_for(wanted):
        asked.append(list(wanted))
        return {i: [0.0] for i in wanted if i in held}
    got = embed_marks.complete_uids(codex, records, vectors_for)
    assert got == ["gutenberg:1", "gutenberg:3"], "every chunk held; nothing to embed; not the one missing a chunk"
    assert len(asked) == 1 and set(asked[0]) == set(ids["gutenberg:1"]) | set(ids["gutenberg:2"]), "one lookup"

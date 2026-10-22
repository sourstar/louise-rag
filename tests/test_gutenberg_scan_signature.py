"""gutenberg's scan signature (2026-10-22): the book directory's (mtime_ns,
size) AND the files that decide its record — pg<id>.rdf and the form file —
so a file rewritten IN PLACE, which leaves the directory's mtime alone, is
seen by the next scan, not only by a forced rescan."""
import os
import shutil
from pathlib import Path

from app import collections as collections_mod, db, discovery
from app.plugins import cursor

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"


def _collection(tmp_path):
    corpus = tmp_path / "corpus_g"
    shutil.copytree(FIXTURE_CORPUS, corpus)
    (corpus / "99998").mkdir()          # a transfer stub: an empty book directory
    c = collections_mod.Collection(
        name="g", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / "g" / "manifest.db",
        fts_dir=tmp_path / "g" / "fts", lance_dir=tmp_path / "g" / "lancedb")
    con = db.connect(c)
    return c, con


def _rewrite_in_place(path: Path, data: bytes) -> None:
    """As `rsync --inplace` does: the same inode written over, the
    directory's mtime left as it was."""
    d = path.parent.stat()
    with path.open("r+b") as f:
        f.truncate(0)
        f.write(data)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns - 5_000_000_000))   # settled before the scan
    os.utime(path.parent, ns=(d.st_atime_ns, d.st_mtime_ns))


def _hash(con, uid):
    return con.execute("SELECT txt_hash FROM documents WHERE uid=?", (uid,)).fetchone()[0]


def test_a_file_rewritten_in_place_is_seen_by_the_next_scan(tmp_path, isolated_config):
    c, con = _collection(tmp_path)
    first = discovery.sync_collection(con, c)
    assert first["added"] == 3
    assert discovery.sync_collection(con, c)["seen"] == 0, "unchanged books are skipped"

    text = c.corpus_dir / "10001" / "pg10001.txt.utf8"
    before = _hash(con, "gutenberg:10001")
    _rewrite_in_place(text, text.read_bytes() + b"\nA line the mirror added in place.\n")
    out = discovery.sync_collection(con, c)
    assert (out["seen"], out["changed"]) == (1, 1), out
    assert _hash(con, "gutenberg:10001") != before

    rdf = c.corpus_dir / "10002" / "pg10002.rdf"
    before = _hash(con, "gutenberg:10002")
    _rewrite_in_place(rdf, rdf.read_bytes().replace(b"</pgterms:ebook>",
                      b"<dcterms:description>a revised note</dcterms:description></pgterms:ebook>", 1))
    out = discovery.sync_collection(con, c)
    assert (out["seen"], out["changed"]) == (1, 1), out
    assert _hash(con, "gutenberg:10002") != before
    assert discovery.sync_collection(con, c)["seen"] == 0, "and skipped again after"


def test_a_stub_is_recorded_with_no_members_and_skipped(tmp_path, isolated_config):
    c, con = _collection(tmp_path)
    discovery.sync_collection(con, c)
    saved = cursor.load(c.manifest_path.parent, "gutenberg")
    assert saved["99998"][2:] == [cursor.MEMBERS], "a stub: the marker, no member"
    assert saved["10001"][2] == cursor.MEMBERS and \
        {m[0] for m in saved["10001"][3:]} == {"pg10001.txt.utf8", "pg10001.rdf"}
    assert discovery.sync_collection(con, c)["seen"] == 0


def test_a_cursor_written_before_members_is_reexamined_once(tmp_path, isolated_config):
    c, con = _collection(tmp_path)
    discovery.sync_collection(con, c)
    state_dir = c.manifest_path.parent
    old = {k: v[:2] for k, v in cursor.load(state_dir, "gutenberg").items()}   # the old shape
    cursor.save(state_dir, "gutenberg", old)
    out = discovery.sync_collection(con, c)
    assert out["seen"] == 3 and out["changed"] == 0, "re-examined, nothing changed"
    assert discovery.sync_collection(con, c)["seen"] == 0

"""Extra formats (spec.extras) — the local-only download policy.

POLICY under test: peers exchange only the data that fulfils Louise's
purpose. Extras (gutenberg's epubs) are an opt-in LOCAL download from the
corpus source — classified apart in the listing and the fetch ledger,
never inflating the Louise counters, and never entering p2p material
(the torrent-publish exclusion has its own test module, it needs
libtorrent).
"""
import json

from app import collections as collections_mod, db
from app.acquire import engine
from app.acquire.spec import Filter, RemoteFile
from tests.acquire.test_engine import _collection, _spec, _write_origin_file


def _extras_spec(mock_server, **overrides):
    return _spec(
        mock_server,
        filters=("*.txt",),
        extras={"epub": Filter(include=("*.epub",), exclude=("*images*",))},
        **overrides)


# ---------------- classification ----------------

def test_extras_not_selected_by_default(tmp_path, mock_server):
    _write_origin_file(mock_server, "a.txt", b"text")
    _write_origin_file(mock_server, "a.epub", b"epub")
    spec = _extras_spec(mock_server)
    collection = _collection(tmp_path)          # extras=() — not opted in
    con = db.connect(collection)

    engine.run_acquisition(con, collection, spec, "demo")
    rows = con.execute("SELECT rel_path FROM fetch_files ORDER BY rel_path").fetchall()
    assert [r["rel_path"] for r in rows] == ["a.txt"]
    assert not (collection.corpus_dir / "a.epub").exists()


def test_opted_in_extras_fetched_and_flagged(tmp_path, mock_server):
    _write_origin_file(mock_server, "a.txt", b"text")
    _write_origin_file(mock_server, "a.epub", b"epub!")
    _write_origin_file(mock_server, "a-images.epub", b"pics")  # extras exclude
    spec = _extras_spec(mock_server)
    collection = _collection(tmp_path, extras=("epub",))
    con = db.connect(collection)

    engine.run_acquisition(con, collection, spec, "demo")
    rows = {r["rel_path"]: r["extra"] for r in con.execute(
        "SELECT rel_path, extra FROM fetch_files WHERE fetch_done=1")}
    assert rows == {"a.txt": 0, "a.epub": 1}
    assert (collection.corpus_dir / "a.epub").read_bytes() == b"epub!"


def test_classification_is_exclusive_louise_wins(tmp_path):
    """A file the base filters claim is Louise data even when an extras
    pattern also matches it — exclusive classification, never
    double-counted."""
    files = [RemoteFile(rel_path="a.txt", size=1),
             RemoteFile(rel_path="a.epub", size=1)]
    spec = _FakeSpec(extras={"all": Filter(include=("*",))})
    louise = [files[0]]
    chosen = engine._select_extras(files, spec, ("all",), louise)
    assert [f.rel_path for f in chosen] == ["a.epub"]
    assert all(f.extra for f in chosen)


class _FakeSpec:
    def __init__(self, extras):
        self.extras = extras


# ---------------- the listing record splits the totals ----------------

def test_listing_record_counts_extras_apart(tmp_path, mock_server):
    _write_origin_file(mock_server, "a.txt", b"x" * 10)
    _write_origin_file(mock_server, "b.txt", b"x" * 10)
    _write_origin_file(mock_server, "a.epub", b"y" * 30)
    spec = _extras_spec(mock_server)
    collection = _collection(tmp_path, extras=("epub",))
    con = db.connect(collection)

    engine.run_acquisition(con, collection, spec, "demo")
    listing = json.loads(db.get_control(con, engine.LISTING_KEY))
    # Louise totals never inflated by extras
    assert listing["files_total"] == 2
    assert listing["bytes_total"] == 20
    assert listing["extra_files_total"] == 1
    assert listing["extra_bytes_total"] == 30


# ---------------- TOML round-trip (desired state survives) ----------------

def test_collection_toml_round_trips_extras(isolated_config, data_dir, config_dir, tmp_path):
    from app.web import _write_collection_toml
    from tests.conftest import codex_collection
    codex = codex_collection("gut", louise_root=data_dir, config_dir=config_dir)
    path = config_dir / "collections" / "gut.toml"
    _write_collection_toml(
        path, codex=codex, plugin="gutenberg", profile="full", acquire_enabled=True,
        corpus_dir=tmp_path / "corpus", manifest=tmp_path / "m.db",
        fts=tmp_path / "fts", lancedb=tmp_path / "lance",
        extras=("epub",))
    loaded = collections_mod._load_one(path)
    assert loaded.extras == ("epub",)
    # and a rewrite WITHOUT extras drops them — the writer carries desired
    # state explicitly, so every caller passes the collection's current value
    _write_collection_toml(
        path, codex=codex, plugin="gutenberg", profile="full", acquire_enabled=True,
        corpus_dir=tmp_path / "corpus", manifest=tmp_path / "m.db",
        fts=tmp_path / "fts", lancedb=tmp_path / "lance")
    assert collections_mod._load_one(path).extras == ()


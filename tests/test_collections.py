"""app/collections.py: a node has exactly the Codex collections whose files
are in `$CONFIG_DIR/collections/` — possibly none, and none when the
directory is absent (the synthesized legacy default left on 2026-10-08,
with the pre-Codex shape). A file that names no Codex is refused, never
served, and said in `refusals()`.
"""
from app import collections as collections_mod
from app import config
from tests.conftest import codex_collection


def test_an_empty_collections_directory_means_zero_collections(isolated_config, data_dir, config_dir):
    """An empty directory and an absent one are the same node: no
    collections, never a default coming back."""
    from app import db, worker
    d = collections_mod.collections_dir()
    (d / "gutenberg.toml").unlink()
    assert collections_mod.load_all() == []
    d.rmdir()
    assert collections_mod.load_all() == []
    assert collections_mod.load_all(skip_unreadable=True) == ([], [])
    assert collections_mod.by_name("gutenberg") is None
    # the control manifest falls back to the DATA_DIR path so the node runs;
    # it is listed nowhere
    fallback = collections_mod.default_collection()
    assert fallback.manifest_path == config.DATA_DIR / "manifest.db"
    con = db.connect()
    assert con.collection_name == "control" and db.worker_state(con)
    con.close()
    assert worker._sync_collections([], {}, {}) == []
    # a Codex file appearing makes it the one and only collection
    codex_collection("solo", louise_root=data_dir, config_dir=config_dir)
    assert [c.name for c in collections_mod.load_all()] == ["solo"]


def test_a_codex_less_file_is_refused_and_the_others_are_served(isolated_config, config_dir):
    (config_dir / "collections" / "old.toml").write_text(
        'name = "old"\nplugin = "gutenberg"\n\n[paths]\nlancedb = "/x"\n', encoding="utf-8")
    assert [c.name for c in collections_mod.load_all()] == ["gutenberg"]
    (refused,) = collections_mod.refusals()
    assert refused.name == "old" and "not converted" in refused.reason
    assert collections_mod.by_name("old") is None


def test_the_codex_collection_runs_from_its_tree(isolated_config, data_dir, corpus_dir):
    """07 §7.8: state, index and vectors under `<LOUISE_ROOT>/collections/
    <name>/`, the plugin from the Codex."""
    (c,) = collections_mod.load_all()
    tree = data_dir / "collections" / "gutenberg"
    assert c.name == "gutenberg" and c.plugin == "gutenberg" and c.codex is not None
    assert c.manifest_path == tree / "state" / "manifest.sqlite"
    assert c.fts_dir == tree / "index"
    assert c.lance_dir == tree / "lance"
    assert c.corpus_dir == corpus_dir

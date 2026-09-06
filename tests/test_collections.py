"""app/collections.py: the no-toml fallback and a present collections/*.toml
with empty path fields must resolve to the same legacy DATA_DIR-based layout —
the transparency guarantee that lets an existing deployment upgrade with zero
data movement and no required new config.
"""
from app import collections as collections_mod
from app import config


def test_an_empty_collections_directory_means_zero_collections(isolated_config):
    """The directory EXISTING is what selects TOML mode — an empty one is a
    node with no collections, never the legacy default coming back."""
    from app import config, db, worker
    d = collections_mod.collections_dir()
    assert not d.is_dir() and len(collections_mod.load_all()) == 1
    d.mkdir(parents=True)
    assert collections_mod.load_all() == []
    assert collections_mod.by_name("gutenberg") is None
    # the control manifest falls back to the legacy path so the node runs
    fallback = collections_mod.default_collection()
    assert fallback.manifest_path == config.DATA_DIR / "manifest.db"
    assert not collections_mod.is_synthesized(fallback), "not listed, not synthesized: a fallback"
    con = db.connect()
    assert con.collection_name == "gutenberg" and db.worker_state(con)
    con.close()
    assert worker._sync_collections([], {}, {}) == []
    # a TOML appearing makes it the one and only collection
    (d / "solo.toml").write_text('name = "solo"\nplugin = "pubmed"\n', encoding="utf-8")
    assert [c.name for c in collections_mod.load_all()] == ["solo"]


def test_no_toml_directory_falls_back_to_legacy_paths(isolated_config):
    cols = collections_mod.load_all()
    assert len(cols) == 1
    c = cols[0]
    assert c.name == "gutenberg"
    assert c.plugin == "gutenberg"
    assert c.manifest_path == config.DATA_DIR / "manifest.db"
    assert c.fts_dir == config.DATA_DIR / "fts"
    assert c.lance_dir == config.DATA_DIR / "lancedb"
    assert c.corpus_dir == config.CORPUS_DIR


def test_shipped_toml_with_empty_paths_matches_fallback(isolated_config, config_dir):
    (config_dir / "collections").mkdir()
    (config_dir / "collections" / "gutenberg.toml").write_text(
        'name = "gutenberg"\n'
        'plugin = "gutenberg"\n'
        'profile = "full"\n'
        'embed_mode = "lazy"\n'
        "\n"
        "[paths]\n"
        'manifest = ""\n'
        'fts = ""\n'
        'lancedb = ""\n'
    )
    cols = collections_mod.load_all()
    assert len(cols) == 1
    c = cols[0]
    assert c.manifest_path == config.DATA_DIR / "manifest.db"
    assert c.fts_dir == config.DATA_DIR / "fts"
    assert c.lance_dir == config.DATA_DIR / "lancedb"
    assert c.profile == "full"
    assert c.embed_mode_default == "lazy"


def test_toml_can_override_paths(isolated_config, config_dir, tmp_path):
    custom_lance = tmp_path / "custom-lancedb"
    (config_dir / "collections").mkdir()
    (config_dir / "collections" / "gutenberg.toml").write_text(
        'name = "gutenberg"\n'
        'plugin = "gutenberg"\n'
        "\n"
        "[paths]\n"
        f'lancedb = "{custom_lance.as_posix()}"\n'
    )
    cols = collections_mod.load_all()
    assert cols[0].lance_dir == custom_lance

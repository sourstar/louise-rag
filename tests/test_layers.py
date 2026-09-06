"""Chunker layers (amendment 41): one layer = the plugin's chunker and an
unchanged space id; several layers = one chunker per document by a
declared rule, the ordered chunkers in the space id."""
from dataclasses import replace

import pytest

from app import chunking, collections as collections_mod, layers, spaces
from app.bundles import genesis


def _collection(isolated_config, tmp_path, plugin="gutenberg", **kw):
    return collections_mod.Collection(
        name="c", plugin=plugin, profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb", **kw)


def test_one_implicit_layer_is_the_plugins_chunker(isolated_config, tmp_path):
    c = _collection(isolated_config, tmp_path)
    (layer,) = layers.layers_for(c)
    assert layer.chunker == chunking.BOOK and layer.rule == "default"
    assert layers.layer_index(c, 42) == 0
    assert layers.space_payload(c) == {}
    p = _collection(isolated_config, tmp_path, plugin="pubmed")
    assert layers.layers_for(p)[0].chunker == chunking.ABSTRACT


def test_parse_and_validate_declared_layers():
    parsed = layers.parse_layers([{"chunker": "verbatim", "rule": "archive:medcpt-pubmed"},
                                  {"chunker": "cdc"}], where="t.toml")
    assert [l.chunker for l in parsed] == ["verbatim", "cdc"]
    assert parsed[1].rule == "default" and parsed[1].version == chunking.CDC_CHUNK_VERSION
    assert layers.parse_layers(None, where="t") is None
    with pytest.raises(layers.LayerError, match="unknown chunker"):
        layers.parse_layers([{"chunker": "banana"}], where="t")
    with pytest.raises(layers.LayerError, match="last layer"):
        layers.parse_layers([{"chunker": "cdc"}, {"chunker": "book", "rule": "archive:x"}], where="t")
    with pytest.raises(layers.LayerError, match="only the last"):
        layers.parse_layers([{"chunker": "cdc", "rule": "default"}, {"chunker": "book"}], where="t")
    with pytest.raises(layers.LayerError, match="unknown rule"):
        layers.parse_layers([{"chunker": "cdc", "rule": "sometimes"}], where="t")


def test_a_document_belongs_to_one_layer_by_the_archive_rule(isolated_config, tmp_path, monkeypatch):
    """The MedCPT case: documents the archive declares it covers keep its
    chunker; everything else (post-snapshot ids) gets ours."""
    arc = genesis.GenesisArchive(archive_id="arc", base_url="", adapter="medcpt-npy",
                                 shards=("0", "1"), uid_prefix="pubmed:", ids_per_shard=10)
    monkeypatch.setitem(genesis.ARCHIVES, "arc", arc)
    two = layers.parse_layers([{"chunker": "abstract", "rule": "archive:arc"},
                               {"chunker": "book"}], where="t")
    c = _collection(isolated_config, tmp_path, plugin="pubmed", layers=two)
    assert layers.layer_index(c, 5) == 0 and layers.layer_index(c, 19) == 0
    assert layers.layer_index(c, 20) == 1, "beyond the archive's declared range"
    sections = [("Title", "T"), ("Abstract", "a b c")]
    covered = list(layers.chunker_for_document(c, 5)("pubmed:5", 5, sections))
    ours = list(layers.chunker_for_document(c, 20)("pubmed:20", 20, sections))
    assert len(covered) == 1 and covered[0]["id"].endswith(f":0:{chunking.ABSTRACT_CHUNK_VERSION}")
    assert len(ours) == 2, "the book chunker cuts title and abstract apart"
    # an unknown archive covers nothing; a local archive covers everything
    unknown = layers.parse_layers([{"chunker": "abstract", "rule": "archive:nope"}, {"chunker": "book"}], where="t")
    assert layers.layer_index(replace(c, layers=unknown), 5) == 1
    local = layers.parse_layers([{"chunker": "verbatim", "rule": "archive:upstash-wikipedia-2024-06"},
                                 {"chunker": "cdc"}], where="t")
    assert layers.layer_index(replace(c, layers=local), 5) == 0


def test_space_id_is_unchanged_with_one_layer_and_moves_with_two(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a, **k: ("deadbeef", True))
    c = _collection(isolated_config, tmp_path, plugin="pubmed")
    one = spaces.compute_space_id(spaces.gather_components(c))
    assert spaces.gather_components(c).layers is None
    two = layers.parse_layers([{"chunker": "abstract", "rule": "archive:medcpt-pubmed"},
                               {"chunker": "book"}], where="t")
    comps = spaces.gather_components(replace(c, layers=two))
    assert comps.layers == [["abstract", chunking.ABSTRACT_CHUNK_VERSION], ["book", 1]]
    assert spaces.compute_space_id(comps) != one
    assert spaces._config_id(comps) != spaces._config_id(spaces.gather_components(c))
    # the order of layers is identity too
    swapped = layers.parse_layers([{"chunker": "book", "rule": "archive:medcpt-pubmed"},
                                   {"chunker": "abstract"}], where="t")
    assert spaces.compute_space_id(spaces.gather_components(replace(c, layers=swapped))) \
        != spaces.compute_space_id(comps)
    # a pinned components row from before layers existed still loads
    assert spaces.SpaceComponents(**{k: v for k, v in comps.__dict__.items() if k != "layers"}).layers is None


def test_toml_layers_are_loaded_into_the_collection(isolated_config, config_dir):
    (config_dir / "collections").mkdir()
    (config_dir / "collections" / "wv.toml").write_text(
        'name = "wv"\nplugin = "embedded"\n'
        '[[layers]]\nchunker = "verbatim"\nrule = "archive:upstash-wikipedia-2024-06"\n'
        '[[layers]]\nchunker = "cdc"\n', encoding="utf-8")
    c = collections_mod.by_name("wv")
    assert c is not None and [l.chunker for l in c.layers] == ["verbatim", "cdc"]
    (config_dir / "collections" / "bad.toml").write_text(
        'name = "bad"\nplugin = "embedded"\n[[layers]]\nchunker = "cdc"\nrule = "archive:x"\n',
        encoding="utf-8")
    with pytest.raises(layers.LayerError):
        collections_mod.load_all()

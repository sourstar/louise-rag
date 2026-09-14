"""`pack adopt`: the origin collection's vectors move into its packed twin
without re-embedding — same space, same uids, same chunk ids — with the
marks and provenance; refused across spaces or onto an existing table."""
from dataclasses import replace

import numpy as np
import pytest

from app import collections as collections_mod, db, discovery, embeddings, layers, plugin_io, spaces
from app.bundles import ledger
from app.pack import adopt as pack_adopt, build as pack_build

DIM = 8


def _unit(seed: str):
    rng = np.random.default_rng(abs(hash(seed)) % (2**32))
    v = rng.normal(size=DIM)
    return (v / np.linalg.norm(v)).tolist()


@pytest.fixture
def pair(isolated_config, tmp_path, corpus_dir, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a, **k: ("abc123", False))
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, timeout=300, recipe=None, query=False: [_unit(t) for t in texts])
    recipe = collections_mod.EmbedRecipe(url="http://fake:1", api="openai", model="m", dtype="f16",
                                         ctx=512, dim=DIM)
    d = tmp_path / "g"
    origin = collections_mod.Collection(
        name="g", plugin="gutenberg", profile=None, embed_mode_default=None, corpus_dir=corpus_dir,
        manifest_path=d / "manifest.db", fts_dir=d / "fts", lance_dir=d / "lancedb", embed=recipe)
    con = db.connect(origin)
    discovery.sync_collection(con, origin)
    plugin = origin.plugin_instance()
    for row in con.execute("SELECT uid, native_id, path, lang FROM documents WHERE error IS NULL").fetchall():
        chunks = layers.chunks_for_document(origin, row, plugin_io.sections(plugin, row))
        embeddings.upsert_book(int(row["native_id"]), "en", chunks, origin)
        con.execute("UPDATE documents SET embed_done=1, embed_version=1, fts_done=1, fts_version=1 WHERE uid=?",
                    (row["uid"],))
    # one document's chunks came from a peer: provenance must travel
    first = con.execute("SELECT uid FROM documents ORDER BY uid LIMIT 1").fetchone()["uid"]
    ledger.mark_imported(con, [f"{first}:0:1"], first, "peerkey", "bundle-1")
    spaces.validate_boot([origin])
    con.close()
    pack_dir = tmp_path / "packs" / "g"
    pack_build.build(origin, pack_dir, records_per_shard=1, sign=False, log=lambda *_: None)
    p = tmp_path / "gp"
    packed = collections_mod.Collection(
        name="g-pack", plugin="packed", profile=None, embed_mode_default=None, corpus_dir=pack_dir,
        manifest_path=p / "manifest.db", fts_dir=p / "fts", lance_dir=p / "lancedb", embed=recipe)
    con = db.connect(packed)
    discovery.sync_collection(con, packed)
    con.close()
    return origin, packed


def test_adopt_moves_vectors_marks_and_provenance(pair):
    origin, packed = pair
    with pytest.raises(pack_adopt.AdoptError, match="no pinned space"):
        pack_adopt.adopt(origin, packed, log=lambda *_: None)
    spaces.validate_boot([packed])          # the worker's boot pins the twin
    (origin.fts_dir / "meta.json").write_text("{}")      # stands in for the tantivy index
    n_origin = embeddings._conn(origin).open_table(embeddings.TABLE).count_rows()
    result = pack_adopt.adopt(origin, packed, fts=True, log=lambda *_: None)
    assert result["vectors"] == n_origin and result["documents"] == 3 and result["missing"] == 0
    assert result["fts"] is True
    assert embeddings._conn(packed).open_table(embeddings.TABLE).count_rows() == n_origin
    con = db.connect(packed)
    marks = con.execute("SELECT SUM(embed_done) e, SUM(fts_done) f, SUM(rdf_done) r FROM documents").fetchone()
    assert marks["e"] == 3 and marks["f"] == 3 and marks["r"] == 0, "rdf is re-parsed from the pack"
    assert (packed.fts_dir / "meta.json").is_file(), "the index directory came along"
    prov = con.execute("SELECT uid, producer, bundle_id FROM chunk_verification").fetchall()
    assert len(prov) == 1 and prov[0]["producer"] == "peerkey"
    import json
    adopted = json.loads(db.get_control(con, "adopted_from"))
    assert adopted["collection"] == "g" and adopted["vectors"] == n_origin and adopted["documents"] == 3
    # the origin is untouched, and a search on the twin finds a vector
    assert embeddings._conn(origin).open_table(embeddings.TABLE).count_rows() == n_origin
    assert embeddings.search("anything", k=2, collection=packed)
    # a second adoption is refused: the twin already holds a table
    with pytest.raises(pack_adopt.AdoptError, match="already has a vector table"):
        pack_adopt.adopt(origin, packed, log=lambda *_: None)
    con.close()


def test_adopt_refuses_a_different_space(pair, monkeypatch):
    origin, packed = pair
    other = replace(packed, embed=replace(packed.embed, ctx=1024))     # truncation policy differs
    spaces.validate_boot([other])
    with pytest.raises(pack_adopt.AdoptError, match="different spaces"):
        pack_adopt.adopt(origin, other, log=lambda *_: None)

"""Two-node fixtures for the Phase-1 integration suite (F1).

A "node" here is a real collection with its own manifest/Lance/corpus, driven
through the real code paths — export, coordinator upload, inbox drain, import,
verify-on-use. The only fake is the embed endpoint, replaced by a DETERMINISTIC
text->vector function so producer and consumer genuinely agree the way two
nodes running the same model would.
"""
import hashlib
import shutil

import numpy as np
import pytest
from nacl import signing

from app import db, spaces
from tests.bundles.helpers import DIM, make_collection, seed_document

PRODUCER_KEY = signing.SigningKey(bytes(range(32)))
ATTACKER_KEY = signing.SigningKey(bytes(range(64, 96)))


def model_embed(texts, *, salt="model-A"):
    """The 'recorded vectors' model: deterministic text->unit vector.

    Two nodes calling this with the same salt behave exactly like two nodes
    sharing one model; a different salt IS a different model. The shared
    positive component keeps the output anisotropic so admission checks see
    embedding-like geometry rather than noise.
    """
    out = []
    for text in texts:
        seed = int.from_bytes(hashlib.blake2b(
            f"{salt}\x00{text}".encode(), digest_size=8).digest(), "big")
        rng = np.random.default_rng(seed)
        v = rng.normal(size=DIM).astype(np.float32) + 0.6
        out.append(v / np.linalg.norm(v))
    return out


@pytest.fixture(autouse=True)
def confirmed_space(monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash",
                        lambda: ("f1f1f1f1f1f1f1f1", False))


@pytest.fixture
def producer(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "CORPUS_DIR", tmp_path / "corpus")
    collection = make_collection(tmp_path / "producer", name="gutenberg")
    con = db.connect(collection)
    space_id = spaces.compute_space_id(spaces.gather_components())
    con.execute("INSERT OR REPLACE INTO space(id,space_id,dim,components,pinned_at) "
                "VALUES(1,?,?,'{}',0)", (space_id, DIM))
    return con, collection


@pytest.fixture
def consumer(tmp_path, producer):
    """Same raw corpus bytes as the producer (possession binding requires it),
    no vectors, nothing embedded."""
    producer_con, producer_col = producer
    collection = make_collection(tmp_path / "consumer", name="gutenberg")
    con = db.connect(collection)
    space_id = spaces.compute_space_id(spaces.gather_components())
    con.execute("INSERT OR REPLACE INTO space(id,space_id,dim,components,pinned_at) "
                "VALUES(1,?,?,'{}',0)", (space_id, DIM))
    return con, collection


def seed_node(con, collection, uids_to_ids: dict[str, int], *, embed=True):
    """Seed documents with REAL vectors from the deterministic model (not the
    helpers' arbitrary unit_vectors), so cross-node re-embedding agrees."""
    all_rows = []
    for uid, native_id in uids_to_ids.items():
        rows = seed_document(con, collection, uid, native_id, n_chunks=4,
                              seed=native_id)
        texts = [r["text"] for r in rows]
        vectors = model_embed(texts)
        rows = seed_document(con, collection, uid, native_id, n_chunks=4,
                              seed=native_id, vectors=vectors)
        all_rows.extend(rows)
    if not embed:
        con.execute("UPDATE documents SET embed_done=0")
    return all_rows


def mirror_corpus(source_col, dest_con, dest_col, uids_to_ids: dict[str, int]):
    """Copy the producer's raw files byte-for-byte into the consumer and
    register them in its manifest, un-embedded — the state a real consumer is
    in after acquisition + discovery but before any vectors exist."""
    for uid, native_id in uids_to_ids.items():
        src = source_col.corpus_dir / str(native_id)
        dst = dest_col.corpus_dir / str(native_id)
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        dest_con.execute(
            "INSERT OR REPLACE INTO documents(uid,source,native_id,path,lang,title,"
            "rdf_done,fts_done,embed_done,updated_at) VALUES(?,?,?,?,?,?,1,1,0,0)",
            (uid, dest_col.plugin, str(native_id), str(dst), "en", f"Doc {native_id}"))

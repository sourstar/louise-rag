"""app/worker.py: round-robin ingestion across ALL configured collections —
P0.5's fix for a real bug found while reading worker.py's own code: before
this, run_forever() hardcoded collections[0], so a second configured
collection was silently, permanently never discovered or ingested.
"""
import shutil
from pathlib import Path

from app import collections as collections_mod, db, discovery, worker

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"


def _collection(tmp_path, name, corpus_dir):
    return collections_mod.Collection(
        name=name, plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus_dir, manifest_path=tmp_path / name / "manifest.db",
        fts_dir=tmp_path / name / "fts", lance_dir=tmp_path / name / "lancedb")


def test_round_robin_advances_every_collection(tmp_path):
    corpus_a = tmp_path / "corpus_a"
    shutil.copytree(FIXTURE_CORPUS, corpus_a)
    corpus_b = tmp_path / "corpus_b"
    shutil.copytree(FIXTURE_CORPUS, corpus_b)

    alpha = _collection(tmp_path, "alpha", corpus_a)
    beta = _collection(tmp_path, "beta", corpus_b)

    con_alpha = db.connect(alpha)
    con_beta = db.connect(beta)
    discovery.sync_collection(con_alpha, alpha)
    discovery.sync_collection(con_beta, beta)

    last_idle_log: dict = {}
    # Simulate what run_forever()'s round-robin loop does, without the
    # infinite while loop itself: alternate one step per collection per
    # round. 3 books each, rdf+fts stages -> at most 6 steps per collection
    # needed; run comfortably more rounds to reach steady idle state.
    for _ in range(20):
        worker._run_one_step(con_alpha, alpha, last_idle_log)
        worker._run_one_step(con_beta, beta, last_idle_log)

    for name, con in (("alpha", con_alpha), ("beta", con_beta)):
        rows = con.execute("SELECT rdf_done, fts_done FROM documents").fetchall()
        assert len(rows) == 3, f"{name}: expected 3 documents"
        assert all(r["rdf_done"] == 1 for r in rows), f"{name}: not all rdf_done"
        assert all(r["fts_done"] == 1 for r in rows), f"{name}: not all fts_done"

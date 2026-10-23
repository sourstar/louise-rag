"""A compaction's commit is the only one (München, 2026-10-23): normattiva's
compaction lost all 13 commits from 10-21 to 10-23 to embed batches that
committed underneath it — the writers checked for a rewrite only when their
stage started. Every commit now registers (`vector_index.writing`, through
`embeddings._write_lock`); a rewrite refuses new ones and waits, bounded,
for those in flight."""
import threading
import time
from types import SimpleNamespace

import pytest

from app import embeddings, vector_index, worker


def coll(tmp_path, name="normattiva"):
    return SimpleNamespace(name=name, lance_dir=tmp_path / name / "lance")


def test_a_write_during_a_rewrite_is_a_deferral(tmp_path):
    c = coll(tmp_path)
    with vector_index.rewrite(c, drain_s=1):
        with pytest.raises(vector_index.RewriteInProgress) as e:
            with vector_index.writing(c):
                pass
    assert worker.vector_store_contended(e.value), "the worker defers it; the document is not errored"
    with vector_index.writing(c):
        assert vector_index.in_flight(c) == 1, "after the rewrite, writes go through"
    assert vector_index.in_flight(c) == 0


def test_the_rewrite_waits_for_the_write_in_flight(tmp_path):
    c = coll(tmp_path)
    order = []
    inside = threading.Event()

    def writer():
        with embeddings._write_lock(None, c):
            inside.set()
            time.sleep(0.3)
            order.append("write committed")
    t = threading.Thread(target=writer)
    t.start()
    inside.wait(2)
    with vector_index.rewrite(c, drain_s=5):
        order.append("rewrite started")
    t.join()
    assert order == ["write committed", "rewrite started"]


def test_a_write_that_does_not_finish_defers_the_rewrite_and_frees_the_table(tmp_path):
    c = coll(tmp_path)
    w = vector_index.writing(c).__enter__()
    try:
        with pytest.raises(vector_index.RewriteInProgress, match="still in flight"):
            with vector_index.rewrite(c, drain_s=0.2):
                pass
        assert not vector_index.rewriting(c) and not vector_index.busy(c), "nothing left marked"
    finally:
        w.__exit__(None, None, None)
    with vector_index.writing(c):
        pass


def test_other_collections_are_not_held(tmp_path):
    a, b = coll(tmp_path, "normattiva"), coll(tmp_path, "pubmed")
    with vector_index.rewrite(a, drain_s=1):
        with vector_index.writing(b):
            assert vector_index.in_flight(b) == 1

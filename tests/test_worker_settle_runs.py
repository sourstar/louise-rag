"""tests/test_worker_settle_runs.py — the run of a collection that holds
vectors and does not embed here is settled for search once per process
(03 §3.3). Live, 2026-10-07: the node took in the second peer's bundles,
and its vector leg stayed withheld, because the re-check ran only inside
the embed path."""
from types import SimpleNamespace

from app import run_check, worker


def test_a_collection_holding_vectors_is_settled_once_and_one_without_is_left(tmp_path, monkeypatch):
    held = tmp_path / "held"
    (held / "lance" / "chunks.lance").mkdir(parents=True)
    empty = tmp_path / "empty"
    (empty / "lance").mkdir(parents=True)
    a = SimpleNamespace(name="a", manifest_path=held / "manifest.sqlite", lance_dir=held / "lance")
    b = SimpleNamespace(name="b", manifest_path=empty / "manifest.sqlite", lance_dir=empty / "lance")
    monkeypatch.setattr(worker.collections_mod, "load_all", lambda: [a, b])
    monkeypatch.setattr(run_check, "applies", lambda c: True)
    monkeypatch.setattr(worker.db, "connect", lambda c: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(worker, "_settled_for_search", set())
    monkeypatch.setattr(worker, "SETTLE_BEAT_S", 0.0)
    settled = []
    beats = []

    def fake_allowed(con, collection, *, stop=None):
        settled.append(collection.name)
        return True

    class Stop:
        def wait(self, s):
            beats.append(s)
            return len(beats) > 2              # two beats, then stop

        def is_set(self):
            return False
    monkeypatch.setattr(run_check, "embed_allowed", fake_allowed)
    monkeypatch.setattr(worker, "_stop", Stop())
    worker._settle_runs_for_search()
    assert settled == ["a"], "settled once, on the collection that holds vectors"


def test_a_deferral_is_tried_again_at_the_next_beat(tmp_path, monkeypatch):
    held = tmp_path / "held"
    (held / "lance" / "chunks.lance").mkdir(parents=True)
    a = SimpleNamespace(name="a", manifest_path=held / "manifest.sqlite", lance_dir=held / "lance")
    monkeypatch.setattr(worker.collections_mod, "load_all", lambda: [a])
    monkeypatch.setattr(run_check, "applies", lambda c: True)
    monkeypatch.setattr(worker.db, "connect", lambda c: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(worker, "_settled_for_search", set())
    answers = [False, True]
    calls = []

    def fake_allowed(con, collection, *, stop=None):
        calls.append(1)
        return answers.pop(0)
    beats = []

    class Stop:
        def wait(self, s):
            beats.append(s)
            return len(beats) > 3

        def is_set(self):
            return False
    monkeypatch.setattr(run_check, "embed_allowed", fake_allowed)
    monkeypatch.setattr(worker, "_stop", Stop())
    worker._settle_runs_for_search()
    assert len(calls) == 2, "the encoder away once: asked again, then settled and left"

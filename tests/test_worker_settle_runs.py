"""tests/test_worker_settle_runs.py — the run of a collection that holds
vectors and does not embed here is settled for search once per process
(03 §3.3). Live, 2026-10-07: the node took in the second peer's bundles,
and its vector leg stayed withheld, because the re-check ran only inside
the embed path."""
from types import SimpleNamespace

from app import run_check, worker


def _running(monkeypatch):
    """The node in a mode that embeds, nothing paused: a settle embeds."""
    from app import node_state
    monkeypatch.setattr(node_state, "worker_state", lambda *a: "running")
    monkeypatch.setattr(node_state, "paused_collections", lambda *a: set())
    monkeypatch.setattr(worker, "_sweep_unheld_embedders", lambda collections: [])


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
    _running(monkeypatch)
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
    _running(monkeypatch)
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


def test_a_mode_that_does_not_embed_settles_nothing(tmp_path, monkeypatch):
    """A settle embeds sixteen texts: the EMBED activity of the node's mode.
    2026-10-23: the node went seed-only because the operator needed the GPU,
    and this thread, asking every minute, would have loaded an encoder for a
    collection not yet settled. Nor is a collection the node paused settled."""
    from app import node_state
    held = tmp_path / "held"
    (held / "lance" / "chunks.lance").mkdir(parents=True)
    other = tmp_path / "other"
    (other / "lance" / "chunks.lance").mkdir(parents=True)
    a = SimpleNamespace(name="a", manifest_path=held / "manifest.sqlite", lance_dir=held / "lance")
    p = SimpleNamespace(name="p", manifest_path=other / "manifest.sqlite", lance_dir=other / "lance")
    monkeypatch.setattr(worker.collections_mod, "load_all", lambda: [a, p])
    monkeypatch.setattr(run_check, "applies", lambda c: True)
    monkeypatch.setattr(worker.db, "connect", lambda c: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(worker, "_settled_for_search", set())
    monkeypatch.setattr(worker, "_sweep_unheld_embedders", lambda collections: [])
    mode = ["seed-only"]
    monkeypatch.setattr(node_state, "worker_state", lambda *a: mode[0])
    monkeypatch.setattr(node_state, "paused_collections", lambda *a: {"p"})
    settled = []
    monkeypatch.setattr(run_check, "embed_allowed",
                        lambda con, collection, *, stop=None: settled.append(collection.name) or True)
    beats = []

    class Stop:
        def wait(self, s):
            beats.append(s)
            if len(beats) == 3:
                mode[0] = "running"            # the operator resumes
            return len(beats) > 4

        def is_set(self):
            return False
    monkeypatch.setattr(worker, "_stop", Stop())
    worker._settle_runs_for_search()
    assert settled == ["a"], "nothing while seed-only; once running, the held one, never the paused"


def test_a_quiet_profile_still_settles_for_search(tmp_path, monkeypatch):
    """A settle serves searches, and a profile never limits a query
    (node-profiles-design.md decision 6): a worker restarted in a quiet
    window settles as by day, or its vector leg would wait for 08:00."""
    from datetime import datetime, timedelta

    from app import cpu_limit, node_profile, node_state
    from app.node_profile import Active, GpuLevel, Profile
    held = tmp_path / "held"
    (held / "lance" / "chunks.lance").mkdir(parents=True)
    a = SimpleNamespace(name="a", manifest_path=held / "manifest.sqlite", lance_dir=held / "lance")
    monkeypatch.setattr(worker.collections_mod, "load_all", lambda: [a])
    monkeypatch.setattr(run_check, "applies", lambda c: True)
    monkeypatch.setattr(worker.db, "connect", lambda c: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(worker, "_settled_for_search", set())
    _running(monkeypatch)
    monkeypatch.setattr(cpu_limit, "TTL_S", 0.0)
    cpu_limit.reset()
    quiet = Active(profile=Profile("quiet", 0, "full", "acquire-only"), level=GpuLevel("full"),
                   until=datetime.now() + timedelta(hours=1), next="day")
    monkeypatch.setattr(node_profile, "active", lambda now=None: quiet)
    assert not node_state.runs("running", node_state.EMBED), "the quiet window holds embedding"
    settled = []

    class Stop:
        n = 0

        def wait(self, s):
            Stop.n += 1
            return Stop.n > 1

        def is_set(self):
            return False
    monkeypatch.setattr(run_check, "embed_allowed",
                        lambda con, collection, *, stop=None: settled.append(collection.name) or True)
    monkeypatch.setattr(worker, "_stop", Stop())
    worker._settle_runs_for_search()
    assert settled == ["a"]

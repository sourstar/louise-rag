"""tests/share/test_worker_gossip.py — the worker's gossip beat on a node
that holds nothing (04 §4.2). A fresh second peer stayed silent
(2026-10-07): the stats beat skipped everything while no collection was
held, so its digest never left and nobody heard of it."""
import inspect

from app import worker
from app.share import gossip


def test_a_node_that_holds_nothing_still_beats_and_logs_to_stdout(monkeypatch, capsys):
    seen = []

    def fake_beat(collections, *, stop=None, log=None, **kw):
        seen.append(list(collections))
        log("digest sent to 1 recipient(s)")
        return {}
    monkeypatch.setattr(gossip, "beat", fake_beat)
    worker._gossip_beat([], None)
    assert seen == [[]]
    assert "[node info:share] digest sent to 1 recipient(s)" in capsys.readouterr().out


def test_the_stats_beat_gossips_before_its_empty_guard():
    src = inspect.getsource(worker._stats_publisher)
    guard = src.index("if not collections:")
    assert "_gossip_beat([], None)" in src[guard:guard + 600], \
        "the empty guard beats first, then continues"


def test_a_failing_beat_never_kills_the_worker(monkeypatch):
    monkeypatch.setattr(gossip, "beat", lambda *a, **k: 1 / 0)
    worker._gossip_beat([], None)

"""`louise node start` / `node stop` (07 §7.3, §7.7): stop asked for is
exit 0, a signal 130; the request crosses processes through node_state."""
from __future__ import annotations

import pytest

from app import louise, node_state, worker


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("LOUISE_ROOT", str(tmp_path))
    yield tmp_path


@pytest.fixture
def events():
    worker._stop.clear()
    worker._stop_requested.clear()
    yield
    worker._stop.clear()
    worker._stop_requested.clear()


def test_a_stop_request_is_taken_once(root):
    assert node_state.take_stop_request() is False
    node_state.request_stop()
    assert node_state.take_stop_request() is True
    assert node_state.take_stop_request() is False


def test_the_watch_stops_the_worker_with_exit_0(root, events):
    node_state.request_stop()
    worker._watch_stop_request(poll_s=0.01)
    assert worker._stop.is_set() and worker._stop_requested.is_set()
    assert worker.exit_code() == 0


def test_a_signal_is_130(events):
    worker._handle_sigterm(None, None)
    assert worker.exit_code() == 130


def test_a_request_left_from_before_the_start_is_cleared(root, events):
    node_state.request_stop()
    t = worker._start_stop_watch()
    try:
        t.join(timeout=3 * worker.STOP_POLL_S)
        assert t.is_alive() and not worker._stop_requested.is_set()
    finally:
        worker._stop.set()
        t.join(timeout=5)


def test_node_stop_writes_the_request(root, capsys):
    a = louise.build_parser().parse_args(["node", "stop"])
    assert a.fn(a) == 0
    assert node_state.take_stop_request() is True


class _Web:
    def __init__(self, argv):
        self.argv, self.terminated = argv, False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def test_node_start_runs_the_worker_and_stops_the_web(root, monkeypatch):
    import subprocess
    started: list[_Web] = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv: started.append(_Web(argv)) or started[-1])
    monkeypatch.setattr(worker, "main", lambda: 130)
    a = louise.build_parser().parse_args(["node", "start", "--seed-only"])
    assert a.fn(a) == 130
    assert started and "app.api:app" in started[0].argv and started[0].terminated
    assert node_state.worker_state() == "seed-only"


def test_node_start_only_worker_starts_no_web(root, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda argv: pytest.fail("web started"))
    monkeypatch.setattr(worker, "main", lambda: 0)
    a = louise.build_parser().parse_args(["node", "start", "--only", "worker"])
    assert a.fn(a) == 0


def test_07s_spellings_parse():
    p = louise.build_parser()
    a = p.parse_args(["hold", "collection", "continue", "c", "--platform-image", "D", "--through", "S"])
    assert a.continues_through == "S"
    a = p.parse_args(["share", "serve", "gutenberg"])
    assert a.fn is louise.cmd_share and a.collection == "gutenberg"


def test_every_share_verb_escapes_the_bare_form():
    """`share <collection>` is rewritten to `share collection <collection>`
    unless the word is a verb: a verb missing from `_SHARE_VERBS` would be
    taken for a collection's name."""
    import argparse
    p = louise.build_parser()
    surfaces = next(x for x in p._actions if isinstance(x, argparse._SubParsersAction))
    share = surfaces.choices["share"]
    verbs = next(x for x in share._actions if isinstance(x, argparse._SubParsersAction))
    assert set(verbs.choices) <= set(louise._SHARE_VERBS)

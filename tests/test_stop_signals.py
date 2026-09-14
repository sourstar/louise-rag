"""A hangup ends a command, or the worker, the way SIGINT and SIGTERM do.

rsync leads its own session (app/acquire/transport_rsync.py), so a terminal
that goes away — a dropped ssh, a `docker exec -it` — no longer reaches it.
Left at its default, the hangup killed the command and stranded rsync
writing its .part, its bandwidth lease already released (review 2026-09-14).
"""
import argparse
import signal
from types import SimpleNamespace

import pytest

from app import cli, worker

_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class _Done(Exception):
    pass


@pytest.fixture
def handlers():
    saved = {s: signal.getsignal(s) for s in _SIGNALS}
    yield
    for s, h in saved.items():
        signal.signal(s, h)


def test_corpus_add_routes_a_hangup_to_its_stop(handlers, monkeypatch):
    seen = {}

    def run_acquisition(con, collection, spec, profile, stop=None):
        seen.update({s: signal.getsignal(s) for s in _SIGNALS})
        raise _Done

    monkeypatch.setattr(cli, "_collection_by_name",
                        lambda name: SimpleNamespace(name=name, plugin="p"))
    monkeypatch.setattr(cli, "ACQUIRE_REGISTRY", {"p": SimpleNamespace(terms_text_path=None)})
    monkeypatch.setattr(cli.db, "connect", lambda collection: None)
    monkeypatch.setattr(cli.engine, "run_acquisition", run_acquisition)
    with pytest.raises(_Done):
        cli.cmd_add(argparse.Namespace(collection="c", ack_terms=None,
                                       profile="default", dry_run=False))
    assert seen == {s: cli._handle_sigterm for s in _SIGNALS}, seen


def test_a_hangup_before_any_stop_is_bound_is_a_ctrl_c(handlers, monkeypatch):
    """`corpus status` lists without a stop: the hangup must arrive as the
    KeyboardInterrupt the transport cleans up after, not kill the process."""
    seen = []
    monkeypatch.setattr(cli, "cmd_status",
                        lambda args: seen.append(signal.getsignal(signal.SIGHUP)) or 0)
    assert cli.main(["corpus", "status"]) == 0
    assert seen == [signal.default_int_handler]


def test_the_worker_routes_a_hangup_to_its_stop(monkeypatch):
    installed = {}
    monkeypatch.setattr(worker.signal, "signal", lambda s, h: installed.__setitem__(s, h))

    def load_all():
        raise _Done

    monkeypatch.setattr(worker.collections_mod, "load_all", load_all)
    with pytest.raises(_Done):
        worker.run_forever()
    assert installed == {s: worker._handle_sigterm for s in _SIGNALS}, installed

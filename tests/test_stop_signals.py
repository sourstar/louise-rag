"""A hangup ends a command, or the worker, the way SIGINT and SIGTERM do.

rsync leads its own session (app/acquire/transport_rsync.py), so a terminal
that goes away — a dropped ssh, a `docker exec -it` — no longer reaches it.
Left at its default, the hangup killed the command and stranded rsync
writing its .part, its bandwidth lease already released (review 2026-09-14).
The command is `louise hold records fetch` (was `corpus add`, 07 §7.6).
"""
import signal
from types import SimpleNamespace

import pytest

from app import collections as collections_mod, db, louise, worker
from app.acquire import engine
from app.acquire.specs import ACQUIRE_REGISTRY
from app.hold import records_fetch

_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class _Done(Exception):
    pass


@pytest.fixture
def handlers():
    saved = {s: signal.getsignal(s) for s in _SIGNALS}
    yield
    for s, h in saved.items():
        signal.signal(s, h)


@pytest.fixture
def wired(monkeypatch):
    c = SimpleNamespace(name="c", plugin="p", profile=None)
    monkeypatch.setattr(collections_mod, "by_name", lambda name: c)
    monkeypatch.setitem(ACQUIRE_REGISTRY, "p", SimpleNamespace(terms_text_path=None))
    monkeypatch.setattr(db, "connect", lambda collection: SimpleNamespace(close=lambda: None))
    return c


def test_records_fetch_routes_a_hangup_to_its_stop(handlers, wired, monkeypatch):
    seen = {}

    def run_acquisition(con, collection, spec, profile, stop=None):
        seen.update({s: signal.getsignal(s) for s in _SIGNALS})
        raise _Done

    monkeypatch.setattr(engine, "run_acquisition", run_acquisition)
    with pytest.raises(_Done):
        louise.main(["hold", "records", "fetch", "c"])
    assert seen == {s: records_fetch._handle_sigterm for s in _SIGNALS}, seen


def test_a_hangup_before_any_stop_is_bound_is_a_ctrl_c(handlers, wired, monkeypatch):
    """A dry run lists without a stop: the hangup must arrive as the
    KeyboardInterrupt the transport cleans up after, not kill the process."""
    seen = []
    monkeypatch.setattr(engine, "plan_acquisition",
                        lambda *a: seen.append(signal.getsignal(signal.SIGHUP)) or _Plan())
    assert louise.main(["hold", "records", "fetch", "c", "--dry-run"]) == 0
    assert seen == [signal.default_int_handler]


class _Plan:
    files_pending = files_total = bytes_pending = bytes_total = 0
    eta_seconds = 0.0
    license_state = "not-required"


def test_the_worker_routes_a_hangup_to_its_stop(monkeypatch):
    installed = {}
    monkeypatch.setattr(worker.signal, "signal", lambda s, h: installed.__setitem__(s, h))

    def load_all():
        raise _Done

    monkeypatch.setattr(worker.collections_mod, "load_all", load_all)
    with pytest.raises(_Done):
        worker.run_forever()
    assert installed == {s: worker._handle_sigterm for s in _SIGNALS}, installed


def test_the_worker_dumps_every_threads_stack_on_sigusr1_and_goes_on(monkeypatch):
    """Perugia, 2026-10-20: a worker at 98 % CPU with the GPU idle could not
    say where it was, and SIGUSR1 — the CLI's instrument — would have killed
    it. The worker registers the same handler: every thread, chain=False
    (chained, the default action ran after the dump and terminated the
    process, exit 138), and a registration that fails never stops a start."""
    import faulthandler
    import signal

    from app import worker

    seen = {}

    def register(sig, file=None, all_threads=True, chain=False):
        seen.update(sig=sig, all_threads=all_threads, chain=chain)
    monkeypatch.setattr(faulthandler, "register", register)
    assert worker._register_stack_dump() is True
    assert seen == {"sig": signal.SIGUSR1, "all_threads": True, "chain": False}

    def refuses(*a, **k):
        raise RuntimeError("no signal support in this thread")
    monkeypatch.setattr(faulthandler, "register", refuses)
    assert worker._register_stack_dump() is False, "never a reason not to start"

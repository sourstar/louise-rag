"""A STOP ends the listing, not only the transfer after it.

The listing ran with no stop at all: `rsync --list-only` was one
`subprocess.run` (~12 minutes measured on gutenberg, capped at 30 per
mirror), a magnet's metadata wait took up to 120 s, a mirror benchmark up to
20 s per mirror. A pause, seed-only, removal or SIGTERM arriving in any of
them was seen only when the transfer pool started. And the ways an abort
could escape were wrong in themselves: TransferAborted IS a TransportError,
so the mirrored listing marked the mirror down and listed the next one in
full; the benchmark recorded a stopped probe as a FAILED mirror for six
hours; the torrent-index resolve swallowed it as "plain torrent for now";
the worker logged it as an ERROR; and shutdown never joined the fetch at
all.

These tests drive the real paths — the engine's listing, run_acquisition,
the worker's own `_acquire_worker` — with transports that block until the
bound stop fires, never a function called by hand in place of the wiring.
The fakes set the stop THEMSELVES, from inside the listing or probe: a
timer could fire before the code under test got there, and the test would
pass on an earlier check without exercising the bind (review, 2026-09-13).
"""
import subprocess
import threading
import time

import pytest

from app import collections as collections_mod, db
from app.acquire import claim as claim_mod
from app.acquire import engine, mirrors, transport_ftp, transport_torrent
from app.acquire import transport_rsync as tr
from app.acquire.spec import (AcquisitionSpec, ChecksumSource, DeltaSpec, Endpoint,
                              Filter, TransferAborted, Transport, TransportError)
from app.acquire.transports import REGISTRY
from app.plugins.base import LicenseClass
from app.transfer.bucket import Priority
from app.transfer.manager import Direction, current_handle, get_manager


def _spec(*urls, probe=None, transport=Transport.RSYNC):
    return AcquisitionSpec(
        source_id="g", transport=transport,
        endpoints=tuple(Endpoint(transport=transport, url=u) for u in urls),
        layout="one-dir-per-doc", checksums=ChecksumSource(kind="none"),
        delta=DeltaSpec(mode="full-resync"), profiles={"demo": Filter()},
        estimated_bytes={"demo": 1}, license_class=LicenseClass.GREEN,
        probe_file=probe)


def _collection(tmp_path, name="g"):
    base = tmp_path / name
    (base / "corpus").mkdir(parents=True)
    return collections_mod.Collection(
        name=name, plugin="gutenberg", profile="demo", embed_mode_default=None,
        corpus_dir=base / "corpus", manifest_path=base / "manifest.db",
        fts_dir=base / "fts", lance_dir=base / "lancedb")


def _bound(stop):
    return get_manager().bind(Priority.BACKFILL, Direction.DOWN, stop=stop)


class _ListsUntilStopped:
    """A transport whose listing blocks until the stop it finds bound fires
    — the shape of a long rsync listing — and records what it listed.
    With `stop_on_start`, it fires that stop itself once it is INSIDE the
    listing, under the bind."""

    def __init__(self, stop_on_start: threading.Event | None = None):
        self.listed: list[str] = []
        self.started = threading.Event()
        self._stop_on_start = stop_on_start

    def list_files(self, endpoint, spec):
        self.listed.append(endpoint.url)
        handle = current_handle()
        assert handle is not None and handle.stop is not None, \
            "the listing runs under the pass's bound stop"
        self.started.set()
        if self._stop_on_start is not None:
            self._stop_on_start.set()
        deadline = time.monotonic() + 10
        while not handle.stop.is_set():
            if time.monotonic() > deadline:
                raise AssertionError("the stop never fired")
            time.sleep(0.01)
        raise TransferAborted("listing stopped")
        yield   # a generator, as every real listing is


# ---------------- the rsync listing itself ----------------

class _EndlessListing:
    """`rsync --list-only` that never finishes on its own."""

    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def communicate(self, timeout=None):
        if self.terminated or self.killed:
            self.returncode = -15
            return "-rw-r--r--      1 2020/01/01 10:00:00 half\n", ""
        time.sleep(min(timeout or 0.0, 0.01))
        raise subprocess.TimeoutExpired("rsync", timeout)

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_a_stop_ends_the_rsync_listing_and_nothing_partial_is_returned(monkeypatch):
    proc = _EndlessListing()
    polled = threading.Event()
    real_communicate = proc.communicate

    def communicate(timeout=None):
        polled.set()                # the listing is running, under the bind
        return real_communicate(timeout)

    proc.communicate = communicate
    monkeypatch.setattr(tr.subprocess, "Popen", lambda argv, **k: proc)
    monkeypatch.setattr(tr, "_POLL_SECONDS", 0.01)
    stop = threading.Event()
    endpoint = Endpoint(transport=Transport.RSYNC, url="host::mod")
    got: list = []

    def _consume():
        with _bound(stop):
            try:
                got.extend(tr.RsyncTransport().list_files(endpoint, None))
            except TransferAborted as e:
                got.append(e)

    t = threading.Thread(target=_consume)
    t.start()
    assert polled.wait(5), "the listing began"
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive(), "the listing ended within its poll"
    assert proc.terminated, "the child is terminated, not orphaned"
    assert len(got) == 1 and isinstance(got[0], TransferAborted), \
        f"an abort RAISES; the half-read listing is never yielded: {got}"


def test_a_listing_past_its_deadline_is_a_transport_error_not_a_stop(monkeypatch):
    proc = _EndlessListing()
    monkeypatch.setattr(tr.subprocess, "Popen", lambda argv, **k: proc)
    monkeypatch.setattr(tr, "_POLL_SECONDS", 0.01)
    monkeypatch.setattr(tr, "_LISTING_TIMEOUT", 0.05)
    with pytest.raises(TransportError) as e:
        list(tr.RsyncTransport().list_files(
            Endpoint(transport=Transport.RSYNC, url="host::mod"), None))
    assert not isinstance(e.value, TransferAborted)
    assert proc.killed


def test_a_failed_listing_carries_rsyncs_own_words(monkeypatch):
    class _Failed:
        returncode = 23

        def communicate(self, timeout=None):
            return "", "@ERROR: Unknown module 'mod'"

    monkeypatch.setattr(tr.subprocess, "Popen", lambda argv, **k: _Failed())
    with pytest.raises(TransportError, match="exit 23.*Unknown module"):
        list(tr.RsyncTransport().list_files(
            Endpoint(transport=Transport.RSYNC, url="host::mod"), None))


# ---------------- the other listings ----------------

def test_the_torrent_listing_waits_for_metadata_under_the_bound_stop(monkeypatch):
    seen = {}

    def fake_info(endpoint, stop=None):
        seen["stop"] = stop
        raise TransferAborted("stopped while waiting for torrent metadata")

    monkeypatch.setattr(transport_torrent, "torrent_info", fake_info)
    stop = threading.Event()
    with _bound(stop), pytest.raises(TransferAborted):
        list(transport_torrent.TorrentTransport().list_files(
            Endpoint(transport=Transport.TORRENT, url="magnet:?xt=urn:btih:" + "0" * 40),
            None))
    assert seen["stop"] is stop


def test_a_stopped_ftp_listing_closes_rather_than_quits(monkeypatch):
    """QUIT mid-listing can block for the socket timeout or raise an ftplib
    error that REPLACES the abort — fetch_one learned this; the listing had
    not."""
    calls = []

    class _FTP:
        def mlsd(self):
            return iter([("a.xml.gz", {"type": "file", "size": "1"}),
                         ("b.xml.gz", {"type": "file", "size": "2"})])

        def quit(self):
            calls.append("quit")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(transport_ftp, "_connect", lambda endpoint: _FTP())
    stop = threading.Event()
    endpoint = Endpoint(transport=Transport.FTP, url="ftp.example/baseline")
    with _bound(stop):
        listing = transport_ftp.FtpTransport().list_files(endpoint, None)
        assert next(listing).rel_path == "a.xml.gz"
        stop.set()
        with pytest.raises(TransferAborted):
            next(listing)
    assert calls == ["close"]

    calls.clear()
    assert [f.rel_path for f in transport_ftp.FtpTransport().list_files(endpoint, None)] \
        == ["a.xml.gz", "b.xml.gz"]
    assert calls == ["quit"], "a clean listing still says goodbye"


# ---------------- the engine ----------------

def test_a_stopped_mirrored_listing_marks_no_mirror_down_and_lists_no_other(monkeypatch):
    mirrors.reset_down_state()
    stop = threading.Event()
    fake = _ListsUntilStopped(stop_on_start=stop)
    monkeypatch.setitem(REGISTRY, Transport.RSYNC, fake)
    spec = _spec("host-a::mod", "host-b::mod", probe="1/pg1.rdf")
    with pytest.raises(TransferAborted):
        engine._list_and_filter(spec, "demo", endpoints=list(spec.endpoints), stop=stop)
    assert fake.listed == ["host-a::mod"], "the next mirror is not listed in full"
    assert not mirrors.is_down("host-a::mod"), "the stop is not the mirror's fault"


def test_without_a_stop_the_listing_binds_nothing(monkeypatch):
    """A dry-run plan (web, CLI) lists exactly as before."""
    seen = []

    class _Plain:
        def list_files(self, endpoint, spec):
            seen.append(current_handle())
            return iter(())

    monkeypatch.setitem(REGISTRY, Transport.RSYNC, _Plain())
    engine._list_and_filter(_spec("host::mod"), "demo")
    assert seen == [None]


def test_a_pass_stopped_during_its_listing_returns_stopped_and_records_nothing(
        isolated_config, tmp_path, monkeypatch):
    stop = threading.Event()
    fake = _ListsUntilStopped(stop_on_start=stop)
    monkeypatch.setitem(REGISTRY, Transport.RSYNC, fake)
    collection = _collection(tmp_path)
    con = db.connect(collection)

    result = engine.run_acquisition(con, collection, _spec("host::mod"), "demo", stop=stop)

    assert fake.listed == ["host::mod"], "the stop was seen INSIDE the listing"
    assert result["fetched"] == 0 and "stopped" in result, result
    assert db.get_control(con, engine.LISTING_KEY) is None, \
        "a stopped pass records no listing"
    assert db.get_control(con, claim_mod.CLAIM_KEY) in (None, ""), "the claim is released"


def test_a_stopped_multi_torrent_pass_still_reports_every_counter(isolated_config,
                                                                  tmp_path, monkeypatch):
    """Review, reproduced: stopped inside the FIRST torrent's listing, the
    pass returned `{"stopped": ...}` alone and `corpus add` died on
    `result['fetched']`. The stop-aware metadata wait opened that path."""
    stop = threading.Event()
    fake = _ListsUntilStopped(stop_on_start=stop)
    monkeypatch.setitem(REGISTRY, Transport.TORRENT, fake)
    collection = _collection(tmp_path)
    spec = _spec("magnet:?xt=urn:btih:" + "1" * 40, "magnet:?xt=urn:btih:" + "2" * 40,
                 transport=Transport.TORRENT)

    result = engine.run_acquisition(db.connect(collection), collection, spec, "demo",
                                    stop=stop)

    assert fake.listed == [spec.endpoints[0].url], "stopped inside the first torrent"
    assert "stopped" in result
    for key in ("fetched", "skipped", "failed", "aborted", "adopted", "total"):
        assert result[key] == 0, (key, result)


def test_a_stopped_benchmark_records_nothing(isolated_config, tmp_path, monkeypatch):
    """A probe the stop cut short says nothing about the mirror. Recorded,
    it ranked every mirror FAILED for BENCH_TTL_S — and without
    copy_context the probe thread never saw the stop at all."""
    stop = threading.Event()

    class _Probe:
        def fetch_one(self, endpoint, remote, dest, resume_from):
            handle = current_handle()
            assert handle is not None and handle.stop is stop, \
                "the bind reaches the probe's own thread"
            stop.set()              # the stop fires while the probe is in flight
            raise TransferAborted("probe stopped")

    monkeypatch.setitem(REGISTRY, Transport.RSYNC, _Probe())
    con = db.connect(_collection(tmp_path))
    with _bound(stop), pytest.raises(TransferAborted):
        mirrors.benchmark(con, _spec("host-a::mod", "host-b::mod", probe="1/pg1.rdf"),
                          stop=stop)
    assert mirrors.read_bench(con) is None


def test_a_stopped_index_resolve_is_not_a_plain_torrent(monkeypatch):
    from app.acquire import torrent_collection
    monkeypatch.setattr(torrent_collection, "cached_index", lambda c, s: None)

    def stopped(collection, source, *, stop=None, force=False):
        raise TransferAborted("stopped while waiting for torrent metadata")

    monkeypatch.setattr(torrent_collection, "resolve", stopped)
    with pytest.raises(TransferAborted):
        engine._index_for(object(), "magnet:?xt=urn:btih:" + "0" * 40, threading.Event())

    def unreachable(collection, source, *, stop=None, force=False):
        raise TransportError("no peer holds it")

    monkeypatch.setattr(torrent_collection, "resolve", unreachable)
    assert engine._index_for(object(), "magnet:?xt=urn:btih:" + "0" * 40) is None, \
        "unreachable is still 'plain torrent for now'"


def _index_torrent(monkeypatch, fetch_one):
    """A magnet whose metadata says it is a torrent collection, with the
    index download replaced by `fetch_one`."""
    from app.acquire import torrent_collection
    monkeypatch.setattr(transport_torrent, "torrent_info", lambda endpoint, stop=None: object())
    monkeypatch.setattr(transport_torrent, "_paths_and_sizes",
                        lambda info: [("coll/" + torrent_collection.INDEX_FILE, 100)])
    monkeypatch.setattr(torrent_collection, "_hash_of", lambda info: "h")
    monkeypatch.setattr(transport_torrent.TorrentTransport, "fetch_one", fetch_one)
    return torrent_collection


def _served_never(self, endpoint, remote, dest, resume_from):
    handle = current_handle()
    while not handle.stop.is_set():
        time.sleep(0.01)
    raise TransferAborted("stopped")


def test_an_index_the_swarm_will_not_serve_ends_at_its_deadline(isolated_config,
                                                                tmp_path, monkeypatch):
    """fetch_one's download loop has no deadline of its own, and the grow
    plan runs this inside a WEB REQUEST with no stop: a swarm announcing the
    index but serving none of it held the request forever. The deadline is
    a TransportError — "unreachable, plain torrent for now" — never a stop."""
    tc = _index_torrent(monkeypatch, _served_never)
    monkeypatch.setattr(tc, "INDEX_FETCH_TIMEOUT_S", 0.1)
    started = time.monotonic()
    with pytest.raises(TransportError) as e:
        tc.resolve(_collection(tmp_path), "magnet:?xt=urn:btih:" + "0" * 40)
    assert not isinstance(e.value, TransferAborted)
    assert time.monotonic() - started < 5


def test_the_callers_stop_still_ends_the_index_download_as_a_stop(isolated_config,
                                                                   tmp_path, monkeypatch):
    tc = _index_torrent(monkeypatch, _served_never)
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()
    with pytest.raises(TransferAborted):
        tc.resolve(_collection(tmp_path), "magnet:?xt=urn:btih:" + "0" * 40, stop=stop)


# ---------------- the worker ----------------

def _events(collection):
    return [(r["level"], r["message"]) for r in db.connect(collection).execute(
        "SELECT level, message FROM events ORDER BY id")]


def test_a_pause_during_the_listing_stops_the_fetch_and_rearms_it(
        isolated_config, tmp_path, monkeypatch):
    from app import worker
    fake = _ListsUntilStopped()
    monkeypatch.setitem(REGISTRY, Transport.RSYNC, fake)
    collection = _collection(tmp_path)
    t = threading.Thread(target=worker._acquire_worker,
                         args=(collection, _spec("host::mod"), "demo", "test"),
                         daemon=True)
    t.start()
    try:
        assert fake.started.wait(5), "the listing began"
        worker._paused.set()
        t.join(timeout=10)
        assert not t.is_alive(), "the pause ended the listing"
        with worker._acquire_lock:
            assert collection.name in worker._paused_aborts, \
                "a fetch the pause stopped is re-armed by the resume"
        events = _events(collection)
        assert not [m for level, m in events if level == "error"], events
        assert any("stopped" in m for _, m in events), events
    finally:
        worker._paused.clear()
        with worker._acquire_lock:
            worker._paused_aborts.discard(collection.name)
            worker._acquire_done.pop(collection.name, None)


def test_shutdown_waits_for_a_listing_to_stop(isolated_config, tmp_path, monkeypatch):
    """SIGTERM used to abandon the fetch thread at exit — listing and all,
    its claim row left for the next boot to wait out."""
    from app import worker
    fake = _ListsUntilStopped()
    monkeypatch.setitem(REGISTRY, Transport.RSYNC, fake)
    collection = _collection(tmp_path)
    t = threading.Thread(target=worker._acquire_worker,
                         args=(collection, _spec("host::mod"), "demo", "test"),
                         daemon=True)
    with worker._acquire_lock:
        worker._acquire_threads[collection.name] = t
    t.start()
    try:
        assert fake.started.wait(5)
        worker._stop.set()
        worker._join_pipeline_threads(timeout=10)
        assert not t.is_alive(), "shutdown joined the fetch instead of abandoning it"
        assert db.get_control(db.connect(collection), claim_mod.CLAIM_KEY) in (None, ""), \
            "and its claim was released"
        with worker._acquire_lock:
            assert collection.name not in worker._paused_aborts, \
                "SIGTERM is not a pause: nothing to re-arm"
    finally:
        worker._stop.clear()
        with worker._acquire_lock:
            worker._acquire_threads.pop(collection.name, None)
            worker._acquire_done.pop(collection.name, None)

"""P0.7 acceptance against the real acquisition engine: SIGTERM drains
losslessly, per-endpoint etiquette is respected even when global worker slots
are free, and parallel workers never touch the SQLite connection.

Every test that starts a thread carries a watchdog — there is no
pytest-timeout in this repo, so a lost wakeup would hang CI rather than fail.
"""
import threading
import time

import pytest

from app import db, platform_config
from app.acquire import engine
from app.acquire.spec import (AcquisitionSpec, ChecksumSource, DeltaSpec, Endpoint,
                               Filter, RemoteFile, Transport)
from app import collections as collections_mod
from app.plugins.base import LicenseClass
from app.transfer import schedule as sched
from app.transfer.manager import TransferManager, set_manager

WATCHDOG = 20.0


@pytest.fixture(autouse=True)
def _isolated_manager():
    """Every test gets a fresh manager; the singleton is process-wide."""
    yield
    set_manager(None)


def _collection(tmp_path, **overrides):
    defaults = dict(
        name="test", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb",
        acquire_enabled=True, license_ack=None,
    )
    defaults.update(overrides)
    defaults["corpus_dir"].mkdir(parents=True, exist_ok=True)
    return collections_mod.Collection(**defaults)


def _spec(mock_server, endpoints=None, **overrides):
    static_files = tuple(
        RemoteFile(rel_path=p.name, size=p.stat().st_size)
        for p in sorted(mock_server.root.iterdir()) if p.is_file())
    defaults = dict(
        source_id="test", transport=Transport.HTTPS,
        endpoints=endpoints or (Endpoint(transport=Transport.HTTPS, url=mock_server.base_url,
                                          static_files=static_files),),
        layout="single-file", checksums=ChecksumSource(kind="none"),
        delta=DeltaSpec(mode="full-resync"),
        profiles={"demo": Filter()}, estimated_bytes={"demo": 1},
        license_class=LicenseClass.GREEN,
    )
    defaults.update(overrides)
    return AcquisitionSpec(**defaults)


def _manager(down=None, workers=8, per_endpoint=4):
    platform = platform_config.PlatformConfig(
        bandwidth=platform_config.BandwidthConfig(
            down_max_bps=sched.parse_rate(down), reserve_interactive=0.2),
        transfer=platform_config.TransferConfig(workers=workers, per_endpoint=per_endpoint))
    manager = TransferManager(platform=platform)
    set_manager(manager)
    return manager


def _write(mock_server, name, data):
    (mock_server.root / name).write_bytes(data)


# ---------------- SIGTERM drain ----------------

def test_sigterm_drains_losslessly_and_leaves_retries_untouched(tmp_path, mock_server):
    """Acceptance: 'SIGTERM drains: in-flight files finish or checkpoint;
    nothing corrupts.' The BUCKET is what makes this transfer slow, so the
    stop event has to be observed inside the bandwidth wait — which is where
    threads actually park on a real shutdown."""
    payload = bytes((i % 256) for i in range(400_000))
    _write(mock_server, "big.dat", payload)
    _manager(down="60KB", workers=1)          # ~7 s to transfer: bucket-bound
    collection = _collection(tmp_path)
    spec = _spec(mock_server)

    stop = threading.Event()
    box = {}

    def _run():
        # run_acquisition's own thread owns `con` exclusively (SQLite
        # connections are thread-affine), so it is opened here rather than
        # handed in from the main thread.
        con = db.connect(collection)
        box["result"] = engine.run_acquisition(con, collection, spec, "demo", stop=stop)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(1.0)                            # let it get genuinely underway
    started = time.monotonic()
    stop.set()
    from app.transfer.manager import get_manager
    get_manager().wake_all()
    t.join(WATCHDOG)
    assert not t.is_alive(), "run_acquisition did not drain on stop"
    assert time.monotonic() - started < 3.0, "stop was not observed promptly"

    result = box["result"]
    assert result["aborted"] >= 1
    assert result["failed"] == 0, "a clean shutdown must not be recorded as a failure"

    con = db.connect(collection)
    row = con.execute(
        "SELECT fetch_done, bytes_fetched, retries, error FROM fetch_files "
        "WHERE rel_path='big.dat'").fetchone()
    assert row["fetch_done"] == 0
    assert row["retries"] == 0, "a clean SIGTERM must not burn a retry"
    assert row["error"] is None, "a clean SIGTERM is not an error"

    part = collection.corpus_dir / "big.dat.part"
    assert part.is_file()
    assert row["bytes_fetched"] == part.stat().st_size    # manifest matches disk
    assert part.read_bytes() == payload[:part.stat().st_size]
    assert not (collection.corpus_dir / "big.dat").exists()


def test_aborted_run_resumes_with_a_range_request(tmp_path, mock_server):
    payload = bytes((i % 256) for i in range(400_000))
    _write(mock_server, "big.dat", payload)
    _manager(down="60KB", workers=1)
    collection = _collection(tmp_path)
    spec = _spec(mock_server)

    stop = threading.Event()

    def _run():
        con = db.connect(collection)           # thread-affine; opened here
        engine.run_acquisition(con, collection, spec, "demo", stop=stop)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(1.0)
    stop.set()
    from app.transfer.manager import get_manager
    get_manager().wake_all()
    t.join(WATCHDOG)
    assert not t.is_alive()
    part_size = (collection.corpus_dir / "big.dat.part").stat().st_size
    assert part_size > 0

    _manager(down=None, workers=1)             # unthrottled second run
    con = db.connect(collection)
    result = engine.run_acquisition(con, collection, spec, "demo")
    assert result["fetched"] == 1
    assert mock_server.last_range("big.dat") == f"bytes={part_size}-"
    assert (collection.corpus_dir / "big.dat").read_bytes() == payload


def test_stop_before_start_fetches_nothing(tmp_path, mock_server):
    _write(mock_server, "a.txt", b"x" * 100)
    _manager(workers=4)
    collection = _collection(tmp_path)
    spec = _spec(mock_server)
    con = db.connect(collection)

    stop = threading.Event()
    stop.set()
    result = engine.run_acquisition(con, collection, spec, "demo", stop=stop)
    assert result["fetched"] == 0
    assert mock_server.request_count("a.txt") == 0, "no new file may start after stop"


# ---------------- per-endpoint etiquette ----------------

def test_endpoint_concurrency_cap_holds_even_with_free_global_slots(tmp_path, mock_server):
    """Acceptance: 'etiquette cap respected per endpoint even when global slots
    are free.' 8 global workers, but the endpoint declares max_conns=2."""
    for i in range(8):
        _write(mock_server, f"f{i}.dat", b"y" * 20_000)
        mock_server.drip(f"f{i}.dat", 200_000)     # slow enough to overlap
    static_files = tuple(
        RemoteFile(rel_path=p.name, size=p.stat().st_size)
        for p in sorted(mock_server.root.iterdir()) if p.is_file())
    endpoint = Endpoint(transport=Transport.HTTPS, url=mock_server.base_url,
                         max_conns=2, static_files=static_files)
    _manager(workers=8, per_endpoint=8)            # global limits deliberately loose
    collection = _collection(tmp_path)
    spec = _spec(mock_server, endpoints=(endpoint,))
    con = db.connect(collection)

    result = engine.run_acquisition(con, collection, spec, "demo")
    assert result["fetched"] == 8
    assert mock_server.peak_concurrency <= 2, (
        f"etiquette breached: {mock_server.peak_concurrency} concurrent requests "
        f"against an endpoint declaring max_conns=2")
    assert mock_server.peak_concurrency > 1, "test did not actually exercise parallelism"


def test_global_per_endpoint_setting_can_tighten_below_the_endpoint_declaration(
        tmp_path, mock_server):
    for i in range(6):
        _write(mock_server, f"g{i}.dat", b"z" * 20_000)
        mock_server.drip(f"g{i}.dat", 200_000)
    static_files = tuple(
        RemoteFile(rel_path=p.name, size=p.stat().st_size)
        for p in sorted(mock_server.root.iterdir()) if p.is_file())
    endpoint = Endpoint(transport=Transport.HTTPS, url=mock_server.base_url,
                         max_conns=8, static_files=static_files)
    _manager(workers=8, per_endpoint=1)
    collection = _collection(tmp_path)
    spec = _spec(mock_server, endpoints=(endpoint,))
    con = db.connect(collection)

    engine.run_acquisition(con, collection, spec, "demo")
    assert mock_server.peak_concurrency == 1


# ---------------- parallelism correctness ----------------

def test_parallel_workers_never_touch_the_sqlite_connection(tmp_path, mock_server):
    """db.connect() uses check_same_thread=True, so a worker touching `con`
    raises ProgrammingError. Eight files fetched successfully through eight
    workers IS the proof that every manifest write stayed on this thread."""
    for i in range(8):
        _write(mock_server, f"p{i}.dat", b"w" * 5_000)
    _manager(workers=8, per_endpoint=8)
    collection = _collection(tmp_path)
    spec = _spec(mock_server)
    con = db.connect(collection)

    result = engine.run_acquisition(con, collection, spec, "demo")
    assert result["fetched"] == 8
    assert result["failed"] == 0
    rows = con.execute("SELECT COUNT(*) c FROM fetch_files WHERE fetch_done=1").fetchone()
    assert rows["c"] == 8


def test_one_failing_file_does_not_lose_results_for_the_others(tmp_path, mock_server):
    """A worker that raised would make the executor's iterator throw partway
    through and silently drop every later item's manifest row (invariant 2)."""
    for i in range(6):
        _write(mock_server, f"q{i}.dat", b"v" * 5_000)
    _manager(workers=4, per_endpoint=4)
    collection = _collection(tmp_path)
    spec = _spec(mock_server)
    con = db.connect(collection)
    mock_server.stall_after_bytes("q2.dat", 100)

    result = engine.run_acquisition(con, collection, spec, "demo")
    assert result["failed"] == 1
    assert result["fetched"] == 5
    counted = con.execute("SELECT COUNT(*) c FROM fetch_files").fetchone()["c"]
    assert counted == 6, "every file must have a manifest row, failures included"


# ---------------- sidecar pairing ----------------

def test_failed_sidecar_refuses_to_record_an_unverified_fetch(tmp_path, mock_server):
    """Before P0.7, a sidecar whose download failed left verify_sidecar
    silently no-opping and the data file marked fetch_done=1 UNVERIFIED."""
    _write(mock_server, "d.dat", b"payload-bytes")
    _write(mock_server, "d.dat.md5", b"MD5(d.dat)= 00000000000000000000000000000000")
    mock_server.stall_after_bytes("d.dat.md5", 3)
    _manager(workers=2)
    collection = _collection(tmp_path)
    spec = _spec(mock_server, checksums=ChecksumSource(kind="sidecar", sidecar_suffix=".md5"))
    con = db.connect(collection)

    result = engine.run_acquisition(con, collection, spec, "demo")
    assert result["fetched"] == 0
    row = con.execute(
        "SELECT fetch_done, error FROM fetch_files WHERE rel_path='d.dat'").fetchone()
    assert row["fetch_done"] == 0
    assert "sidecar" in (row["error"] or "")


def test_sidecar_and_data_are_both_fetched_and_verified(tmp_path, mock_server):
    import hashlib
    payload = b"payload-bytes"
    _write(mock_server, "e.dat", payload)
    _write(mock_server, "e.dat.md5",
           f"MD5(e.dat)= {hashlib.md5(payload).hexdigest()}".encode())
    _manager(workers=2)
    collection = _collection(tmp_path)
    spec = _spec(mock_server, checksums=ChecksumSource(kind="sidecar", sidecar_suffix=".md5"))
    con = db.connect(collection)

    result = engine.run_acquisition(con, collection, spec, "demo")
    assert result["fetched"] == 2      # data + sidecar
    assert result["failed"] == 0
    assert (collection.corpus_dir / "e.dat").read_bytes() == payload


# ---------------- budget enforcement ----------------

def test_throttled_acquisition_is_actually_slower_than_unthrottled(tmp_path, mock_server):
    """Invariant 5, end to end: the bucket must genuinely pace real bytes, not
    merely count them."""
    payload = b"k" * 300_000
    _write(mock_server, "t.dat", payload)
    _manager(down="150KB", workers=1)
    collection = _collection(tmp_path)
    spec = _spec(mock_server)
    con = db.connect(collection)

    started = time.monotonic()
    result = engine.run_acquisition(con, collection, spec, "demo")
    elapsed = time.monotonic() - started
    assert result["fetched"] == 1
    # 300 KB at 150 KB/s is ~2 s; allow generous slack for a loaded CI box but
    # require it to be clearly slower than the sub-100 ms loopback baseline.
    assert elapsed > 1.0, f"throttling had no effect (took {elapsed:.2f}s)"

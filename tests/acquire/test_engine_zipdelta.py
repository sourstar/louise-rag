"""The engine's `zip-directory` delta (app/acquire/engine.py `_zip_delta`;
09 §9.17 amended 2026-10-12; review §3.10, decision 5): a zip the origin
rebuilt in place is brought up to date by its central directory and one
Range per changed member; every pass asks the directory; the whole fetch
follows wherever the delta cannot."""
import random
import zipfile
from pathlib import Path

import pytest

from app import db
from app.acquire import engine, zipdelta
from app.acquire.spec import DeltaSpec, Endpoint, RemoteFile, Transport

from tests.acquire.test_engine import _collection, _spec

V1 = {f"D/{i:03d}_X_ORIGINALE_V0.xml": random.Random(i).randbytes(20_000) for i in range(40)}
V2 = dict(V1)
V2["D/003_X_ORIGINALE_V0.xml"] = random.Random(9003).randbytes(20_000)      # corrected at the origin
V2["D/NEW_X_VIGENZA_2026-10-12_V1.xml"] = random.Random(7).randbytes(9_000)  # a new version


def _zip(path: Path, members: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)


def _members(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        return {i.filename: z.read(i) for i in z.infolist() if not i.is_dir()}


def _delta_spec(mock_server, name="m.zip"):
    """Normattiva's shape: a zip listed without a size, fetched by name."""
    return _spec(mock_server, delta=DeltaSpec(mode="zip-directory"), layout="container-files",
                 filters=("*.zip",),
                 endpoints=(Endpoint(transport=Transport.HTTPS, url=mock_server.base_url,
                                     static_files=(RemoteFile(rel_path=name),)),))


@pytest.fixture
def small_tail(monkeypatch):
    """A directory read sized for test zips: the 4 MB default would be the
    whole file here."""
    monkeypatch.setattr(zipdelta, "DIRECTORY_TAIL", 8192)


def test_a_rebuilt_zip_is_brought_up_to_date_by_its_directory(tmp_path, mock_server, small_tail):
    collection = _collection(tmp_path)
    con = db.connect(collection)
    _zip(mock_server.root / "m.zip", V1)
    spec = _delta_spec(mock_server)
    first = engine.run_acquisition(con, collection, spec, "demo")
    assert first["fetched"] == 1 and mock_server.request_count("m.zip") == 2, "no copy yet: the size, then the file in one closed range"
    dest = collection.corpus_dir / "m.zip"
    assert _members(dest) == V1

    _zip(mock_server.root / "m.zip", V2)                 # the origin rebuilds the zip
    before = mock_server.request_count("m.zip")
    second = engine.run_acquisition(con, collection, spec, "demo")
    assert second["fetched"] == 1 and second["skipped"] == 0, "every pass asks the directory"
    assert _members(dest) == V2, "the copy holds the origin's members"
    assert mock_server.request_count("m.zip") - before == 4, \
        "the size, the directory, two members — not the file"
    with zipfile.ZipFile(dest) as z:
        assert z.testzip() is None
    row = con.execute("SELECT fetch_done, bytes_fetched, blake2, error FROM fetch_files "
                      "WHERE rel_path='m.zip'").fetchone()
    assert row["fetch_done"] == 1 and row["bytes_fetched"] == dest.stat().st_size and row["blake2"]
    assert row["error"] is None

    before = mock_server.request_count("m.zip")
    third = engine.run_acquisition(con, collection, spec, "demo")    # nothing changed
    assert third["fetched"] == 1 and _members(dest) == V2
    assert mock_server.request_count("m.zip") - before == 2, "the size and the directory, no member"


def test_without_ranges_the_whole_fetch_follows(tmp_path, mock_server, small_tail):
    collection = _collection(tmp_path)
    con = db.connect(collection)
    _zip(mock_server.root / "m.zip", V1)
    mock_server.no_ranges.add("m.zip")
    spec = _delta_spec(mock_server)
    engine.run_acquisition(con, collection, spec, "demo")
    _zip(mock_server.root / "m.zip", V2)
    before = mock_server.request_count("m.zip")
    got = engine.run_acquisition(con, collection, spec, "demo")
    assert got["fetched"] == 1 and _members(collection.corpus_dir / "m.zip") == V2
    assert mock_server.request_count("m.zip") - before == 3, \
        "the delta's probe and the ranged fetch's both answered 200 — no ranges — and the whole file followed"
    assert any("RangeUnsupported" in why for why in engine._zip_delta_fallbacks)


def test_a_delta_moving_most_of_the_file_hands_back_to_the_whole_fetch(tmp_path, mock_server, small_tail):
    collection = _collection(tmp_path)
    con = db.connect(collection)
    _zip(mock_server.root / "m.zip", V1)
    spec = _delta_spec(mock_server)
    engine.run_acquisition(con, collection, spec, "demo")
    changed = {k: random.Random(i + 500).randbytes(20_000) for i, k in enumerate(V1)}
    _zip(mock_server.root / "m.zip", changed)
    before = mock_server.request_count("m.zip")
    got = engine.run_acquisition(con, collection, spec, "demo")
    assert got["fetched"] == 1 and got["adopted"] == 0
    assert mock_server.request_count("m.zip") - before == 4, "size, directory, the size again, then the whole file in one closed range"
    assert _members(collection.corpus_dir / "m.zip") == changed, "not adopted by its size once the directory said it differs"
    assert any("DeltaNotWorthIt" in why for why in engine._zip_delta_fallbacks)


def test_every_pass_treats_a_zip_directory_file_as_pending(tmp_path, mock_server):
    collection = _collection(tmp_path)
    con = db.connect(collection)
    f = RemoteFile(rel_path="m.zip")
    con.execute("INSERT INTO fetch_files(source_id, rel_path, fetch_done, remote_size, remote_mtime) "
                "VALUES('test', 'm.zip', 1, NULL, NULL)")
    assert engine._is_pending(con, _delta_spec(mock_server), f) is True
    assert engine._is_pending(con, _spec(mock_server, endpoints=(Endpoint(
        transport=Transport.HTTPS, url=mock_server.base_url, static_files=(f,)),)), f) is False, \
        "a full-resync file listed without a size is done once fetched, as before"


def test_a_transient_fault_on_a_range_request_is_retried_not_the_pass_ended(tmp_path, mock_server,
                                                                          small_tail, monkeypatch):
    """2026-10-12: one ReadTimeout on one Range request ended Normattiva's
    whole 7.8 GB fetch (the delta ran outside the per-file boundary, and the
    error was httpx's class). Now the reader asks again, and nothing a file
    does escapes its own result."""
    monkeypatch.setattr(zipdelta, "_sleep", lambda s: None)
    collection = _collection(tmp_path)
    con = db.connect(collection)
    _zip(mock_server.root / "m.zip", V1)
    spec = _delta_spec(mock_server)
    engine.run_acquisition(con, collection, spec, "demo")
    _zip(mock_server.root / "m.zip", V2)
    mock_server.fail_next("m.zip", 1)                    # the size probe drops once
    before = mock_server.request_count("m.zip")
    got = engine.run_acquisition(con, collection, spec, "demo")
    assert got["fetched"] == 1 and got["failed"] == 0
    assert _members(collection.corpus_dir / "m.zip") == V2
    assert mock_server.request_count("m.zip") - before == 5, "one request more: the one asked again"


def test_an_origin_that_keeps_faulting_fails_the_file_and_the_pass_goes_on(tmp_path, mock_server,
                                                                        small_tail, monkeypatch):
    monkeypatch.setattr(zipdelta, "_sleep", lambda s: None)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    collection = _collection(tmp_path)
    con = db.connect(collection)
    _zip(mock_server.root / "m.zip", V1)
    _zip(mock_server.root / "n.zip", V1)
    spec = _spec(mock_server, delta=DeltaSpec(mode="zip-directory"), layout="container-files",
                 filters=("*.zip",),
                 endpoints=(Endpoint(transport=Transport.HTTPS, url=mock_server.base_url,
                                     static_files=(RemoteFile(rel_path="m.zip"),
                                                   RemoteFile(rel_path="n.zip"))),))
    engine.run_acquisition(con, collection, spec, "demo")
    _zip(mock_server.root / "m.zip", V2)
    _zip(mock_server.root / "n.zip", V2)
    mock_server.fail_next("m.zip", 1000)                 # m is gone for the whole pass
    got = engine.run_acquisition(con, collection, spec, "demo")
    assert (got["fetched"], got["skipped"], got["failed"]) == (1, 1, 0), \
        "n fetched; m's directory unread, its complete copy stands as a deferral; the pass returned"
    assert _members(collection.corpus_dir / "n.zip") == V2
    assert _members(collection.corpus_dir / "m.zip") == V1, "the copy on disk is untouched"
    row = con.execute("SELECT fetch_done, error FROM fetch_files WHERE rel_path='m.zip'").fetchone()
    assert row["fetch_done"] == 1 and row["error"] is None, "nothing recorded against a file nothing is known about"
    assert any("directory unread, the copy stands" in why and "attempts" in why
               for why in engine._zip_delta_fallbacks), "the size probe asked ATTEMPTS times first"


# ---- the file in closed ranges (transport_https.fetch_ranged, 2026-10-13) --------------

from app.acquire import transport_https


@pytest.fixture
def small_steps(monkeypatch):
    """Ranges sized for test zips (the 64 MiB default would be the whole file)."""
    monkeypatch.setattr(transport_https, "RANGE_STEP", 256 * 1024)
    monkeypatch.setattr(transport_https.time, "sleep", lambda s: None)
    monkeypatch.setattr(zipdelta, "_sleep", lambda s: None)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)


def test_an_origin_that_cuts_every_stream_is_fetched_in_closed_ranges(tmp_path, mock_server,
                                                                       small_tail, small_steps):
    """Normattiva's largest zip: every whole stream cut at the same byte, an
    open-ended resume from there never answered — a file no stream could
    finish is fetched in closed ranges, each a request of its own."""
    collection = _collection(tmp_path)
    con = db.connect(collection)
    _zip(mock_server.root / "m.zip", V1)                       # ~800 KB
    size = (mock_server.root / "m.zip").stat().st_size
    mock_server.cut_after_bytes("m.zip", 500_000)
    mock_server.refuse_open_ranges("m.zip")
    spec = _delta_spec(mock_server)
    got = engine.run_acquisition(con, collection, spec, "demo")
    assert got["fetched"] == 1 and got["failed"] == 0, got
    assert _members(collection.corpus_dir / "m.zip") == V1
    steps = -(-size // (256 * 1024))
    assert mock_server.request_count("m.zip") == 1 + steps, "the size probe, then one request a step"
    assert all(r.endswith("-") is False for r in mock_server.range_requests["m.zip"]), \
        "every range closed: the origin's open-ended stall is never met"
    assert not (collection.corpus_dir / "m.zip.part").exists()


def test_a_step_dropped_once_is_asked_again_and_a_partial_step_continues(tmp_path, mock_server,
                                                                       small_tail, small_steps):
    collection = _collection(tmp_path)
    con = db.connect(collection)
    _zip(mock_server.root / "m.zip", V1)
    spec = _delta_spec(mock_server)
    mock_server.fail_next("m.zip", 2)                 # the probe drops once, the first step too
    got = engine.run_acquisition(con, collection, spec, "demo")
    assert got["fetched"] == 1 and _members(collection.corpus_dir / "m.zip") == V1
    # a step answered short (the origin's cut inside a step) continues from where it ended
    _zip(mock_server.root / "n.zip", V2)
    mock_server.cut_after_bytes("n.zip", 100_000)      # smaller than a step
    spec2 = _delta_spec(mock_server, name="n.zip")
    got = engine.run_acquisition(con, collection, spec2, "demo")
    assert got["fetched"] == 1 and _members(collection.corpus_dir / "n.zip") == V2
    starts = [int(r.split("=", 1)[1].split("-")[0]) for r in mock_server.range_requests["n.zip"] if r != "bytes=0-0"]
    assert starts == sorted(starts) and len(set(starts)) == len(starts), "each request asks from where the last ended"


def test_an_origin_without_ranges_falls_back_to_the_stream(tmp_path, mock_server, small_tail, small_steps):
    collection = _collection(tmp_path)
    con = db.connect(collection)
    _zip(mock_server.root / "m.zip", V1)
    mock_server.no_ranges.add("m.zip")
    spec = _delta_spec(mock_server)
    got = engine.run_acquisition(con, collection, spec, "demo")
    assert got["fetched"] == 1 and _members(collection.corpus_dir / "m.zip") == V1
    assert mock_server.request_count("m.zip") == 2, "the probe answered 200 — no ranges — then the whole stream"


# ---- the origin's own 5xx: an episode, never the file (2026-10-13 01:26 UTC) --------------

def test_a_directory_that_cannot_be_read_leaves_a_complete_copy_standing(tmp_path, mock_server,
                                                                        small_tail, small_steps):
    """Three files failed in eight seconds of 500s, one complete on disk: a
    5xx is retried (15 s of waiting), and a directory still unread after that
    is a deferral — the copy stands, the row is untouched, the pass goes on,
    the next pass asks again. Never the file's failure."""
    collection = _collection(tmp_path)
    con = db.connect(collection)
    _zip(mock_server.root / "m.zip", V1)
    spec = _delta_spec(mock_server)
    engine.run_acquisition(con, collection, spec, "demo")
    row = con.execute("SELECT fetch_done, retries, error, updated_at FROM fetch_files "
                      "WHERE rel_path='m.zip'").fetchone()
    assert row["fetch_done"] == 1 and row["error"] is None
    _zip(mock_server.root / "m.zip", V2)                       # the origin rebuilt it...
    mock_server.answer_next("m.zip", 500, 1000)                 # ...and is having an episode
    before = mock_server.request_count("m.zip")
    got = engine.run_acquisition(con, collection, spec, "demo")
    assert got == {"fetched": 0, "skipped": 1, "failed": 0, "aborted": 0, "adopted": 0, "total": 1}
    assert _members(collection.corpus_dir / "m.zip") == V1, "the copy stands"
    after = con.execute("SELECT fetch_done, retries, error, updated_at FROM fetch_files "
                        "WHERE rel_path='m.zip'").fetchone()
    assert tuple(after) == tuple(row), "the row is exactly as it was"
    assert mock_server.request_count("m.zip") - before == zipdelta.HttpRangeReader.ATTEMPTS, \
        "the size probe asked ATTEMPTS times, then nothing else"
    assert any("directory unread, the copy stands" in why for why in engine._zip_delta_fallbacks)
    mock_server.statuses.clear()                                # the episode is over
    got = engine.run_acquisition(con, collection, spec, "demo")
    assert got["fetched"] == 1 and _members(collection.corpus_dir / "m.zip") == V2


def test_a_500_during_the_episode_is_retried_within_the_pass(tmp_path, mock_server, small_tail, small_steps):
    collection = _collection(tmp_path)
    con = db.connect(collection)
    _zip(mock_server.root / "m.zip", V1)
    spec = _delta_spec(mock_server)
    mock_server.answer_next("m.zip", 500, 3)                    # the probe twice, the first step once
    got = engine.run_acquisition(con, collection, spec, "demo")
    assert got["fetched"] == 1 and got["failed"] == 0
    assert _members(collection.corpus_dir / "m.zip") == V1
    row = con.execute("SELECT error FROM fetch_files WHERE rel_path='m.zip'").fetchone()
    assert row["error"] is None


def test_the_origins_faults_read_as_deferrals():
    """The recorded error of a file that failed on such a fault stays quiet
    until DEFER_ATTEMPTS: it is the origin's, not the file's."""
    for text in ("TransportError: https://x/y: 500 to Range 1243676672-1310785535",
                 "TransportError: https://x/y: 500 to a Range request after 5 attempts",
                 "TransportError: https://x/y: The read operation timed out",
                 "TransportError: https://x/y: ReadTimeout: The read operation timed out on Range 0-9 after 5 attempts",
                 "TransportError: https://x/y: Server disconnected without sending a response."):
        assert engine.transient(text), text
    assert not engine.transient("TransportError: https://x/y: 404 to Range 0-9")
    assert not engine.transient("checksum mismatch: sidecar says a, file says b")

"""Multi-connection downloads of a SINGLE file (architecture-v2.md §5.2's
third parallelism axis).

Opt-in, because §5.2 calls it "default-off against origins": it buys nothing
on a link the origin already saturates, and every extra socket is a cost the
origin pays. What the tests pin is that when it IS on, it stays inside every
etiquette limit, resumes truthfully, and degrades to a single stream against
an origin that will not serve ranges.
"""
import json
import threading

import pytest

from app import db, platform_config
from app.acquire import engine, transport_https
from app.acquire.spec import RemoteFile
from app.transfer import manager as transfer_manager
from app.transfer.schedule import ScheduleError

from tests.acquire.test_engine import _collection, _spec, _write_origin_file

BODY = b"".join(bytes([i % 251]) for i in range(300_000))


@pytest.fixture
def segmented(monkeypatch):
    """A manager configured for N segments per file, installed as the
    process singleton the engine reads."""
    def configure(segments, per_endpoint=8):
        platform = platform_config.PlatformConfig(
            transfer=platform_config.TransferConfig(
                workers=4, per_endpoint=per_endpoint,
                segments_per_file=segments))
        manager = transfer_manager.TransferManager(platform=platform)
        monkeypatch.setattr(transfer_manager, "_manager", manager)
        monkeypatch.setattr(engine, "_SEGMENT_MIN_BYTES", 1024)
        return manager
    return configure


# ---------------- range arithmetic ----------------

def test_ranges_tile_the_file_exactly():
    for total, n in ((300_000, 4), (1000, 3), (7, 7), (10, 3), (1, 4)):
        ranges = transport_https.segment_ranges(total, n)
        assert ranges[0][0] == 0
        assert ranges[-1][1] == total - 1
        for (_, prev_end), (start, _) in zip(ranges, ranges[1:]):
            assert start == prev_end + 1, "no gap, no overlap"
        assert sum(e - s + 1 for s, e in ranges) == total
        assert len(ranges) <= max(1, min(n, total)), "never more segments than bytes"


def test_a_single_segment_is_the_whole_file():
    assert transport_https.segment_ranges(500, 1) == [(0, 499)]


# ---------------- the download ----------------

def test_a_segmented_download_reassembles_the_exact_bytes(tmp_path, mock_server,
                                                           segmented):
    segmented(4)
    _write_origin_file(mock_server, "big.bin", BODY)
    spec = _spec(mock_server)
    collection = _collection(tmp_path)
    con = db.connect(collection)

    result = engine.run_acquisition(con, collection, spec, "demo")
    assert result["fetched"] == 1, result
    assert (collection.corpus_dir / "big.bin").read_bytes() == BODY

    ranges = mock_server.range_requests.get("big.bin", [])
    assert len(ranges) == 4, f"one closed Range per segment, got {ranges}"
    assert all("-" in r and not r.endswith("-") for r in ranges), ranges

    row = con.execute("SELECT * FROM fetch_files WHERE rel_path='big.bin'").fetchone()
    assert row["fetch_done"] == 1
    assert row["bytes_fetched"] == len(BODY)
    # the scratch state is gone once the file is whole
    assert not (collection.corpus_dir / "big.bin.part").exists()
    assert not transport_https._sidecar_path(collection.corpus_dir / "big.bin").exists()


def test_segments_never_exceed_the_endpoints_connection_allowance(
        tmp_path, mock_server, segmented):
    """Splitting a file must not smuggle in more sockets than per_endpoint,
    Endpoint.max_conns and RateLimit.max_conns already allow. Asserted
    against the server's own observed concurrency, not the client's intent."""
    manager = segmented(8, per_endpoint=2)
    _write_origin_file(mock_server, "big.bin", BODY)
    spec = _spec(mock_server)
    collection = _collection(tmp_path)

    endpoint = spec.endpoints[0]
    assert engine._segments_for(manager, endpoint, spec,
                                 RemoteFile(rel_path="big.bin", size=len(BODY))) == 2

    mock_server.drip("big.bin", 400_000)      # slow enough to overlap
    engine.run_acquisition(db.connect(collection), collection, spec, "demo")
    assert (collection.corpus_dir / "big.bin").read_bytes() == BODY
    assert mock_server.peak_concurrency <= 2, mock_server.peak_concurrency


def test_small_files_are_not_split(tmp_path, mock_server, segmented, monkeypatch):
    """Three extra round trips to parallelise a 2 MB file is cost with no
    benefit."""
    manager = segmented(4)
    monkeypatch.setattr(engine, "_SEGMENT_MIN_BYTES", 32 << 20)
    _write_origin_file(mock_server, "small.bin", b"s" * 1000)
    spec = _spec(mock_server)
    collection = _collection(tmp_path)

    endpoint = spec.endpoints[0]
    assert engine._segments_for(manager, endpoint, spec,
                                 RemoteFile(rel_path="small.bin", size=1000)) == 1
    engine.run_acquisition(db.connect(collection), collection, spec, "demo")
    assert (collection.corpus_dir / "small.bin").read_bytes() == b"s" * 1000
    assert mock_server.range_requests.get("small.bin") is None, "no Range at all"


def test_a_file_of_unknown_size_is_not_split(tmp_path, mock_server, segmented):
    manager = segmented(4)
    spec = _spec(mock_server)
    assert engine._segments_for(manager, spec.endpoints[0], spec,
                                 RemoteFile(rel_path="x", size=None)) == 1


def test_an_origin_that_ignores_range_falls_back_to_one_stream(
        tmp_path, mock_server, segmented):
    """A 200 answer to a partial request means the origin is handing back the
    whole file. Segmenting is impossible; the file must still arrive."""
    segmented(4)
    _write_origin_file(mock_server, "big.bin", BODY)
    mock_server.no_ranges.add("big.bin")
    spec = _spec(mock_server)
    collection = _collection(tmp_path)

    result = engine.run_acquisition(db.connect(collection), collection, spec, "demo")
    assert result["fetched"] == 1, result
    assert (collection.corpus_dir / "big.bin").read_bytes() == BODY
    # and the abandoned segmented scratch state is not left behind
    assert not transport_https._sidecar_path(collection.corpus_dir / "big.bin").exists()


# ---------------- resume ----------------

def test_resume_continues_each_segment_from_its_own_offset(tmp_path, mock_server,
                                                            segmented):
    """The .part file is full-size from byte one, so segment progress cannot
    be inferred from its length — it lives in the sidecar."""
    segmented(4)
    _write_origin_file(mock_server, "big.bin", BODY)
    spec = _spec(mock_server)
    collection = _collection(tmp_path)
    dest = collection.corpus_dir / "big.bin"
    dest.parent.mkdir(parents=True, exist_ok=True)

    # stage a half-finished download by hand: full-size .part, half of each
    # segment already present and recorded
    ranges = transport_https.segment_ranges(len(BODY), 4)
    part = dest.with_name("big.bin.part")
    part.write_bytes(b"\0" * len(BODY))
    done = []
    with open(part, "r+b") as f:
        for start, end in ranges:
            half = (end - start + 1) // 2
            f.seek(start)
            f.write(BODY[start:start + half])
            done.append(half)
    transport_https._save_progress(transport_https._sidecar_path(dest), ranges, done)

    engine.run_acquisition(db.connect(collection), collection, spec, "demo")

    assert dest.read_bytes() == BODY
    # the segments run concurrently, so requests arrive in no fixed order —
    # compare the SET of resume offsets, not their sequence
    asked = sorted(int(h.split("=")[1].split("-")[0])
                   for h in mock_server.range_requests["big.bin"])
    expected = sorted(start + (end - start + 1) // 2 for start, end in ranges)
    assert asked == expected, "each segment resumed past exactly what it held"
    assert all(a > 0 for a in asked), "nothing restarted from the beginning"


def test_a_mismatched_sidecar_is_discarded_rather_than_trusted(tmp_path):
    """Re-downloading is cheap and always correct; trusting an offset table
    that describes a different layout is neither."""
    dest = tmp_path / "f.bin"
    ranges = transport_https.segment_ranges(1000, 4)
    sidecar = transport_https._sidecar_path(dest)

    transport_https._save_progress(sidecar, ranges, [10, 20, 30, 40])
    assert transport_https._load_progress(sidecar, ranges) == [10, 20, 30, 40]

    # a different segment count -> the recorded offsets mean nothing
    other = transport_https.segment_ranges(1000, 2)
    assert transport_https._load_progress(sidecar, other) == [0, 0]

    for junk in ("", "not json", '{"ranges": [], "done": "x"}'):
        sidecar.write_text(junk)
        assert transport_https._load_progress(sidecar, ranges) == [0] * 4

    # and a recorded value can never exceed its segment's length
    transport_https._save_progress(sidecar, ranges, [10 ** 9, 0, 0, 0])
    assert transport_https._load_progress(sidecar, ranges)[0] == ranges[0][1] + 1


def test_the_sidecar_is_written_atomically(tmp_path):
    """A torn sidecar must read as "start over", never as a plausible-looking
    wrong offset."""
    dest = tmp_path / "f.bin"
    sidecar = transport_https._sidecar_path(dest)
    ranges = transport_https.segment_ranges(1000, 2)
    transport_https._save_progress(sidecar, ranges, [1, 2])
    assert json.loads(sidecar.read_text())["done"] == [1, 2]
    assert not sidecar.with_name(sidecar.name + ".tmp").exists()


# ---------------- configuration ----------------

def test_the_config_ceiling_is_etiquette_not_performance(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    path = tmp_path / "platform.toml"

    path.write_text("[transfer]\nsegments_per_file = 4\n")
    assert platform_config.load().transfer.segments_per_file == 4

    path.write_text("[transfer]\nsegments_per_file = 1\n")
    assert platform_config.load().transfer.segments_per_file == 1

    path.write_text(
        f"[transfer]\nsegments_per_file = {platform_config.MAX_SEGMENTS_PER_FILE + 1}\n")
    with pytest.raises(ScheduleError, match="ceiling"):
        platform_config.load()


def test_the_default_is_still_one_segment(tmp_path, monkeypatch):
    """§5.2: default-off against origins. An existing deployment that adds
    nothing to platform.toml keeps making exactly one connection per file."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    assert platform_config.PlatformConfig().transfer.segments_per_file == 1
    (tmp_path / "platform.toml").write_text("[bandwidth]\ndown_max = \"10Mbit\"\n")
    assert platform_config.load().transfer.segments_per_file == 1

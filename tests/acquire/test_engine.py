"""app/acquire/engine.py — filters, delta gate, disk preflight, license
gate, checksum verification, and the resumability acceptance criterion
(kill mid-acquisition -> restart resumes without re-downloading completed
files).
"""
import hashlib

import pytest

from app import collections as collections_mod, db
from app.acquire import engine
from app.acquire.spec import (AcquisitionSpec, ChecksumSource, DeltaSpec, DiskPreflightError,
                               Endpoint, Filter, LicenseAckRequiredError, RemoteFile, Transport)
from app.plugins.base import LicenseClass


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


def _write_origin_file(mock_server, name: str, content: bytes) -> None:
    path = mock_server.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _spec(mock_server, **overrides) -> AcquisitionSpec:
    static_files = tuple(
        RemoteFile(rel_path=p.name, size=p.stat().st_size)
        for p in sorted(mock_server.root.iterdir()) if p.is_file())
    defaults = dict(
        source_id="test",
        transport=Transport.HTTPS,
        endpoints=(Endpoint(transport=Transport.HTTPS, url=mock_server.base_url,
                             static_files=static_files),),
        layout="single-file",
        checksums=ChecksumSource(kind="none"),
        delta=DeltaSpec(mode="full-resync"),
        profiles={"demo": Filter()},
        estimated_bytes={"demo": 10_000_000},
        license_class=LicenseClass.GREEN,
    )
    defaults.update(overrides)
    return AcquisitionSpec(**defaults)


# ---------------- filters / profiles / dry-run plan ----------------

def test_filters_and_profile_limit_files(tmp_path, mock_server):
    _write_origin_file(mock_server, "a.txt", b"x" * 100)
    _write_origin_file(mock_server, "b.txt", b"y" * 100)
    _write_origin_file(mock_server, "c.dat", b"z" * 100)
    spec = _spec(mock_server, filters=("*.txt",), profiles={"demo": Filter(max_files=1)})
    collection = _collection(tmp_path)
    con = db.connect(collection)

    plan = engine.plan_acquisition(con, collection, spec, "demo")
    assert plan.files_total == 1          # *.txt -> {a.txt,b.txt}, max_files=1 caps it
    assert plan.files_pending == 1
    assert plan.bytes_total == 100
    assert plan.license_state == "not_required"


def test_dry_run_plan_never_writes_or_raises_on_missing_license(tmp_path, mock_server):
    _write_origin_file(mock_server, "a.txt", b"x" * 10)
    spec = _spec(mock_server, license_class=LicenseClass.YELLOW,
                 terms_text_path=None)   # no terms file at all -> "missing"
    collection = _collection(tmp_path)
    con = db.connect(collection)

    plan = engine.plan_acquisition(con, collection, spec, "demo")   # must not raise
    assert plan.license_state == "missing"
    assert not any(collection.corpus_dir.iterdir())


# ---------------- disk preflight ----------------

def test_disk_preflight_refuses_without_writing(tmp_path, mock_server):
    _write_origin_file(mock_server, "big.dat", b"x" * 1000)
    spec = _spec(mock_server, estimated_bytes={"demo": 10 ** 18})
    collection = _collection(tmp_path)
    con = db.connect(collection)

    with pytest.raises(DiskPreflightError):
        engine.run_acquisition(con, collection, spec, "demo")
    assert not any(collection.corpus_dir.iterdir())


# ---------------- license-ack gate ----------------

def test_license_gate_blocks_yellow_source_without_ack(tmp_path, mock_server):
    terms = tmp_path / "terms.txt"
    terms.write_text("terms v1")
    _write_origin_file(mock_server, "a.txt", b"x" * 10)
    spec = _spec(mock_server, license_class=LicenseClass.YELLOW, terms_text_path=terms)
    collection = _collection(tmp_path, license_ack=None)
    con = db.connect(collection)

    with pytest.raises(LicenseAckRequiredError):
        engine.run_acquisition(con, collection, spec, "demo")
    assert not any(collection.corpus_dir.iterdir())


def test_license_gate_passes_with_valid_ack(tmp_path, mock_server):
    terms = tmp_path / "terms.txt"
    terms.write_text("terms v1")
    _write_origin_file(mock_server, "a.txt", b"x" * 10)
    spec = _spec(mock_server, license_class=LicenseClass.YELLOW, terms_text_path=terms)
    ack = engine.make_ack(terms)
    collection = _collection(tmp_path, license_ack=ack)
    con = db.connect(collection)

    result = engine.run_acquisition(con, collection, spec, "demo")
    assert result["fetched"] == 1


def test_license_gate_rejects_stale_ack_after_terms_change(tmp_path, mock_server):
    terms = tmp_path / "terms.txt"
    terms.write_text("terms v1")
    stale_ack = engine.make_ack(terms)
    terms.write_text("terms v2 (changed)")   # terms updated after the ack was recorded
    _write_origin_file(mock_server, "a.txt", b"x" * 10)
    spec = _spec(mock_server, license_class=LicenseClass.YELLOW, terms_text_path=terms)
    collection = _collection(tmp_path, license_ack=stale_ack)
    con = db.connect(collection)

    with pytest.raises(LicenseAckRequiredError):
        engine.run_acquisition(con, collection, spec, "demo")


# ---------------- checksum verification ----------------

def test_sidecar_checksum_verified_when_declared(tmp_path, mock_server):
    content = b"hello world" * 10
    _write_origin_file(mock_server, "a.txt", content)
    _write_origin_file(mock_server, "a.txt.md5", hashlib.md5(content).hexdigest().encode())
    spec = _spec(mock_server, checksums=ChecksumSource(kind="sidecar"))
    collection = _collection(tmp_path)
    con = db.connect(collection)

    result = engine.run_acquisition(con, collection, spec, "demo")
    assert result["fetched"] == 2   # a.txt AND its .md5 sidecar are both fetched (no filter excludes it)
    row = con.execute(
        "SELECT blake2, fetch_done FROM fetch_files WHERE rel_path='a.txt'").fetchone()
    assert row["fetch_done"] == 1
    assert row["blake2"]   # blake2 always computed, regardless of sidecar presence


def test_sidecar_checksum_mismatch_fails_that_file(tmp_path, mock_server):
    content = b"hello world" * 10
    _write_origin_file(mock_server, "a.txt", content)
    _write_origin_file(mock_server, "a.txt.md5", b"0" * 32)   # deliberately wrong
    # No filter override: both a.txt AND its sidecar must actually be
    # fetched, or verify_sidecar() has nothing local to check against and
    # silently no-ops (matches PubmedSource's pre-existing best-effort
    # behavior when a sidecar wasn't fetched at all).
    spec = _spec(mock_server, checksums=ChecksumSource(kind="sidecar"))
    collection = _collection(tmp_path)
    con = db.connect(collection)

    result = engine.run_acquisition(con, collection, spec, "demo")
    assert result["failed"] == 1        # a.txt: checksum mismatch
    assert result["fetched"] == 1       # a.txt.md5 itself: fetched fine (no sidecar of its own)
    row = con.execute(
        "SELECT fetch_done, error FROM fetch_files WHERE rel_path='a.txt'").fetchone()
    assert row["fetch_done"] == 0
    assert "mismatch" in row["error"]


# ---------------- User-Agent policy ----------------

def test_https_fetch_sends_descriptive_user_agent(tmp_path, mock_server):
    # dumps.wikimedia.org 403s generic library UAs (Wikimedia User-Agent
    # policy) — observed live, it stalled the wikipedia collection at zero
    # bytes. The transport must identify the project, with a contact URL,
    # on every request.
    _write_origin_file(mock_server, "a.txt", b"x" * 10)
    spec = _spec(mock_server)
    collection = _collection(tmp_path)
    con = db.connect(collection)

    result = engine.run_acquisition(con, collection, spec, "demo")
    assert result["fetched"] == 1
    ua = mock_server.last_user_agent["a.txt"]
    assert ua.startswith("louise-rag/")
    assert "https://github.com/sourstar/louise-rag" in ua
    assert "httpx/" in ua                 # underlying library, per the policy


def test_https_fetch_follows_redirects(tmp_path, mock_server):
    # Hugging Face /resolve/ URLs 302 to a CDN — observed live: every
    # wikivec shard failed with "Redirect response '302 Found'" because
    # httpx does not follow redirects by default.
    from dataclasses import replace
    _write_origin_file(mock_server, "real_target.bin", b"p" * 500)
    mock_server.redirects["pointer.bin"] = "real_target.bin"
    # static file list advertises the POINTER path only
    spec = replace(_spec(mock_server),
                   endpoints=(Endpoint(
                       transport=Transport.HTTPS, url=mock_server.base_url,
                       static_files=(RemoteFile(rel_path="pointer.bin",
                                                 size=500),)),))
    collection = _collection(tmp_path)
    con = db.connect(collection)

    result = engine.run_acquisition(con, collection, spec, "demo")
    assert result["fetched"] == 1, "302 must be followed, not fatal"
    assert (collection.corpus_dir / "pointer.bin").read_bytes() == b"p" * 500


# ---------------- delta gate (idempotence) ----------------

def test_idempotent_rerun_skips_unchanged_files(tmp_path, mock_server):
    _write_origin_file(mock_server, "a.txt", b"x" * 100)
    spec = _spec(mock_server)
    collection = _collection(tmp_path)
    con = db.connect(collection)

    # "aborted" is P0.7's addition: a clean SIGTERM is counted separately from
    # a failure, so it never increments `retries` and never makes the CLI exit
    # nonzero (CLAUDE.md invariant 2). Purely additive to this dict.
    r1 = engine.run_acquisition(con, collection, spec, "demo")
    assert r1 == {"fetched": 1, "skipped": 0, "failed": 0, "aborted": 0,
                  "adopted": 0, "total": 1}
    assert mock_server.request_count("a.txt") == 1

    r2 = engine.run_acquisition(con, collection, spec, "demo")
    assert r2 == {"fetched": 0, "skipped": 1, "failed": 0, "aborted": 0,
                  "adopted": 0, "total": 1}
    assert mock_server.request_count("a.txt") == 1   # never re-fetched


# ---------------- resumability (the acceptance criterion) ----------------

def test_kill_mid_acquisition_then_restart_resumes(tmp_path, mock_server):
    # Large enough, and stalled far enough in, to span several of
    # HttpsTransport's 64 KiB read chunks — a stall smaller than one chunk
    # loses all buffered progress when the connection drops (httpx discards
    # an incomplete chunk's accumulated bytes on error; see transport_https.py).
    content = bytes((i % 256 for i in range(500_000)))
    stall_at = 200_000
    _write_origin_file(mock_server, "bigfile.dat", content)
    _write_origin_file(mock_server, "already_done.txt", b"small")
    spec = _spec(mock_server)
    collection = _collection(tmp_path)
    con = db.connect(collection)

    mock_server.stall_after_bytes("bigfile.dat", stall_at)
    result = engine.run_acquisition(con, collection, spec, "demo")
    assert result["failed"] == 1
    assert result["fetched"] == 1   # already_done.txt still succeeds independently
    part = collection.corpus_dir / "bigfile.dat.part"
    assert part.is_file()
    part_size = part.stat().st_size
    assert 0 < part_size <= stall_at   # truthful partial progress, flushed to disk
    assert part.read_bytes() == content[:part_size]
    row = con.execute(
        "SELECT fetch_done, bytes_fetched FROM fetch_files WHERE rel_path='bigfile.dat'").fetchone()
    assert row["fetch_done"] == 0
    assert row["bytes_fetched"] == part_size

    mock_server.unstall("bigfile.dat")
    result2 = engine.run_acquisition(con, collection, spec, "demo")
    assert result2["fetched"] == 1     # bigfile.dat completes now
    assert result2["skipped"] == 1     # already_done.txt is NOT re-downloaded
    assert mock_server.last_range("bigfile.dat") == f"bytes={part_size}-"   # genuinely resumed
    assert mock_server.request_count("already_done.txt") == 1

    dest = collection.corpus_dir / "bigfile.dat"
    assert dest.is_file()
    assert dest.read_bytes() == content
    assert not part.exists()

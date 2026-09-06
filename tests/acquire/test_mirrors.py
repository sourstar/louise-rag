"""Mirror structure probe, benchmark, persisted ranking and failover
(app/acquire/mirrors.py + the engine hook).

Requirements these encode, verbatim from the operator:
- a mirror is admitted only if it serves EXACTLY the official structure
  (the probe file at its official relative path IS that test);
- light benchmarking picks the best mirror, the ranking persists so a
  restart starts with the winner, and it re-runs periodically so a better
  mirror takes over unattended;
- a mirror that is unavailable or times out is switched away from — at
  benchmark time (ranked last) AND mid-pass (marked down, the file retried
  on the next-best mirror immediately).
"""
import json

import pytest

from app import collections as collections_mod, db
from app.acquire import engine, mirrors
from app.acquire.spec import (AcquisitionSpec, ChecksumSource, DeltaSpec, Endpoint,
                               Filter, Transport)
from app.plugins.base import LicenseClass

from tests.acquire.mock_http_server import MockServer

DEAD = "http://127.0.0.1:9"      # nothing listens: connection refused


@pytest.fixture(autouse=True)
def clean_down_state():
    mirrors.reset_down_state()
    yield
    mirrors.reset_down_state()


@pytest.fixture
def second_server(tmp_path):
    root = tmp_path / "origin2"
    root.mkdir()
    server = MockServer(root)
    yield server
    server.shutdown()


def _collection(tmp_path):
    c = collections_mod.Collection(
        name="m", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "m" / "manifest.db",
        fts_dir=tmp_path / "m" / "fts", lance_dir=tmp_path / "m" / "lancedb",
        acquire_enabled=True)
    c.corpus_dir.mkdir(parents=True, exist_ok=True)
    return c


def _spec(*endpoint_urls, probe="1/pg1.rdf", files=()):
    return AcquisitionSpec(
        source_id="m", transport=Transport.HTTPS,
        endpoints=tuple(Endpoint(transport=Transport.HTTPS, url=u,
                                 static_files=tuple(files)) for u in endpoint_urls),
        layout="one-dir-per-doc", checksums=ChecksumSource(kind="none"),
        delta=DeltaSpec(mode="full-resync"), profiles={"demo": Filter()},
        estimated_bytes={"demo": 1_000_000}, license_class=LicenseClass.GREEN,
        probe_file=probe)


def _serve(server, rel, content=b"x" * 100):
    path = server.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_benchmark_ranks_reachable_first_and_persists(tmp_path, mock_server):
    _serve(mock_server, "1/pg1.rdf")
    spec = _spec(DEAD, mock_server.base_url)     # declared order: dead first
    con = db.connect(_collection(tmp_path))

    results = mirrors.benchmark(con, spec)
    assert [r["url"] for r in results] == [mock_server.base_url, DEAD], \
        "the reachable mirror must outrank the dead one, whatever the declared order"
    assert results[0]["seconds"] is not None and results[1]["error"]

    stored = mirrors.read_bench(con)
    assert stored["probe"] == "1/pg1.rdf" and stored["ts"] > 0

    order = [e.url for e in mirrors.seed_order(con, spec)]
    assert order == [mock_server.base_url, DEAD], \
        "a later pass (or a restart) must start with the recorded winner"


def test_benchmark_is_cached_within_its_ttl(tmp_path, mock_server, second_server):
    _serve(mock_server, "1/pg1.rdf")
    _serve(second_server, "1/pg1.rdf")
    spec = _spec(mock_server.base_url, second_server.base_url)
    con = db.connect(_collection(tmp_path))

    mirrors.benchmark(con, spec)
    before = mock_server.request_count("1/pg1.rdf")
    mirrors.benchmark(con, spec)                 # fresh record: no new probe
    assert mock_server.request_count("1/pg1.rdf") == before
    mirrors.benchmark(con, spec, force=True)     # explicit: probes again
    assert mock_server.request_count("1/pg1.rdf") == before + 1


def test_a_wrong_structure_mirror_is_ranked_out(tmp_path, mock_server, second_server):
    """The admission rule as code: a mirror that cannot serve the probe file
    at its official path carries a different layout and loses, however
    reachable it is."""
    _serve(mock_server, "1/pg1.rdf")
    _serve(second_server, "different/layout.rdf")    # reachable, wrong tree
    spec = _spec(second_server.base_url, mock_server.base_url)
    con = db.connect(_collection(tmp_path))

    results = mirrors.benchmark(con, spec)
    assert [r["url"] for r in results] == [mock_server.base_url, second_server.base_url]
    assert results[1]["error"], "wrong structure must be recorded as a failure"


def test_a_failing_mirror_is_marked_down_and_the_file_retried_elsewhere(
        tmp_path, mock_server):
    """Mid-pass failover: first-ranked mirror dead → the file lands from the
    second, in the same pass, and the dead mirror sits out DOWN_S."""
    from app.acquire.spec import RemoteFile
    _serve(mock_server, "7/pg7.txt", b"y" * 50)
    files = (RemoteFile(rel_path="7/pg7.txt", size=50),)
    spec = _spec(DEAD, mock_server.base_url, probe=None, files=files)
    collection = _collection(tmp_path)
    con = db.connect(collection)

    r = engine.run_acquisition(con, collection, spec, "demo")
    assert r["fetched"] == 1 and r["failed"] == 0, r
    assert (collection.corpus_dir / "7/pg7.txt").read_bytes() == b"y" * 50
    assert mirrors.is_down(DEAD), "the failing mirror must sit out"
    row = con.execute("SELECT fetch_done, retries, endpoint_url FROM fetch_files "
                      "WHERE rel_path='7/pg7.txt'").fetchone()
    assert row["fetch_done"] == 1 and row["retries"] == 0, \
        "a successful failover burns no retry"
    assert row["endpoint_url"] == mock_server.base_url


def test_seed_order_skips_down_mirrors_unless_nothing_is_left(tmp_path, mock_server):
    _serve(mock_server, "1/pg1.rdf")
    spec = _spec(mock_server.base_url, DEAD)
    con = db.connect(_collection(tmp_path))
    mirrors.benchmark(con, spec)

    mirrors.mark_down(mock_server.base_url)
    assert [e.url for e in mirrors.seed_order(con, spec)] == [DEAD], \
        "a down mirror is skipped while an alternative exists"
    mirrors.mark_down(DEAD)
    assert len(mirrors.seed_order(con, spec)) == 2, \
        "with everything down, a wrong ranking still beats not fetching"

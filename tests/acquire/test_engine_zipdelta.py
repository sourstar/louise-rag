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
    assert first["fetched"] == 1 and mock_server.request_count("m.zip") == 1, "no copy yet: the whole file"
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
    assert mock_server.request_count("m.zip") - before == 2, \
        "the one-byte probe answered 200 — no ranges — and the whole file followed"
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
    assert mock_server.request_count("m.zip") - before == 3, "size, directory, then the whole file"
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

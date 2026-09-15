"""Files already on disk are ADOPTED, not re-downloaded.

Corpora arrive by routes other than the acquisition engine: a dataset
copied from another machine, a `zfs send` of someone else's mirror, files
staged by hand before the collection existed. Re-fetching bytes that are
already sitting at the destination is pure waste — on a 238 GB corpus it
is a week of someone's uplink — but a shortcut past the *transfer* must
never become a shortcut past the *checking*.
"""
from app import db
from app.acquire import engine
from app.acquire.spec import Endpoint, RemoteFile, Transport

from tests.acquire.test_engine import _collection, _spec, _write_origin_file


def _sizeless_spec(mock_server, name):
    """The spec a dated dump has: the file listed, its length not."""
    return _spec(mock_server, endpoints=(Endpoint(
        transport=Transport.HTTPS, url=mock_server.base_url,
        static_files=(RemoteFile(rel_path=name),)),))


def _staged(tmp_path, mock_server, body, local):
    _write_origin_file(mock_server, "a.txt", body)
    collection = _collection(tmp_path)
    dest = collection.corpus_dir / "a.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(local)
    return collection, db.connect(collection), dest


def test_a_preseeded_file_is_verified_and_recorded_without_a_download(
        tmp_path, mock_server):
    body = b"x" * 4096
    _write_origin_file(mock_server, "a.txt", body)
    spec = _spec(mock_server)
    collection = _collection(tmp_path)
    con = db.connect(collection)

    # stage the file exactly where acquisition would have put it
    dest = collection.corpus_dir / "a.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)

    result = engine.run_acquisition(con, collection, spec, "demo")

    assert result["fetched"] == 1
    assert result["adopted"] == 1
    assert mock_server.request_count("a.txt") == 0, "no bytes may move"

    row = con.execute("SELECT * FROM fetch_files WHERE rel_path='a.txt'").fetchone()
    assert row["fetch_done"] == 1
    assert row["bytes_fetched"] == len(body)
    assert row["blake2"], "an adopted row carries the same digest a fetch records"
    assert row["error"] is None

    # and it is now indistinguishable from a fetched file: the next run skips
    again = engine.run_acquisition(con, collection, spec, "demo")
    assert again == {"fetched": 0, "skipped": 1, "failed": 0, "aborted": 0,
                      "adopted": 0, "total": 1}


def test_a_wrong_sized_local_file_is_downloaded_not_adopted(tmp_path, mock_server):
    """Truncated, stale, or half-copied: size is the only evidence available
    when the origin publishes no checksum, so anything but an exact match
    goes down the normal path."""
    body = b"y" * 4096
    _write_origin_file(mock_server, "a.txt", body)
    spec = _spec(mock_server)
    collection = _collection(tmp_path)
    con = db.connect(collection)

    dest = collection.corpus_dir / "a.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"y" * 4095)          # one byte short

    result = engine.run_acquisition(con, collection, spec, "demo")
    assert result["adopted"] == 0
    assert result["fetched"] == 1
    assert mock_server.request_count("a.txt") == 1
    assert dest.read_bytes() == body


def test_adoption_refuses_a_file_that_fails_its_checksum(tmp_path, mock_server,
                                                          monkeypatch):
    """Same-size but different content is exactly what a checksum is for.
    When verification fails the file must be re-downloaded, not recorded."""
    body = b"z" * 4096
    _write_origin_file(mock_server, "a.txt", body)
    spec = _spec(mock_server)
    collection = _collection(tmp_path)
    con = db.connect(collection)

    dest = collection.corpus_dir / "a.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"q" * 4096)          # right size, wrong bytes

    real = engine.checksum.digest_and_verify

    def fail_on_impostor(path, checksums):
        if path.is_file() and path.read_bytes().startswith(b"q"):
            raise ValueError("sidecar mismatch (simulated)")
        return real(path, checksums)

    monkeypatch.setattr(engine.checksum, "digest_and_verify", fail_on_impostor)

    result = engine.run_acquisition(con, collection, spec, "demo")
    assert result["adopted"] == 0, "a file that fails verification is not adopted"
    assert result["fetched"] == 1
    assert mock_server.request_count("a.txt") == 1
    assert dest.read_bytes() == body


def test_the_plan_excludes_adoptable_bytes_from_the_download_estimate(
        tmp_path, mock_server):
    """The ETA and the disk preflight both read bytes_pending. Counting
    bytes that will never move makes both of them lie — and the preflight
    can refuse a run that needs no space at all."""
    body = b"w" * 8192
    _write_origin_file(mock_server, "a.txt", body)
    spec = _spec(mock_server)
    collection = _collection(tmp_path)
    con = db.connect(collection)

    before = engine.plan_acquisition(con, collection, spec, "demo")
    assert before.files_pending == 1
    assert before.bytes_pending == len(body)
    assert before.files_adoptable == 0

    dest = collection.corpus_dir / "a.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)

    after = engine.plan_acquisition(con, collection, spec, "demo")
    assert after.files_pending == 1, "still work to do: verify and record it"
    assert after.files_adoptable == 1
    assert after.bytes_adoptable == len(body)
    assert after.bytes_pending == 0, "nothing will be downloaded"
    assert after.eta_seconds == 0


def test_a_stale_part_file_is_cleared_when_the_destination_is_adopted(
        tmp_path, mock_server):
    """A leftover .part beside a complete file is a dead partial. Left in
    place it would be read as a resume offset by a later re-fetch, and the
    Range request would append to an already-complete file."""
    body = b"v" * 2048
    _write_origin_file(mock_server, "a.txt", body)
    spec = _spec(mock_server)
    collection = _collection(tmp_path)
    con = db.connect(collection)

    dest = collection.corpus_dir / "a.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    part = dest.with_name("a.txt.part")
    part.write_bytes(b"v" * 900)

    engine.run_acquisition(con, collection, spec, "demo")
    assert not part.exists()
    assert dest.read_bytes() == body


def test_a_file_listed_without_a_size_is_adopted_at_the_origins_length(
        tmp_path, mock_server):
    """A dated dump's URL is immutable and its spec still carries no size
    (09 §9.17 allows null; once a Codex pins the file the size is inside
    its id). Observed live 2026-09-14: the 27 GB enwiki dump, hard-linked
    into place, was fetched again. The origin can say how long it is."""
    body = b"s" * 4096
    collection, con, _ = _staged(tmp_path, mock_server, body, body)
    spec = _sizeless_spec(mock_server, "a.txt")

    result = engine.run_acquisition(con, collection, spec, "demo")
    assert result["adopted"] == 1
    assert mock_server.request_count("a.txt") == 0, "no bytes may move"
    assert mock_server.head_count("a.txt") == 1
    row = con.execute("SELECT * FROM fetch_files WHERE rel_path='a.txt'").fetchone()
    assert row["fetch_done"] == 1 and row["bytes_fetched"] == len(body) and row["blake2"]
    assert row["remote_size"] is None, \
        "the listing's size, so the next run agrees the file is done"

    again = engine.run_acquisition(con, collection, spec, "demo")
    assert again["skipped"] == 1
    assert mock_server.head_count("a.txt") == 1, "a done file is not asked about again"


def test_a_sizeless_copy_the_origin_disagrees_with_is_downloaded(tmp_path, mock_server):
    body = b"t" * 4096
    collection, con, dest = _staged(tmp_path, mock_server, body, b"t" * 4095)
    result = engine.run_acquisition(con, collection, _sizeless_spec(mock_server, "a.txt"),
                                    "demo")
    assert (result["adopted"], result["fetched"]) == (0, 1)
    assert mock_server.request_count("a.txt") == 1
    assert dest.read_bytes() == body


def test_an_origin_that_will_not_say_leaves_the_file_to_the_download(tmp_path, mock_server):
    body = b"u" * 4096
    collection, con, dest = _staged(tmp_path, mock_server, body, body)
    mock_server.no_head.add("a.txt")
    result = engine.run_acquisition(con, collection, _sizeless_spec(mock_server, "a.txt"),
                                    "demo")
    assert (result["adopted"], result["fetched"]) == (0, 1)
    assert mock_server.request_count("a.txt") == 1
    assert dest.read_bytes() == body


def test_is_adoptable_says_no_when_the_remote_size_is_unknown(tmp_path):
    """Without a declared size there is no way to tell complete from
    truncated, and the honest answer is no."""
    from app.acquire.spec import RemoteFile

    collection = _collection(tmp_path)
    dest = collection.corpus_dir / "a.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"abc")

    assert engine.is_adoptable(collection, RemoteFile(rel_path="a.txt", size=3))
    assert not engine.is_adoptable(collection, RemoteFile(rel_path="a.txt", size=None))
    assert not engine.is_adoptable(collection, RemoteFile(rel_path="a.txt", size=0))
    assert not engine.is_adoptable(collection, RemoteFile(rel_path="gone.txt", size=3))

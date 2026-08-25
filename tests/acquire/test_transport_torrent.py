"""The torrent transport, against a real swarm of two on loopback.

No mocking of libtorrent in the end-to-end tests: a seeding session and a
downloading session, a real .torrent, real pieces over 127.0.0.1. Mocking
the swarm would test the mock — and the parts most likely to be wrong here
(file priorities, the `.part` rename, per-file progress) are precisely the
ones only the real library can confirm.

DHT and local service discovery are off throughout: these tests must never
reach for the public network, and the peer is added directly.
"""
import os
import threading
import time
from pathlib import Path

import pytest

from app.acquire import transport_torrent as tt
from app.acquire.spec import Endpoint, RemoteFile, TransferAborted, Transport, TransportError

lt = pytest.importorskip("libtorrent")

CONTENT = {
    "corpus/alpha.bin": bytes(range(256)) * 400,      # 102,400 bytes
    "corpus/beta.bin": b"beta" * 12_000,              # 48,000 bytes
}


def _quiet_session(port_hint: int = 0):
    return lt.session({
        "listen_interfaces": f"127.0.0.1:{port_hint}",
        "alert_mask": 0, "enable_dht": False, "enable_lsd": False,
        "enable_upnp": False, "enable_natpmp": False,
    })


@pytest.fixture
def swarm(tmp_path):
    """A seeder holding CONTENT, plus the .torrent describing it."""
    seed_root = tmp_path / "seeder"
    for rel, data in CONTENT.items():
        p = seed_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    fs = lt.file_storage()
    lt.add_files(fs, str(seed_root / "corpus"))
    ct = lt.create_torrent(fs, piece_size=16 * 1024)
    ct.set_creator("louise-rag tests")
    lt.set_piece_hashes(ct, str(seed_root))
    torrent_path = tmp_path / "corpus.torrent"
    torrent_path.write_bytes(lt.bencode(ct.generate()))

    seeder = _quiet_session()
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(str(torrent_path))
    params.save_path = str(seed_root)
    seed_handle = seeder.add_torrent(params)
    deadline = time.time() + 30
    while not seed_handle.status().is_seeding and time.time() < deadline:
        time.sleep(0.1)
    assert seed_handle.status().is_seeding, "the seeder must be ready to serve"

    yield {"torrent": torrent_path,
           "port": seeder.listen_port(),
           "seed_root": seed_root}

    seeder.remove_torrent(seed_handle)


@pytest.fixture(autouse=True)
def clean_session(monkeypatch):
    """Each test gets its own process-global session, on loopback only."""
    tt.shutdown_session()
    monkeypatch.setenv("TORRENT_LISTEN", "127.0.0.1:0")
    monkeypatch.setenv("TORRENT_DHT", "0")
    monkeypatch.setenv("TORRENT_LSD", "0")
    yield
    tt.shutdown_session()


def _endpoint(swarm):
    return Endpoint(transport=Transport.TORRENT, url=str(swarm["torrent"]))


def _connect_peer(swarm):
    """Wire the downloader straight to the seeder: no tracker, no DHT."""
    for handle in list(tt._handles.values()):
        handle.connect_peer(("127.0.0.1", swarm["port"]))


# ---------------- metadata ----------------

def test_list_files_reads_the_tree_without_downloading(swarm, tmp_path):
    """A .torrent is parsed outright — no session, no save path, no peer."""
    files = sorted(TorrentT().list_files(_endpoint(swarm), None),
                   key=lambda f: f.rel_path)
    assert [f.rel_path for f in files] == ["corpus/alpha.bin", "corpus/beta.bin"]
    assert [f.size for f in files] == [len(CONTENT["corpus/alpha.bin"]),
                                        len(CONTENT["corpus/beta.bin"])]
    assert all(f.size and f.size > 0 for f in files), \
        "a torrent always knows its sizes — adoption and the preflight rely on it"


def TorrentT():
    return tt.TorrentTransport()


def test_a_missing_torrent_file_is_a_clear_error(tmp_path):
    ep = Endpoint(transport=Transport.TORRENT, url=str(tmp_path / "nope.torrent"))
    with pytest.raises(TransportError, match="not a magnet URI"):
        list(TorrentT().list_files(ep, None))


# ---------------- the download ----------------

def test_one_file_is_fetched_and_the_other_is_not(swarm, tmp_path):
    """The engine's unit of work is one file and one manifest row. Pulling
    the whole torrent would fetch bytes no profile asked for and record none
    of them."""
    corpus = tmp_path / "corpus_dir"
    dest = corpus / "corpus/alpha.bin"
    remote = RemoteFile(rel_path="corpus/alpha.bin",
                        size=len(CONTENT["corpus/alpha.bin"]))

    done = {}
    def run():
        try:
            done["size"] = TorrentT().fetch_one(_endpoint(swarm), remote, dest, 0)
        except Exception as e:      # surfaced by the assertions below
            done["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    for _ in range(60):             # let the handle exist, then wire the peer
        if tt._handles:
            break
        time.sleep(0.1)
    _connect_peer(swarm)
    t.join(timeout=120)

    assert "error" not in done, done.get("error")
    assert done.get("size") == len(CONTENT["corpus/alpha.bin"])
    assert dest.read_bytes() == CONTENT["corpus/alpha.bin"], "bytes must match exactly"
    # the file nobody asked for was never written
    assert not (corpus / "corpus/beta.bin").exists()


def test_no_partial_file_ever_occupies_the_final_path(swarm, tmp_path):
    """The hazard this transport is shaped around: libtorrent writes into
    save_path as pieces arrive, and discovery's (mtime, size) -> hash gate
    would ingest a half-written file as a complete document."""
    corpus = tmp_path / "corpus_dir"
    dest = corpus / "corpus/alpha.bin"
    remote = RemoteFile(rel_path="corpus/alpha.bin",
                        size=len(CONTENT["corpus/alpha.bin"]))

    seen_sizes = []
    stop_watch = threading.Event()

    def watch():
        while not stop_watch.is_set():
            if dest.exists():
                seen_sizes.append(dest.stat().st_size)
            time.sleep(0.01)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    result = {}
    worker = threading.Thread(
        target=lambda: result.update(
            size=TorrentT().fetch_one(_endpoint(swarm), remote, dest, 0)),
        daemon=True)
    worker.start()
    for _ in range(60):
        if tt._handles:
            break
        time.sleep(0.1)
    _connect_peer(swarm)
    worker.join(timeout=120)
    stop_watch.set()
    watcher.join(timeout=5)

    assert result.get("size"), "the fetch must have completed"
    full = len(CONTENT["corpus/alpha.bin"])
    assert all(s == full for s in seen_sizes), \
        f"the final path was seen at partial sizes: {sorted(set(seen_sizes))[:5]}"


def test_only_content_files_are_staged_as_parts(swarm, tmp_path):
    """The mechanism behind the guarantee above, asserted where it is
    decided: which indices get renamed. Padding is libtorrent's own
    bookkeeping and must be left alone, and the rename must be CONFIRMED
    before anything resumes — rename_file is asynchronous, and racing the
    disk thread is how pieces end up at the real path."""
    root = tmp_path / "root"
    handle = tt._add_for_download(_endpoint(swarm), root)
    tt._await_metadata(handle)

    info = handle.torrent_file()
    fs = info.files()
    content = {i for i, _, _ in tt.content_files(info)}
    pads = {i for i in range(fs.num_files()) if tt._is_pad(fs, i)}
    assert pads, "this torrent is expected to contain padding"

    renamed = []
    real_rename = handle.rename_file
    def spy(index, name):
        renamed.append((index, name))
        real_rename(index, name)
    handle.rename_file = spy          # type: ignore[method-assign]

    key = str(swarm['torrent'])
    tt._rename_to_parts(handle, key)  # returns only once libtorrent confirms

    assert {i for i, _ in renamed} == content, "pads must never be renamed"
    assert all(name.endswith(".part") for _, name in renamed), renamed

    renamed.clear()
    tt._rename_to_parts(handle, key)
    assert renamed == [], "staging must be idempotent across retries"


def test_part_name_is_computed_not_read_back():
    """torrent_file() is immutable metadata and never reflects a rename.
    Reading the stored name back was the original bug: it looked for a file
    libtorrent had never been asked to write under that name."""
    assert tt.part_name("corpus/alpha.bin") == "corpus/alpha.bin.part"


def test_stopping_mid_download_aborts_rather_than_fails(swarm, tmp_path):
    """A clean SIGTERM is not a failure: the engine records `aborted`, which
    never increments retries and never writes an error (invariant 2)."""
    corpus = tmp_path / "corpus_dir"
    remote = RemoteFile(rel_path="corpus/alpha.bin",
                        size=len(CONTENT["corpus/alpha.bin"]))
    stop = threading.Event()
    stop.set()          # already stopping when the fetch begins

    from app.transfer import manager as tm
    from app.transfer.bucket import Priority
    from app.transfer.manager import Direction

    with tm.get_manager().bind(Priority.BACKFILL, Direction.DOWN, stop=stop):
        with pytest.raises(TransferAborted):
            TorrentT().fetch_one(_endpoint(swarm), remote,
                                  corpus / "corpus/alpha.bin", 0)


# ---------------- wiring ----------------

def test_the_registry_exposes_a_usable_transport():
    from app.acquire.transports import REGISTRY
    transport = REGISTRY[Transport.TORRENT]
    assert hasattr(transport, "list_files") and hasattr(transport, "fetch_one")
    assert transport.seeds is True, "this is the one transport that gives back"


def test_save_root_strips_the_torrent_relative_path():
    """A completed file must already be where the platform expects it, so
    the save root is exactly dest minus the torrent-relative name."""
    assert tt.save_root(Path("/corpora/x/a/b/c.bin"), "a/b/c.bin") == Path("/corpora/x")
    assert tt.save_root(Path("/corpora/x/c.bin"), "c.bin") == Path("/corpora/x")


def test_the_session_is_shared_so_finished_torrents_keep_seeding(swarm, tmp_path):
    first = tt.get_session()
    assert tt.get_session() is first, "one session per process, or seeding stops"
    handle_a = tt._add_for_download(_endpoint(swarm), tmp_path / "root")
    handle_b = tt._add_for_download(_endpoint(swarm), tmp_path / "root")
    assert handle_a is handle_b, \
        "a session refuses the same infohash twice — the handle must be reused"


# ---------------- publishing (the seeding half) ----------------

def test_publish_produces_a_usable_torrent_and_magnet(tmp_path):
    """The seeding half: a corpus this node holds becomes something a peer
    can ask for."""
    from app.acquire import torrent_publish

    corpus = tmp_path / "mycorpus"
    (corpus / "sub").mkdir(parents=True)
    (corpus / "a.bin").write_bytes(b"a" * 5000)
    (corpus / "sub" / "b.bin").write_bytes(b"b" * 7000)

    out = tmp_path / "out" / "mycorpus.torrent"
    published = torrent_publish.publish_corpus(
        corpus, out, piece_size=16 * 1024, comment="test")

    assert out.is_file()
    assert published.files == 2
    assert published.total_bytes == 12000
    assert published.magnet.startswith("magnet:?")
    assert published.info_hash and published.info_hash != "0" * len(published.info_hash)

    # and the transport can read back exactly what was published
    ep = Endpoint(transport=Transport.TORRENT, url=str(out))
    listed = sorted(TorrentT().list_files(ep, None), key=lambda f: f.rel_path)
    assert [f.rel_path for f in listed] == ["mycorpus/a.bin", "mycorpus/sub/b.bin"]
    assert [f.size for f in listed] == [5000, 7000]


def test_publishing_an_empty_directory_is_refused(tmp_path):
    from app.acquire import torrent_publish
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(TransportError, match="nothing to publish"):
        torrent_publish.publish_corpus(empty, tmp_path / "x.torrent")


def test_web_seeds_are_recorded_so_a_dead_swarm_still_works(tmp_path):
    """BEP 19: the origin URL as a permanent seed. This is what stops a
    brand-new swarm with no peers from being useless."""
    from app.acquire import torrent_publish
    corpus = tmp_path / "c"
    corpus.mkdir()
    (corpus / "f.bin").write_bytes(b"z" * 4000)

    out = tmp_path / "c.torrent"
    torrent_publish.publish_corpus(
        corpus, out, piece_size=16 * 1024,
        web_seeds=("https://example.invalid/corpus/",))
    info = lt.torrent_info(str(out))
    assert any("example.invalid" in s["url"] for s in info.web_seeds()), \
        info.web_seeds()


def test_the_published_torrent_is_hybrid_v1_and_v2(tmp_path):
    """v2 for per-file merkle trees (unchanged files shared between
    snapshots), v1 so older clients are not excluded."""
    from app.acquire import torrent_publish
    corpus = tmp_path / "c"
    corpus.mkdir()
    (corpus / "f.bin").write_bytes(b"z" * 100_000)

    out = tmp_path / "c.torrent"
    torrent_publish.publish_corpus(corpus, out, piece_size=16 * 1024)
    hashes = lt.torrent_info(str(out)).info_hashes()
    assert hashes.has_v1(), "v1-only clients must still be able to join"
    assert hashes.has_v2(), "v2 is why snapshots can share unchanged files"


def test_publish_excludes_extra_format_files(tmp_path):
    """POLICY (spec.extras): extra formats are a local convenience and are
    NEVER p2p material — a published corpus torrent must not carry them,
    whether or not the collection currently opts in (files from a past
    opt-in stay on disk)."""
    from app.acquire import torrent_publish

    corpus = tmp_path / "gut"
    (corpus / "1").mkdir(parents=True)
    (corpus / "1" / "pg1.txt").write_bytes(b"t" * 4000)
    (corpus / "1" / "pg1.epub").write_bytes(b"e" * 6000)

    out = tmp_path / "gut.torrent"
    published = torrent_publish.publish_corpus(
        corpus, out, piece_size=16 * 1024,
        exclude_patterns=("*.epub",))

    assert published.files == 1
    assert published.total_bytes == 4000
    ep = Endpoint(transport=Transport.TORRENT, url=str(out))
    listed = [f.rel_path for f in TorrentT().list_files(ep, None)]
    assert any(p.endswith("pg1.txt") for p in listed)
    assert not any(p.endswith(".epub") for p in listed)


def test_downloaded_bytes_are_credited_to_the_budget(swarm, tmp_path):
    """Invariant 5's accounting half: libtorrent moves bytes on its own
    threads, so the transport credits each handle's download delta as
    external bytes — the Network page's totals then see a torrent
    download (live: 2.5 GB of wikivec arrived while the panel read 0)."""
    from app.transfer.bucket import Priority
    from app.transfer.manager import Direction, get_manager
    before = get_manager().bucket(Direction.DOWN).core.granted[Priority.BACKFILL]
    remote = RemoteFile(rel_path="corpus/alpha.bin", size=len(CONTENT["corpus/alpha.bin"]))
    dest = tmp_path / "dl" / "corpus" / "alpha.bin"
    t = threading.Thread(target=lambda: TorrentT().fetch_one(_endpoint(swarm), remote, dest, 0))
    t.start()
    for _ in range(100):
        if tt._handles:
            _connect_peer(swarm)
            break
        time.sleep(0.05)
    t.join(60)
    assert dest.read_bytes() == CONTENT["corpus/alpha.bin"]
    after = get_manager().bucket(Direction.DOWN).core.granted[Priority.BACKFILL]
    assert after - before >= len(CONTENT["corpus/alpha.bin"]), \
        "every byte the torrent pulled must be credited to the budget"

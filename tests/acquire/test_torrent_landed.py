"""app/acquire/torrent_landed.py — a torrent file moves into place only once
the disk holds what libtorrent verified (2026-10-08).

libtorrent 2.x counts a piece in `file_progress` once its hash passed and
writes its blocks afterwards, the file already at full length. A fake
session whose disk lags `file_progress`, as the share transport's test
does (tests/share/test_transport.py, `_LaggingDisk`), over REAL torrent
metadata of the three kinds: v1 without padding (a file's end pieces
shared with a neighbour this node does not hold), hybrid (padded: every
piece checkable), v2-only (the pieces root)."""
import re
import types
from pathlib import Path

import pytest

lt = pytest.importorskip("libtorrent")

from app.acquire import engine, torrent_landed, transport_torrent as tt  # noqa: E402
from app.acquire.spec import Endpoint, RemoteFile, Transport, TransportError  # noqa: E402
from app.hold import drag as drag_mod  # noqa: E402

PIECE = 16 * 1024
ALPHA = bytes(range(251)) * 400                   # 100,400 bytes: ends inside a piece
BETA = b"beta" * 12_000


def _torrent_bytes(tmp_path, kind: str) -> bytes:
    src = tmp_path / f"src-{kind}"
    (src / "c").mkdir(parents=True)
    (src / "c" / "alpha.bin").write_bytes(ALPHA)
    (src / "c" / "beta.bin").write_bytes(BETA)
    fs = lt.file_storage()
    lt.add_files(fs, str(src / "c"))
    flags = {"v1": lt.create_torrent.v1_only, "v2": lt.create_torrent.v2_only, "hybrid": 0}[kind]
    ct = lt.create_torrent(fs, PIECE, flags=flags) if flags else lt.create_torrent(fs, PIECE)
    lt.set_piece_hashes(ct, str(src))
    return lt.bencode(ct.generate())


def _torrent(tmp_path, kind: str):
    return lt.torrent_info(lt.bdecode(_torrent_bytes(tmp_path, kind)))


class _Flushed:
    """What libtorrent posts when a flush's fence job has run."""

    def __init__(self, handle):
        self.handle = handle


_Flushed.__name__ = "cache_flushed_alert"


class _LaggingDisk:
    """Session and handle at once. `file_progress` says alpha is whole from
    the first look; on disk the `.part` stands at full length with the
    block at `lag_at` still zeros until `polls_left` sleeps have passed —
    or, when `fence_writes`, until a flush's fence has run: the write is
    done when its alert is popped, as libtorrent posts it after the fence."""

    def __init__(self, ti, save: Path, *, lag_at: int, polls_left: int = 2, fence_writes=False):
        self.ti, self.save = ti, save
        self.part = save / tt.part_name("c/alpha.bin")
        self.part.parent.mkdir(parents=True, exist_ok=True)
        held = bytearray(ALPHA)
        held[lag_at:lag_at + 1024] = bytes(min(1024, len(ALPHA) - lag_at))
        self.part.write_bytes(bytes(held))
        self.polls_left, self.fence_writes = polls_left, fence_writes
        self.flushes, self.fences_due = 0, 0

    def catch_up(self):
        self.part.write_bytes(ALPHA)

    def poll(self):
        self.polls_left -= 1
        if self.polls_left == 0:
            self.catch_up()

    # the session
    def pop_alerts(self):
        due, self.fences_due = self.fences_due, 0
        if due and self.fence_writes:
            self.catch_up()
        return [_Flushed(self) for _ in range(due)]

    # the handle
    def torrent_file(self):
        return self.ti

    def info_hashes(self):
        return self.ti.info_hashes()

    def status(self):
        return types.SimpleNamespace(save_path=str(self.save), total_download=len(ALPHA), errc=None)

    def file_progress(self):
        fs = self.ti.files()
        return [fs.file_size(i) for i in range(fs.num_files())]

    def flush_cache(self):
        self.flushes += 1
        self.fences_due += 1

    def file_priority(self, i, p):
        pass

    def prioritize_files(self, p):
        pass

    def resume(self):
        pass

    def set_download_limit(self, n):
        pass


@pytest.fixture
def wired(monkeypatch):
    """fetch_one around a fake handle: no session, no lease, no stop."""
    def wire(disk):
        from app.transfer import manager as tm

        class Lease:
            def __enter__(self): return None
            def __exit__(self, *a): return False

        monkeypatch.setattr(tm, "get_manager", lambda: types.SimpleNamespace(rsync_lease=lambda url: Lease()))
        monkeypatch.setattr(tt, "_current_stop", lambda: None)
        monkeypatch.setattr(tt, "_add_for_download", lambda ep, root: disk)
        monkeypatch.setattr(tt, "_await_metadata", lambda h, s=None: None)
        monkeypatch.setattr(tt, "_rename_to_parts", lambda *a, **k: None)
        monkeypatch.setattr(tt, "_save_resume", lambda h: None)
        monkeypatch.setattr(tt, "_credit_download", lambda key, h: None)
        monkeypatch.setattr(tt, "get_session", lambda: disk)
        clock = [1000.0]                 # a sleep is a poll, and the only time that passes

        def sleep(s):
            clock[0] += s
            disk.poll()
        monkeypatch.setattr(torrent_landed, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
        monkeypatch.setattr(torrent_landed, "_flushes", {})
    return wire


def _fetch(disk, root: Path) -> Path:
    dest = root / "c/alpha.bin"
    ep = Endpoint(transport=Transport.TORRENT, url="magnet:?xt=urn:btih:" + "0" * 40)
    tt.TorrentTransport().fetch_one(ep, RemoteFile(rel_path="c/alpha.bin", size=len(ALPHA)), dest, 0)
    return dest


@pytest.mark.parametrize("kind", ["v1", "hybrid", "v2"])
def test_a_file_received_whole_moves_into_place_only_once_the_disk_holds_it(tmp_path, wired, kind):
    disk = _LaggingDisk(_torrent(tmp_path, kind), tmp_path / "root", lag_at=2 * PIECE + 100)
    wired(disk)
    dest = _fetch(disk, tmp_path / "root")
    assert dest.read_bytes() == ALPHA, "moved into place while a block was still due"
    assert not disk.part.exists()


def _alpha(ti) -> tuple[int, int]:
    """alpha's index and offset in the torrent (libtorrent orders the files)."""
    index = tt._index_of(ti, "c/alpha.bin")
    return index, ti.files().file_offset(index)


def _shared_end(ti) -> int:
    """An offset in alpha inside a piece it shares with beta."""
    _, off = _alpha(ti)
    if off % PIECE:
        return 10                                         # beta first: alpha's first piece
    return len(ALPHA) // PIECE * PIECE + 10               # alpha first: its last piece


def test_a_v1_end_piece_shared_with_a_neighbour_waits_for_libtorrent_s_fence(tmp_path, wired):
    """In a v1 torrent without padding, alpha's end piece holds beta's
    bytes too, which this node never wrote: no hash can be checked from
    alpha alone, so the flush's fence says when the writes are done — and
    the lagging block is in that piece."""
    ti = _torrent(tmp_path, "v1")
    disk = _LaggingDisk(ti, tmp_path / "root", lag_at=_shared_end(ti), polls_left=-1, fence_writes=True)
    wired(disk)
    dest = _fetch(disk, tmp_path / "root")
    assert dest.read_bytes() == ALPHA, "alpha's own pieces hashed right; the shared one was still due"
    assert disk.flushes == 1, "one flush, its alert waited for"


def test_a_disk_that_never_comes_right_is_a_deferral_never_a_verdict(tmp_path, wired, monkeypatch):
    disk = _LaggingDisk(_torrent(tmp_path, "hybrid"), tmp_path / "root", lag_at=PIECE, polls_left=-1)
    wired(disk)
    with pytest.raises(TransportError) as e:
        _fetch(disk, tmp_path / "root")
    assert engine.transient(str(e.value)), "read by the engine as a deferral, not an error on the file"
    assert not (tmp_path / "root/c/alpha.bin").exists(), "nothing moved into place"
    assert disk.part.exists(), "the received bytes stay for the next pass"


def test_check_pieces_names_the_bad_and_the_shared_pieces(tmp_path):
    ti = _torrent(tmp_path, "v1")
    index, off = _alpha(ti)
    p = tmp_path / "a"
    held = bytearray(ALPHA)
    flipped = 3 * PIECE + 5                               # inside alpha whatever its offset
    held[flipped] ^= 0xFF
    p.write_bytes(bytes(held))
    pieces = torrent_landed.file_pieces(ti, index)
    bad, foreign = torrent_landed.check_pieces(ti, index, p, pieces)
    assert bad == {(off + flipped) // PIECE}
    assert foreign == {(off + _shared_end(ti)) // PIECE}, "the piece shared with beta, and only it"
    p.write_bytes(ALPHA[:-1])
    assert torrent_landed.check_pieces(ti, index, p, pieces)[0], "a short file is bad"
    hy = _torrent(tmp_path, "hybrid")
    i, _ = _alpha(hy)
    p.write_bytes(ALPHA)
    assert torrent_landed.check_pieces(hy, i, p, torrent_landed.file_pieces(hy, i)) == (set(), set()),         "padded: every piece checkable, none bad"


def test_the_streamed_pieces_root_is_the_in_memory_one(tmp_path):
    B = drag_mod.BLOCK
    for n in (0, 1, B, B + 1, 2 * B, 3 * B - 7, 5 * B, 17 * B + 3, 64 * B):
        data = bytes((i * 7 + n) % 256 for i in range(n))
        p = tmp_path / f"f{n}"
        p.write_bytes(data)
        assert drag_mod.pieces_root_of(p) == drag_mod.pieces_root(data), n


def test_a_lost_flush_alert_never_holds_back_the_next(monkeypatch):
    """A flush whose alert never came (a reader that did not pass it on) is
    dropped once stale, so the next flush of the same torrent is confirmed
    by its own alert."""
    class H:
        def info_hashes(self):
            return types.SimpleNamespace(get_best=lambda: "k")

        def flush_cache(self):
            pass

    h = H()
    clock = [1000.0]
    monkeypatch.setattr(torrent_landed, "time", types.SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s)))
    monkeypatch.setattr(torrent_landed, "_flushes", {})
    session = types.SimpleNamespace(pop_alerts=lambda: [])
    assert not torrent_landed.flushed(session, h, until=1000.5, poll_s=0.25)   # its alert lost
    clock[0] += torrent_landed.FLUSH_LOST_S + 1
    alerts = []
    session = types.SimpleNamespace(pop_alerts=lambda: [alerts.pop()] if alerts else [])
    alerts.append(_Flushed(h))
    assert torrent_landed.flushed(session, h, until=clock[0] + 5, poll_s=0.25)


def test_every_reader_of_the_session_s_alerts_passes_them_on():
    """An alert popped is gone for every other reader: a flush confirmation
    popped by a DHT probe would leave the transfer waiting for it."""
    app = Path(__file__).resolve().parents[2] / "app"
    offenders = []
    for py in app.rglob("*.py"):
        for n, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.pop_alerts\(\)", line) and "note_alerts(session.pop_alerts())" not in line:
                offenders.append(f"{py.relative_to(app)}:{n}")
    assert offenders == [], offenders


# ---- against the real library ----------------------------------------------

@pytest.fixture(autouse=True)
def clean_session(monkeypatch):
    tt.shutdown_session()
    monkeypatch.setenv("TORRENT_LISTEN", "127.0.0.1:0")
    monkeypatch.setenv("TORRENT_DHT", "0")
    monkeypatch.setenv("TORRENT_LSD", "0")
    yield
    tt.shutdown_session()


def test_two_files_of_one_torrent_fetched_at_once_never_switch_each_other_off(tmp_path):
    """Up to `per_endpoint` workers fetch files of one torrent at once. Each
    set the WHOLE priority vector, so the second turned the first one's file
    off and left it waiting forever. Against the real library: the vector
    once, then one file at a time — and each wait re-asserting its own, as
    libtorrent can lose one of two changes made at once."""
    import time
    tfile = tmp_path / "t.torrent"
    tfile.write_bytes(_torrent_bytes(tmp_path, "v1"))
    handle = tt._add_for_download(Endpoint(transport=Transport.TORRENT, url=str(tfile)), tmp_path / "dl")
    fs = handle.torrent_file().files()
    tt._set_wanted(handle, str(tfile), fs, 0, True)
    tt._set_wanted(handle, str(tfile), fs, 1, True)
    deadline = time.monotonic() + 5
    while list(handle.get_file_priorities()) != [4, 4] and time.monotonic() < deadline:
        time.sleep(0.05)
        for i in (0, 1):                  # each worker's wait loop
            tt._keep_wanted(handle, str(tfile), fs, i)
    time.sleep(0.5)                       # and no change in flight undoes it afterwards
    for i in (0, 1):
        tt._keep_wanted(handle, str(tfile), fs, i)
    deadline = time.monotonic() + 5
    while list(handle.get_file_priorities()) != [4, 4] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert list(handle.get_file_priorities()) == [4, 4], "the first worker's file is still wanted"
    tt._set_wanted(handle, str(tfile), fs, 1, False)       # a stop turns its own file off, only
    deadline = time.monotonic() + 5
    while list(handle.get_file_priorities()) != [4, 0] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert list(handle.get_file_priorities()) == [4, 0]


"""No torrent moves a byte before its priorities and its lease are set
(2026-10-08).

libtorrent's `add_torrent_params` default to auto-managed, and the
auto-manager starts an auto-managed torrent whatever `paused` says. A magnet
added as a magnet therefore ran at once, and its metadata arrived with
every file at the default priority 4: bytes of files no profile asked for,
ahead of the file priorities and of the lease. A fake session models just
that much of libtorrent — when a torrent runs, when its metadata is known,
which files it then wants — and records every file requested and whether
the lease's ceiling was on it yet."""
import os
import types

import pytest

lt = pytest.importorskip("libtorrent")

from app.acquire import torrent_landed, transport_torrent as tt  # noqa: E402
from app.acquire.spec import Endpoint, RemoteFile, Transport  # noqa: E402
from tests.acquire.test_torrent_landed import ALPHA, _torrent_bytes  # noqa: E402


class _Torrent:
    """One torrent in the fake session: it RUNS from the add when
    auto-managed or not paused (the auto-manager starts it), else from
    `resume()`; its metadata is known from the add when given, else once it
    runs; and while it runs with metadata, it requests every file whose
    priority is above 0 — the default being 4."""

    def __init__(self, session, params, ti):
        self.session, self.ti = session, ti
        flags = int(params.flags)
        self.upload_mode = bool(flags & int(lt.torrent_flags.upload_mode))
        self.running = bool(flags & int(lt.torrent_flags.auto_managed)) or not flags & int(lt.torrent_flags.paused)
        self.with_metadata = params.ti is not None
        self.prio = list(params.file_priorities)
        self.limited = False
        self.valid = True
        self.save = params.save_path
        self._tick()

    def _n(self):
        return self.ti.files().num_files()

    def _tick(self):
        if self.running and not self.with_metadata:
            self.with_metadata = True                # the swarm answered
        if self.running and self.with_metadata and not self.upload_mode:
            prio = self.prio + [4] * (self._n() - len(self.prio))
            for i, p in enumerate(prio):
                if p > 0:
                    self.session.requested.add(i)
                    if not self.limited:
                        self.session.unlimited.add(i)

    # the handle
    def status(self):
        self._tick()
        return types.SimpleNamespace(has_metadata=self.with_metadata, total_download=0, errc=None,
                                     save_path=self.save, num_peers=1, paused=not self.running)

    def torrent_file(self):
        return self.ti

    def resume(self):
        self.running = True
        self._tick()

    def prioritize_files(self, p):
        self.prio = list(p)
        self._tick()

    def file_priority(self, i, p=None):
        prio = self.prio + [4] * (self._n() - len(self.prio))
        if p is None:
            return prio[i]
        prio[i] = p
        self.prio = prio
        self._tick()

    def file_progress(self):
        self._tick()
        fs = self.ti.files()
        return [fs.file_size(i) if i in self.session.requested else 0 for i in range(self._n())]

    def set_download_limit(self, n):
        self.limited = True

    def connect_peer(self, h):
        pass

    def flush_cache(self):
        pass

    def is_valid(self):
        return self.valid

    def info_hashes(self):
        return self.ti.info_hashes()


class _Session:
    def __init__(self, ti):
        self.ti, self.requested, self.unlimited = ti, set(), set()

    def add_torrent(self, params):
        return _Torrent(self, params, self.ti)

    def remove_torrent(self, h):
        h.valid = False

    def pop_alerts(self):
        return []


def _lease(monkeypatch):
    from app.transfer import manager as tm

    class Lease:
        def __enter__(self): return 1_000_000
        def __exit__(self, *a): return False

    monkeypatch.setattr(tm, "get_manager", lambda: types.SimpleNamespace(rsync_lease=lambda url: Lease()))


@pytest.mark.parametrize("source", ["magnet", "torrent"])
def test_an_acquired_torrent_requests_only_the_file_asked_for(tmp_path, monkeypatch, source):
    raw = _torrent_bytes(tmp_path, "v1")
    ti = lt.torrent_info(lt.bdecode(raw))
    session = _Session(ti)
    tt.shutdown_session()
    monkeypatch.setattr(tt, "get_session", lambda: session)
    monkeypatch.setattr(tt, "_current_stop", lambda: None)
    monkeypatch.setattr(tt, "_rename_to_parts", lambda *a, **k: None)
    monkeypatch.setattr(tt, "_save_resume", lambda h: None)
    monkeypatch.setattr(tt, "_credit_download", lambda key, h: None)
    monkeypatch.setattr(torrent_landed, "wait_landed", lambda *a, **k: None)
    monkeypatch.setattr(tt, "os", types.SimpleNamespace(replace=lambda a, b: None, getenv=os.getenv))
    _lease(monkeypatch)
    if source == "magnet":
        url = lt.make_magnet_uri(ti)
    else:
        url = str(tmp_path / "t.torrent")
        (tmp_path / "t.torrent").write_bytes(raw)
    alpha = tt._index_of(ti, "c/alpha.bin")
    tt.TorrentTransport().fetch_one(Endpoint(transport=Transport.TORRENT, url=url),
                                   RemoteFile(rel_path="c/alpha.bin", size=len(ALPHA)),
                                   tmp_path / "root" / "c" / "alpha.bin", 0)
    assert session.requested == {alpha}, "a file no profile asked for was requested"
    assert session.unlimited == set(), "a byte was requested before the lease was its ceiling"
    tt.shutdown_session()


def test_a_member_torrent_requests_nothing_before_its_lease(tmp_path, monkeypatch):
    from app.hold import drag as drag_mod
    from app.share import members as mem, transport
    data = b"m" * 40_000
    member = mem.Member("records-x", (drag_mod.MemberFile("records/x/1.jsonl.zst", drag_mod.pieces_root(data), len(data)),))
    t = member.torrents()[0]
    ti = lt.torrent_info(lt.bdecode(mem.bencode({"info": t.info})))
    session = _Session(ti)
    _lease(monkeypatch)
    monkeypatch.setattr(transport, "landed_whole", lambda *a: True)
    monkeypatch.setattr(tt, "_credit_download", lambda key, h: None)
    transport.fetch_file(session, torrent=t, member=member.name, staging=tmp_path, poll_s=0.0)
    assert session.requested == {0}
    assert session.unlimited == set(), "a byte was requested before the lease was its ceiling"

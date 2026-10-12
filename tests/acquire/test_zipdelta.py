"""A zip's delta by its central directory (app/acquire/zipdelta.py; review
§3.10, decision 5): the directory read from the tail, changed members fetched
one Range each, the local copy rebuilt from raw bytes, every fallback a
named exception the engine turns into the whole fetch."""
import random
import zipfile
from pathlib import Path

import pytest

from app.acquire import zipdelta


def _zip(path: Path, members: dict[str, bytes], *, method=zipfile.ZIP_DEFLATED) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", method) as z:
        z.writestr("DIR_1/", b"")
        for name, data in members.items():
            z.writestr(name, data)
    return path


def _members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {i.filename: z.read(i) for i in z.infolist() if not i.is_dir()}


#: incompressible members, as the origin's deflated XML is to a second deflate
V1 = {f"DIR_1/1999-01-01_X{i:04d}_ORIGINALE_V0.xml": random.Random(i).randbytes(2048) for i in range(20)}
V2 = dict(V1)
V2["DIR_1/1999-01-01_X0003_ORIGINALE_V0.xml"] = random.Random(1003).randbytes(2048)   # changed
V2["DIR_1/2026-10-12_NEW01_VIGENZA_2026-10-12_V1.xml"] = random.Random(2001).randbytes(1500)  # new
del V2["DIR_1/1999-01-01_X0007_ORIGINALE_V0.xml"]                                    # gone


def test_the_directory_is_read_from_the_tail_and_grows_when_it_must(tmp_path):
    remote = _zip(tmp_path / "remote.zip", V2)
    reader = zipdelta.FileRangeReader(remote)
    infos, total, read = zipdelta.directory(reader, tail=1 << 20)
    assert total == remote.stat().st_size and read <= total
    assert {i.filename for i in infos} == set(V2) | {"DIR_1/"}
    small = zipdelta.FileRangeReader(remote)
    infos2, _, read2 = zipdelta.directory(small, tail=64)
    assert [i.filename for i in infos2] == [i.filename for i in infos]
    assert len(small.reads) > 1 and read2 > 64, "a directory longer than the tail is read again, longer"


def test_the_plan_keeps_what_matches_and_fetches_the_rest(tmp_path):
    local = _zip(tmp_path / "local.zip", V1)
    remote = _zip(tmp_path / "remote.zip", V2)
    infos, _, _ = zipdelta.directory(zipdelta.FileRangeReader(remote))
    p = zipdelta.plan(local, infos)
    assert {i.filename for i in p.fetch} == {"DIR_1/1999-01-01_X0003_ORIGINALE_V0.xml",
                                             "DIR_1/2026-10-12_NEW01_VIGENZA_2026-10-12_V1.xml"}
    assert len(p.keep) == 18 + 1 and "DIR_1/" in p.keep, "eighteen unchanged acts and the directory entry"
    assert p.dropped == ["DIR_1/1999-01-01_X0007_ORIGINALE_V0.xml"]
    assert [i.filename for i in p.order] == [i.filename for i in infos]
    assert zipdelta.plan(None, infos).keep.keys() == {"DIR_1/"}, "no local copy: everything is fetched"


def test_a_member_is_fetched_by_one_range_verified_and_rebuilt_raw(tmp_path):
    local = _zip(tmp_path / "local.zip", V1)
    remote = _zip(tmp_path / "remote.zip", V2)
    reader = zipdelta.FileRangeReader(remote)
    out = tmp_path / "out.zip"
    got = zipdelta.update(reader, local, out, tail=2048)
    assert (got.members, got.fetched, got.kept, got.dropped) == (len(V2) + 1, 2, 19, 1)
    assert got.total == remote.stat().st_size
    assert got.bytes_read < got.total // 2, "the directory and two members, not the file"
    requests = len(reader.reads)
    assert requests == 1 + 2, "one read for the directory, one per fetched member"
    with zipfile.ZipFile(out) as z:
        assert z.testzip() is None
    assert _members(out) == V2, "every member decompresses to the origin's bytes"
    with zipfile.ZipFile(out) as z, zipfile.ZipFile(remote) as r:
        assert [i.filename for i in z.infolist()] == [i.filename for i in r.infolist()]
        assert all(z.getinfo(n).CRC == r.getinfo(n).CRC for n in V2)
        kept_raw = zipdelta.raw_member(open(out, "rb"), z.getinfo("DIR_1/1999-01-01_X0001_ORIGINALE_V0.xml"))
        with zipfile.ZipFile(local) as lz:
            local_raw = zipdelta.raw_member(open(local, "rb"), lz.getinfo("DIR_1/1999-01-01_X0001_ORIGINALE_V0.xml"))
        assert kept_raw == local_raw, "a kept member is its own bytes copied, never recompressed"


def test_a_stored_zip_and_a_first_fetch_rebuild_too(tmp_path):
    remote = _zip(tmp_path / "remote.zip", V2, method=zipfile.ZIP_STORED)
    out = tmp_path / "out.zip"
    got = zipdelta.update(zipdelta.FileRangeReader(remote), None, out, whole_is_cheaper=1.0)
    assert got.fetched == len(V2) and _members(out) == V2
    with zipfile.ZipFile(out) as z:
        assert z.testzip() is None


def test_a_member_that_does_not_check_out_is_refused(tmp_path):
    remote = _zip(tmp_path / "remote.zip", V2)
    infos, total, _ = zipdelta.directory(zipdelta.FileRangeReader(remote))
    info = next(i for i in infos if i.filename.endswith("X0003_ORIGINALE_V0.xml"))

    span = 30 + len(info.filename.encode()) + len(info.extra)

    class Tampered(zipdelta.FileRangeReader):
        def read(self, start, end):
            data = super().read(start, end)
            if start == info.header_offset:
                return data[:span] + bytes(b ^ 0xFF for b in data[span:span + 4]) + data[span + 4:]
            return data
    with pytest.raises(zipdelta.MemberMismatch):
        zipdelta.fetch_member(Tampered(remote), info, total)
    bad = zipfile.ZipInfo("x")
    bad.compress_type = 99
    with pytest.raises(zipdelta.MemberMismatch, match="not handled"):
        zipdelta.verify(bad, b"")


def test_a_delta_that_moves_most_of_the_file_hands_back_to_the_whole_fetch(tmp_path):
    local = _zip(tmp_path / "local.zip", {k: v + b"!" for k, v in V1.items()})   # every member differs
    remote = _zip(tmp_path / "remote.zip", V2)
    reader = zipdelta.FileRangeReader(remote)
    with pytest.raises(zipdelta.DeltaNotWorthIt):
        zipdelta.update(reader, local, tmp_path / "out.zip")
    assert len(reader.reads) == 1, "decided from the directory alone: no member was fetched"


def test_a_stop_between_members_replaces_nothing(tmp_path):
    import threading
    from app.acquire.spec import TransferAborted
    local = _zip(tmp_path / "local.zip", V1)
    remote = _zip(tmp_path / "remote.zip", V2)
    stop = threading.Event()
    stop.set()
    out = tmp_path / "out.zip"
    with pytest.raises(TransferAborted):
        zipdelta.update(zipdelta.FileRangeReader(remote), local, out, stop=stop)
    assert not out.exists() and _members(local) == V1


def test_many_members_rebuild_with_a_zip64_directory(tmp_path):
    """The royal decrees' zip holds 201,929 members: past 65,535 entries the
    directory needs zip64 records, which `zipfile` writes from `filelist`."""
    big = {f"D/{i:06d}_X_ORIGINALE_V0.xml": f"<a>{i}</a>".encode() for i in range(66_000)}
    remote = _zip(tmp_path / "remote.zip", big)
    out = tmp_path / "out.zip"
    got = zipdelta.update(zipdelta.FileRangeReader(remote), None, out, whole_is_cheaper=1.0, tail=1 << 22)
    assert got.members == len(big) + 1
    with zipfile.ZipFile(out) as z:
        assert len(z.infolist()) == len(big) + 1 and z.testzip() is None
        assert z.read("D/065999_X_ORIGINALE_V0.xml") == b"<a>65999</a>"

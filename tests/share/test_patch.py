"""tests/share/test_patch.py — part patches (app/share/patch.py; operator,
2026-10-12; docs/notes/part-deltas.md): a new version of a record part for
the price of its changed lines, rebuilt byte for byte, the root deciding;
the answering peer's cache; the pull; the fetch that tries a patch first."""
import json

import pytest

from app import platform_config
from app.hold import shard as shard_mod, snapshot as sn
from app.pack import records as pack_records
from app.share import holdings, messages, patch, transport
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.share.test_seeder import SLICE, a_shared_collection


def rec(n, text="text"):
    return {"uid": f"pubmed:{n}", "native_id": str(n), "lang": "en", "title": f"T{n}",
            "sections": [["", f"{text} {n}"]], "content_hash": f"h{n}", "origin": str(n),
            "origin_hash": f"o{n}", "chunks": 2}


def part(path, recs, frame_records=pack_records.FRAME_RECORDS):
    pack_records.write_shard(path, recs, frame_records=frame_records)
    return path


def test_the_writer_rebuilds_a_part_byte_for_byte_from_its_lines(tmp_path):
    """The proof patches rest on: the same lines, the producer's chunks and
    lines per frame, give write_shard's bytes, so the rebuilt root is the
    snapshot's."""
    a = part(tmp_path / "a.jsonl.zst", [rec(n) for n in range(1, 8)], frame_records=3)
    ix = pack_records.read_index(a)
    b = tmp_path / "b.jsonl.zst"
    pack_records.write_lines(b, patch.lines_of(a), key_of=lambda line: json.loads(line)["uid"],
                             chunks=ix.chunks, frame_counts=[f[3] for f in ix.frames])
    assert b.read_bytes() == a.read_bytes()
    assert shard_mod.pieces_root_of(b) == shard_mod.pieces_root_of(a)


def test_a_patch_carries_only_the_changed_lines_and_rebuilds_the_new_version(tmp_path):
    old = part(tmp_path / "old.jsonl.zst", [rec(1), rec(2), rec(3)])
    new = part(tmp_path / "new.jsonl.zst", [rec(1), rec(2, "revised"), rec(4)])
    data = patch.make(new, patch.have_of(old))
    body = json.loads(__import__("gzip").decompress(data))
    assert len(body["new"]) == 2, "the revised book and the new one, nothing else"
    assert sum(isinstance(o, str) for o in body["order"]) == 1
    out = patch.apply(old, data, tmp_path / "rebuilt.jsonl.zst")
    assert out.read_bytes() == new.read_bytes()
    assert patch.make(new, patch.have_of(old)) == data, "deterministic"
    with pytest.raises(patch.PatchError, match="does not hold"):
        patch.apply(part(tmp_path / "other.jsonl.zst", [rec(9)]), data, tmp_path / "x.jsonl.zst")


def test_the_answering_peer_caches_a_patch_and_zero_keeps_nothing(tmp_path):
    built = []

    def build():
        built.append(1)
        return b"patch bytes"
    cache = tmp_path / "patches"
    assert patch.cached(cache, "sha256:new", ["b", "a"], build, ttl_s=3600, now=1000.0) == b"patch bytes"
    assert patch.cached(cache, "sha256:new", ["a", "b"], build, ttl_s=3600, now=1001.0) == b"patch bytes"
    assert built == [1], "the second peer holding the same version: no recompute"
    patch.cached(cache, "sha256:new", ["a", "b"], build, ttl_s=10, now=10_000_000_000.0)
    assert built == [1, 1], "older than the cache time: gone, built again"
    zero = tmp_path / "zero"
    patch.cached(zero, "sha256:new", ["a"], build, ttl_s=0)
    assert not list(zero.glob("*")) if zero.exists() else True, "0: deleted once served"


def test_a_peer_answers_a_patch_pull_for_the_version_its_snapshot_lists(node, monkeypatch):  # noqa: F811
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    path = d / f"records/{SLICE}/00001.jsonl.zst"
    part(path, [rec(1), rec(2, "revised")])
    entry = snap.shards[0]
    snap2 = sn.Snapshot(collection_id=snap.collection_id, snapshot_id="2026-08", previous=None,
                        shards=(sn.Shard(slice=entry.slice, n=1, root=shard_mod.pieces_root_of(path),
                                         records_root=entry.records_root, first_id=entry.first_id,
                                         last_id=entry.last_id, records=2, chunks=4,
                                         chunks_root=entry.chunks_root, tokens=entry.tokens,
                                         bytes=path.stat().st_size), snap.shards[1]),
                        delta=snap.delta, files={})
    (d / "snapshots" / "2026-08.json").write_bytes(snap2.canonical())
    monkeypatch.setattr(platform_config, "load", lambda: platform_config.PlatformConfig())
    old = part(node / "old.jsonl.zst", [rec(1), rec(2)])
    body = {"what": "patch", "collection_id": codex.collection_id, "snapshot_id": "2026-08",
            "part": f"{SLICE}/00001", "have": patch.have_of(old)}
    items = messages.answer_pull(node, body)["items"]
    assert [i["path"] for i in items] == [f"patch/{SLICE}/00001.json.gz"]
    (_, data), = messages.verify_items(items)
    assert patch.apply(old, data, node / "rebuilt.jsonl.zst").read_bytes() == path.read_bytes()
    assert list((node / c.name / "state" / "patches").glob("*.patch")), "kept for the next peer"
    assert messages.answer_pull(node, {**body, "snapshot_id": "2026-07"})["items"] == []


def test_a_patch_pull_names_a_part_and_line_hashes():
    ok = messages.pull_body(key="ed25519:" + "a" * 64, what="patch", collection_id="sha256:x",
                            snapshot_id="2026-08", part="lang=en/00001", have=["0" * 64])
    assert ok["part"] == "lang=en/00001" and ok["have"] == ["0" * 64]
    assert "part" not in messages.pull_body(key="k", what="codex", collection_id="sha256:x")
    assert patch.part_of("records/lang=en/pages=3/00007.jsonl.zst") == "lang=en/pages=3/00007"
    assert patch.part_of("figures/lang=en/00001.fig.zst") is None


def test_the_fetch_tries_a_patch_first_and_a_miss_costs_the_download(node, monkeypatch):  # noqa: F811
    """A new version of a held part: the patcher's rebuild is promoted when its
    root is the snapshot's (the old version kept under its superseded name);
    a rebuild that misses is set aside and the part is fetched whole."""
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    newer = node / "newer.jsonl.zst"
    part(newer, [rec(1, "next month")])
    e = snap.shards[0]
    snap2 = sn.Snapshot(collection_id=snap.collection_id, snapshot_id="2026-09", previous=snap.hash,
                        shards=(sn.Shard(slice=e.slice, n=1, root=shard_mod.pieces_root_of(newer),
                                         records_root=e.records_root, first_id=e.first_id, last_id=e.last_id,
                                         records=1, chunks=2, chunks_root=e.chunks_root, tokens=e.tokens,
                                         bytes=newer.stat().st_size), snap.shards[1]),
                        delta=snap.delta, files={})
    (d / "snapshots" / "2026-09.json").write_bytes(snap2.canonical())
    import sqlite3
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.row_factory = sqlite3.Row
    fetched = []

    def fake_fetch_file(session, *, torrent, member, staging, **kw):
        fetched.append(torrent.path)
        out = staging / torrent.path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(newer.read_bytes())
        return out
    monkeypatch.setattr(transport, "fetch_file", fake_fetch_file)

    def good(f, out, sid):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(newer.read_bytes())
        return out
    got = transport.fetch_members(node, c.name, codex, slices="all", kinds=("records",),
                                  ack_missing=False, session=None, con=con, patcher=good)
    p1 = f"records/{SLICE}/00001.jsonl.zst"
    assert got["patched"] == [p1] and fetched == [], got
    assert (d / p1).read_bytes() == newer.read_bytes()
    assert (d / f"records/{SLICE}/00001.2026-08.jsonl.zst").read_bytes() == files[1], \
        "the 2026-08 version kept under its superseded name"

    (d / p1).write_bytes(files[1])                    # back to the old version, its mark removed
    holdings.unmark(con, p1)

    def bad(f, out, sid):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * newer.stat().st_size)
        return out
    got = transport.fetch_members(node, c.name, codex, slices="all", kinds=("records",),
                                  ack_missing=False, session=None, con=con, patcher=bad)
    assert got["patched"] == [] and fetched == [p1] and got["promoted"] == [p1], got
    assert (d / p1).read_bytes() == newer.read_bytes(), "the miss cost the download, nothing else"


# ---- plates containers -----------------------------------------------------------------

def plates(*names):
    out = {}
    for n in names:
        data = (f"plate {n} ".encode() * 300) + bytes(range(256))
        out[__import__("hashlib").sha256(data).hexdigest()] = data
    return out


def test_a_plates_patch_carries_the_new_frames_verbatim_and_rebuilds_the_container(tmp_path):
    """A container is one zstd frame per plate, so the plates the asker
    lacks travel as the producer's own frames and nothing is recompressed:
    the rebuilt container is the producer's bytes whatever zstd the asker has."""
    from app.hold import figures
    old_plates, new_plates = plates("a", "b", "c"), plates("a", "c", "d", "e")
    old = tmp_path / "old.fig.zst"
    new = tmp_path / "new.fig.zst"
    figures.write_container(old, old_plates)
    figures.write_container(new, new_plates)
    data = patch.make_figures(new, patch.plates_of(old))
    head = json.loads(data.split(b"\n", 1)[0])
    sent = [p for p in head["plates"] if p[2]]
    assert len(sent) == 2 and len(head["plates"]) == 4, "d and e, nothing else"
    out = patch.apply_figures(old, data, tmp_path / "rebuilt.fig.zst")
    assert out.read_bytes() == new.read_bytes()
    assert patch.make_figures(new, patch.plates_of(old)) == data, "deterministic"
    with pytest.raises(patch.PatchError, match="does not hold"):
        other = tmp_path / "other.fig.zst"
        figures.write_container(other, plates("z"))
        patch.apply_figures(other, data, tmp_path / "x.fig.zst")
    with pytest.raises(patch.PatchError, match="shorter"):
        patch.apply_figures(old, data[:-1], tmp_path / "y.fig.zst")
    assert not (tmp_path / "y.fig.zst").exists() and not (tmp_path / "y.fig.zst.tmp").exists()


def test_a_write_from_frames_is_the_container_write():
    """write_container is write_frames over its own frames: one layout."""
    import tempfile
    from pathlib import Path
    from app.hold import figures
    with tempfile.TemporaryDirectory() as t:
        a = figures.write_container(Path(t) / "a.fig.zst", plates("x", "y"))
        b = figures.write_frames(Path(t) / "b.fig.zst", patch._frames(a.path))
        assert b.path.read_bytes() == a.path.read_bytes() and b.sha256 == a.sha256


def test_a_peer_answers_a_plates_patch_pull(node, monkeypatch):  # noqa: F811
    from app.hold import figures
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    cont = d / f"figures/{SLICE}/00001.fig.zst"
    info = figures.write_container(cont, plates("a", "b"))
    snap2 = sn.Snapshot(collection_id=snap.collection_id, snapshot_id="2026-08", previous=None,
                        shards=snap.shards, delta=snap.delta, files={},
                        figures=(sn.FiguresShard(slice=SLICE, n=1, root=shard_mod.pieces_root_of(cont),
                                                 figures=2, bytes=info.stored),))
    (d / "snapshots" / "2026-08.json").write_bytes(snap2.canonical())
    monkeypatch.setattr(platform_config, "load", lambda: platform_config.PlatformConfig())
    old = node / "old.fig.zst"
    figures.write_container(old, plates("a"))
    body = messages.pull_body(key="k", what="patch", collection_id=codex.collection_id,
                              snapshot_id="2026-08", part=f"{SLICE}/00001",
                              have=patch.plates_of(old), kind="figures")
    assert body["part_kind"] == "figures"
    items = messages.answer_pull(node, body)["items"]
    assert [i["path"] for i in items] == [f"patch/figures/{SLICE}/00001.patch"]
    (_, data), = messages.verify_items(items)
    assert patch.apply_figures(old, data, node / "rebuilt.fig.zst").read_bytes() == cont.read_bytes()
    assert patch.figures_part_of(f"figures/{SLICE}/00001.fig.zst") == f"{SLICE}/00001"


# ---- the writer beside the part --------------------------------------------------------

def test_a_part_entry_names_its_writer_and_an_old_snapshot_keeps_its_bytes():
    e = dict(slice=SLICE, n=1, root="sha256:" + "0" * 64, records_root="r", first_id="a",
             last_id="b", records=1, chunks=1, chunks_root="c", tokens=1, bytes=1)
    assert "writer" not in sn.Shard(**e).to_dict(), "absent before 2026-10-12: old bytes unchanged"
    assert sn.Shard(**e, writer=pack_records.WRITER).to_dict()["writer"].startswith("pyarrow ")
    assert sn.Shard(**sn.Shard(**e, writer="pyarrow 1.0").to_dict()).writer == "pyarrow 1.0"


def test_a_part_written_by_another_zstd_is_not_asked_for(node, monkeypatch):  # noqa: F811
    """A rebuild under a writer other than the producer's may miss the root:
    the peer does not ask, it fetches the part; a part with no writer named,
    or this platform's, is asked for."""
    from app import identity
    from app.bundles import keys as node_keys
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    asked = []
    monkeypatch.setattr(node_keys, "load_signing_key", lambda: object())
    monkeypatch.setattr(identity, "own", lambda: "ed25519:" + "a" * 64)
    monkeypatch.setattr(patch, "ask", lambda *a, **k: asked.append(a[4]) or None)
    from app.share import fetch as fetch_mod
    monkeypatch.setattr(fetch_mod, "sources", lambda *a, **k: [])
    import sqlite3
    patcher = transport._patcher(node, c, d, sqlite3.connect(":memory:"), now=0.0)
    for sid, writer in (("2026-09", "pyarrow 0.0.1"), ("2026-10", pack_records.WRITER), ("2026-11", "")):
        shards = tuple(sn.Shard(**{**s.to_dict(), "writer": writer}) for s in snap.shards)
        s2 = sn.Snapshot(collection_id=snap.collection_id, snapshot_id=sid, previous=None,
                         shards=shards, delta=snap.delta, files={})
        (d / "snapshots" / f"{sid}.json").write_bytes(s2.canonical())
        f = type("F", (), {"path": snap.shards[0].path})()
        patcher(f, node / "out.jsonl.zst", sid)
    assert asked == [f"{SLICE}/00001", f"{SLICE}/00001"], "2026-09's writer is another zstd: not asked"

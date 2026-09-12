"""09 §9.5's figures containers, end to end.

One container per record shard, not one file per plate — §9.16 makes every
FILE of a member its own BitTorrent v2 torrent, and ~275,000 plates over
~1,600 shards is two orders of magnitude more swarm state under one file
each. Everything here is about that container: what it holds, how a reader
finds one plate in it, and what it refuses.
"""
import hashlib
import struct

import pytest

from app.hold import figures as fig


def _plate(seed: bytes, n: int = 4096) -> tuple[str, bytes]:
    data = (seed * ((n // len(seed)) + 1))[:n]
    return "sha256:" + hashlib.sha256(data).hexdigest(), data


def test_a_container_round_trips_and_is_ordered_by_hash(tmp_path):
    plates = dict(_plate(b"alpha") for _ in range(1))
    for word in (b"beta", b"gamma", b"delta"):
        h, d = _plate(word)
        plates[h] = d
    path = fig.path_for(tmp_path, "lang=en", 7)
    assert path.name == "00007.fig.zst", "named for the SHARD, never a figure"

    info = fig.write_container(path, plates)
    assert info.figures == 4
    assert info.bytes == sum(len(v) for v in plates.values())
    assert info.sha256.startswith("sha256:")

    index = fig.read_index(path)
    assert set(index) == {fig.bare(h) for h in plates}
    # entries ordered by hash (09 §9.5), so two writers agree byte for byte
    offsets = [index[h].offset for h in sorted(index)]
    assert offsets == sorted(offsets)
    # `length` is the size of the BYTES, not of the frame (09 §9.5)
    assert {h: index[fig.bare(h)].bytes for h in plates} == \
        {h: len(d) for h, d in plates.items()}

    for h, data in plates.items():
        assert fig.read_figure(path, h) == data
    assert [h for h, _ in fig.walk(path)] == sorted(index)


def test_a_plate_is_stored_once_however_often_it_appears(tmp_path):
    """`figures` on the record line lists APPEARANCES; the container stores
    each plate once, being addressed by hash. 39% of Gutenberg's
    illustrated books repeat a plate, one of them 703 times."""
    h, data = _plate(b"repeated")
    path = fig.path_for(tmp_path, "lang=en", 1)
    info = fig.write_container(path, {h: data})
    assert info.figures == 1
    assert len(fig.read_index(path)) == 1


def test_bytes_that_disagree_with_the_name_are_refused_before_landing(tmp_path):
    h, _ = _plate(b"honest")
    path = fig.path_for(tmp_path, "lang=en", 1)
    with pytest.raises(fig.FigureError, match="disagree"):
        fig.write_container(path, {h: b"different bytes entirely"})
    assert not path.exists(), "nothing lands when one plate is wrong"


def test_the_prefix_is_optional_and_a_non_hash_is_refused():
    assert fig.bare("sha256:" + "ab" * 32) == "ab" * 32
    assert fig.bare("ab" * 32) == "ab" * 32
    for bad in ("sha256:beef", "ZZ" * 32, "", "sha256:"):
        with pytest.raises(fig.FigureError):
            fig.bare(bad)


def test_a_truncated_or_foreign_file_is_not_read_as_a_container(tmp_path):
    h, data = _plate(b"whole")
    path = fig.path_for(tmp_path, "lang=en", 1)
    fig.write_container(path, {h: data})
    raw = path.read_bytes()

    (tmp_path / "short").write_bytes(raw[:4])
    with pytest.raises(fig.FigureError, match="too short"):
        fig.read_index(tmp_path / "short")

    (tmp_path / "foreign").write_bytes(raw[:-4] + b"XXXX")
    with pytest.raises(fig.FigureError, match="footer"):
        fig.read_index(tmp_path / "foreign")

    # a footer that survives but a frame length that points nowhere
    bad = raw[:-8] + struct.pack("<I", len(raw) + 1000) + fig.FOOTER
    (tmp_path / "long").write_bytes(bad)
    with pytest.raises(fig.FigureError, match="exceeds the file"):
        fig.read_index(tmp_path / "long")


def test_a_corrupted_plate_is_caught_by_its_own_hash(tmp_path):
    """The record line's hash is the whole of what a peer checks a plate
    against — so reading one verifies it, every time."""
    h, data = _plate(b"corruptible")
    path = fig.path_for(tmp_path, "lang=en", 1)
    fig.write_container(path, {h: data})
    entry = fig.read_index(path)[fig.bare(h)]

    raw = bytearray(path.read_bytes())
    # flip a byte deep inside the figure's own frame, past the zstd header
    raw[entry.offset + entry.frame_bytes - 1] ^= 0xFF
    path.write_bytes(bytes(raw))
    with pytest.raises(Exception):      # a bad frame or a bad hash; both fail
        fig.read_figure(path, h)


def test_held_reads_every_container_of_a_slice(tmp_path):
    a, da = _plate(b"one")
    b, db_ = _plate(b"two")
    fig.write_container(fig.path_for(tmp_path, "lang=en", 1), {a: da})
    fig.write_container(fig.path_for(tmp_path, "lang=en", 2), {b: db_})
    assert fig.held(tmp_path, "lang=en") == {fig.bare(a), fig.bare(b)}
    assert fig.held(tmp_path, "lang=fr") == set()
    assert [p.name for p in fig.containers(tmp_path, "lang=en")] == \
        ["00001.fig.zst", "00002.fig.zst"]


def test_an_empty_container_is_still_a_container(tmp_path):
    """A shard whose records all carry `figures: []` writes none — but a
    container that IS written and holds nothing must still parse, or the
    reader's error would be about the format rather than the content."""
    path = fig.path_for(tmp_path, "lang=en", 1)
    info = fig.write_container(path, {})
    assert info.figures == 0 and info.bytes == 0
    assert fig.read_index(path) == {}
    assert list(fig.walk(path)) == []


def test_verify_names_every_bad_plate_and_stops_at_none(tmp_path):
    """One damaged picture must not hide the two hundred sound ones: a
    check reports a container, not the first thing wrong with it."""
    plates = {}
    for word in (b"a1", b"b2", b"c3", b"d4"):
        h, d = _plate(word)
        plates[h] = d
    path = fig.path_for(tmp_path, "lang=en", 1)
    fig.write_container(path, plates)
    assert fig.verify(path) == (4, [])

    index = fig.read_index(path)
    raw = bytearray(path.read_bytes())
    broken = sorted(index)[:2]
    for h in broken:
        raw[index[h].offset + index[h].frame_bytes - 1] ^= 0xFF
    path.write_bytes(bytes(raw))

    read, failed = fig.verify(path)
    assert sorted(failed) == sorted(broken), "both named, neither stopped the pass"
    assert read + len(failed) >= 4

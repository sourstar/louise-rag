"""09 §9.5's figures containers — the plate bytes, beside the shards.

A figure is named in the record by `hash` and carried here; its CAPTION
stays in `sections` and stays indexed. So `content_hash` never covers a
figure, and a collection that gains plates keeps every text vector it had
(02 §2.3).

ONE CONTAINER PER RECORD SHARD, not one file per plate:
`figures/<slice>/<NNNNN>.fig.zst`, the same slice and number as the shard
whose records name them. 09 §9.16 makes every FILE of a member its own
BitTorrent v2 torrent, and the measured corpus holds ~275,000 plates over
~1,600 record shards — one file each is two orders of magnitude more swarm
state for the same bytes and the same integrity, since the per-figure
hashes on the record lines are what verify a plate either way. It also
keeps §9.1's "path segments from digests" rule away from figures entirely:
no directory is ever named after one.

ADDRESSED BY HASH INSIDE the container, stored once each, entries ordered
by hash. A plate used twice is two entries in the record and one here —
39% of Gutenberg's illustrated books repeat a plate, one of them 703 times
over 66 distinct images.

RESUMABLE AT CONTAINER GRANULARITY (invariant 2). A container is written
whole, to a `.tmp` and renamed, exactly like its shard: the two are
re-issued together because a plate arriving rewrites the record line and
so moves the shard's own root.

WRITTEN IN A SECOND PASS, not in the parse pool. `_record_of` runs in a
child that has the epub open and could hand the bytes over for free, but
it receives neither the pack directory nor the slice, and threading both
through the pool's contract to save one re-read of a file that is still in
the page cache is the wrong trade — the pool's contract is what makes a
failed child one skipped row instead of a failed build.

THE LAYOUT is the record shard's (09 §9.5: "its layout is that of the
record shard above"), with its own magic:

    <zstd frame per figure, in hash order> <zstd skippable frame: the index>

    payload = TAG (16 bytes) | count u32 | entries | frame_len u32 | FOOTER (4)
    entry   = hash (32 bytes) | offset u64 | length u64          (48 bytes)

`offset` is where that figure's own zstd frame starts, so one plate is one
pread and one decompress; `length` is the length OF ITS BYTES — the size
the origin ships, the same number the record line carries as `bytes`. The
COMPRESSED extent needs no field: frames are written in the index's order,
so one runs to the next frame's offset and the last to the start of the
index frame. Two numbers per entry, exactly as §9.5 says, and the reader
never has to trust a third that could disagree with the other two.

The entry table is NOT itself compressed: it is 32 bytes of digest per 48,
and a digest does not compress — measured or not, that one is arithmetic.

`frame_len` counts the WHOLE skippable frame, its 8-byte header included,
which is `app/pack/records.py`'s convention byte for byte. The reader
takes the last 8 bytes, seeks back `frame_len`, and checks the header it
lands on before trusting anything after it. One convention, two members:
a reader that can walk a shard's trailer can walk this one.

ZSTD LEVEL 1, where the record shard uses 9. Measured on 506 real plates
(42.2 MB) from this corpus, 2026-09-12: level 1 leaves 99.507% of the
bytes at 1,190 MB/s, level 9 leaves 99.334% at 487 MB/s, level 19 leaves
98.801% at 24 MB/s. A JPEG is already compressed; the frame is here for
the structure — per-figure addressing that standard zstd tooling can
still walk — and not for the ratio. Over the projected 51 GB member that
is 43 seconds against 105 for 0.17 percentage points.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping

import pyarrow as pa

FIGURES_DIR = "figures"
SUFFIX = ".fig.zst"
TAG = b"LOUISE-FIGURES-1"          # 16 bytes, like LOUISE-PACK-IDX1
FOOTER = b"LFG1"
SKIPPABLE_MAGIC = 0x184D2A5E
ZSTD_LEVEL = 1
ENTRY = struct.Struct("<32sQQ")    # hash, offset, length — 48 bytes


class FigureError(ValueError):
    """A plate that cannot be carried as the record says it is."""


def _codec():
    return pa.Codec("zstd", compression_level=ZSTD_LEVEL)


def container_dir(pack_dir: Path, slice_name: str) -> Path:
    return Path(pack_dir) / FIGURES_DIR / slice_name


def container_name(shard_n: int) -> str:
    """`<NNNNN>.fig.zst` — the shard's own number, zero-padded to five as
    every other member of a slice is."""
    return f"{shard_n:05d}{SUFFIX}"


def path_for(pack_dir: Path, slice_name: str, shard_n: int) -> Path:
    return container_dir(pack_dir, slice_name) / container_name(shard_n)


def bare(sha256: str) -> str:
    """The hex, with or without 09 §9.2's `sha256:` prefix. A record line
    carries the prefixed form and a 32-byte index entry cannot, so one
    function knows about the difference instead of every call site."""
    h = sha256[len("sha256:"):] if sha256.startswith("sha256:") else sha256
    if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
        raise FigureError(f"a figure is addressed by its sha256, got {sha256!r}")
    return h


@dataclass(frozen=True)
class Entry:
    """Where one plate is, and how big it is both ways. `bytes` is what the
    index carries — the size the origin ships — and `frame_bytes` is the
    compressed extent the layout implies."""
    offset: int
    frame_bytes: int
    bytes: int


@dataclass(frozen=True)
class ContainerInfo:
    path: Path
    figures: int          # distinct plates stored
    bytes: int            # their size as the origin ships them
    stored: int           # the container's own size on disk
    sha256: str           # over the container file, for the snapshot


def write_container(path: Path, plates: Mapping[str, bytes]) -> ContainerInfo:
    """Write one container holding `plates` — {hash: bytes}, hash in either
    form. Atomically (tmp + rename), entries ordered by hash.

    Every plate is VERIFIED against the name it is stored under before
    anything lands. This is the one place the two can disagree, and a
    container whose entry does not hash to its own key would fail every
    check downstream with nothing pointing back here.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    codec = _codec()

    ordered: list[tuple[str, bytes]] = []
    for key, data in plates.items():
        want = bare(key)
        actual = hashlib.sha256(data).hexdigest()
        if actual != want:
            raise FigureError(
                f"figure bytes hash to {actual[:12]}… but the record names "
                f"{want[:12]}… — the plate and the record line disagree")
        ordered.append((want, data))
    ordered.sort(key=lambda kv: kv[0])

    tmp = path.with_name(path.name + ".tmp")
    sha = hashlib.sha256()
    raw_total = 0
    entries = bytearray()
    offset = 0
    with open(tmp, "wb") as f:
        for h, data in ordered:
            frame = codec.compress(data, asbytes=True)
            f.write(frame)
            sha.update(frame)
            entries += ENTRY.pack(bytes.fromhex(h), offset, len(data))
            offset += len(frame)
            raw_total += len(data)
        body = TAG + struct.pack("<I", len(ordered)) + bytes(entries)
        # frame_len = the whole skippable frame: 8-byte header + payload,
        # where payload = body + this u32 + FOOTER (pack/records.py:162)
        payload = body + struct.pack("<I", len(body) + 16) + FOOTER
        frame = struct.pack("<II", SKIPPABLE_MAGIC, len(payload)) + payload
        f.write(frame)
        sha.update(frame)
        stored = offset + len(frame)
    tmp.replace(path)          # atomic: a reader never sees a half container
    return ContainerInfo(path=path, figures=len(ordered), bytes=raw_total,
                         stored=stored, sha256="sha256:" + sha.hexdigest())


def read_index(path: Path) -> dict[str, Entry]:
    """{hash hex: Entry} — what the container holds and where."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size < 16:
            raise FigureError(f"{path.name}: too short to be a figures container")
        f.seek(size - 8)
        frame_len, footer = struct.unpack("<I4s", f.read(8))
        if footer != FOOTER:
            raise FigureError(f"{path.name}: not a figures container "
                              f"(footer {footer!r}, want {FOOTER!r})")
        start = size - frame_len
        if start < 0:
            raise FigureError(f"{path.name}: index frame length exceeds the file")
        f.seek(start)
        magic, declared = struct.unpack("<II", f.read(8))
        if magic != SKIPPABLE_MAGIC or declared != frame_len - 8:
            raise FigureError(f"{path.name}: index frame is not a zstd "
                              f"skippable frame")
        payload = f.read(declared)
    if payload[:16] != TAG:
        raise FigureError(f"{path.name}: not a figures container "
                          f"(magic {payload[:16]!r}, want {TAG!r})")
    (count,) = struct.unpack("<I", payload[16:20])
    table = payload[20:len(payload) - 8]
    if len(table) != count * ENTRY.size:
        raise FigureError(f"{path.name}: index declares {count} figures and "
                          f"carries {len(table) / ENTRY.size:.2f}")
    raw_entries = [ENTRY.unpack_from(table, i * ENTRY.size) for i in range(count)]
    # THE COMPRESSED EXTENT, from the layout and not from a third field:
    # frames sit back to back in this order, so one runs to the next
    # frame's offset and the last to the start of the index frame.
    out: dict[str, Entry] = {}
    for i, (raw, offset, length) in enumerate(raw_entries):
        end = raw_entries[i + 1][1] if i + 1 < count else start
        if not (0 <= offset < end <= start):
            raise FigureError(f"{path.name}: figure {raw.hex()[:12]}… is at "
                              f"{offset}..{end}, outside the container's data")
        out[raw.hex()] = Entry(offset=offset, frame_bytes=end - offset,
                               bytes=length)
    if len(out) != count:
        raise FigureError(f"{path.name}: index carries {count} entries over "
                          f"{len(out)} distinct hashes — figures are stored "
                          f"once each (09 §9.5)")
    return out


def read_figure(path: Path, sha256: str) -> bytes:
    """One plate's bytes, by hash. Raises FigureError when the container
    does not hold it, or when what it holds does not hash to its key —
    the check `hold check`'s `figures` row runs over every entry."""
    want = bare(sha256)
    entry = read_index(path).get(want)
    if entry is None:
        raise FigureError(f"{Path(path).name}: holds no figure {want[:12]}…")
    with open(path, "rb") as f:
        f.seek(entry.offset)
        frame = f.read(entry.frame_bytes)
    data = bytes(_codec().decompress(frame, decompressed_size=entry.bytes))
    actual = hashlib.sha256(data).hexdigest()
    if actual != want:
        raise FigureError(
            f"{Path(path).name}: figure {want[:12]}… hashes to {actual[:12]}…")
    return data


def walk(path: Path) -> Iterator[tuple[str, bytes]]:
    """Every plate in the container, in hash order. One open, one pass —
    the shape a check wants, where `read_figure` is the shape a reader
    serving one plate wants."""
    index = sorted(read_index(path).items())
    codec = _codec()
    with open(path, "rb") as f:
        for h, entry in index:
            f.seek(entry.offset)
            frame = f.read(entry.frame_bytes)
            try:
                data = bytes(codec.decompress(frame, decompressed_size=entry.bytes))
            except Exception as e:                       # noqa: BLE001
                # zstd's own errors say nothing about WHICH plate: the
                # module that knows the layout is the one that can name it,
                # and a caller checking a container must not have to catch
                # a codec exception to find out one picture is damaged.
                raise FigureError(
                    f"{Path(path).name}: figure {h[:12]}… does not "
                    f"decompress ({type(e).__name__}: {e})") from e
            yield h, data


def verify(path: Path) -> tuple[int, list[str]]:
    """(plates read, the hashes that failed) — one pass over a container.

    THE CHECK'S SHAPE, and it lives here because this module owns what a
    broken container looks like. A plate that will not decompress and a
    plate that decompresses to the wrong bytes are the same finding to
    whoever receives it, and neither may stop the pass: a container with
    one damaged picture must still report the other two hundred as sound.
    """
    index = sorted(read_index(path).items())
    codec = _codec()
    read, failed = 0, []
    with open(path, "rb") as f:
        for h, entry in index:
            f.seek(entry.offset)
            frame = f.read(entry.frame_bytes)
            try:
                data = bytes(codec.decompress(frame, decompressed_size=entry.bytes))
            except Exception:                                # noqa: BLE001
                failed.append(h)
                continue
            read += 1
            if hashlib.sha256(data).hexdigest() != h:
                failed.append(h)
    return read, failed


def held(pack_dir: Path, slice_name: str) -> set[str]:
    """Every hash the slice's containers hold. Used to answer "do we have
    this plate already" without re-reading a book."""
    d = container_dir(pack_dir, slice_name)
    if not d.is_dir():
        return set()
    out: set[str] = set()
    for p in sorted(d.iterdir()):
        if p.is_file() and p.name.endswith(SUFFIX):
            out |= set(read_index(p))
    return out


def containers(pack_dir: Path, slice_name: str) -> list[Path]:
    d = container_dir(pack_dir, slice_name)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_file() and p.name.endswith(SUFFIX))

"""A zip's delta by its central directory (docs/notes/normattiva-multivigenza-review.md
§3.10, operator's decision 5 of 2026-10-12; 09 §9.17 `delta.mode:
"zip-directory"`).

An origin that rebuilds a zip in place every day — Normattiva: 7.3 GB of
multivigente archives, nearly every member unchanged — is fetched whole by
`full-resync` whenever anything moved, and not at all when nothing did.
Where the origin serves Range, the zip's own index says what changed: its
central directory, the last few MB of the file, lists every member with
its CRC-32 and sizes. So a pass reads the directory, compares it with the
copy on disk, fetches each changed or new member by ONE Range request, and
REBUILDS the local copy: the members it already holds are copied as their
raw compressed bytes, the fetched ones written beside them, every member
under a fresh local header at its new offset, and the directory written by
`zipfile`. The result is a valid zip whose members decompress to the
origin's bytes — which is what `origin_hash` covers (06 §6.4) — and not a
byte-identical copy of the origin's file, which no checksum declares.

Every byte read from the origin passes through one object, a `RangeReader`
— `total()` and `read(start, end)` — so the engine wires the HTTPS client,
the budget and the etiquette in (`HttpRangeReader`), and the tests wire a
file (`FileRangeReader`). A fetched member is verified against the
directory's CRC-32 and size before it is kept; a member that does not
check out, an origin that will not serve Range, or a delta that would move
more than half the file hand the pass back to the whole fetch, which is
what it did before.
"""
from __future__ import annotations

import os
import struct
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .spec import TransportError

#: how much of the file's tail the first directory read takes; doubled while
#: the directory is not inside it, up to the limit
DIRECTORY_TAIL = 4 << 20
DIRECTORY_LIMIT = 512 << 20
#: a delta that would fetch more than this share of the file is not a delta:
#: the whole fetch is cheaper and is what the caller falls back to
WHOLE_IS_CHEAPER = 0.5
_LOCAL_HEADER = 0x04034B50
_ZIP64_LIMIT = (1 << 31) - 1
_HEADER_SLACK = 1024          # the local header's extra field may be longer than the directory's


class RangeUnsupported(TransportError):
    """The origin answered a Range request with the whole file."""


class MemberMismatch(TransportError):
    """A fetched member's bytes do not check out against the directory."""


class DeltaNotWorthIt(TransportError):
    """More than `WHOLE_IS_CHEAPER` of the file changed: fetch it whole."""


class RangeReader(Protocol):
    def total(self) -> int: ...
    def read(self, start: int, end: int) -> bytes: ...     # inclusive, as HTTP Range


class FileRangeReader:
    """A local file as a range source: the tests', and the shape any reader
    has."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.reads: list[tuple[int, int]] = []

    def total(self) -> int:
        return self.path.stat().st_size

    def read(self, start: int, end: int) -> bytes:
        self.reads.append((start, end))
        with open(self.path, "rb") as f:
            f.seek(start)
            return f.read(end - start + 1)


class HttpRangeReader:
    """The origin over HTTPS: a one-byte request for the size (this origin's
    HEAD names no length), 206 answers for the ranges, every byte drawn from
    the budget AFTER it arrived (invariant 5)."""

    #: A transient fault — a read timeout, a dropped connection — is asked
    #: again this many times, a second apart and doubling, before it is the
    #: transport's error. 2026-10-12: one ReadTimeout on one Range request
    #: ended Normattiva's whole 7.8 GB fetch and waited four hours for the
    #: next discovery (STATUS); a fault that passes in seconds costs seconds.
    #: Five attempts: 1 + 2 + 4 + 8 = 15 s of waiting, which outlasts the
    #: eight-second episode of 500s the origin had on 2026-10-13.
    ATTEMPTS = 5

    def __init__(self, url: str, headers: dict, *, charge: Callable[[int], None] | None = None,
                 timeout: float = 60.0, sleep: Callable[[float], None] | None = None) -> None:
        self.url, self.headers, self.timeout = url, dict(headers), timeout
        if charge is None:
            from . import budget
            charge = budget.acquire_bytes
        self.charge = charge
        self.sleep = sleep if sleep is not None else _sleep
        self._total: int | None = None

    def _get(self, range_header: str):
        """One Range request, answered or the transport's error (`TransportError`,
        never httpx's own class: the engine's per-file boundary catches ours),
        a transient fault — a timeout, a drop, the origin's own 5xx — retried
        `ATTEMPTS` times."""
        import httpx
        last: str | None = None
        cause: BaseException | None = None
        for attempt in range(self.ATTEMPTS):
            if attempt:
                self.sleep(float(1 << (attempt - 1)))
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    r = client.get(self.url, headers={**self.headers, "Range": range_header})
            except httpx.TransportError as e:          # timeouts, drops, resets
                last, cause = f"{type(e).__name__}: {e}", e
                continue
            except httpx.HTTPError as e:
                raise TransportError(f"{self.url}: {e}") from e
            if r.status_code >= 500:                   # the origin's episode, not the file
                last, cause = f"{r.status_code} to Range {range_header}", None
                continue
            return r
        raise TransportError(f"{self.url}: {last} after {self.ATTEMPTS} attempts") from cause

    def total(self) -> int:
        if self._total is None:
            r = self._get("bytes=0-0")
            if r.status_code == 200:
                raise RangeUnsupported(f"{self.url}: the origin answered a one-byte Range with the whole file")
            if r.status_code != 206:
                raise TransportError(f"{self.url}: {r.status_code} to a Range request")
            cr = r.headers.get("content-range", "")
            try:
                self._total = int(cr.rsplit("/", 1)[1])
            except (IndexError, ValueError):
                raise TransportError(f"{self.url}: no total in Content-Range {cr!r}") from None
            self.charge(len(r.content))
        return self._total

    def read(self, start: int, end: int) -> bytes:
        r = self._get(f"bytes={start}-{end}")
        if r.status_code == 200:
            raise RangeUnsupported(f"{self.url}: the origin ignored Range and answered 200")
        if r.status_code != 206:
            raise TransportError(f"{self.url}: {r.status_code} to Range {start}-{end}")
        self.charge(len(r.content))
        return r.content


def _sleep(seconds: float) -> None:
    import time
    time.sleep(seconds)


# ---- the directory ---------------------------------------------------------------

def directory(reader: RangeReader, *, tail: int | None = None,
              limit: int | None = None) -> tuple[list[zipfile.ZipInfo], int, int]:
    """`(the remote zip's entries, its size, the bytes read)`. The tail is
    written into a sparse file of the full size so `zipfile` reads the
    directory where it expects it; a directory longer than the tail is read
    again with a longer one."""
    tail = DIRECTORY_TAIL if tail is None else tail        # read at call time: a test sizes it
    limit = DIRECTORY_LIMIT if limit is None else limit
    total = reader.total()
    want = min(tail, total)
    read = 0
    while True:
        start = total - want
        data = reader.read(start, total - 1)
        read += len(data)
        fd, path = tempfile.mkstemp(prefix="louise-zipdir-", suffix=".zip")
        try:
            with os.fdopen(fd, "r+b") as f:
                f.truncate(total)
                f.seek(start)
                f.write(data)
            try:
                with zipfile.ZipFile(path) as z:
                    return z.infolist(), total, read
            except (zipfile.BadZipFile, EOFError, OSError, struct.error):
                if want >= total or want >= limit:
                    raise
                want = min(total, want * 4)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


# ---- the plan -----------------------------------------------------------------------

@dataclass
class Plan:
    order: list[zipfile.ZipInfo]                       # the remote's entries, in its order
    keep: dict[str, zipfile.ZipInfo] = field(default_factory=dict)   # name -> the LOCAL entry kept
    fetch: list[zipfile.ZipInfo] = field(default_factory=list)       # remote entries to fetch
    dropped: list[str] = field(default_factory=list)                 # local names the origin no longer lists

    @property
    def fetch_bytes(self) -> int:
        return sum(i.compress_size for i in self.fetch)


def plan(local: Path | None, remote: list[zipfile.ZipInfo]) -> Plan:
    """Keep a member the local copy holds with the same CRC-32 and size;
    fetch every other; a directory entry costs nothing either way."""
    have: dict[str, zipfile.ZipInfo] = {}
    if local is not None and Path(local).is_file():
        try:
            with zipfile.ZipFile(local) as z:
                have = {i.filename: i for i in z.infolist()}
        except zipfile.BadZipFile:
            have = {}
    out = Plan(order=list(remote))
    for info in remote:
        mine = have.get(info.filename)
        if info.is_dir():
            out.keep[info.filename] = info        # a directory entry: written empty, nothing to move
        elif mine is not None and mine.CRC == info.CRC and mine.file_size == info.file_size:
            out.keep[info.filename] = mine
        else:
            out.fetch.append(info)
    names = {i.filename for i in remote}
    out.dropped = sorted(n for n in have if n not in names)
    return out


# ---- the members ------------------------------------------------------------------

def _header_span(blob: bytes) -> int:
    if len(blob) < 30 or struct.unpack("<I", blob[:4])[0] != _LOCAL_HEADER:
        raise MemberMismatch("no local file header where the directory pointed")
    nlen, xlen = struct.unpack("<HH", blob[26:30])
    return 30 + nlen + xlen


def verify(info: zipfile.ZipInfo, data: bytes) -> None:
    """The member's raw bytes against the directory's CRC-32 and size."""
    if info.compress_type == zipfile.ZIP_DEFLATED:
        try:
            d = zlib.decompressobj(-15)
            raw = d.decompress(data) + d.flush()
        except zlib.error as e:
            raise MemberMismatch(f"{info.filename}: the fetched bytes do not inflate: {e}") from None
    elif info.compress_type == zipfile.ZIP_STORED:
        raw = data
    else:
        raise MemberMismatch(f"{info.filename}: compression method {info.compress_type} is not handled")
    if len(raw) != info.file_size or (zlib.crc32(raw) & 0xFFFFFFFF) != info.CRC:
        raise MemberMismatch(f"{info.filename}: the fetched bytes do not match the directory's CRC and size")


def fetch_member(reader: RangeReader, info: zipfile.ZipInfo, total: int) -> tuple[bytes, int]:
    """`(the member's raw compressed bytes, bytes read)` — one request, a
    second only where the local header's extra field outran the slack."""
    if info.compress_size == 0 and info.file_size == 0:
        return b"", 0
    start = info.header_offset
    guess = 30 + len(info.filename.encode("utf-8")) + len(info.extra) + _HEADER_SLACK
    end = min(total - 1, start + guess + info.compress_size - 1)
    blob = reader.read(start, end)
    read = len(blob)
    span = _header_span(blob)
    data = blob[span:span + info.compress_size]
    if len(data) < info.compress_size:
        more = reader.read(start + span + len(data), start + span + info.compress_size - 1)
        read += len(more)
        data += more
    if len(data) != info.compress_size:
        raise MemberMismatch(f"{info.filename}: {len(data)} bytes where the directory says {info.compress_size}")
    verify(info, data)
    return data, read


def raw_member(f, info: zipfile.ZipInfo) -> bytes:
    """A member's raw compressed bytes out of an open local zip."""
    f.seek(info.header_offset)
    span = _header_span(f.read(30))
    f.seek(info.header_offset + span)
    data = f.read(info.compress_size)
    if len(data) != info.compress_size:
        raise MemberMismatch(f"{info.filename}: the local copy is short")
    return data


# ---- the rebuild -----------------------------------------------------------------

def _append_raw(zo: zipfile.ZipFile, info: zipfile.ZipInfo, src: zipfile.ZipInfo, data: bytes) -> None:
    """One member under a fresh local header at the current offset: the
    bytes as compressed by whoever compressed them, the sizes and CRC the
    directory states, no data descriptor (the sizes are known). `zipfile`
    writes the directory and the zip64 records from `filelist` on close."""
    ni = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    ni.compress_type = src.compress_type
    ni.CRC = src.CRC
    ni.compress_size = len(data)
    ni.file_size = src.file_size
    ni.flag_bits = src.flag_bits & ~0x08
    ni.external_attr = info.external_attr
    ni.create_system = info.create_system
    ni.comment = info.comment
    zip64 = ni.file_size > _ZIP64_LIMIT or ni.compress_size > _ZIP64_LIMIT
    ni.header_offset = zo.fp.tell()
    zo.fp.write(ni.FileHeader(zip64=zip64))
    zo.fp.write(data)
    zo.filelist.append(ni)
    zo.NameToInfo[ni.filename] = ni
    zo._didModify = True
    zo.start_dir = zo.fp.tell()


def _append_dir(zo: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
    """A directory entry, as `zipfile` writes one: stored, empty."""
    ni = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    ni.compress_type = zipfile.ZIP_STORED
    ni.CRC = 0
    ni.compress_size = 0
    ni.file_size = 0
    ni.flag_bits = 0
    ni.external_attr = info.external_attr or (0o40775 << 16) | 0x10
    ni.create_system = info.create_system
    ni.header_offset = zo.fp.tell()
    zo.fp.write(ni.FileHeader(zip64=False))
    zo.filelist.append(ni)
    zo.NameToInfo[ni.filename] = ni
    zo._didModify = True
    zo.start_dir = zo.fp.tell()


def rebuild(local: Path | None, p: Plan, fetched: dict[str, bytes], out: Path) -> int:
    """The new local zip: every remote entry in the remote's order, kept
    members copied raw from the old copy, fetched ones written as received.
    Returns the member count."""
    src = open(local, "rb") if local is not None and Path(local).is_file() else None
    try:
        with zipfile.ZipFile(out, "w", allowZip64=True) as zo:
            for info in p.order:
                if info.is_dir():
                    _append_dir(zo, info)
                elif info.filename in fetched:
                    _append_raw(zo, info, info, fetched[info.filename])
                elif info.filename in p.keep:
                    if src is None:
                        raise MemberMismatch(f"{info.filename}: kept, but there is no local copy to keep it from")
                    mine = p.keep[info.filename]
                    _append_raw(zo, info, mine, raw_member(src, mine))
                else:
                    raise MemberMismatch(f"{info.filename}: neither kept nor fetched")
    finally:
        if src is not None:
            src.close()
    return len(p.order)


@dataclass(frozen=True)
class Outcome:
    members: int
    fetched: int
    kept: int
    dropped: int
    bytes_read: int        # what crossed the network, directory included
    total: int             # the remote file's size


def update(reader: RangeReader, local: Path | None, out: Path, *, stop=None,
           tail: int | None = None, whole_is_cheaper: float = WHOLE_IS_CHEAPER) -> Outcome:
    """The whole delta: directory, plan, members, rebuild into `out`. Raises
    `DeltaNotWorthIt` before any member is fetched when the plan would move
    more than `whole_is_cheaper` of the file, `RangeUnsupported` when the
    origin will not serve ranges, `MemberMismatch` when a member does not
    check out — each of them the caller's cue to fetch the file whole."""
    from .spec import TransferAborted
    infos, total, read = directory(reader, tail=tail)
    p = plan(local, infos)
    if total and p.fetch_bytes > total * whole_is_cheaper:
        raise DeltaNotWorthIt(f"{p.fetch_bytes} of {total} bytes changed: the whole file is cheaper")
    fetched: dict[str, bytes] = {}
    for info in p.fetch:
        if stop is not None and stop.is_set():
            raise TransferAborted("stopped during a zip delta; nothing was replaced")
        data, n = fetch_member(reader, info, total)
        read += n
        fetched[info.filename] = data
    n = rebuild(local, p, fetched, out)
    return Outcome(members=n, fetched=len(p.fetch), kept=len(p.keep), dropped=len(p.dropped),
                   bytes_read=read, total=total)

"""HTTPS transport — httpx streaming GET with Range resume.

Plain HTTPS has no generic directory-listing protocol, so list_files() just
returns endpoint.static_files (declared by the per-source spec) rather than
crawling anything.
"""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from typing import Iterator

import httpx

from . import budget
from .spec import (AcquisitionSpec, Endpoint, RemoteFile, TransferAborted,
                    TransportError)

# httpx's iter_bytes(chunk_size) only yields once it has accumulated a FULL
# chunk; if the connection drops after fewer bytes than one chunk, whatever
# was buffered is discarded when the read error propagates (confirmed
# directly: a 1 MiB chunk size lost an entire 4096-byte partial transfer).
# 64 KiB bounds how much progress a single interruption can ever cost while
# still being efficient for large files — the same granularity trade-off any
# resumable downloader (wget --continue, aria2, ...) makes.
_CHUNK_SIZE = 1 << 16

# Wikimedia's User-Agent policy (meta.wikimedia.org/wiki/Policy:User-Agent_policy)
# returns 403 Forbidden to generic library UAs like httpx's default — observed
# live against dumps.wikimedia.org. The policy asks for a descriptive product
# string with a contact URL and the underlying library; other mirrors simply
# ignore the header, so it is sent unconditionally.
_USER_AGENT = (f"louise-rag/0.1 (+https://github.com/sourstar/louise-rag) "
               f"httpx/{httpx.__version__}")


class HttpsTransport:
    # Declares that this transport can split ONE file across several
    # connections; the engine checks for it rather than knowing which
    # transports are which (rsync meters by lease, hf_hub goes through the
    # hub client, ftp has no Range guarantee).
    supports_segments = True

    def list_files(self, endpoint: Endpoint, spec: AcquisitionSpec) -> Iterator[RemoteFile]:
        return iter(endpoint.static_files)

    def fetch_segments(self, endpoint: Endpoint, remote: RemoteFile, dest: Path,
                        segments: int) -> int:
        return _fetch_segmented(self, endpoint, remote, dest, segments)

    def fetch_one(self, endpoint: Endpoint, remote: RemoteFile, dest: Path,
                  resume_from: int) -> int:
        part = dest.with_name(dest.name + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        url = f"{endpoint.url.rstrip('/')}/{remote.rel_path}"
        headers = {"User-Agent": _USER_AGENT}
        mode = "wb"
        if resume_from > 0 and part.is_file():
            headers["Range"] = f"bytes={resume_from}-"
            mode = "ab"
        else:
            resume_from = 0
        if endpoint.auth_token_path is not None:
            token = endpoint.auth_token_path.read_text().strip()
            headers["Authorization"] = f"Bearer {token}"
        try:
            # follow_redirects: Hugging Face /resolve/ URLs 302 to their CDN
            # (observed live: every wikivec shard failed with "Redirect
            # response '302 Found'" — dumps.wikimedia/UCSC/NCBI serve bytes
            # directly, so this never surfaced before). httpx forwards Range
            # to the redirect target and drops Authorization on cross-origin
            # hops, which is exactly the right behavior for both headers.
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                with client.stream("GET", url, headers=headers) as r:
                    if resume_from > 0 and r.status_code == 200:
                        # server ignored Range -> must restart from scratch
                        mode = "wb"
                    elif r.status_code not in (200, 206):
                        r.raise_for_status()
                    with open(part, mode) as f:
                        for chunk in r.iter_bytes(_CHUNK_SIZE):
                            f.write(chunk)
                            f.flush()
                            os.fsync(f.fileno())
                            budget.acquire_bytes(len(chunk))
        except httpx.HTTPError as e:
            raise TransportError(f"{url}: {e}") from e
        size = part.stat().st_size
        os.replace(part, dest)
        return size


# ---------------- multi-connection single-file downloads ----------------
#
# architecture-v2.md §5.2's third parallelism axis, and the one it calls
# "default-off against origins": several Range requests for disjoint slices
# of ONE file. It buys nothing on a link the origin can already saturate, so
# platform.toml keeps segments_per_file = 1 unless an operator opts in.
#
# The slices are written straight into their final offsets in a single
# preallocated .part file, not into per-segment files that are concatenated
# afterwards. Concatenation would need a second full-size copy on disk (110 GB
# transient for a 55 GB dump) and a second pass over every byte; writing in
# place needs neither.
#
# Resume state therefore cannot be inferred from file sizes the way the
# single-stream path infers it — the .part file is full-size from the start —
# so per-segment progress lives in a small sidecar written beside it. The
# sidecar is updated AFTER the bytes it describes are fsynced, so a crash can
# only ever leave it LAGGING: the next run re-downloads a little and rewrites
# identical bytes at identical offsets. Lagging is idempotent; leading would
# be corruption.

_SEGMENT_SIDECAR_SUFFIX = ".segments.json"
_PROGRESS_FLUSH_BYTES = 8 << 20     # persist the sidecar at most every 8 MiB


class RangeUnsupported(TransportError):
    """The origin ignored a Range request (answered 200, not 206), or
    disagrees about the file's total size. Segmented download is impossible;
    the caller falls back to a single stream."""


def segment_ranges(total: int, segments: int) -> list[tuple[int, int]]:
    """`segments` contiguous, disjoint, inclusive byte ranges covering
    [0, total). The last one absorbs the remainder, so the ranges always
    tile the file exactly whatever the division leaves over."""
    if total <= 0 or segments <= 1:
        return [(0, max(0, total - 1))]
    segments = min(segments, total)          # never more segments than bytes
    step = total // segments
    out = []
    for i in range(segments):
        start = i * step
        end = (total - 1) if i == segments - 1 else (start + step - 1)
        out.append((start, end))
    return out


def _sidecar_path(dest: Path) -> Path:
    return dest.with_name(dest.name + _SEGMENT_SIDECAR_SUFFIX)


def _load_progress(path: Path, ranges: list[tuple[int, int]]) -> list[int]:
    """Bytes already written per segment, or zeros. Any disagreement with the
    current range layout (a changed segment count, a changed file size, a
    truncated sidecar) discards the whole thing: re-downloading is cheap and
    always correct, while trusting a mismatched offset table is neither."""
    zeros = [0] * len(ranges)
    try:
        raw = json.loads(path.read_text())
        if raw.get("ranges") != [list(r) for r in ranges]:
            return zeros
        done = raw["done"]
        if len(done) != len(ranges):
            return zeros
        return [min(max(0, int(d)), r[1] - r[0] + 1) for d, r in zip(done, ranges)]
    except (OSError, ValueError, TypeError, KeyError):
        return zeros


def _save_progress(path: Path, ranges: list[tuple[int, int]], done: list[int]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"ranges": [list(r) for r in ranges],
                                "done": list(done)}))
    os.replace(tmp, path)     # atomic: a torn sidecar reads as "start over"


def _fetch_range(url: str, headers: dict, part: Path, start: int, end: int,
                  already: int, on_bytes) -> int:
    """One segment, written at its absolute offset. Returns bytes written now.

    Its own file handle, so the seek position is never shared between threads.
    """
    first = start + already
    if first > end:
        return 0
    seg_headers = dict(headers)
    seg_headers["Range"] = f"bytes={first}-{end}"
    written = 0
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        with client.stream("GET", url, headers=seg_headers) as r:
            if r.status_code == 200:
                raise RangeUnsupported(
                    f"{url}: origin ignored Range and answered 200 — it is "
                    f"serving the whole file for a partial request")
            if r.status_code != 206:
                r.raise_for_status()
                raise RangeUnsupported(f"{url}: unexpected status {r.status_code}")
            with open(part, "r+b") as f:
                f.seek(first)
                for chunk in r.iter_bytes(_CHUNK_SIZE):
                    f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
                    written += len(chunk)
                    # charged AFTER the bytes are durable, the same ordering
                    # (and for the same reason) as the single-stream path
                    budget.acquire_bytes(len(chunk))
                    on_bytes(len(chunk))
    return written


def _fetch_segmented(transport, endpoint: Endpoint, remote: RemoteFile,
                      dest: Path, segments: int) -> int:
    """Download one file over `segments` concurrent Range requests.

    Raises RangeUnsupported when the origin will not serve ranges, so the
    caller can fall back; TransferAborted propagates unchanged, with every
    segment's durable progress recorded in the sidecar first.
    """
    total = remote.size or 0
    if total <= 0:
        raise RangeUnsupported("no declared remote size: cannot split a file "
                                "of unknown length into ranges")
    part = dest.with_name(dest.name + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    ranges = segment_ranges(total, segments)
    sidecar = _sidecar_path(dest)
    done = _load_progress(sidecar, ranges)

    # Preallocate so every segment can write at its own absolute offset from
    # the first byte. Never shrink an existing .part: it holds the progress
    # the sidecar is describing.
    with open(part, "r+b" if part.exists() else "wb") as f:
        if part.stat().st_size < total:
            f.truncate(total)

    url = f"{endpoint.url.rstrip('/')}/{remote.rel_path}"
    headers = {"User-Agent": _USER_AGENT}
    if endpoint.auth_token_path is not None:
        headers["Authorization"] = f"Bearer {endpoint.auth_token_path.read_text().strip()}"

    lock = threading.Lock()
    since_flush = [0]

    def _progress(index):
        def _on_bytes(n):
            with lock:
                done[index] += n
                since_flush[0] += n
                if since_flush[0] >= _PROGRESS_FLUSH_BYTES:
                    since_flush[0] = 0
                    _save_progress(sidecar, ranges, done)
        return _on_bytes

    errors: list[Exception] = []
    try:
        with ThreadPoolExecutor(max_workers=len(ranges)) as ex:
            futures = []
            for i, (start, end) in enumerate(ranges):
                # ContextVars are NOT inherited by threads, and the transfer
                # handle (priority + stop event) lives in one. Without an
                # explicit copy per thread every segment would meter itself as
                # an unbound BACKFILL caller and ignore SIGTERM — bytes still
                # drawn from the bucket, but at the wrong class and with a
                # shutdown that hangs until the socket drains.
                ctx = copy_context()
                futures.append(ex.submit(ctx.run, _fetch_range, url, headers,
                                          part, start, end, done[i], _progress(i)))
            for future in futures:
                try:
                    future.result()
                except Exception as e:      # noqa: BLE001 — collected, re-raised below
                    errors.append(e)
    finally:
        _save_progress(sidecar, ranges, done)

    if errors:
        # RangeUnsupported first: it is the one the caller can act on by
        # retrying the whole file as a single stream.
        for e in errors:
            if isinstance(e, RangeUnsupported):
                raise e
        for e in errors:
            if isinstance(e, TransferAborted):
                raise e
        raise TransportError(f"{url}: {errors[0]}") from errors[0]

    expected = [end - start + 1 for start, end in ranges]
    if done != expected:
        raise TransportError(
            f"{url}: segmented download ended short "
            f"({sum(done)}/{total} bytes) — not recording a partial file")
    os.replace(part, dest)
    sidecar.unlink(missing_ok=True)
    return total

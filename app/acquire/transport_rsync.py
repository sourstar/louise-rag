"""RSYNC transport — subprocess. list_files() shells `rsync --list-only`;
fetch_one() runs `rsync -a --partial --inplace` into a STAGING path and
os.replace()s it into place on success.

Why a staging path (P0.7 fix): `--inplace` writes directly to the destination,
so a killed rsync used to leave a torn hybrid at the FINAL path, which
app/discovery.py's mtime/size->hash gate would happily ingest as a complete
corpus file. HTTPS and FTP never had this hole because they use `.part` +
os.replace. Parallelism and a stop event both make an interrupted rsync far
likelier, so this closes it: `--partial --inplace` against the staging file
remains the resume mechanism (rsync re-checksums and continues), and the final
path only ever appears atomically and complete.

Why Popen instead of subprocess.run: `run()` blocks the calling thread
absolutely, so a stop event could never interrupt it. The poll loop below uses
communicate(timeout=...) rather than a hand-rolled poll because the pipes must
keep draining — a full pipe buffer would otherwise deadlock the child.

resume_from is accepted for Protocol uniformity but ignored: rsync takes no
byte offset, it has its own native resume.
"""
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Iterator

from . import budget
from .spec import AcquisitionSpec, Endpoint, RemoteFile, TransferAborted, TransportError

# rsync ≥3.1 prints sizes with COMMA GROUPING ("2,417,305"). The old
# `\d+` matched only plain digits, so every file of 1,000 bytes or more was
# silently dropped from the listing — observed live: a complete 2.8M-entry
# recursive listing of gutenberg-epub filtered down to the FIVE files that
# happened to be under 1 KB, and the historical "total: 1" fetch summaries
# were the same bug against the module's top level.
_LISTING_RE = re.compile(
    r"^(?P<perm>[-dl][rwxstST-]{9})\s+(?P<size>[\d,]+)\s+(?P<date>\d{4}/\d{2}/\d{2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2})\s+(?P<name>.+)$")

_POLL_SECONDS = 1.0
_DEFAULT_TIMEOUT = 300


def _with_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


_LISTING_TIMEOUT = 1800


class RsyncTransport:
    def list_files(self, endpoint: Endpoint, spec: AcquisitionSpec) -> Iterator[RemoteFile]:
        # -r: without it `--list-only` shows the module's TOP LEVEL only —
        # for a one-dir-per-doc corpus that is 79k DIRECTORIES (measured
        # against gutenberg-epub), every one skipped below, so the listing
        # was empty and gutenberg's in-app acquisition never enumerated a
        # single book. The listing contract (AcquisitionSpec docstring,
        # amendment 33) demands the origin's COMPLETE listing; recursive is
        # the only complete one. The timeout matches: a full recursive
        # listing of ~800k entries takes minutes, not the old 60 s.
        argv = ["rsync", "-r", "--list-only"]
        if endpoint.ssh_key_path is not None:
            argv += ["-e", f"ssh -i {endpoint.ssh_key_path}"]
        argv.append(_with_trailing_slash(endpoint.url))
        try:
            result = subprocess.run(argv, capture_output=True, text=True,
                                    timeout=_LISTING_TIMEOUT, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            raise TransportError(f"rsync --list-only {endpoint.url}: {e}") from e
        for line in result.stdout.splitlines():
            m = _LISTING_RE.match(line)
            if not m or m.group("name") in (".", ".."):
                continue
            if m.group("perm")[0] == "d":
                continue   # directories are traversed implicitly by the transfer itself
            mtime = int(time.mktime(time.strptime(
                f"{m.group('date')} {m.group('time')}", "%Y/%m/%d %H:%M:%S")))
            yield RemoteFile(rel_path=m.group("name"),
                             size=int(m.group("size").replace(",", "")), mtime=mtime)

    def fetch_one(self, endpoint: Endpoint, remote: RemoteFile, dest: Path,
                  resume_from: int) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        stop = _current_stop()

        argv = ["rsync", "-a", "--partial", "--inplace"]
        if endpoint.ssh_key_path is not None:
            argv += ["-e", f"ssh -i {endpoint.ssh_key_path}"]

        with budget.rsync_reservation(endpoint.url) as bwlimit_kbps:
            if bwlimit_kbps is not None:
                argv.append(f"--bwlimit={bwlimit_kbps}")
            argv += [f"{_with_trailing_slash(endpoint.url)}{remote.rel_path}", str(part)]
            self._run(argv, endpoint, remote, stop, progress_path=part)

        size = part.stat().st_size
        os.replace(part, dest)      # the final path only ever appears complete
        return size

    def _run(self, argv: list[str], endpoint: Endpoint, remote: RemoteFile,
             stop=None, progress_path: Path | None = None) -> None:
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True)
        except FileNotFoundError as e:
            raise TransportError(f"rsync {endpoint.url}/{remote.rel_path}: {e}") from e

        deadline = time.monotonic() + _DEFAULT_TIMEOUT

        counted = 0

        def _note_progress() -> None:
            # the growing .part file is the only observable byte flow of an
            # rsync subprocess: credit each second's delta to the per-class
            # accounting so the Network graph shows this transfer live
            nonlocal counted
            if progress_path is None:
                return
            try:
                size = progress_path.stat().st_size
            except OSError:
                return
            if size > counted:
                budget.note_external_bytes(size - counted)
                counted = size

        while True:
            if stop is not None and stop.is_set():
                proc.terminate()
                try:
                    proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                _note_progress()   # bytes landed before the stop are real traffic
                # --partial leaves a truthful staging file; the next run resumes
                # from it. Nothing is moved into place, so nothing is corrupt.
                raise TransferAborted(
                    f"rsync aborted by stop event: {endpoint.url}/{remote.rel_path}")
            try:
                _, stderr = proc.communicate(timeout=_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                _note_progress()
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.communicate()
                    raise TransportError(
                        f"rsync {endpoint.url}/{remote.rel_path}: timed out after "
                        f"{_DEFAULT_TIMEOUT}s")
                continue
            _note_progress()       # the tail of a fast file that never hit a poll
            if proc.returncode != 0:
                raise TransportError(
                    f"rsync {endpoint.url}/{remote.rel_path}: exit {proc.returncode}: "
                    f"{(stderr or '').strip()[:300]}")
            return


def _current_stop():
    """The stop event bound by the worker pool, if any. Imported lazily so the
    transport keeps no import-time dependency on the transfer package."""
    from ..transfer.manager import current_handle
    handle = current_handle()
    return handle.stop if handle is not None else None

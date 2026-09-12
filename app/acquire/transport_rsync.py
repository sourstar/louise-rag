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

#: rsync's OWN I/O timeout, in seconds: it exits 30 ("Timeout in data
#: send/receive") when no data has moved for this long. `_DEFAULT_TIMEOUT`
#: above bounds a transfer's TOTAL duration, which is a different thing
#: and does not catch the case that matters.
#:
#: Observed live on gutenberg 2026-09-12. Ten rsync processes against
#: ftp.ibiblio.org, every one of them ESTABLISHED with empty queues in
#: both directions, each frozen at exactly 786,432 bytes: the mirror had
#: accepted the connection, sent 768 KiB and gone silent. A fresh
#: connection made by hand at the same moment pulled a 1.2 MB file
#: instantly, so the mirror was serving — it had simply abandoned ours.
#: Each dead session then held a worker slot for the full 300 s, and with
#: most sessions dead the fetch ran at SIXTY FILES AN HOUR against a
#: measured 1.2 MB/s per healthy connection. 393,151 files at that rate is
#: 273 days.
#:
#: 60 s of total silence is already ten to sixty times the whole-file time
#: for this corpus (1-6 MB files at 1.2 MB/s). The flag measures
#: INACTIVITY, not rate, so a genuinely slow but progressing transfer is
#: never killed — and a clean rsync exit is a TransportError the caller
#: already retries, against the other mirror.
_IO_TIMEOUT = 60
_CONNECT_TIMEOUT = 30


def _with_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def _timeout_args(url: str, io: int = _IO_TIMEOUT) -> list[str]:
    """The idleness bounds, for a REMOTE endpoint only.

    `--contimeout` is refused outright on a local transfer ("--contimeout
    cannot be used with local transfers", exit 1), and neither flag means
    anything there: a local copy has no connection to time out and no
    server to go quiet on it. A plain path is local; `host::module`,
    `host:path` and `rsync://` are not.
    """
    remote = url.startswith("rsync://") or "::" in url or (
        ":" in url.split("/", 1)[0])
    if not remote:
        return []
    return [f"--timeout={io}", f"--contimeout={_CONNECT_TIMEOUT}"]


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
        # `--timeout` here too, and for the same reason: a listing that
        # stalls silently is the same failure as a transfer that does, and
        # it holds the whole pass rather than one worker slot. The window
        # is wider because a recursive listing of ~800k entries has long
        # legitimate pauses while the server walks its tree.
        argv = ["rsync", "-r", "--list-only"] + _timeout_args(
            endpoint.url, io=_IO_TIMEOUT * 5)
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

        argv = ["rsync", "-a", "--partial", "--inplace"] + _timeout_args(endpoint.url)
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

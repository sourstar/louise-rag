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

Why a process group of its own (start_new_session): a daemon-protocol rsync
(host::module) FORKS a receiver, and the receiver inherits stdout and stderr.
When the parent exits first — exit 35 "timeout waiting for daemon
connection", exit 10, or our own kill — the receiver is re-parented to init
and can live on for ten minutes and more, still holding the write ends of
our pipes. communicate() reaps only after EOF on both, and EOF never came.
Observed live on the node 2026-09-14: rsync zombies 700+ s old, each with a
live orphan born 2 s after it, their fetch threads parked in poll() and
their transfer slots held; and the deadline and stop paths, which killed the
already-dead parent and then communicate()d with NO timeout, waited on the
orphan without a bound — a SIGTERM could hang a fetch thread indefinitely.
As the leader of its own group, rsync has every process it forks born into
that group, so one killpg reaches the receiver too; `_supervise` bounds every
wait. A STOP still goes to the leader alone first, as it always did: rsync's
own shutdown SIGUSR1s its receiver and is over in under a second, whereas a
SIGTERM to the whole group reaches the receiver too, which then waits for a
controlled shutdown from a generator already gone — re-parented, holding the
pipes, until a SIGKILL (measured against a real daemon, review 2026-09-14:
0.5 s leader-first, 10.5 s group-first). The group is signalled only for
what the leader leaves behind.

Leading its own session also means no signal reaches rsync from the
terminal: the CLI and the worker turn SIGTERM, SIGINT and SIGHUP into the
stop event, and where no stop is bound (a listing under `corpus status`)
Ctrl-C or a hangup arrives as KeyboardInterrupt. Nothing leaves `_supervise`
— an exception included — with rsync's group still alive: nothing else would
ever signal it, and it would go on moving bytes after the caller released
its bandwidth lease (invariant 5).

resume_from is accepted for Protocol uniformity but ignored: rsync takes no
byte offset, it has its own native resume.
"""
import contextlib
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterator, NamedTuple

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

#: Once the direct rsync child has exited, how long its pipes may stay
#: open before whatever still holds them is SIGKILLed. Normally EOF follows
#: the exit at once — on success rsync's parent waits for its receiver — so
#: a pipe still open after this is an orphaned receiver, which on the node
#: lived for 10+ minutes.
_EXIT_GRACE = 2.0
#: What a stop's SIGTERM — to the LEADER alone — gives rsync to end the
#: transfer its own way before its group is SIGKILLed: the 10 s it always
#: had. Real rsync needs 0.5-0.9 s; this bounds a leader that does not
#: react. Once the leader has exited, whatever still holds the pipes gets
#: `_EXIT_GRACE`, not the rest of this.
_STOP_GRACE = 10.0
#: How often the stop path looks for the leader's exit.
_STOP_POLL = 0.2
#: What the pipes get to close after the group's SIGKILL; past it they are
#: abandoned unread.
_KILL_GRACE = 5.0

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
        for line in self._list(argv, endpoint).splitlines():
            m = _LISTING_RE.match(line)
            if not m or m.group("name") in (".", ".."):
                continue
            if m.group("perm")[0] == "d":
                continue   # directories are traversed implicitly by the transfer itself
            mtime = int(time.mktime(time.strptime(
                f"{m.group('date')} {m.group('time')}", "%Y/%m/%d %H:%M:%S")))
            yield RemoteFile(rel_path=m.group("name"),
                             size=int(m.group("size").replace(",", "")), mtime=mtime)

    def _list(self, argv: list[str], endpoint: Endpoint) -> str:
        """The listing's stdout, supervised like `_run`'s so a STOP ends it.

        It was one `subprocess.run`, which blocks its thread absolutely: a
        pause, seed-only or SIGTERM arriving during gutenberg's listing
        (~12 min measured, capped at `_LISTING_TIMEOUT` per mirror) was not
        seen until the transfer pool started. A `communicate(timeout=)`
        retried after TimeoutExpired loses no output (Python docs), so the
        listing reads exactly as before. On a stop the group is terminated
        as a transfer's is, and the abort RAISES — a partial listing is
        never returned, because the recorded listing and the delta gate
        take it as complete (the listing contract, spec.py).

        The stop is the one engine._list_and_filter binds; this runs at the
        generator's first next(), inside that bind.
        """
        stop = _current_stop()
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True,
                                    start_new_session=True)
        except FileNotFoundError as e:
            raise TransportError(f"rsync --list-only {endpoint.url}: {e}") from e
        end = _supervise(proc, stop, _LISTING_TIMEOUT)
        if end.how == "stopped":
            raise TransferAborted(
                f"rsync --list-only aborted by stop event: {endpoint.url}")
        what = f"rsync --list-only {endpoint.url}"
        if end.how == "timed_out":
            raise TransportError(f"{what}: timed out after {_LISTING_TIMEOUT}s")
        _raise_unless_clean(what, end)
        return end.stdout

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
                                     text=True, start_new_session=True)
        except FileNotFoundError as e:
            raise TransportError(f"rsync {endpoint.url}/{remote.rel_path}: {e}") from e

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

        end = _supervise(proc, stop, _DEFAULT_TIMEOUT, tick=_note_progress)
        # the tail of a fast file that never hit a poll — and on a stop or a
        # deadline, bytes landed before it are real traffic all the same
        _note_progress()
        if end.how == "stopped":
            # --partial leaves a truthful staging file; the next run resumes
            # from it. Nothing is moved into place, so nothing is corrupt.
            raise TransferAborted(
                f"rsync aborted by stop event: {endpoint.url}/{remote.rel_path}")
        what = f"rsync {endpoint.url}/{remote.rel_path}"
        if end.how == "timed_out":
            raise TransportError(f"{what}: timed out after {_DEFAULT_TIMEOUT}s")
        _raise_unless_clean(what, end)


class _Ended(NamedTuple):
    """How one supervised rsync ended.

    `how` is "exited", "stopped" or "timed_out". `returncode` is the one the
    kernel reported for the direct child — None when a stop or deadline left
    it unreaped by the last bound, never a stand-in; only "exited" is judged
    by it, and "exited" always has a real one. `held`: the child exited but
    something it forked kept the pipes open past `_EXIT_GRACE` and had to be
    signalled, so the output may be short and an exit 0 is not a success.
    """
    how: str
    returncode: int | None
    stdout: str
    stderr: str
    held: bool = False


def _supervise(proc: subprocess.Popen, stop, limit: float,
               tick: Callable[[], None] = lambda: None) -> _Ended:
    """Drain `proc`'s pipes until it exits, `stop` fires or `limit` seconds
    pass — and never wait on a pipe or a process without a bound, on any
    path.

    `tick` runs once per poll. Between polls, poll() REAPS the direct child
    the moment it exits, so no zombie outlives it while an orphan still
    holds the pipes (communicate() alone reaps only after EOF). The pipes
    then get `_EXIT_GRACE` to reach EOF — the normal case, the receiver
    exiting with its parent — and past it the whole group is SIGKILLed.

    Bounds: a stop `_STOP_GRACE` + `_KILL_GRACE`; an exit `_POLL_SECONDS`
    + `_EXIT_GRACE` + `_KILL_GRACE`; the deadline `limit` + `_KILL_GRACE`.
    And nothing leaves here with rsync's group alive: an exception — `tick`
    raising, a KeyboardInterrupt where nothing turns Ctrl-C into the stop —
    SIGKILLs the group before it propagates.
    """
    try:
        return _watch(proc, stop, limit, tick)
    except BaseException:
        # the caller's exception is the one that matters; a failure to
        # clean up must not replace it
        with contextlib.suppress(Exception):
            _kill_group(proc)
        raise


def _watch(proc: subprocess.Popen, stop, limit: float,
           tick: Callable[[], None]) -> _Ended:
    deadline = time.monotonic() + limit
    while True:
        if stop is not None and stop.is_set():
            out, err = _stop(proc)
            return _Ended("stopped", proc.returncode, out, err)
        try:
            out, err = proc.communicate(timeout=_POLL_SECONDS)
            return _Ended("exited", proc.returncode, out or "", err or "")
        except subprocess.TimeoutExpired:
            pass
        tick()
        if proc.poll() is None:
            if time.monotonic() > deadline:
                out, err = _kill_group(proc)
                return _Ended("timed_out", proc.returncode, out, err)
            continue
        try:
            out, err = proc.communicate(timeout=_EXIT_GRACE)
            return _Ended("exited", proc.returncode, out or "", err or "")
        except subprocess.TimeoutExpired:
            # Straight to SIGKILL: a lone rsync receiver does not end on
            # SIGTERM (it waits for a generator that is gone), so asking
            # only burned the grace; and this exit is already judged not
            # complete (`held`), so there is nothing to let it finish.
            out, err = _kill_group(proc)
            return _Ended("exited", proc.returncode, out, err, held=True)


def _stop(proc: subprocess.Popen) -> tuple[str, str]:
    """End rsync for a stop; return what its pipes held (stdout, stderr).

    SIGTERM to the LEADER alone: rsync's own shutdown SIGUSR1s its receiver,
    waits for it and exits, the pipes closing with it — 0.5-0.9 s against a
    real daemon, nothing left behind. A SIGTERM to the whole group instead
    left the receiver waiting on the gone generator until the SIGKILL, 10 s
    later on every stop (review, 2026-09-14). The leader gets `_STOP_GRACE`
    to do it its own way; once it has exited, what it left holding the pipes
    gets `_EXIT_GRACE`; then the group is SIGKILLed.
    """
    proc.terminate()       # the leader only; a no-op once it has been reaped
    give_up = time.monotonic() + _STOP_GRACE
    exited_at: float | None = None
    while True:
        now = time.monotonic()
        if exited_at is None and proc.poll() is not None:
            exited_at = now
        until = give_up if exited_at is None else min(give_up, exited_at + _EXIT_GRACE)
        if now >= until:
            return _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=min(_STOP_POLL, until - now))
            return out or "", err or ""
        except subprocess.TimeoutExpired:
            pass


def _kill_group(proc: subprocess.Popen) -> tuple[str, str]:
    """SIGKILL rsync's whole group and drain what its pipes still hold;
    return (stdout, stderr). Past `_KILL_GRACE` it closes the pipes unread,
    so it returns within that on any path.

    Only that last step loses output, and only what a holder the SIGKILL
    could not end kept from EOF; what was read by then is kept
    (TimeoutExpired carries everything read across the calls), so an error
    still quotes rsync's words.
    """
    _signal_group(proc, signal.SIGKILL)
    try:
        out, err = proc.communicate(timeout=_KILL_GRACE)
        return out or "", err or ""
    except subprocess.TimeoutExpired as e:
        partial = e
    # Even SIGKILL left the pipes open: a holder that left the group
    # (setsid), or one stuck in uninterruptible I/O. Stop reading rather
    # than wait on it. poll() reaps the child if the SIGKILL has landed by
    # now; if not, Popen's finaliser queues it and the next Popen reaps it.
    proc.poll()
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            with contextlib.suppress(OSError):
                pipe.close()
    return _text(partial.output), _text(partial.stderr)


def _signal_group(proc: subprocess.Popen, sig: signal.Signals) -> None:
    """Send `sig` to rsync and everything it forked.

    A group outlives its leader while any member lives, and Linux keeps a
    pid number allocated while a group of that id exists, so after the
    leader is reaped this still reaches exactly its orphans. An empty group
    is ProcessLookupError: all of it is gone already, which is the point.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, sig)


def _text(data: bytes | str | None) -> str:
    """TimeoutExpired carries what was read so far as BYTES, text mode or not."""
    if isinstance(data, bytes):
        return data.decode(errors="replace")
    return data or ""


def _raise_unless_clean(what: str, end: _Ended) -> None:
    """The verdict on an rsync that EXITED: success is exit 0 with its
    output closed on its own. A returncode is reported only as the kernel
    gave it; an exit 0 whose forks had to be killed is not taken as a
    complete transfer or listing — the caller retries, as for any error."""
    if end.returncode != 0:
        raise TransportError(
            f"{what}: exit {end.returncode}: {end.stderr.strip()[:300]}")
    if end.held:
        raise TransportError(
            f"{what}: exit 0, but a process it forked still held its output "
            f"{_EXIT_GRACE:g}s later and was killed; not taken as complete: "
            f"{end.stderr.strip()[:300]}")


def _current_stop():
    """The stop event bound by the worker pool, if any. Imported lazily so the
    transport keeps no import-time dependency on the transfer package."""
    from ..transfer.manager import current_handle
    handle = current_handle()
    return handle.stop if handle is not None else None

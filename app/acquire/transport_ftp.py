"""FTP transport — stdlib ftplib. Dynamic listing via MLSD (falls back to a
bare NLST when the server doesn't support it); resume via REST."""
import ftplib
import os
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from . import budget
from .spec import AcquisitionSpec, Endpoint, RemoteFile, TransferAborted, TransportError


def _connect(endpoint: Endpoint) -> ftplib.FTP:
    parsed = urlparse(endpoint.url if "://" in endpoint.url else f"ftp://{endpoint.url}")
    ftp = ftplib.FTP(timeout=30)
    ftp.connect(parsed.hostname, parsed.port or 21)
    ftp.login()   # anonymous — every current spec is a public FTP dump
    if parsed.path:
        ftp.cwd(parsed.path)
    return ftp


def _parse_mlsd_modify(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(time.mktime(time.strptime(value[:14], "%Y%m%d%H%M%S")))
    except ValueError:
        return None


def _current_stop():
    """The stop bound around the listing (engine._list_and_filter), if any —
    the same mechanism the rsync and torrent transports read."""
    from ..transfer.manager import current_handle
    handle = current_handle()
    return handle.stop if handle is not None else None


def _check_stop(stop, endpoint: Endpoint) -> None:
    if stop is not None and stop.is_set():
        raise TransferAborted(f"ftp listing aborted by stop event: {endpoint.url}")


class FtpTransport:
    def list_files(self, endpoint: Endpoint, spec: AcquisitionSpec) -> Iterator[RemoteFile]:
        """The stop is checked before connecting and per entry. MLSD and
        NLST read the WHOLE directory before the first entry comes back
        (ftplib buffers the data channel), so that read itself is bounded
        only by the 30 s socket timeout per operation — seconds, for
        pubmed's ~1,300-entry baseline."""
        stop = _current_stop()
        _check_stop(stop, endpoint)
        ftp = _connect(endpoint)
        clean_exit = False
        try:
            try:
                for name, facts in ftp.mlsd():
                    _check_stop(stop, endpoint)
                    if facts.get("type") != "file":
                        continue
                    size = int(facts["size"]) if "size" in facts else None
                    yield RemoteFile(rel_path=name, size=size,
                                      mtime=_parse_mlsd_modify(facts.get("modify")))
            except ftplib.error_perm:
                for name in ftp.nlst():
                    _check_stop(stop, endpoint)
                    yield RemoteFile(rel_path=name)
            clean_exit = True
        finally:
            # quit() only after a clean listing, as in fetch_one: on an abort
            # it can block for the socket timeout or raise an ftplib error
            # that REPLACES the TransferAborted
            if clean_exit:
                try:
                    ftp.quit()
                except ftplib.all_errors:
                    ftp.close()
            else:
                ftp.close()

    def fetch_one(self, endpoint: Endpoint, remote: RemoteFile, dest: Path,
                  resume_from: int) -> int:
        part = dest.with_name(dest.name + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        mode = "ab" if resume_from > 0 and part.is_file() else "wb"
        if mode == "wb":
            resume_from = 0
        ftp = _connect(endpoint)
        clean_exit = False
        try:
            with open(part, mode) as f:
                def _write(chunk: bytes) -> None:
                    f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
                    budget.acquire_bytes(len(chunk))
                try:
                    ftp.retrbinary(f"RETR {remote.rel_path}", _write,
                                   rest=resume_from or None)
                except ftplib.error_perm as e:
                    # "554 invalid REST argument": the server rejects our
                    # resume offset — typically a .part poisoned past the
                    # remote size by an earlier bad append. Retrying with the
                    # same offset fails forever (observed live, retries=7);
                    # the only honest move is to discard the partial and pull
                    # the file whole.
                    if resume_from and str(e).startswith(("554", "501")):
                        f.seek(0)
                        f.truncate()
                        resume_from = 0
                        ftp.retrbinary(f"RETR {remote.rel_path}", _write)
                    else:
                        raise
            clean_exit = True
        except ftplib.all_errors as e:
            raise TransportError(f"{endpoint.url}/{remote.rel_path}: {e}") from e
        finally:
            # QUIT is a protocol exchange: on the abort path the data channel
            # is half-read, so quit() would either block for the full socket
            # timeout or raise an ftplib error that REPLACES the in-flight
            # TransferAborted — losing the abort and, with it, the correct
            # manifest handling. close() just drops the socket.
            if clean_exit:
                try:
                    ftp.quit()
                except ftplib.all_errors:
                    ftp.close()
            else:
                ftp.close()
        size = part.stat().st_size
        os.replace(part, dest)
        return size

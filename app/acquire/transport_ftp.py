"""FTP transport — stdlib ftplib. Dynamic listing via MLSD (falls back to a
bare NLST when the server doesn't support it); resume via REST."""
import ftplib
import os
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from . import budget
from .spec import AcquisitionSpec, Endpoint, RemoteFile, TransportError


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


class FtpTransport:
    def list_files(self, endpoint: Endpoint, spec: AcquisitionSpec) -> Iterator[RemoteFile]:
        ftp = _connect(endpoint)
        try:
            try:
                for name, facts in ftp.mlsd():
                    if facts.get("type") != "file":
                        continue
                    size = int(facts["size"]) if "size" in facts else None
                    yield RemoteFile(rel_path=name, size=size,
                                      mtime=_parse_mlsd_modify(facts.get("modify")))
            except ftplib.error_perm:
                for name in ftp.nlst():
                    yield RemoteFile(rel_path=name)
        finally:
            ftp.quit()

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

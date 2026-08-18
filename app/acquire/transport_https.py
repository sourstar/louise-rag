"""HTTPS transport — httpx streaming GET with Range resume.

Plain HTTPS has no generic directory-listing protocol, so list_files() just
returns endpoint.static_files (declared by the per-source spec) rather than
crawling anything.
"""
import os
from pathlib import Path
from typing import Iterator

import httpx

from . import budget
from .spec import AcquisitionSpec, Endpoint, RemoteFile, TransportError

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
    def list_files(self, endpoint: Endpoint, spec: AcquisitionSpec) -> Iterator[RemoteFile]:
        return iter(endpoint.static_files)

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
            with httpx.Client(timeout=60) as client:
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

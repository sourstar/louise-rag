"""Torrent transport — stub interface only (Phase 2, architecture-v2.md §5,
§16.1 roadmap). Declared so AcquisitionSpec/engine code can reference
Transport.TORRENT without a NameError; every method raises until a real
implementation lands.
"""
from pathlib import Path
from typing import Iterator

from .spec import AcquisitionSpec, Endpoint, RemoteFile

_NOT_IMPLEMENTED = "torrent transport is a Phase 2 stub (architecture-v2.md §5)"


class TorrentTransport:
    def list_files(self, endpoint: Endpoint, spec: AcquisitionSpec) -> Iterator[RemoteFile]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def fetch_one(self, endpoint: Endpoint, remote: RemoteFile, dest: Path,
                  resume_from: int) -> int:
        raise NotImplementedError(_NOT_IMPLEMENTED)

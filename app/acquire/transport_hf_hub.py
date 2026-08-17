"""Hugging Face Hub transport — plain HTTPS resolve URLs, no huggingface_hub
dependency. Only fetches an explicitly declared file list
(endpoint.static_files); repo-browsing/snapshot features need the SDK and
are out of scope — a profile's file set is known in advance anyway.
"""
from pathlib import Path
from typing import Iterator

from .spec import AcquisitionSpec, Endpoint, RemoteFile
from .transport_https import HttpsTransport

_HF_BASE = "https://huggingface.co"


class HfHubTransport:
    """endpoint.url is the repo id, optionally "<repo_id>@<revision>"
    (revision defaults to "main"), e.g. "datasets/Upstash/wikipedia-2024-06-bge-m3"."""

    def __init__(self):
        self._https = HttpsTransport()

    def list_files(self, endpoint: Endpoint, spec: AcquisitionSpec) -> Iterator[RemoteFile]:
        return iter(endpoint.static_files)

    def fetch_one(self, endpoint: Endpoint, remote: RemoteFile, dest: Path,
                  resume_from: int) -> int:
        repo_id, _, revision = endpoint.url.partition("@")
        resolve_url = f"{_HF_BASE}/{repo_id}/resolve/{revision or 'main'}"
        https_endpoint = Endpoint(transport=endpoint.transport, url=resolve_url,
                                   auth_token_path=endpoint.auth_token_path)
        return self._https.fetch_one(https_endpoint, remote, dest, resume_from)

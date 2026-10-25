"""Hugging Face Hub transport — plain HTTPS resolve URLs, no huggingface_hub
dependency.

Fetches an explicitly declared file list (endpoint.static_files) when one is
given; otherwise lists the repo dynamically through the Hub's tree API
(paginated via Link headers). Dynamic listing is what lets a spec cover a
dataset whose shard count is not worth hardcoding (Upstash's Wikipedia set
is ~1,450 parquet files across 11 languages) — and any third-party
pre-embedded dataset a collection points at (amendment 11).
"""
from pathlib import Path
from typing import Iterator

import httpx

from . import budget
from .spec import AcquisitionSpec, Endpoint, RemoteFile, TransferAborted, TransportError
from .transport_https import HttpsTransport, _USER_AGENT

_HF_BASE = "https://huggingface.co"


class HfHubTransport:
    """endpoint.url is the repo id, optionally "<repo_id>@<revision>"
    (revision defaults to "main"), e.g. "datasets/Upstash/wikipedia-2024-06-bge-m3"."""

    def __init__(self):
        self._https = HttpsTransport()

    def list_files(self, endpoint: Endpoint, spec: AcquisitionSpec) -> Iterator[RemoteFile]:
        if endpoint.static_files:
            return iter(endpoint.static_files)
        return self._list_tree(endpoint)

    def _list_tree(self, endpoint: Endpoint) -> Iterator[RemoteFile]:
        repo_id, _, revision = endpoint.url.partition("@")
        url = (f"{_HF_BASE}/api/{repo_id}/tree/{revision or 'main'}"
               f"?recursive=true")
        headers = {"User-Agent": _USER_AGENT}
        if endpoint.auth_token_path is not None:
            token = endpoint.auth_token_path.read_text().strip()
            headers["Authorization"] = f"Bearer {token}"
        # the stop bound around the listing (engine._list_and_filter),
        # checked before each page: a paginated tree has no bound on pages
        from ..transfer.manager import current_handle
        handle = current_handle()
        stop = handle.stop if handle is not None else None
        try:
            with httpx.Client(timeout=60, headers=headers) as client:
                while url:
                    if stop is not None and stop.is_set():
                        raise TransferAborted(
                            f"hf listing aborted by stop event: {endpoint.url}")
                    r = client.get(url)
                    # a listing page is bytes moved like any other (invariant 5)
                    budget.acquire_bytes(len(r.content))
                    r.raise_for_status()
                    for entry in r.json():
                        if entry.get("type") == "file":
                            yield RemoteFile(rel_path=entry["path"],
                                             size=entry.get("size"))
                    url = r.links.get("next", {}).get("url")
        except httpx.HTTPError as e:
            raise TransportError(f"{url}: {e}") from e

    def fetch_one(self, endpoint: Endpoint, remote: RemoteFile, dest: Path,
                  resume_from: int) -> int:
        repo_id, _, revision = endpoint.url.partition("@")
        resolve_url = f"{_HF_BASE}/{repo_id}/resolve/{revision or 'main'}"
        https_endpoint = Endpoint(transport=endpoint.transport, url=resolve_url,
                                   auth_token_path=endpoint.auth_token_path)
        return self._https.fetch_one(https_endpoint, remote, dest, resume_from)

"""09 §9.17's `delta.mode: "updatefiles-dir"` — an origin that publishes a
baseline and, BESIDE it, a directory of update files (PubMed: NLM's annual
baseline in `pubmed/baseline/`, the daily files in `pubmed/updatefiles/`,
each with an `.md5`, numbered after the baseline). docs/notes/
pubmed-updatefiles.md is the design; this module is the engine's half of it.

WHERE THE DIRECTORY IS. `delta.subdir` is a RELATIVE REFERENCE resolved
against the Codex's `url` by RFC 3986 §5.2 (`urllib.parse.urljoin`, the
standard's own algorithm — nothing of ours): the live PubMed Codex names
`ftp.ncbi.nlm.nih.gov/pubmed/baseline` and `updatefiles`, which resolve to
`ftp.ncbi.nlm.nih.gov/pubmed/updatefiles`; 09 §9.17's earlier worked example
named `…/pubmed/` with a trailing slash, which resolves to the same
directory. The same rule for every endpoint of the spec (a mirror's update
directory is its own, beside its baseline).

WHERE ITS FILES LAND. Under `<subdir>/` in the corpus directory, so the
corpus mirrors the origin's relative layout and a record parsed from an
update file carries `origin = "<subdir>/<file>#<member>"` — 09 §9.5's own
PubMed example (`updatefiles/pubmed24n1220.xml.gz#5117`). A verifier
resolves such an origin against the update directory, every other one
against `url` (09 §9.17, amended 2026-11-07).

HOW THEY MOVE. As every other file of the spec: the engine's pool, one
`_FileResult` per file, `.part` resumed at byte granularity, the `.md5`
paired before its data file and checked in the same streaming pass, every
byte drawn from the one budget (invariant 5) — this module only says which
directory a file is listed in and requested from. No network I/O here.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Iterator
from urllib.parse import urljoin

from .spec import AcquisitionSpec, Endpoint, RemoteFile, Transport

MODE = "updatefiles-dir"


def subdir_of(spec: AcquisitionSpec) -> str | None:
    """The update directory's relative reference, or None when the spec
    publishes no update files (any other delta mode, or no subdir), or when
    its transport is the peer plane — a torrent of the corpus carries the
    update files at their corpus paths already (engine.effective_spec)."""
    if spec.delta.mode != MODE or not spec.delta.updatefiles_subdir:
        return None
    if spec.transport is Transport.TORRENT:
        return None
    return spec.delta.updatefiles_subdir.strip("/") or None


def directory_url(base_url: str, subdir: str) -> str:
    """`subdir` resolved against `base_url` by RFC 3986 §5.2. A base without
    a scheme (`ftp.ncbi.nlm.nih.gov/pubmed/baseline`, how the specs write an
    FTP endpoint) resolves as a path, by the same algorithm."""
    return urljoin(base_url, subdir)


def endpoint_for(endpoint: Endpoint, subdir: str) -> Endpoint:
    """The endpoint the update directory is listed and fetched from: the
    same host, transport and etiquette, at the resolved directory. Its
    declared file list is the base directory's and does not apply."""
    return replace(endpoint, url=directory_url(endpoint.url, subdir), static_files=())


def is_update(spec: AcquisitionSpec, rel_path: str) -> bool:
    subdir = subdir_of(spec)
    return subdir is not None and rel_path.startswith(subdir + "/")


def listed(transport, endpoint: Endpoint, spec: AcquisitionSpec) -> Iterator[RemoteFile]:
    """The update directory's COMPLETE listing (AcquisitionSpec's listing
    contract), each file under `<subdir>/` and requested by its name in
    that directory. Nothing when the spec publishes none."""
    subdir = subdir_of(spec)
    if subdir is None:
        return
    for f in transport.list_files(endpoint_for(endpoint, subdir), spec):
        if "/" in f.rel_path:
            continue                    # the directory is flat; anything deeper is not NLM's
        yield replace(f, rel_path=f"{subdir}/{f.rel_path}", remote=f.remote or f.rel_path)


def listing(transport, endpoint: Endpoint, spec: AcquisitionSpec) -> Iterator[RemoteFile]:
    """The base directory's listing, then the update directory's."""
    yield from transport.list_files(endpoint, spec)
    yield from listed(transport, endpoint, spec)


def request_endpoint(endpoint: Endpoint, spec: AcquisitionSpec, f: RemoteFile) -> Endpoint:
    """The endpoint `f` is requested from: the update directory for a file
    listed there, else `endpoint` itself."""
    if is_update(spec, f.rel_path):
        return endpoint_for(endpoint, subdir_of(spec))
    return endpoint

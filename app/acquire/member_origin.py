"""The origin as its zips' members, over the network by Range — what
`records-vs-origin` re-fetches for a collection whose Codex says
`origin.unit: "index-page"` (02 §2.3; docs/notes/normattiva-multivigenza-review.md
§3.10, D5). A zip's central directory is the index that makes the container
seekable, so a record's origin unit is its MEMBER — tens of KB — and not the
1.2 GB zip it ships in. The check's bound (03 §3.2, 256 MiB) then buys
thousands of records instead of one file.

How: the directory of each zip a sampled record points into is read once
by Range (app/acquire/zipdelta.py); a record's unit is its member and its
size the member's compressed size; to re-parse, the member is fetched by
one Range, verified against the directory, and written as a one-member zip
under a temporary base at the zip's relative path, so the plugin reads it
exactly as it reads the corpus (`<base>/<rel>::<member>`). Nothing here
knows the plugin: it serves `louise.py`'s `_reparse_from` and
`_origin_unit_from`, which do.
"""
from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path
from typing import Callable

from . import transport_https, zipdelta
from .spec import AcquisitionSpec, Transport

_MEMBER_SEP = "#"


class MemberOrigin:
    def __init__(self, spec: AcquisitionSpec, *,
                 reader_for: Callable[[str], zipdelta.RangeReader] | None = None) -> None:
        self.spec = spec
        self.files = {f.rel_path: (ep, f) for ep in spec.endpoints
                      for f in (getattr(ep, "static_files", None) or ())}
        self._reader_for = reader_for or self._http_reader
        self._dirs: dict[str, tuple[zipdelta.RangeReader, int, dict[str, zipfile.ZipInfo]]] = {}
        self.base = Path(tempfile.mkdtemp(prefix="louise-member-origin-"))
        self.bytes_read = 0

    def _http_reader(self, rel: str) -> zipdelta.RangeReader:
        endpoint, f = self.files[rel]
        return zipdelta.HttpRangeReader(transport_https._url(endpoint, f),
                                        transport_https.headers_for(endpoint))

    def _directory(self, rel: str):
        got = self._dirs.get(rel)
        if got is None:
            reader = self._reader_for(rel)
            infos, total, read = zipdelta.directory(reader)
            self.bytes_read += read
            got = self._dirs[rel] = (reader, total, {i.filename: i for i in infos})
        return got

    @staticmethod
    def split(locator: str) -> tuple[str, str]:
        rel, _, member = str(locator).partition(_MEMBER_SEP)
        return rel, member

    def unit(self, locator: str) -> tuple[str, int | None]:
        """`(the origin unit, its size)`: the member itself, weighed by its
        compressed size in the directory; a zip the spec does not list, or a
        member the directory does not name, weighs nothing."""
        rel, member = self.split(locator)
        if rel not in self.files or not member:
            return locator, None
        try:
            _, _, infos = self._directory(rel)
        except Exception:   # noqa: BLE001 — an origin that will not answer: unweighed, reported by the re-parse
            return locator, None
        info = infos.get(member)
        return locator, (info.compress_size if info is not None else None)

    def materialise(self, locator: str) -> Path:
        """The member fetched and written as a one-member zip at
        `<base>/<rel>`; the path returned. Raises `zipdelta.MemberMismatch`
        when the origin's bytes do not check out, `KeyError` when the
        directory does not name the member."""
        rel, member = self.split(locator)
        reader, total, infos = self._directory(rel)
        info = infos[member]
        data, read = zipdelta.fetch_member(reader, info, total)
        self.bytes_read += read
        out = self.base / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        zipdelta.rebuild(None, zipdelta.Plan(order=[info], fetch=[info]), {member: data}, out)
        return out


def for_codex(codex) -> MemberOrigin | None:
    """A member origin for a Codex whose `origin.unit` is `index-page` and
    whose plugin's spec lists its zips over HTTPS; None for any other."""
    origin = getattr(codex, "origin", None)
    if getattr(origin, "unit", None) != "index-page":
        return None
    from .specs import ACQUIRE_REGISTRY
    spec = ACQUIRE_REGISTRY.get(getattr(codex, "plugin", None))
    if spec is None or spec.transport is not Transport.HTTPS:
        return None
    mo = MemberOrigin(spec)
    return mo if mo.files else None

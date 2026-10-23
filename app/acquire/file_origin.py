"""The origin as its files, over the network — what `records-vs-origin`
re-fetches for a copy that holds no origin of its own: a peer that fetched
the SHARD (`source = "shard"`, Perugia's gutenberg) from a Codex whose
origin is a mirror tree of one directory per document (gutenberg's rsync
module). The operator, 2026-10-23: no `ack --origin-unchecked`; the copy
"will be cleared when the origin is checked". Before this the check had no
origin to put a question to and stayed inconclusive for good
(docs/notes/p2p-review-open-issues.md, "Perugia's records-vs-origin").

How: a record's `origin` is a file of its document's directory, relative
to the module (09 §9.5), and the unit is that directory. It is listed on the spec's endpoints, mirrors
first as acquisition orders them, and weighed by the sizes the listing
gives; its admitted files (the spec's filters, as acquisition's) are
fetched with the plugin's own transport — `fetch_one`, which draws from the
one bandwidth budget (invariant 5) — under a temporary base at the same
relative path, so the plugin reads it exactly as it reads a corpus. Only
the units the check's bound takes are weighed and fetched (03 §3.2: 256 MiB
by default), so the etiquette question 06 §6.2 left open is answered by the
bound: one sample per cadence, from the mirrors, inside the budget.

Nothing new is built for the transfer: the transport, its listing, its
staging file, its stop and its budget lease are acquisition's. The member
origin (app/acquire/member_origin.py) is the same idea for zip members by
HTTP Range. THE STANDARD ALTERNATIVE: a verifier that fetches by content
address (the origin's own checksums, or a BitTorrent v2 piece tree) would
need no listing; gutenberg publishes neither (`checksums = none`).
Other layouts (a file per unit, a container per unit) are not served here:
their units are files, and a file's listing is the transport's to add.
"""
from __future__ import annotations

import atexit
import dataclasses
import shutil
import signal
import tempfile
import threading
from pathlib import Path

from .spec import AcquisitionSpec, Endpoint, RemoteFile, Transport, TransportError, admitted

#: transports that list a directory themselves (rsync --list-only, FTP)
LISTING_TRANSPORTS = (Transport.RSYNC, Transport.FTP)
_MEMBER_SEP = "#"


class FileOrigin:
    def __init__(self, spec: AcquisitionSpec, filters, *, transport=None) -> None:
        if transport is None:
            from .transports import REGISTRY
            transport = REGISTRY[spec.transport]
        self.spec = spec
        self.filters = tuple(filters)
        self.transport = transport
        self.base = Path(tempfile.mkdtemp(prefix="louise-file-origin-"))
        # a sample is up to the check's bound (256 MiB), once per cadence:
        # never left behind in the temporary directory
        atexit.register(self.close)
        self.bytes_read = 0
        # unit -> (the endpoint that listed it, its admitted files), or None
        # when no endpoint would list it
        self._listed: dict[str, tuple[Endpoint, list[RemoteFile]] | None] = {}

    @staticmethod
    def _rel(locator: str) -> str:
        """The DOCUMENT'S DIRECTORY: the locator names the file the record
        was read from (`10001/pg10001.txt.utf8`), and the record is the
        directory's — gutenberg's parser reads the catalogue entry beside
        the text (parser 9), and `origin_hash` is over both."""
        return str(locator).partition(_MEMBER_SEP)[0].strip("/").split("/", 1)[0]

    def _at(self, endpoint: Endpoint, rel: str) -> Endpoint:
        return dataclasses.replace(endpoint, url=f"{endpoint.url.rstrip('/')}/{rel}")

    def _listing(self, rel: str) -> tuple[Endpoint, list[RemoteFile]] | None:
        if rel not in self._listed:
            got = None
            for endpoint in self.spec.endpoints:
                try:
                    files = list(self.transport.list_files(self._at(endpoint, rel), self.spec))
                except TransportError:
                    continue
                got = (endpoint, [f for f in files if admitted(f"{rel}/{f.rel_path}", self.filters)])
                break
            self._listed[rel] = got
        return self._listed[rel]

    def unit(self, locator: str) -> tuple[str, int | None]:
        """`(the origin unit, None)`: the document's directory, UNWEIGHED —
        weighing is a listing on the network, and the check weighs only the
        units its bound reaches (`weigh`), not every sampled record's."""
        return self._rel(locator), None

    def weigh(self, unit: str) -> int | None:
        """The unit's admitted bytes as its listing states them; None when no
        endpoint lists it (the origin is away, or the document is gone)."""
        got = self._listing(unit)
        if got is None or not got[1]:
            return None
        return sum(int(f.size or 0) for f in got[1])

    def materialise(self, locator: str) -> Path:
        """The unit's admitted files fetched under `<base>/<rel>`; the path
        returned. FileNotFoundError when no endpoint lists or serves them —
        the check counts that as unreachable, never as a differing record."""
        rel = self._rel(locator)
        out = self.base / rel
        if out.is_dir():
            return out
        got = self._listing(rel)
        if got is None or not got[1]:
            raise FileNotFoundError(f"{rel}: no endpoint of {self.spec.source_id} lists it")
        first, files = got
        # the endpoint that listed it first, then the others: a mirror that
        # listed and then would not serve is the failure acquisition retries
        order = [first] + [e for e in self.spec.endpoints if e != first]
        staging = self.base / ".staging" / rel
        for endpoint in order:
            shutil.rmtree(staging, ignore_errors=True)
            try:
                for f in files:
                    self.bytes_read += self.transport.fetch_one(
                        self._at(endpoint, rel), f, staging / f.rel_path, 0)
            except TransportError:
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(out)       # a unit appears whole or not at all
            return out
        raise FileNotFoundError(f"{rel}: no endpoint of {self.spec.source_id} served it")

    def close(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)


def terminate_ends_the_transfer() -> None:
    """`hold check` binds no stop event, and the scheduled check stops it
    with SIGTERM (app/scheduled_checks.py). Left at its default, that kills
    Python and strands rsync — which leads its own session — moving bytes
    with no bandwidth lease (invariant 5). As KeyboardInterrupt it reaches
    the transport's `_supervise`, which ends rsync's group before letting it
    past (app/acquire/transport_rsync.py), as `hold records fetch` does for
    SIGHUP. Only a handler still at its default is replaced."""
    if threading.current_thread() is not threading.main_thread():
        return
    for sig in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(sig) is signal.SIG_DFL:
            signal.signal(sig, signal.default_int_handler)


def for_codex(codex) -> FileOrigin | None:
    """A file origin for a Codex whose plugin's spec is one directory per
    document over a transport that lists; None for any other."""
    from .specs import ACQUIRE_REGISTRY
    spec = ACQUIRE_REGISTRY.get(getattr(codex, "plugin", None))
    if spec is None or spec.transport not in LISTING_TRANSPORTS or spec.layout != "one-dir-per-doc":
        return None
    # the SPEC's filters, not the Codex's: the record was parsed from what
    # acquisition put in the corpus, and a re-download takes what acquisition
    # takes — gutenberg's parser reads the rdf beside the text and the epub
    # where there is one, whatever filters a Codex once wrote down
    terminate_ends_the_transfer()
    return FileOrigin(spec, spec.filters)

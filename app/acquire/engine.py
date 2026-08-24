"""Generic acquisition engine — consumes an AcquisitionSpec + profile and
drives resumable, checksum-verified downloads through a pluggable transport.
See docs/architecture-v2.md §5.1.

fetch() populates collection.corpus_dir; app/discovery.py and every
CorpusSource stay completely untouched — once files land on disk, the
existing mtime/size -> hash gate does what it already does. Keeping fetch,
discover, and `documents` as three separable layers avoids re-opening the
CorpusSource Protocol, already stabilized across P0.1/P0.2/P0.3.
"""
import fnmatch
import hashlib
import json
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .. import config
from .. import db
from .. import stats
from ..plugins.base import LicenseClass
from ..transfer import pool as transfer_pool
from ..transfer.bucket import Priority
from ..transfer.manager import Direction, get_manager
from . import checksum
from . import claim as claim_mod
from . import mirrors
from . import transport_https
from .transport_https import RangeUnsupported
from .spec import (AcquisitionPlan, AcquisitionSpec, DiskPreflightError, Filter,
                    LicenseAckRequiredError, RemoteFile, TransferAborted, TransportError,
                    estimated_bytes_for, resolve_profile)
from .transports import REGISTRY

_DRY_RUN_ASSUMED_RATE_BPS = 5_000_000   # ~5 MB/s — a named placeholder; P0.7 has real measurement
_DISK_HEADROOM = 1.1                     # refuse unless free space clears the estimate by 10%
# control-table key: what the origin's listing held at the start of the
# last acquisition pass (files/bytes for the fetched profile) — the honest
# denominator for the Library's fetched counter while a pass streams
LISTING_KEY = "acquire_listing"


def _apply_filters(files: list[RemoteFile], spec_filters: tuple[str, ...],
                    profile_filter: Filter) -> list[RemoteFile]:
    positive = [p for p in spec_filters if not p.startswith("!")]

    def _included(rel_path: str) -> bool:
        # Negative patterns veto (ALL must miss); positive patterns are
        # alternatives (ANY may match). The old all-must-match reading made
        # two positive patterns like ("*.xml.gz", "*.xml.gz.md5") mutually
        # exclusive and selected NOTHING — observed live on the first real
        # PubMed fetch (rsync sources never hit it: the mirror pre-filters).
        for pat in spec_filters:
            if pat.startswith("!") and fnmatch.fnmatch(rel_path, pat[1:]):
                return False
        if positive and not any(fnmatch.fnmatch(rel_path, p) for p in positive):
            return False
        included = any(fnmatch.fnmatch(rel_path, p) for p in profile_filter.include)
        excluded = any(fnmatch.fnmatch(rel_path, p) for p in profile_filter.exclude)
        return included and not excluded

    # deterministic order: listing order is transport-dependent (FTP returns
    # arbitrary order), and sorting keeps a data file adjacent to its
    # checksum sidecar so size/count caps take coherent pairs
    out = sorted((f for f in files if _included(f.rel_path)),
                 key=lambda f: f.rel_path)
    if profile_filter.max_bytes is not None:
        kept, total = [], 0
        for f in out:
            size = f.size or 0
            if total + size > profile_filter.max_bytes:
                break
            kept.append(f)
            total += size
        out = kept
    if profile_filter.max_files is not None:
        out = out[: profile_filter.max_files]
    return out


def _license_state(spec: AcquisitionSpec, collection) -> str:
    if spec.license_class == LicenseClass.GREEN:
        return "not_required"
    if not collection.license_ack or "/" not in collection.license_ack:
        return "missing"
    if spec.terms_text_path is None or not spec.terms_text_path.is_file():
        return "missing"
    _, _, acked_hash = collection.license_ack.partition("/")
    current_hash = hashlib.sha256(spec.terms_text_path.read_bytes()).hexdigest()
    return "acknowledged" if acked_hash == current_hash else "missing"


def _require_license_ack(spec: AcquisitionSpec, collection) -> None:
    """Raised before any listing or transport call — CLAUDE.md invariant 9:
    "before the first byte moves"."""
    if _license_state(spec, collection) == "missing":
        raise LicenseAckRequiredError(
            f"{spec.source_id}: license_class={spec.license_class.value} requires an "
            f"acknowledgement (timestamp + sha256 of {spec.terms_text_path}) recorded in "
            f"collections/{collection.name}.toml before any byte moves. "
            f"Run: python -m app.cli corpus add {collection.name} --ack-terms <path>")


def make_ack(terms_text_path: Path) -> str:
    """timestamp + sha256 of the terms text — CLAUDE.md invariant 9."""
    digest = hashlib.sha256(terms_text_path.read_bytes()).hexdigest()
    return f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}/{digest}"


def _list_and_filter(spec: AcquisitionSpec, profile: str,
                      snapshot: str | None = None,
                      endpoints: list | None = None) -> list[RemoteFile]:
    # resolve_profile also understands a COMBINED profile ("tr,it") on an
    # additive source, where it is the union of those slices
    profile_filter = resolve_profile(spec, profile)
    transport = REGISTRY[spec.transport]
    all_files: list[RemoteFile] = []
    seen: set[str] = set()
    if snapshot and spec.snapshot_files is not None:
        # A pinned collection asks the origin for THAT dated snapshot, not
        # for whatever it currently calls latest. Those URLs are immutable,
        # which is what makes the corpus reproducible and shareable.
        candidates = list(spec.snapshot_files(snapshot))
    elif spec.probe_file and len(spec.endpoints) > 1:
        # MIRRORED source: the admission rule makes every listed mirror
        # structurally identical, so listing all of them is the same listing
        # N times — measured at 12 minutes EACH on gutenberg-epub (2.8M
        # entries). List from the best-ranked mirror only, falling to the
        # next on failure; the union semantic below stays for specs whose
        # endpoints genuinely carry different subsets.
        candidates = []
        last_error: Exception | None = None
        for endpoint in (endpoints if endpoints is not None else spec.endpoints):
            try:
                candidates = list(transport.list_files(endpoint, spec))
                break
            except TransportError as e:
                last_error = e
                mirrors.mark_down(endpoint.url)
        else:
            raise last_error or TransportError(f"{spec.source_id}: no endpoint could list")
    else:
        candidates = []
        for endpoint in spec.endpoints:
            candidates.extend(transport.list_files(endpoint, spec))
    for f in candidates:
        if f.rel_path not in seen:
            seen.add(f.rel_path)
            all_files.append(f)
    return _apply_filters(all_files, spec.filters, profile_filter)


def is_adoptable(collection, f: RemoteFile) -> bool:
    """Is this file ALREADY on disk, complete, at the corpus path acquisition
    would fetch it to?

    Corpora arrive by routes other than this engine — a dataset copied from
    another machine, a `zfs send` of someone else's mirror, files staged by
    hand before the collection existed. Re-downloading bytes that are
    already sitting there is pure waste, and on a 238 GB corpus it is a
    week of someone's uplink.

    The test is size equality against the origin's declared size, which is
    the same freshness test `_is_pending` applies to a file this engine did
    fetch — so adoption grants no weaker a guarantee than a re-fetch would.
    An unknown remote size means we cannot tell complete from truncated, and
    then the honest answer is no.

    Content is still verified before the file is recorded (see
    `_adopt_one_file`): this predicate only decides whether it is worth
    looking."""
    if not f.size:
        return False
    dest = collection.corpus_dir / f.rel_path
    try:
        return dest.is_file() and dest.stat().st_size == f.size
    except OSError:
        return False


def _plan_from_files(con, collection, spec: AcquisitionSpec, files: list[RemoteFile]) -> AcquisitionPlan:
    pending: list[RemoteFile] = []
    bytes_total = bytes_pending = 0
    files_adoptable = bytes_adoptable = 0
    for f in files:
        row = con.execute(
            "SELECT remote_size,remote_mtime,fetch_done FROM fetch_files "
            "WHERE source_id=? AND rel_path=?", (spec.source_id, f.rel_path)).fetchone()
        size = f.size or 0
        bytes_total += size
        is_pending = (row is None or not row["fetch_done"]
                      or row["remote_size"] != f.size or row["remote_mtime"] != f.mtime)
        if is_pending:
            pending.append(f)
            if is_adoptable(collection, f):
                # counted as pending work (it still needs verifying and
                # recording) but NOT as bytes to move — the ETA and the disk
                # preflight below both read bytes_pending, and neither should
                # plan for a download that will not happen
                files_adoptable += 1
                bytes_adoptable += size
            else:
                bytes_pending += size
    return AcquisitionPlan(
        files_total=len(files), files_pending=len(pending),
        bytes_total=bytes_total, bytes_pending=bytes_pending,
        eta_seconds=round(bytes_pending / _DRY_RUN_ASSUMED_RATE_BPS, 1),
        license_state=_license_state(spec, collection),
        files_adoptable=files_adoptable, bytes_adoptable=bytes_adoptable,
    )


def plan_acquisition(con, collection, spec: AcquisitionSpec, profile: str) -> AcquisitionPlan:
    """Read-only: list, filter, gate-check (but never refuse) — the
    --dry-run / status half. Never raises on a missing license ack; reports
    license_state instead so an operator can see what's blocking them."""
    files = _list_and_filter(spec, profile, getattr(collection, "snapshot", None))
    return _plan_from_files(con, collection, spec, files)


def _upsert_fetched(con, source_id: str, f: RemoteFile, endpoint_url: str, local_path: str,
                     bytes_fetched: int, blake2: str, profile: str) -> None:
    con.execute(
        "INSERT INTO fetch_files(source_id,rel_path,endpoint_url,remote_size,remote_mtime,"
        "local_path,bytes_fetched,blake2,fetch_done,fetch_version,profile,error,retries,"
        "updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?,NULL,0,?) "
        "ON CONFLICT(source_id,rel_path) DO UPDATE SET "
        "endpoint_url=excluded.endpoint_url,remote_size=excluded.remote_size,"
        "remote_mtime=excluded.remote_mtime,local_path=excluded.local_path,"
        "bytes_fetched=excluded.bytes_fetched,blake2=excluded.blake2,fetch_done=1,"
        "fetch_version=excluded.fetch_version,profile=excluded.profile,error=NULL,"
        "updated_at=excluded.updated_at",
        (source_id, f.rel_path, endpoint_url, f.size, f.mtime, local_path, bytes_fetched,
         blake2, config.FETCH_VERSION, profile, int(time.time())))


def _upsert_failed(con, source_id: str, f: RemoteFile, endpoint_url: str, bytes_fetched: int,
                    profile: str, error: str) -> None:
    con.execute(
        "INSERT INTO fetch_files(source_id,rel_path,endpoint_url,remote_size,remote_mtime,"
        "bytes_fetched,fetch_done,profile,error,retries,updated_at) "
        "VALUES(?,?,?,?,?,?,0,?,?,1,?) "
        "ON CONFLICT(source_id,rel_path) DO UPDATE SET "
        "bytes_fetched=excluded.bytes_fetched,error=excluded.error,retries=retries+1,"
        "updated_at=excluded.updated_at",
        (source_id, f.rel_path, endpoint_url, f.size, f.mtime, bytes_fetched, profile,
         error[:500], int(time.time())))


def _upsert_aborted(con, source_id: str, f: RemoteFile, endpoint_url: str, bytes_fetched: int,
                     profile: str) -> None:
    """A clean SIGTERM is NOT a failure and must not go through
    _upsert_failed: incrementing `retries` would permanently blacklist a
    healthy file after MAX_RETRIES restarts, and writing an `error` would fill
    the operator's log with fake errors. Records truthful partial progress and
    clears any stale error, leaving `retries` alone (CLAUDE.md invariant 2:
    "SIGTERM = ... exit 0")."""
    con.execute(
        "INSERT INTO fetch_files(source_id,rel_path,endpoint_url,remote_size,remote_mtime,"
        "bytes_fetched,fetch_done,profile,error,retries,updated_at) "
        "VALUES(?,?,?,?,?,?,0,?,NULL,0,?) "
        "ON CONFLICT(source_id,rel_path) DO UPDATE SET "
        "bytes_fetched=excluded.bytes_fetched,error=NULL,"
        "updated_at=excluded.updated_at",
        (source_id, f.rel_path, endpoint_url, f.size, f.mtime, bytes_fetched, profile,
         int(time.time())))


@dataclass
class _FileResult:
    """One file's outcome, produced by a worker thread and applied to the
    manifest by the caller's thread — workers never touch `con` (SQLite
    connections are thread-affine) and never touch db.log_event either."""
    remote: RemoteFile
    endpoint_url: str
    status: str                    # "fetched" | "failed" | "aborted"
    size: int = 0
    digest: str | None = None
    bytes_now: int = 0
    error: str | None = None
    adopted: bool = False          # already on disk: verified, recorded, not downloaded
    # What this file actually COST: bytes that crossed the network (a resumed
    # file moves less than its size) and the seconds they took. Fed to
    # app/stats.py so the Stats page reports a measured rate instead of the
    # planner's assumed constant.
    moved: int = 0
    seconds: float = 0.0


@dataclass
class _WorkItem:
    """A data file plus, when the origin publishes one and it isn't already on
    disk, its checksum sidecar. Pairing them in ONE work item replaces the old
    global "all sidecars first" barrier: it keeps P0.4's ordering guarantee
    (the sidecar lands before its data file is verified), it parallelises
    fully instead of stalling on a 1,200-file prologue, and it makes
    structurally impossible the hole where a FAILED sidecar left
    verify_sidecar silently no-opping and the data file marked fetch_done=1
    unverified."""
    data: RemoteFile | None
    sidecar: RemoteFile | None = None


def _is_pending(con, spec: AcquisitionSpec, f: RemoteFile) -> bool:
    row = con.execute(
        "SELECT remote_size,remote_mtime,fetch_done FROM fetch_files "
        "WHERE source_id=? AND rel_path=?", (spec.source_id, f.rel_path)).fetchone()
    return (row is None or not row["fetch_done"]
            or row["remote_size"] != f.size or row["remote_mtime"] != f.mtime)


def _build_work_items(files: list[RemoteFile], pending: set[str],
                       spec: AcquisitionSpec) -> list[_WorkItem]:
    suffix = spec.checksums.sidecar_suffix if spec.checksums.kind == "sidecar" else None
    sidecars: dict[str, RemoteFile] = {}
    data_files: list[RemoteFile] = []
    for f in files:
        if suffix and f.rel_path.endswith(suffix):
            sidecars[f.rel_path[: -len(suffix)]] = f
        else:
            data_files.append(f)

    items: list[_WorkItem] = []
    paired: set[str] = set()
    for f in data_files:
        sidecar = sidecars.get(f.rel_path)
        sidecar_pending = sidecar is not None and sidecar.rel_path in pending
        if sidecar_pending:
            paired.add(sidecar.rel_path)
        if f.rel_path in pending or sidecar_pending:
            items.append(_WorkItem(data=f if f.rel_path in pending else None,
                                    sidecar=sidecar if sidecar_pending else None))
    # sidecars whose data file was filtered out of this profile entirely
    for rel_path, sidecar in sidecars.items():
        if sidecar.rel_path in pending and sidecar.rel_path not in paired:
            items.append(_WorkItem(data=None, sidecar=sidecar))
    return items


_SEGMENT_MIN_BYTES = 32 << 20    # below this, splitting costs more than it saves


def _segments_for(manager, endpoint, spec, f: RemoteFile) -> int:
    """How many connections to open for ONE file.

    Three ceilings, all of them etiquette rather than performance:
      - the operator's opt-in (platform.toml transfer.segments_per_file,
        default 1 — §5.2 keeps this default-off against origins)
      - the endpoint's own connection allowance, so splitting a file can
        never smuggle in more sockets than `per_endpoint`, Endpoint.max_conns
        and the spec's RateLimit.max_conns already permit
      - a size floor: three extra round trips to parallelise a 2 MB file is
        cost with no benefit
    """
    configured = manager.platform.transfer.segments_per_file
    if configured <= 1 or not f.size or f.size < _SEGMENT_MIN_BYTES:
        return 1
    return max(1, min(configured, manager.endpoint_slots(endpoint, spec)))


def _adopt_one_file(f: RemoteFile, collection, spec: AcquisitionSpec) -> _FileResult | None:
    """Record an already-present file as fetched, moving zero bytes.

    Verification is the SAME call a real fetch makes, on the same path, so
    an adopted row carries exactly the evidence a fetched row does — a
    sidecar mismatch or an unreadable file returns None and the caller
    downloads it properly. Adoption is a shortcut past the transfer, never
    past the checking.

    Throughput is deliberately NOT recorded: no bytes crossed the network,
    and feeding "size bytes in ~0 seconds" to the endpoint's rate estimate
    would tell the scheduler this origin is infinitely fast."""
    dest = collection.corpus_dir / f.rel_path
    try:
        digest = checksum.digest_and_verify(dest, spec.checksums)
    except Exception:   # noqa: BLE001 — same breadth as the fetch path
        # The bytes on disk are not the bytes the origin is serving. Say
        # nothing here; returning None puts the file back on the normal
        # download path, which overwrites it.
        return None
    part = dest.with_name(dest.name + ".part")
    if part.is_file():
        # Stale by construction: the destination is complete and verified,
        # so a leftover .part is a dead partial. Left in place it would be
        # read as a resume offset by a future re-fetch (FETCH_VERSION bump)
        # and a Range request would append to an already-complete file.
        try:
            part.unlink()
        except OSError:
            pass
    return _FileResult(remote=f, endpoint_url="(adopted from disk)",
                        status="fetched", size=f.size or dest.stat().st_size,
                        digest=digest, adopted=True)


def _fetch_one_file(transport, endpoint, f: RemoteFile, collection,
                     spec: AcquisitionSpec, manager, endpoint_key: str) -> _FileResult:
    dest = collection.corpus_dir / f.rel_path
    # Already there and complete? Verify and record it; move nothing.
    if is_adoptable(collection, f):
        adopted = _adopt_one_file(f, collection, spec)
        if adopted is not None:
            return adopted
    part = dest.with_name(dest.name + ".part")
    # Read at fetch time, never hoisted with the pending list: a file retried
    # within the same run would otherwise resume from a stale offset.
    resume_from = part.stat().st_size if part.is_file() else 0
    started = time.monotonic()
    segments = _segments_for(manager, endpoint, spec, f)
    try:
        if segments > 1:
            try:
                size = transport.fetch_segments(endpoint, f, dest, segments)
                elapsed = time.monotonic() - started
                manager.record_throughput(endpoint_key, size, elapsed)
                return _FileResult(remote=f, endpoint_url=endpoint.url,
                                    status="fetched", size=size,
                                    digest=checksum.digest_and_verify(dest, spec.checksums),
                                    moved=size, seconds=elapsed)
            except RangeUnsupported:
                # The origin will not serve ranges. Clear the segmented
                # scratch state — its offsets describe a layout we are
                # abandoning — and fall through to one stream.
                part.unlink(missing_ok=True)
                transport_https._sidecar_path(dest).unlink(missing_ok=True)
                resume_from = 0
        size = transport.fetch_one(endpoint, f, dest, resume_from)
        # One streaming pass for both digests (md5 sidecar + blake2), instead
        # of reading the whole file into memory twice.
        digest = checksum.digest_and_verify(dest, spec.checksums)
        elapsed = time.monotonic() - started
        manager.record_throughput(endpoint_key, size - resume_from, elapsed)
        return _FileResult(remote=f, endpoint_url=endpoint.url, status="fetched",
                            size=size, digest=digest,
                            moved=max(0, size - resume_from), seconds=elapsed)
    except TransferAborted as e:
        bytes_now = part.stat().st_size if part.is_file() else 0
        return _FileResult(remote=f, endpoint_url=endpoint.url, status="aborted",
                            bytes_now=bytes_now, error=str(e))
    except Exception as e:   # noqa: BLE001 - must never escape into the executor
        # Deliberately broader than (TransportError, OSError): checksum.py
        # raises ValueError on an unsupported sidecar algo, and a worker that
        # raised would make the executor's iterator throw partway through,
        # losing the results of every item after it (invariant 2).
        bytes_now = part.stat().st_size if part.is_file() else 0
        return _FileResult(remote=f, endpoint_url=endpoint.url, status="failed",
                            bytes_now=bytes_now, error=f"{type(e).__name__}: {e}")


def _make_worker(transport, collection, spec, manager, ranked, stop, priority):
    def _work(item: _WorkItem) -> list[_FileResult]:
        slot = transfer_pool.acquire_endpoint_slot(manager, ranked, spec, stop=stop)
        if slot is None:
            return []
        endpoint_key, endpoint, sem = slot
        results: list[_FileResult] = []
        data_result: _FileResult | None = None
        try:
            with manager.bind(priority, Direction.DOWN, stop=stop, endpoint_key=endpoint_key):
                if item.sidecar is not None:
                    sidecar_result = _fetch_one_file(
                        transport, endpoint, item.sidecar, collection, spec,
                        manager, endpoint_key)
                    results.append(sidecar_result)
                    if sidecar_result.status != "fetched" and item.data is not None:
                        # Refuse to fetch-and-mark-done a data file whose
                        # verification material is missing. Previously this
                        # silently produced an UNVERIFIED fetch_done=1 row.
                        results.append(_FileResult(
                            remote=item.data, endpoint_url=endpoint.url,
                            status=sidecar_result.status,
                            error=f"sidecar {item.sidecar.rel_path} unavailable "
                                  f"({sidecar_result.error}) — refusing to record "
                                  f"an unverified fetch"))
                        return results
                if item.data is not None:
                    data_result = _fetch_one_file(
                        transport, endpoint, item.data, collection, spec,
                        manager, endpoint_key)
        finally:
            sem.release()
        if data_result is not None:
            if data_result.status == "failed" and len(ranked) > 1:
                # MIRROR FAILOVER: on a mirrored source, a transport failure
                # is more likely the mirror than the file. Mark this mirror
                # down (mirrors.DOWN_S; the next benchmark re-admits it) and
                # retry ONCE on the next-ranked mirror before recording
                # anything — so a mirror dying mid-pass costs seconds per
                # file, not a failed batch waiting for the next 6-hour pass.
                # A clean stop ('aborted') never triggers this.
                mirrors.mark_down(endpoint.url)
                retried = _retry_on_next_mirror(
                    transport, collection, spec, manager, ranked, stop,
                    priority, item.data, endpoint.url)
                if retried is not None:
                    data_result = retried
            results.append(data_result)
        return results

    return _work


def _retry_on_next_mirror(transport, collection, spec, manager, ranked, stop,
                           priority, remote: RemoteFile, failed_url: str):
    alt = next(((k, e) for k, e in ranked
                if e.url != failed_url and not mirrors.is_down(e.url)), None)
    if alt is None:
        return None
    alt_key, alt_endpoint = alt
    sem = manager.endpoint_semaphore(alt_key,
                                     manager.endpoint_slots(alt_endpoint, spec))
    if not sem.acquire(timeout=30):
        return None
    try:
        with manager.bind(priority, Direction.DOWN, stop=stop, endpoint_key=alt_key):
            return _fetch_one_file(transport, alt_endpoint, remote, collection,
                                   spec, manager, alt_key)
    finally:
        sem.release()


def run_acquisition(con, collection, spec: AcquisitionSpec, profile: str,
                     stop: threading.Event | None = None,
                     priority: Priority = Priority.BACKFILL) -> dict:
    """Idempotent and resumable (CLAUDE.md invariant 2). Raises
    LicenseAckRequiredError / DiskPreflightError before any byte moves;
    per-file failures are isolated so one stalled file doesn't abort an
    otherwise-healthy batch, matching worker.py's per-document try/except.

    RESUMABLE UNIT IS THE BYTE, NOT THE FILE. Invariant 2's literal wording is
    "SIGTERM = finish current document, commit, exit 0", but a 55 GB dump
    cannot be finished on request, so acquisition aborts mid-file instead: the
    `.part` file plus a fetch_done=0 row are truthful, and the next run resumes
    from the `.part` file's size via a Range request. Nothing is lost and
    nothing is corrupt — the deviation is in granularity only.

    Every byte moved here draws from the shared budget (invariant 5) via
    app/acquire/budget.py; workers run under a bound priority so the scheduler
    can preempt them for interactive traffic.

    THREADING CONTRACT: this function's thread owns `con` exclusively. Worker
    threads do pure network/disk/CPU work and return records; every manifest
    write (and every db.log_event) happens here, on the caller's thread.
    """
    _require_license_ack(spec, collection)

    # The CLAIM covers the WHOLE pass, enumeration included, with a
    # keepalive so it cannot go stale under a long listing. It used to be
    # taken only around the transfer loop — so during gutenberg's first
    # real enumeration (~12 minutes, 2.8M entries) the Library card showed
    # a bare "fetched 0/0" with no fetching… badge (the badge reads the
    # claim) and a concurrent `corpus add` could have started a second
    # listing against the same origin.
    with claim_mod.acquisition_claim(con, spec.source_id):
        keepalive = _ClaimKeepalive(collection, spec.source_id)
        keepalive.start()
        try:
            return _run_acquisition_claimed(con, collection, spec, profile,
                                            stop, priority)
        finally:
            keepalive.stop()


class _ClaimKeepalive:
    """Refreshes the acquisition claim every minute from its own thread and
    connection (SQLite connections are thread-affine), so a pass whose
    current phase produces no _apply callbacks for >5 minutes — a first
    recursive listing runs ~12 — does not lose its claim mid-flight."""

    def __init__(self, collection, source_id: str) -> None:
        self._collection = collection
        self._source_id = source_id
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"acquire-claim-{source_id}")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._done.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        from .. import db as _db
        con = _db.connect(self._collection)
        try:
            last = time.time()
            while not self._done.wait(30):
                last = claim_mod.refresh_claim(con, self._source_id, last)
        finally:
            con.close()


def _run_acquisition_claimed(con, collection, spec: AcquisitionSpec, profile: str,
                              stop, priority) -> dict:
    # Mirrored sources: benchmark FIRST — it is the structure gate and it
    # decides which single mirror the (expensive) listing runs against.
    endpoints = list(spec.endpoints)
    if spec.probe_file and len(endpoints) > 1:
        try:
            mirrors.benchmark(con, spec)
        except Exception as e:   # noqa: BLE001 — a broken benchmark must not stop a fetch
            db.log_event(con, "warn", "acquire",
                         f"mirror benchmark failed ({e}); using last known order")
        endpoints = mirrors.seed_order(con, spec)

    files = _list_and_filter(spec, profile, getattr(collection, "snapshot", None),
                             endpoints=endpoints)
    plan = _plan_from_files(con, collection, spec, files)
    # The SOURCE's own file count, recorded the moment it is known. The
    # fetch loop below streams — fetch_files rows appear per file as each
    # is processed — so during a long pass done==total at every instant and
    # the Library card had no honest denominator ("1414/1414" while the
    # origin held 1,447). This is a cached observation, not desired state
    # (invariant 10): each pass overwrites it with what the origin said,
    # scoped to the profile actually being fetched.
    # DOCUMENT total, where the layout makes it knowable from the listing
    # alone: for one-dir-per-doc every distinct top directory is one
    # document, so "fetched" can be reported in the same unit parsed/
    # indexed use. Container layouts (pubmed's gz, wikipedia's dumps,
    # wikivec's parquet) cannot know document counts before unpacking —
    # the card says "compressed containers" instead of implying files are
    # documents.
    docs_total = None
    if spec.layout == "one-dir-per-doc":
        docs_total = len({f.rel_path.split("/", 1)[0]
                          for f in files if "/" in f.rel_path})
    db.set_control(con, LISTING_KEY, json.dumps({
        "files_total": plan.files_total, "bytes_total": plan.bytes_total,
        "docs_total": docs_total, "layout": spec.layout,
        "profile": profile, "ts": int(time.time())}))
    # Preflight against the spec's declared estimate (deliverable's literal
    # "disk preflight vs estimated_bytes") when the profile has one; falls
    # back to the precise listing-derived pending-bytes sum otherwise.
    declared = estimated_bytes_for(spec, profile)
    needed = plan.bytes_pending if declared is None else declared
    free = shutil.disk_usage(collection.corpus_dir).free
    if free < needed * _DISK_HEADROOM:
        raise DiskPreflightError(
            f"{spec.source_id}: need ~{needed} bytes free (profile {profile!r}), "
            f"only {free} available at {collection.corpus_dir} — refusing (never half-fill)")

    # The delta gate runs once, here, rather than per file inside the loop:
    # nothing between this point and fetch time mutates fetch_files except this
    # thread. (Cross-process overlap is prevented by the caller's claim; see
    # app/acquire/claim.py.)
    pending = {f.rel_path for f in files if _is_pending(con, spec, f)}
    skipped = len(files) - len(pending)
    items = _build_work_items(files, pending, spec)

    manager = get_manager()
    transport = REGISTRY[spec.transport]
    # seeded from the benchmark's persisted ranking (a restart starts with
    # the last winner); the manager's EWMA re-ranks live within the pass:
    # measured beats benchmarked beats declared
    ranked = manager.rank_endpoints([(e.url, e) for e in endpoints])
    counters = {"fetched": 0, "failed": 0, "aborted": 0, "adopted": 0}
    claim_ts = [time.time()]

    def _apply(results: list[_FileResult]) -> None:
        # Runs on THIS thread (see run_pool's contract), so touching `con` is
        # safe. Refreshing here keeps a long run's claim from going stale
        # under it without needing a separate timer thread.
        claim_ts[0] = claim_mod.refresh_claim(con, spec.source_id, claim_ts[0])
        for r in results:
            if r.status == "fetched":
                dest = collection.corpus_dir / r.remote.rel_path
                _upsert_fetched(con, spec.source_id, r.remote, r.endpoint_url, str(dest),
                                 r.size, r.digest, profile)
                # Per FILE, not per run: a 1,450-shard fetch would otherwise
                # produce its first measurement only when the whole thing
                # finished. Adopted files record nothing — no bytes crossed
                # the network, and counting them would report an infinite
                # download speed (stats.record_fetch also guards this).
                if not r.adopted:
                    stats.recorder().record_fetch(
                        collection.name, seconds=r.seconds, bytes_moved=r.moved)
            elif r.status == "aborted":
                _upsert_aborted(con, spec.source_id, r.remote, r.endpoint_url,
                                 r.bytes_now, profile)
            else:
                _upsert_failed(con, spec.source_id, r.remote, r.endpoint_url,
                                r.bytes_now, profile, r.error or "unknown error")
                if r.error and "sidecar mismatch" in r.error:
                    # A checksum failure can mean the DATA is bad — but also
                    # that the local SIDECAR is stale or corrupt, in which
                    # case a clean re-download "mismatches" forever (observed
                    # live: identical wrong digest on every retry). Invalidate
                    # BOTH sides so the next attempt re-fetches the pair.
                    dest = collection.corpus_dir / r.remote.rel_path
                    dest.unlink(missing_ok=True)
                    if spec.checksums.kind == "sidecar" and spec.checksums.sidecar_suffix:
                        sidecar_rel = r.remote.rel_path + spec.checksums.sidecar_suffix
                        (collection.corpus_dir / sidecar_rel).unlink(missing_ok=True)
                        con.execute(
                            "UPDATE fetch_files SET fetch_done=0, error=NULL "
                            "WHERE source_id=? AND rel_path=?",
                            (spec.source_id, sidecar_rel))
            counters[r.status] += 1
            if r.adopted:
                # counted INSIDE 'fetched' as well: an adopted file is
                # fetch_done=1 with a verified digest, indistinguishable
                # downstream from a downloaded one. This is the extra
                # detail of how it got there, not a fourth outcome.
                counters["adopted"] += 1

    workers = min(manager.platform.transfer.workers, max(1, len(items)))
    # the claim is already held by run_acquisition, covering the whole pass
    transfer_pool.run_pool(
        items,
        _make_worker(transport, collection, spec, manager, ranked, stop, priority),
        _apply, max_workers=workers, stop=stop)

    return {"fetched": counters["fetched"], "skipped": skipped,
            "failed": counters["failed"], "aborted": counters["aborted"],
            "adopted": counters["adopted"], "total": len(files)}

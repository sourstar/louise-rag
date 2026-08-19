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
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..plugins.base import LicenseClass
from ..transfer import pool as transfer_pool
from ..transfer.bucket import Priority
from ..transfer.manager import Direction, get_manager
from . import checksum
from . import claim as claim_mod
from .spec import (AcquisitionPlan, AcquisitionSpec, DiskPreflightError, Filter,
                    LicenseAckRequiredError, RemoteFile, TransferAborted, TransportError,
                    estimated_bytes_for, resolve_profile)
from .transports import REGISTRY

_DRY_RUN_ASSUMED_RATE_BPS = 5_000_000   # ~5 MB/s — a named placeholder; P0.7 has real measurement
_DISK_HEADROOM = 1.1                     # refuse unless free space clears the estimate by 10%


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


def _list_and_filter(spec: AcquisitionSpec, profile: str) -> list[RemoteFile]:
    # resolve_profile also understands a COMBINED profile ("tr,it") on an
    # additive source, where it is the union of those slices
    profile_filter = resolve_profile(spec, profile)
    transport = REGISTRY[spec.transport]
    all_files: list[RemoteFile] = []
    seen: set[str] = set()
    for endpoint in spec.endpoints:
        for f in transport.list_files(endpoint, spec):
            if f.rel_path not in seen:
                seen.add(f.rel_path)
                all_files.append(f)
    return _apply_filters(all_files, spec.filters, profile_filter)


def _plan_from_files(con, collection, spec: AcquisitionSpec, files: list[RemoteFile]) -> AcquisitionPlan:
    pending: list[RemoteFile] = []
    bytes_total = bytes_pending = 0
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
            bytes_pending += size
    return AcquisitionPlan(
        files_total=len(files), files_pending=len(pending),
        bytes_total=bytes_total, bytes_pending=bytes_pending,
        eta_seconds=round(bytes_pending / _DRY_RUN_ASSUMED_RATE_BPS, 1),
        license_state=_license_state(spec, collection),
    )


def plan_acquisition(con, collection, spec: AcquisitionSpec, profile: str) -> AcquisitionPlan:
    """Read-only: list, filter, gate-check (but never refuse) — the
    --dry-run / status half. Never raises on a missing license ack; reports
    license_state instead so an operator can see what's blocking them."""
    files = _list_and_filter(spec, profile)
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


def _fetch_one_file(transport, endpoint, f: RemoteFile, collection,
                     spec: AcquisitionSpec, manager, endpoint_key: str) -> _FileResult:
    dest = collection.corpus_dir / f.rel_path
    part = dest.with_name(dest.name + ".part")
    # Read at fetch time, never hoisted with the pending list: a file retried
    # within the same run would otherwise resume from a stale offset.
    resume_from = part.stat().st_size if part.is_file() else 0
    started = time.monotonic()
    try:
        size = transport.fetch_one(endpoint, f, dest, resume_from)
        # One streaming pass for both digests (md5 sidecar + blake2), instead
        # of reading the whole file into memory twice.
        digest = checksum.digest_and_verify(dest, spec.checksums)
        manager.record_throughput(endpoint_key, size - resume_from,
                                   time.monotonic() - started)
        return _FileResult(remote=f, endpoint_url=endpoint.url, status="fetched",
                            size=size, digest=digest)
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
                    results.append(_fetch_one_file(
                        transport, endpoint, item.data, collection, spec,
                        manager, endpoint_key))
        finally:
            sem.release()
        return results

    return _work


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

    files = _list_and_filter(spec, profile)
    plan = _plan_from_files(con, collection, spec, files)
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
    ranked = manager.rank_endpoints([(e.url, e) for e in spec.endpoints])
    counters = {"fetched": 0, "failed": 0, "aborted": 0}
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

    workers = min(manager.platform.transfer.workers, max(1, len(items)))
    with claim_mod.acquisition_claim(con, spec.source_id):
        transfer_pool.run_pool(
            items,
            _make_worker(transport, collection, spec, manager, ranked, stop, priority),
            _apply, max_workers=workers, stop=stop)

    return {"fetched": counters["fetched"], "skipped": skipped,
            "failed": counters["failed"], "aborted": counters["aborted"],
            "total": len(files)}

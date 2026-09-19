"""Amendment 38 — a remote genesis archive is FETCHED like a corpus.

The old path downloaded a shard only at apply time, on the embed thread,
and only once the whole collection had settled through rdf/fts — so for
the months a 40M-document corpus takes to index, the archive that had
"already paid the GPU-hours" contributed nothing (pubmed live: 0/37
shards, 44 documents embedded, 8.25M indexed).

This module is the archive's own acquisition stream. It runs on its own
thread beside the corpus fetch (worker._acquire_archive), walks the
declared shards in order and lands each one in the collection's genesis
cache, so that the importer (genesis.drain_next_shard) finds shards ON
DISK and applies them for whatever is indexed so far.

Rules, all doc-mandated:
- BUDGET (invariant 5): every byte draws from the central bucket under
  Priority.ARCHIVE — below BACKFILL, so the corpus (without which the
  vectors are useless) wins the link, above SEEDING.
- RESUMABLE (invariant 2): the transport's `.part` + Range discipline;
  SIGTERM parks in the bucket and leaves a truthful `.part`.
- BOUNDED: shards on disk that are not yet FINAL count against
  config.GENESIS_CACHE_BYTES, and a shard is fetched only when the disk
  has headroom for it — an archive never fills the disk ahead of the
  corpus (the engine's own preflight rule).
- LICENSE (invariant 9 / amendment 36): the collection's gate applies —
  a yellow-class collection without an acknowledgement fetches nothing.
- DEFERRAL IS NOT FAILURE: an unreachable origin marks the shard
  `skipped` (retried after RETRY_AFTER_S); a full cache or a full disk
  logs once and waits for the next cycle.
"""
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from .. import config, db, stats
from ..acquire.spec import TransferAborted
from ..transfer.bucket import Priority
from ..transfer.manager import Direction, get_manager
from .genesis import (ARCHIVES, GenesisArchive, GenesisUnavailable, _digest_files,
                      _https_fetcher, archive_stopped)

RETRY_AFTER_S = 600          # a skipped (unreachable) shard is retried after this
_DISK_HEADROOM = 1.2         # free bytes must exceed the shard estimate by this
# states whose files are (or are about to be) on disk and not yet final
RESIDENT_STATES = ("fetching", "fetched", "applying", "applied")


def cache_dir(collection, archive: GenesisArchive) -> Path:
    return collection.manifest_path.parent / "genesis" / archive.archive_id


def shard_files(archive: GenesisArchive, cache: Path, shard: str) -> list[tuple[str, Path]]:
    """(remote name, local path) for every file a shard consists of."""
    if archive.adapter == "medcpt-npy":
        return [(f"embeds_chunk_{shard}.npy", cache / f"embeds_chunk_{shard}.npy"),
                (f"pmids_chunk_{shard}.json", cache / f"pmids_chunk_{shard}.json")]
    return [(shard, cache / shard)]


def resident_bytes(con, archive: GenesisArchive, collection) -> int:
    """Bytes the archive's non-final shards hold (or will hold) on disk: a
    shard still fetching counts at the declared size hint, a landed one
    at the larger of its real size and the hint — IF ITS FILES ARE HERE.

    THE DEADLOCK THIS CLOSES. This used to count every row in a resident
    state from the ledger alone. `reopen_finals_for_range` turns a final
    shard back into `applied`, and a final shard's files were deleted when
    it finalised, so the reopened shard is due for a re-fetch
    (`next_shard_to_fetch`). But its row counted here at the full hint, so
    the fetcher saw the cache as full and fetched nothing, while the drain
    walks only shards on disk and so had nothing to apply. Meanwhile
    `_covered_ranges` kept the local encoder out of those ranges, because
    they belonged to the archive again. Measured on pubmed 2026-09-19: all
    37 shards reopened, "105 GiB of shards on disk" against a 24 GiB bound,
    an empty cache directory, and 38,324 documents that nothing could
    embed. The ledger and the disk disagreed, and this was the consumer
    that believed the ledger (`genesis._on_disk` has the history)."""
    total = 0
    for r in con.execute(
            "SELECT shard, state, bytes FROM genesis_imports WHERE archive=? "
            f"AND state IN ({','.join('?' * len(RESIDENT_STATES))})",
            (archive.archive_id, *RESIDENT_STATES)):
        if r["state"] != "fetching" and not _files_present(collection, archive, r["shard"]):
            continue            # nothing here: a re-fetch is what it waits for
        total += max(int(r["bytes"] or 0), archive.shard_bytes)
    return total


def next_shard_to_fetch(con, archive: GenesisArchive, now: int | None = None,
                        collection=None) -> str | None:
    """The first declared shard not yet on disk: no row, an interrupted
    fetch (resume), a retryable skip past its backoff, or an APPLIED shard
    that is due for a revisit and whose files are gone. None when a shard
    was REJECTED — a systematically incompatible archive must not burn 36
    more downloads proving itself 36 more times.

    THE LAST CASE CLOSES A LOOP THAT WAS OPEN. `_remote_drain_order` walks
    only shards that are on disk — the fix of 2026-09-11, after shards 8-12
    were selected every round with their files gone and the import ran at
    0 documents/s. That stopped the spin and left the shards stuck: the
    drain would not walk them without files, and this function would not
    re-fetch anything in state `applied`, so a remote shard whose files
    disappeared could never be revisited and never reach `final`.

    Measured on pubmed 2026-09-12: five shards `applied` with `pending`
    unknown and due, 188,323 documents inside the archive's own id range
    still unembedded, and no path by which the archive could deliver them.

    Whatever removed the files — a cache sweep, an operator, a disk — the
    ledger and the disk can disagree, and EVERY consumer of the pair has to
    handle it. This was the one that did not.
    """
    now = int(time.time()) if now is None else now
    rows = {r["shard"]: r for r in con.execute(
        "SELECT shard, state, updated_at, proven_at, docs, pending, next_pass_at "
        "FROM genesis_imports WHERE archive=?", (archive.archive_id,))}
    if archive_stopped(rows.values()):
        return None
    refetch: str | None = None
    for shard in archive.shards:
        row = rows.get(shard)
        if row is None or row["state"] == "fetching":
            return shard
        if row["state"] == "skipped" and now - int(row["updated_at"] or 0) >= RETRY_AFTER_S:
            return shard
        # AFTER the three above, never before: a shard that has never been
        # fetched is free vectors, and one being re-fetched to be finalised
        # is bookkeeping. Order follows the work.
        if refetch is None and collection is not None \
                and row["state"] in ("applied", "applying") \
                and (row["pending"] is None or row["pending"] > 0) \
                and int(row["next_pass_at"] or 0) <= now \
                and not _files_present(collection, archive, shard):
            refetch = shard
    return refetch


def _files_present(collection, archive: GenesisArchive, shard: str) -> bool:
    """`genesis._on_disk`, imported here rather than duplicated: one answer
    to "are this shard's files here", or the fetcher and the drain would
    disagree about it exactly as they did before."""
    from .genesis import _on_disk
    return _on_disk(collection, archive, shard)


class _PartSampler(threading.Thread):
    """Samples a growing `.part` file every few seconds and records the
    delta as fetched bytes for the collection — progress visible while a
    multi-GB shard streams, not only when it lands."""
    INTERVAL_S = 5.0

    def __init__(self, collection: str, part: Path, start_size: int) -> None:
        super().__init__(name=f"archive-sample-{collection}", daemon=True)
        self._collection = collection
        self._part = part
        self._seen = start_size
        self._done = threading.Event()
        self.sampled = 0
        self.last_at = time.time()

    def run(self) -> None:
        while not self._done.wait(self.INTERVAL_S):
            self._sample()

    def _sample(self) -> None:
        try:
            size = self._part.stat().st_size if self._part.is_file() else self._seen
        except OSError:
            return
        delta = size - self._seen
        if delta > 0:
            now = time.time()
            stats.recorder().record_fetch(self._collection,
                                          seconds=max(0.001, now - self.last_at),
                                          bytes_moved=delta)
            self._seen = size
            self.sampled += delta
            self.last_at = now

    def stop(self) -> int:
        self._done.set()
        self.join(timeout=2)
        return self.sampled


def fetch_shard(con, collection, archive: GenesisArchive, shard: str, *,
                fetch=None) -> dict | None:
    """Land one shard's files in the cache and record it `fetched` (with
    its size, digest and declared id range). Returns None on a retryable
    failure (recorded as `skipped`) or an abort (row left `fetching`, the
    `.part` file resumes next time)."""
    cache = cache_dir(collection, archive)
    cache.mkdir(parents=True, exist_ok=True)
    files = shard_files(archive, cache, shard)
    now = int(time.time())
    con.execute(
        "INSERT INTO genesis_imports(archive,shard,source_url,state,updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(archive,shard) DO UPDATE SET "
        "state=excluded.state, updated_at=excluded.updated_at "
        "WHERE genesis_imports.state IN ('skipped','fetching')",
        (archive.archive_id, shard, archive.base_url, "fetching", now))
    fetch = fetch or _https_fetcher(archive)
    t0 = time.time()
    moved = 0
    try:
        for name, path in files:
            if path.is_file():
                continue
            part = path.with_name(path.name + ".part")
            before = part.stat().st_size if part.is_file() else 0
            # a 3 GB file takes minutes: the Network page's per-collection
            # split reads the fetch recorder, so progress is sampled from
            # the growing .part while the transport streams it, and only
            # the unsampled remainder is recorded at the end
            sampler = _PartSampler(collection.name, part, before)
            sampler.start()
            try:
                fetch(name, path)
            finally:
                sampled = sampler.stop()
            landed = path.stat().st_size if path.is_file() else \
                (part.stat().st_size if part.is_file() else before)
            remainder = max(0, landed - before - sampled)
            if remainder:
                stats.recorder().record_fetch(collection.name,
                                              seconds=max(0.001, time.time() - sampler.last_at),
                                              bytes_moved=remainder)
            moved += max(0, landed - before)
    except TransferAborted:
        return None                     # stop requested: resume next time
    except GenesisUnavailable as e:
        con.execute(
            "UPDATE genesis_imports SET state='skipped', detail=?, updated_at=? "
            "WHERE archive=? AND shard=?",
            (f"retryable: {e}"[:500], int(time.time()), archive.archive_id, shard))
        db.log_event(con, "warn", "genesis",
                     f"{archive.archive_id}/{shard}: fetch deferred — {e}"[:400])
        return None
    seconds = time.time() - t0
    paths = [p for _, p in files]
    size = sum(p.stat().st_size for p in paths)
    digest = _digest_files(paths)
    id_range = archive.id_range_of(shard)
    # A SHARD RE-FETCHED FOR A REVISIT KEEPS ITS STATE. `applied` says what
    # this peer has already imported from it; overwriting that with
    # `fetched` would put the shard in `_remote_drain_order`'s
    # fetched-never-applied list and re-import the whole of it — correct,
    # because the import is idempotent, and wasteful by the width of a
    # shard (a million documents re-written to deliver the thousands that
    # are actually new). `pending=NULL` and `next_pass_at=0` make it due
    # either way, which is the whole point of bringing the files back.
    con.execute(
        "UPDATE genesis_imports SET "
        "state=CASE WHEN state IN ('applied','applying') THEN state "
        "ELSE 'fetched' END, "
        "bytes=?, content_digest=?, "
        "id_min=?, id_max=?, pending=NULL, next_pass_at=0, detail=NULL, updated_at=? "
        "WHERE archive=? AND shard=?",
        (size, digest, id_range[0] if id_range else None,
         id_range[1] if id_range else None, int(time.time()),
         archive.archive_id, shard))
    db.log_event(con, "info", "genesis",
                 f"{archive.archive_id}/{shard}: fetched {size >> 20} MiB"
                 + (f" in {seconds:.0f}s" if moved else " (already on disk)"))
    return {"shard": shard, "bytes": size, "moved": moved, "seconds": seconds}


def run_fetcher(con, collection, *, stop=None, fetch=None, log_state: dict | None = None) -> dict:
    """One fetch pass: land shards in order until nothing is pending, the
    cache bound or the disk says wait, the origin is unreachable, or stop
    fires. The worker restarts it with every discovery cycle, like the
    corpus fetch. `log_state` (per collection, held by the caller) keeps
    the "cache full" / "disk full" lines to one per condition change."""
    archive = ARCHIVES.get(collection.genesis_archive or "")
    if archive is None or archive.local:
        return {"fetched": [], "reason": "no remote archive"}
    _require_ack(collection)
    manager = get_manager()
    fetched: list[str] = []
    reason = "nothing pending"
    log_state = {} if log_state is None else log_state
    while stop is None or not stop.is_set():
        shard = next_shard_to_fetch(con, archive, collection=collection)
        if shard is None:
            break
        resident = resident_bytes(con, archive, collection)
        if resident + archive.shard_bytes > config.GENESIS_CACHE_BYTES:
            reason = "cache full"
            if log_state.get("cache") != resident:
                db.log_event(con, "info", "genesis",
                             f"{archive.archive_id}: {resident >> 30} GiB of shards on disk "
                             f"waiting to finalise — next fetch when one is final "
                             f"(GENESIS_CACHE_BYTES={config.GENESIS_CACHE_BYTES >> 30} GiB)")
                log_state["cache"] = resident
            break
        cache = cache_dir(collection, archive)
        cache.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(cache).free
        if free < archive.shard_bytes * _DISK_HEADROOM:
            reason = "disk"
            if not log_state.get("disk"):
                db.log_event(con, "error", "genesis",
                             f"{archive.archive_id}: {free >> 30} GiB free at {cache}, "
                             f"a shard needs ~{archive.shard_bytes >> 30} GiB — waiting")
                log_state["disk"] = True
            break
        log_state.pop("disk", None)
        key = urlparse(archive.base_url).netloc or archive.base_url
        with manager.bind(Priority.ARCHIVE, Direction.DOWN, stop=stop, endpoint_key=key):
            result = fetch_shard(con, collection, archive, shard, fetch=fetch)
        if result is None:
            reason = "deferred"
            break
        fetched.append(shard)
    if stop is not None and stop.is_set():
        reason = "stopped"
    return {"fetched": fetched, "reason": reason}


def _require_ack(collection) -> None:
    """Amendment 36: the collection's license gate covers the archive
    too — vectors are USE of the corpus they describe. Same check, same
    error, as the corpus fetch (engine._require_license_ack validates the
    recorded ack against the byte-exact terms text)."""
    from .. import licensing
    licensing.require_ack(collection, use="the archive is fetched")

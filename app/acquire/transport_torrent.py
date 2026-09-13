"""TORRENT transport — BitTorrent v2 via libtorrent (architecture-v2.md §5).

Fills the same Transport contract the other four satisfy: list_files()
enumerates a torrent's file tree, fetch_one() downloads exactly one file out
of it. Everything above this module — filters, profiles, the delta gate,
checksums, adoption, the manifest — is unchanged and unaware.

WHY BITTORRENT v2 (BEP 52). libtorrent 2.x speaks it natively, and the part
that matters here is a merkle tree PER FILE rather than one piece chain
across the whole torrent: files become independently verifiable, and
identical files deduplicate across torrents. That is the property a
maintained corpus needs, because consecutive snapshots share everything that
did not change. A v1 torrent still works; it only loses that.

WHERE THE BYTES LAND, and why not the obvious way. libtorrent writes into
its save_path as pieces arrive, so a file sits at its FINAL name while still
full of holes. app/discovery.py's (mtime, size) -> hash gate would ingest
that as a complete corpus file — the same class of bug that made 21,018
Gutenberg stubs look like documents. Every other transport avoids it with
`.part` + os.replace, so this one does too: each file is renamed INSIDE the
torrent to `<name>.part` (handle.rename_file) before any data is requested,
and promoted with os.replace when complete. No staging directory, no second
full-size copy of a 55 GB file, one filesystem throughout, and no plugin has
to learn to skip anything.

METADATA WITHOUT A DOWNLOAD. A .torrent file is parsed directly — no
session, no save path, no network. A magnet is only a promise of metadata,
so it is resolved through a short-lived paused torrent that is removed
again immediately: the real add, with the real save path, happens in
fetch_one. Adding one infohash twice to a session is an error, so the two
must not overlap.

RATE LIMITING (invariant 5). libtorrent runs its own threads and its own
limiter and cannot be metered per byte — exactly rsync's shape, and it takes
exactly rsync's treatment: a lease that withdraws refill rate from the
central bucket and hands it to the engine as a ceiling. A lease, never a
bypass.

SEEDING. The session is process-global and outlives any single fetch, so a
completed torrent keeps seeding for as long as the worker runs. That is the
point of using BitTorrent rather than HTTP: the node gives back what it
took.

libtorrent is an OPTIONAL dependency, imported lazily, so a deployment with
no torrent-backed collection never loads it.
"""
import os
import threading
import time
from pathlib import Path
from typing import Iterator

from .spec import AcquisitionSpec, Endpoint, RemoteFile, TransferAborted, TransportError

# A magnet carries no file list, so nothing can be planned — not even the
# sizes the disk preflight reads — until the swarm hands metadata over.
METADATA_TIMEOUT_S = 120.0
_POLL_S = 0.5
# A lease is a coarse instrument; re-deriving it every poll would spend more
# time in the manager's lock than in the download.
_RATE_REFRESH_S = 5.0
_PART_SUFFIX = ".part"


def _lt():
    """The libtorrent module, or a TransportError naming the fix.

    Imported here rather than at module scope so that importing the
    transport registry — which every collection does — never depends on a
    package most deployments do not need.
    """
    try:
        import libtorrent
    except ImportError as e:
        raise TransportError(
            "the torrent transport needs libtorrent, which is not installed. "
            "Add `libtorrent` to requirements.txt and rebuild the image (a "
            "prebuilt manylinux wheel exists for CPython 3.12, so this costs "
            "no compilation)") from e
    return libtorrent


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------- the process-global session ----------------
#
# ONE session for the process, not one per fetch: a torrent that finishes
# downloading keeps SEEDING while the session lives, and tearing it down
# between files would make this node a pure leech.

_session = None
_session_lock = threading.Lock()
_handles: dict[str, object] = {}
# Which file indices we have already staged as `.part`, per torrent. Kept
# HERE because it cannot be read back from libtorrent: torrent_file() is
# immutable metadata and does not reflect rename_file(). We know what we
# renamed because we are the ones who renamed it.
_staged: dict[str, set[int]] = {}


def session_settings() -> dict:
    return {
        "listen_interfaces": os.getenv("TORRENT_LISTEN", "0.0.0.0:6881,[::]:6881"),
        # Storage alerts only: rename_file() is ASYNCHRONOUS and its
        # confirmation arrives as an alert. Without them there is no way to
        # know the `.part` names took effect before data starts landing.
        "alert_mask": 0,   # replaced below once libtorrent is imported
        # DHT and local discovery are the peer-finding mechanisms; both are
        # switchable because a private swarm wants neither (see
        # docs/notes/p2p-discovery.md).
        "enable_dht": _flag("TORRENT_DHT", True),
        "enable_lsd": _flag("TORRENT_LSD", True),
        # Port mapping stays OFF by default: punching a hole in someone's
        # router is not a thing software should do without being asked.
        "enable_upnp": _flag("TORRENT_UPNP", False),
        "enable_natpmp": _flag("TORRENT_NATPMP", False),
        # Identify ourselves the way the HTTPS transport does. Some trackers
        # refuse unknown clients; all of them log.
        "user_agent": "louise-rag/0.1 libtorrent",
    }


def get_session():
    global _session
    with _session_lock:
        if _session is None:
            lt = _lt()
            settings = session_settings()
            settings["alert_mask"] = int(lt.alert.category_t.storage_notification)
            _session = lt.session(settings)
        return _session


def shutdown_session() -> None:
    """Stop seeding and release the listen sockets. Safe when no session was
    ever created; called on worker shutdown."""
    global _session
    with _session_lock:
        _handles.clear()
        _staged.clear()
        _session = None


def _is_magnet(url: str) -> bool:
    return url.strip().startswith("magnet:")


def _strip_part(name: str) -> str:
    return name[: -len(_PART_SUFFIX)] if name.endswith(_PART_SUFFIX) else name


def _is_pad(fs, index: int) -> bool:
    """BitTorrent pads each real file up to a piece boundary with a synthetic
    `.pad/NNNN` entry — that alignment is exactly what lets two torrents
    share the pieces of an identical file, so v2 torrents are full of them.

    They are not content. Left in, they would become fetch_files rows, get
    `.part`-renamed, and land in the corpus as junk files for the plugins to
    trip over. libtorrent flags them, so there is no need to guess from the
    name.
    """
    try:
        return bool(fs.file_flags(index) & _lt().file_storage.flag_pad_file)
    except Exception:   # noqa: BLE001 — a binding without the flag: fall back
        return fs.file_path(index).startswith(".pad/") or "/.pad/" in fs.file_path(index)


def content_files(info) -> list[tuple[int, str, int]]:
    """(torrent index, LOGICAL path, size) for the real files only.

    `torrent_file()` returns immutable metadata: it never reflects a
    rename_file(), which lives in the storage layer. So the logical name is
    simply what the torrent says, and the stored `.part` name is ours to
    compute — reading it back was the bug that made the first version
    silently look for a file that libtorrent had never been asked to write
    under that name.
    """
    fs = info.files()
    return [(i, _strip_part(fs.file_path(i)), fs.file_size(i))
            for i in range(fs.num_files()) if not _is_pad(fs, i)]


def part_name(logical: str) -> str:
    return logical + _PART_SUFFIX


def _paths_and_sizes(info) -> list[tuple[str, int]]:
    return [(name, size) for _, name, size in content_files(info)]


def torrent_info(endpoint: Endpoint, stop=None):
    """Metadata for this endpoint, without downloading any content.

    A .torrent file is parsed outright. A magnet is resolved through a
    paused torrent that is removed as soon as metadata arrives, so that
    fetch_one can add the same infohash for real — a session rejects the
    same torrent twice.
    """
    lt = _lt()
    url = endpoint.url.strip()
    if not _is_magnet(url):
        path = Path(url)
        if not path.is_file():
            raise TransportError(
                f"{url}: not a magnet URI and not a readable .torrent file")
        return lt.torrent_info(str(path))

    params = lt.parse_magnet_uri(url)
    # Nothing is written: the torrent is added paused and removed before it
    # is ever resumed. The path only has to exist.
    params.save_path = os.getenv("TORRENT_METADATA_DIR", "/tmp")
    params.flags |= lt.torrent_flags.upload_mode      # never send data either
    session = get_session()
    handle = session.add_torrent(params)
    try:
        _await_metadata(handle, stop)
        return handle.torrent_file()
    finally:
        try:
            session.remove_torrent(handle)
        except Exception:   # noqa: BLE001 — cleanup must not mask the result
            pass


def _await_metadata(handle, stop=None) -> None:
    deadline = time.monotonic() + METADATA_TIMEOUT_S
    while not handle.status().has_metadata:
        if stop is not None and stop.is_set():
            raise TransferAborted("stopped while waiting for torrent metadata")
        if time.monotonic() > deadline:
            raise TransportError(
                f"no torrent metadata after {METADATA_TIMEOUT_S:.0f}s — the "
                f"magnet found no peer holding it")
        time.sleep(_POLL_S)


def save_root(dest: Path, rel_path: str) -> Path:
    """The directory a torrent's own relative paths resolve against to
    produce the corpus layout.

    dest is corpus_dir/rel_path and rel_path is the torrent-relative name,
    so removing one from the other leaves the root — which means a completed
    file is already exactly where the platform expects it and needs only its
    `.part` suffix removed.
    """
    root = Path(dest)
    for _ in Path(rel_path).parts:
        root = root.parent
    return root


# Bytes libtorrent pulled per torrent, already credited to the budget.
# libtorrent moves bytes on its own threads, so nothing here calls
# acquire_bytes() per chunk; the rate lease PACES it, and this credits what
# arrived as external bytes (invariant 5's accounting half — the same
# remedy as rsync's). Per HANDLE, under the session lock: several engine
# workers may poll the same torrent for different files, and each delta
# must be counted exactly once. Observed live: a peer pulled 2.5 GB of
# wikivec while the Traffic panel read 0.
_accounted: dict[str, int] = {}


def _credit_download(key: str, handle) -> None:
    try:
        total = int(handle.status().total_download)
    except Exception:   # noqa: BLE001 — a dead handle credits nothing
        return
    with _session_lock:
        last = _accounted.get(key, 0)
        delta = max(0, total - last)
        if total > last:
            _accounted[key] = total
    if delta:
        from . import budget
        budget.note_external_bytes(delta)


def _add_for_download(endpoint: Endpoint, save_path: Path):
    """The download handle for this torrent, created once per process and
    keyed on the torrent alone — a session refuses the same infohash twice,
    so the save path must not be part of the identity."""
    lt = _lt()
    key = endpoint.url.strip()
    with _session_lock:
        existing = _handles.get(key)
    if existing is not None:
        return existing

    save_path.mkdir(parents=True, exist_ok=True)
    if _is_magnet(key):
        params = lt.parse_magnet_uri(key)
    else:
        params = lt.add_torrent_params()
        params.ti = lt.torrent_info(key)
    params.save_path = str(save_path)
    # Added with nothing selected: priorities are set per file below, so a
    # torrent is never allowed to pull bytes no profile asked for.
    try:
        params.flags |= lt.torrent_flags.paused
    except Exception:   # noqa: BLE001 — older bindings name flags differently
        pass

    handle = get_session().add_torrent(params)
    with _session_lock:
        _handles[key] = handle
    return handle


RENAME_TIMEOUT_S = 30.0


def _rename_to_parts(handle, key: str, indices=None,
                     timeout: float = RENAME_TIMEOUT_S) -> None:
    """Give the requested files a `.part` name inside the torrent, and WAIT
    for libtorrent to confirm it.

    rename_file() is asynchronous. Issuing it and resuming immediately is a
    race against the disk thread, and losing that race means pieces land at
    the real path — the one thing this whole arrangement exists to prevent.
    The confirmation arrives as file_renamed_alert, so the honest thing is
    to wait for it rather than to hope.
    """
    info = handle.torrent_file()
    done = _staged.setdefault(key, set())
    wanted = {}
    for i, logical, _ in content_files(info):
        if indices is not None and i not in indices:
            continue
        if i in done:
            continue        # staged earlier in this process
        wanted[i] = part_name(logical)
        handle.rename_file(i, wanted[i])
    if not wanted:
        return

    session = get_session()
    lt = _lt()
    deadline = time.monotonic() + timeout
    while wanted and time.monotonic() < deadline:
        for alert in session.pop_alerts():
            if isinstance(alert, lt.file_renamed_alert):
                if wanted.pop(alert.index, None) is not None:
                    done.add(alert.index)
            elif isinstance(alert, lt.file_rename_failed_alert):
                raise TransportError(
                    f"could not stage file {alert.index} as .part: {alert.error}")
        if wanted:
            time.sleep(0.05)
    if wanted:
        raise TransportError(
            f"libtorrent did not confirm .part staging for {sorted(wanted)} "
            f"within {timeout:.0f}s; refusing to download into final paths")


def _index_of(info, rel_path: str) -> int:
    """The TORRENT's file index, which is not the position in the filtered
    list: pad files occupy indices too, and prioritize_files/file_progress
    are indexed by the torrent, not by us."""
    for index, name, _ in content_files(info):
        if name == rel_path:
            return index
    raise TransportError(f"{rel_path}: no such file in the torrent")


def _apply_rate(handle, leased_bps) -> None:
    """Hand the lease to libtorrent's own limiter. None means the central
    budget is unlimited, and so is this."""
    try:
        handle.set_download_limit(-1 if leased_bps is None else max(1, int(leased_bps)))
    except Exception:   # noqa: BLE001 — a limiter we cannot set must not stop the fetch
        pass


def _save_resume(handle) -> None:
    try:
        handle.save_resume_data()
    except Exception:   # noqa: BLE001 — resume is an optimisation, never a requirement
        pass


def _current_stop():
    """The stop event the transfer manager bound for this thread — the same
    mechanism the rsync transport uses to notice SIGTERM."""
    try:
        from ..transfer.manager import current_handle
        handle = current_handle()
        return handle.stop if handle is not None else None
    except Exception:   # noqa: BLE001
        return None


class TorrentTransport:
    # This transport gives bytes back as well as taking them, which is what
    # distinguishes it from every other entry in the registry.
    seeds = True

    def list_files(self, endpoint: Endpoint, spec: AcquisitionSpec) -> Iterator[RemoteFile]:
        """The torrent's file tree, with real sizes from its metadata.

        Sizes are never guessed here, which is the opposite of the HTTPS
        transport's failure mode: there an undeclared size silently disabled
        adoption and reported a 983 MB assembly as 0 bytes. A torrent always
        knows how big its files are.
        """
        info = torrent_info(endpoint)
        for name, size in _paths_and_sizes(info):
            yield RemoteFile(rel_path=name, size=size)

    def fetch_one(self, endpoint: Endpoint, remote: RemoteFile, dest: Path,
                  resume_from: int) -> int:
        """Download exactly one file out of the torrent.

        `resume_from` is accepted for Protocol uniformity and ignored, as it
        is for rsync: BitTorrent resumes by piece, not by byte offset, and it
        resumes from whatever the `.part` file already holds — which it
        re-checks rather than trusts.
        """
        from ..transfer.manager import get_manager

        stop = _current_stop()
        root = save_root(dest, remote.rel_path)
        handle = _add_for_download(endpoint, root)
        _await_metadata(handle, stop)

        info = handle.torrent_file()
        index = _index_of(info, remote.rel_path)
        fs = info.files()
        total = fs.file_size(index)
        # Stage only the file we are about to pull: renaming the whole
        # torrent would touch files no profile asked for.
        _rename_to_parts(handle, endpoint.url.strip(), indices={index})

        # Only the requested file. The engine's unit of work is one file and
        # one manifest row; pulling the whole torrent would fetch bytes no
        # profile asked for and record none of them.
        # One entry per TORRENT file (pads included: libtorrent expects the
        # full vector), everything off except the one file asked for.
        priorities = [0] * fs.num_files()
        priorities[index] = 4
        handle.prioritize_files(priorities)
        handle.resume()

        last_refresh = 0.0
        key = endpoint.url.strip()
        with get_manager().rsync_lease(endpoint.url) as leased_bps:
            while True:
                done = handle.file_progress()[index]
                _credit_download(key, handle)
                if done >= total:
                    break
                if stop is not None and stop.is_set():
                    # TURN THE FILE OFF BEFORE LEAVING. This thread exits on
                    # the raise below, but the handle stays in the session
                    # (it seeds what it holds) and was left resumed with THIS
                    # file at priority 4 — so libtorrent went on downloading
                    # it after the abort, at the last leased rate, OUTSIDE
                    # any lease and credited to nothing (invariant 5): bytes
                    # arriving while the Network page read 0 down, on a node
                    # that had just been told to pause or go seed-only.
                    # Found by adversarial review of the pause fix, 2026-09-13.
                    #
                    # The file's priority to 0, NOT `handle.pause()`: pausing
                    # the handle would also stop its uploads, which is the
                    # opposite of what seed-only exists to keep running.
                    # Then credit once more, for whatever landed between the
                    # loop's last credit and this check.
                    priorities[index] = 0
                    handle.prioritize_files(priorities)
                    _credit_download(key, handle)
                    _save_resume(handle)
                    raise TransferAborted(
                        f"stopped with {done}/{total} bytes of {remote.rel_path}")
                # libtorrent's error_code is a Python OBJECT and therefore
                # always truthy, error or not — testing it directly aborted
                # every healthy download. Only its value says anything.
                error = getattr(handle.status(), "errc", None)
                if error is not None and getattr(error, "value", lambda: 0)():
                    raise TransportError(
                        f"{remote.rel_path}: {error.message()}")
                now = time.monotonic()
                if now - last_refresh > _RATE_REFRESH_S:
                    _apply_rate(handle, leased_bps)
                    last_refresh = now
                time.sleep(_POLL_S)

        # Computed, never read back: torrent_file() is immutable metadata
        # and does not know about the rename.
        part = Path(handle.status().save_path) / part_name(remote.rel_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if part != dest:
            os.replace(part, dest)
        _save_resume(handle)
        # The torrent stays in the session and keeps seeding what it holds.
        return total

"""Worker-managed torrent seeding, with UPLOAD ACCOUNTING.

The node's peer plane gives bytes back as well as taking them — but until
this module existed nothing did the giving on the worker's own: `torrent
publish` wrote a .torrent, `seed()` existed as a CLI convenience, and no
seeder ever reported a byte into the transfer manager's UP bucket
(observed on the Network page: a "seeding / up" column that could only
ever read 0 — an invariant-5 violation in waiting, the same class as the
rsync download accounting once was).

Now:
- `torrent publish` records the published torrent per collection (control
  key TORRENT_PUBLISHED_KEY — a catalogue entry: what to seed and from
  where). Desired state? No: an observation of what this node built; the
  .torrent file itself is the artifact. Deleting the file stops seeding.
- The worker's telemetry thread calls `tick()` every few seconds: it
  starts seeding every published torrent whose file exists, pauses/resumes
  them with the worker state (seeding runs under run everything,
  download/seeding only and seeding only; pause stops it), and ACCOUNTS
  the bytes uploaded since the last tick into the UP bucket under
  Priority.SEEDING — libtorrent's per-torrent total_upload is the
  authoritative counter, sampled as deltas.
- It publishes the seeding table (SEEDING_TABLE_KEY on the control
  collection) for the Network page: torrent, size, peers, uploaded.

Everything here is best-effort telemetry around a libtorrent session; a
failure logs and never stops the worker.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .. import db, node_state
from ..transfer.bucket import Priority
from ..transfer.manager import Direction, get_manager

TORRENT_PUBLISHED_KEY = "torrent_published"    # per collection
SEEDING_TABLE_KEY = "seeding_table"            # on the control collection
#: the modes that seed — read off the one table of what a mode runs
#: (app/node_state.py: MODE_RUNS), never a second list: "pause" seeds nothing
SEEDING_STATES = tuple(state for state in db.WORKER_STATES
                       if node_state.runs(state, node_state.SEED))


def record_published(con, entry: dict) -> None:
    """Called by `torrent publish`: what this collection's seeder should
    serve. `entry` is PublishedTorrent.as_catalog_entry() plus
    torrent_path and root (the directory that was hashed).

    SEVERAL torrents per collection (STATUS 5h — one per language of the
    pre-embedded corpus): the record is a list keyed on info_hash; a
    re-publish of the same torrent replaces its entry, a new one joins."""
    entries = [e for e in read_published(con)
               if e.get("info_hash") != entry.get("info_hash")
               and e.get("torrent_path") != entry.get("torrent_path")]
    entries.append(entry)
    db.set_control(con, TORRENT_PUBLISHED_KEY, json.dumps(entries))


def read_published(con) -> list[dict]:
    """Every torrent this collection has published (older records held a
    single dict — read as a one-entry list)."""
    raw = db.get_control(con, TORRENT_PUBLISHED_KEY)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    entries = value if isinstance(value, list) else [value]
    return [e for e in entries if isinstance(e, dict) and e.get("torrent_path")]


class Seeder:
    """One per worker process. Thread-safe; libtorrent handles are."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles: dict[str, object] = {}       # info_hash -> handle
        self._meta: dict[str, dict] = {}            # info_hash -> entry
        self._uploaded: dict[str, int] = {}         # info_hash -> last total_upload seen
        self._paused = False
        self._upload_limit: int | None = None       # what the session was last told

    def limit_upload(self, session) -> int:
        """The session's upload ceiling set to the UP budget's (invariant 5):
        libtorrent meters every byte it uploads against it — the seeds' and
        a fetch's handles' alike — and 0 is unlimited. `account` only counts
        what went out after the fact, and nothing ever slowed it: an
        `up_max` in platform.toml capped no seed (2026-10-19). Told only
        when the ceiling moves. Returns the limit in force."""
        from ..transfer.manager import get_manager
        manager = get_manager()
        manager.maybe_refresh()
        rate = manager.bucket(Direction.UP).core.rate
        limit = 0 if rate is None else max(1, int(rate))
        if limit != self._upload_limit:
            session.apply_settings({"upload_rate_limit": limit})
            self._upload_limit = limit
        return limit

    # ---------------- lifecycle ----------------

    def ensure(self, collection, entry: dict) -> bool:
        """Seed `entry` if not already; False when the .torrent is gone
        (the operator deleted it — that IS how seeding is stopped)."""
        torrent_path = Path(entry["torrent_path"])
        if not torrent_path.is_file():
            return False
        key = entry.get("info_hash") or str(torrent_path)
        with self._lock:
            if key in self._handles:
                return True
        from .torrent_publish import seed
        root = Path(entry.get("root") or collection.corpus_dir)
        # a SHARD member (app/share/seeder.py's entries name one) is found
        # under its snapshot's keys and dialled by address, never announced
        # by its own infohash: the DHT costs per collection, not per part
        handle = seed(torrent_path, root, announce="member" not in entry)
        with self._lock:
            self._handles[key] = handle
            self._meta[key] = {**entry, "collection": collection.name}
            self._uploaded.setdefault(key, 0)
            if self._paused:
                handle.pause()
        return True

    def retain(self, live_names: set) -> list[str]:
        """Stop seeding anything belonging to a collection this node no
        longer holds. Returns the names dropped.

        REMOVING, not pausing. `set_paused` is a single node-wide flag that
        resumes every handle the moment the worker state allows it, so a
        paused-but-retained handle silently starts uploading again. And
        deleting the `.torrent` is not a stop either: `ensure` consults the
        file only for handles it has not started, so a handle already in
        `_handles` keeps announcing regardless.

        Without this, retiring a collection stopped its INGESTION while the
        node went on announcing it on the DHT and serving its bytes for the
        life of the process — including, for a yellow-class source, after
        the acknowledgement that permitted it had gone with its file
        (invariant 9: not discovered, not imported, NOT EXCHANGED).
        """
        with self._lock:
            doomed = [k for k, m in self._meta.items()
                      if m.get("collection") not in live_names]
            handles = [(k, self._handles.get(k)) for k in doomed]
            for k in doomed:
                self._handles.pop(k, None)
                self._meta.pop(k, None)
                self._uploaded.pop(k, None)
        dropped = []
        for key, handle in handles:
            if handle is None:
                continue
            try:
                from .transport_torrent import get_session
                get_session().remove_torrent(handle)
            except Exception:   # noqa: BLE001 — a dead handle must not stop the others
                pass
            dropped.append(key)
        return dropped

    def drop(self, info_hashes) -> int:
        """Stop serving these torrents, whatever their collection — `share
        <collection> --stop` and a snapshot superseded (app/share/seeder.py).
        Removing, for the reason `retain` gives. How many were dropped."""
        from .transport_torrent import get_session
        n = 0
        with self._lock:
            for key in list(info_hashes):
                handle = self._handles.pop(key, None)
                if handle is None:
                    continue
                self._meta.pop(key, None)
                self._uploaded.pop(key, None)
                try:
                    get_session().remove_torrent(handle)
                except Exception:   # noqa: BLE001 — a dead handle is dropped all the same
                    pass
                n += 1
        return n

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            if paused == self._paused:
                return
            self._paused = paused
            handles = list(self._handles.values())
        for h in handles:
            try:
                (h.pause if paused else h.resume)()
            except Exception:   # noqa: BLE001 — a dead handle must not stop the others
                continue

    # ---------------- accounting ----------------

    def account(self) -> int:
        """Bytes uploaded since the last call, credited to the UP bucket
        under SEEDING. Returns the delta (tests read it)."""
        with self._lock:
            items = list(self._handles.items())
        total_delta = 0
        for key, handle in items:
            try:
                now_total = int(handle.status().total_upload)
            except Exception:   # noqa: BLE001
                continue
            with self._lock:
                delta = max(0, now_total - self._uploaded.get(key, 0))
                self._uploaded[key] = now_total
            if delta:
                get_manager().bucket(Direction.UP).note_granted(delta, Priority.SEEDING)
                total_delta += delta
        return total_delta

    def table(self) -> list[dict]:
        with self._lock:
            items = list(self._handles.items())
        rows = []
        for key, handle in items:
            meta = self._meta.get(key, {})
            try:
                st = handle.status()
                rows.append({
                    "collection": meta.get("collection"),
                    "name": meta.get("name"),
                    "info_hash": key,
                    "files": meta.get("files"),
                    "bytes": meta.get("bytes"),
                    "uploaded": int(st.total_upload),
                    "peers": int(st.num_peers),
                    "seeding": bool(st.is_seeding),
                    "checking": bool(getattr(st, "state", None) in (1, 2)),
                    "paused": self._paused,
                    # torrent COLLECTIONS (STATUS 5l): the page groups the
                    # members under their collection and offers its magnet
                    "kind": meta.get("kind") or "corpus",
                    "slice": meta.get("slice"),
                    "magnet": meta.get("magnet"),
                    "peer_hint": meta.get("peer_hint") or "",
                    "members": meta.get("members") or [],
                })
            except Exception:   # noqa: BLE001
                rows.append({"collection": meta.get("collection"),
                             "name": meta.get("name"), "info_hash": key,
                             "error": "no status"})
        return rows


_seeder: Seeder | None = None
_seeder_lock = threading.Lock()


def get_seeder() -> Seeder:
    global _seeder
    with _seeder_lock:
        if _seeder is None:
            _seeder = Seeder()
        return _seeder


def tick(collections, cons: dict, control_con, worker_state: str) -> None:
    """One telemetry beat: start what should be seeding, gate on the worker
    state, account uploads, publish the table. Never raises."""
    seeder = get_seeder()
    try:
        # the annotations collections held are served like any (02 §2.10):
        # left out here, `retain` below would stop what `share.seeder.sync`
        # just published for them
        from app.share import seeder as share_seeder
        collections, cons = share_seeder.with_annotations(collections, cons)
        seeder.set_paused(worker_state not in SEEDING_STATES)
        # Stop serving what this node no longer holds, BEFORE starting what
        # it does. `collections` is the live list, so a collection whose
        # file was retired drops out here rather than seeding on until the
        # process exits.
        # Invariant 9 says a yellow/red collection without an ack is "not
        # discovered, not imported, not exchanged". Acquisition, scan and
        # archive were gated; the OUTBOUND path never consulted
        # app/licensing.py at all, so this peer went on serving a corpus
        # whose terms it had not accepted. Withheld collections are
        # excluded from the live set, which both refuses to start them and
        # — via retain — stops any handle already running.
        from .. import licensing
        allowed = set()
        for c in collections:
            try:
                if licensing.state_for(c) != licensing.MISSING:
                    allowed.add(c.name)
            except Exception:   # noqa: BLE001 — a gate error withholds, never serves
                continue
        dropped = seeder.retain(allowed)
        if dropped:
            db.log_event(control_con, "info", "seeding",
                         f"stopped seeding {len(dropped)} torrent(s) whose "
                         f"collection this node no longer holds or may not "
                         f"exchange")
        for collection in collections:
            con = cons.get(collection.name)
            if con is None or collection.name not in allowed:
                continue
            for entry in read_published(con):
                try:
                    seeder.ensure(collection, entry)
                except Exception as e:   # noqa: BLE001 — one bad torrent, not the seeder
                    db.log_event(con, "warn", "seeding",
                                 f"cannot seed {entry.get('name')}: {type(e).__name__}: {e}"[:300])
        from . import transport_torrent as tt
        if tt._session is not None:
            seeder.limit_upload(tt._session)
        seeder.account()
        db.set_control(control_con, SEEDING_TABLE_KEY,
                       json.dumps({"at": time.time(), "rows": seeder.table()}))
    except Exception:   # noqa: BLE001 — telemetry must never kill the worker
        pass


def read_table(con, max_age_s: float = 60.0) -> tuple[list[dict], float | None]:
    raw = db.get_control(con, SEEDING_TABLE_KEY)
    if not raw:
        return [], None
    try:
        payload = json.loads(raw)
        age = max(0.0, time.time() - float(payload["at"]))
        rows = payload["rows"]
        if not isinstance(rows, list) or age > max_age_s:
            return [], age
        return rows, age
    except (ValueError, TypeError, KeyError):
        return [], None

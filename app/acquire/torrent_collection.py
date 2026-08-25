"""A TORRENT COLLECTION: one magnet for a whole published corpus (STATUS 5l).

Per-language torrents (5h) made a corpus fetchable a slice at a time, but
a peer had to be handed eleven magnets and keep its TOML in step with the
publisher. Now the publisher also publishes an INDEX torrent — a tiny
directory holding `collection.json` (the member list: slice name, the
include patterns that slice selects, magnet, info hash, size) plus a copy
of every member .torrent — and hands out ITS magnet alone. A peer's
collection is created with that one magnet; which members it pulls is
the peer's PROFILE ("tr", "tr,it", "all"), and growing the profile later
adds members without touching the TOML. The grow dialog offers exactly
the slices the collection publishes.

BitTorrent has no collection object of its own; this is a static index
torrent, so a re-publish (a new snapshot, a new language) is a new index
with a new magnet — the same discipline as dated origin snapshots.
Mutable pointers (BEP 46) would keep the magnet stable and are the
obvious follow-up once peers live beyond one LAN.

Resolution on the peer is cached in the collection's control table under
PEER_INDEX_KEY keyed on the source string; a TOML edit of `peer_source`
resolves afresh. The index content lands under the manifest directory
(`peer-index/<info hash>/`), never in the corpus.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .. import db
from .spec import Endpoint, RemoteFile, Transport, TransportError

KIND = "louise-torrent-collection"
VERSION = 1
INDEX_FILE = "collection.json"
PEER_INDEX_KEY = "peer_index"
_RESOLVE_STALE_S = 24 * 3600


# ---------------- publisher ----------------

def write_index(out_dir: Path, *, collection: str, plugin: str, members: list[dict],
                peer_hint: str = "") -> Path:
    """Materialise the index directory: collection.json + every member
    .torrent (so a peer that has the index has the metadata of every
    member too, no magnet resolution needed on the LAN)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.torrent"):
        old.unlink()
    listed = []
    for m in members:
        entry = {k: m[k] for k in ("name", "slice", "include", "magnet", "info_hash",
                                   "files", "bytes") if k in m}
        src = Path(m["torrent_path"])
        if src.is_file():
            (out_dir / src.name).write_bytes(src.read_bytes())
            entry["torrent_file"] = src.name
        listed.append(entry)
    index = {"kind": KIND, "version": VERSION, "collection": collection, "plugin": plugin,
             "published_at": int(time.time()), "peer_hint": peer_hint, "members": listed}
    path = out_dir / INDEX_FILE
    path.write_text(json.dumps(index, indent=1, sort_keys=True), encoding="utf-8")
    return path


def with_hint(magnet: str, peer_hint: str | None) -> str:
    return f"{magnet}&x.pe={peer_hint}" if peer_hint and magnet.startswith("magnet:") else magnet


# ---------------- peer ----------------

def is_index_listing(paths: list[str]) -> bool:
    return any(p.rsplit("/", 1)[-1] == INDEX_FILE for p in paths)


def resolve(collection, source: str, *, stop=None, force: bool = False) -> dict | None:
    """The index behind `source` (a magnet or .torrent path), or None when
    the source is a plain corpus torrent. Downloads the index content —
    a few hundred KB — once, then serves the cached copy."""
    con = db.connect(collection)
    cached = _cached(con, source)
    if cached is not None and not force:
        return cached["index"]
    from .transport_torrent import TorrentTransport, torrent_info
    endpoint = Endpoint(transport=Transport.TORRENT, url=source)
    try:
        info = torrent_info(endpoint, stop=stop)
    except TransportError:
        raise
    from .transport_torrent import _paths_and_sizes
    listing = _paths_and_sizes(info)
    names = [p for p, _ in listing]
    if not is_index_listing(names):
        _remember(con, source, None)
        return None
    rel = next(p for p in names if p.rsplit("/", 1)[-1] == INDEX_FILE)
    dest_root = collection.manifest_path.parent / "peer-index" / _hash_of(info)
    dest = dest_root / rel
    if not dest.is_file():
        TorrentTransport().fetch_one(endpoint, RemoteFile(rel_path=rel), dest, 0)
    index = json.loads(dest.read_text(encoding="utf-8"))
    if index.get("kind") != KIND:
        raise TransportError(f"{source}: {INDEX_FILE} is not a louise torrent collection")
    _remember(con, source, index)
    return index


def cached_index(collection, source: str) -> dict | None:
    """The resolved index without touching the network (None = unresolved
    or a plain torrent)."""
    entry = _cached(db.connect(collection), source)
    return entry["index"] if entry else None


def members_for_profile(index: dict, spec, profile: str) -> list[dict]:
    """Which members a profile selects, in FETCH ORDER: the parts of a
    combined profile in the order given ("tr,it" → tr then it), an
    everything profile by size ascending (a peer is busy on a small slice
    while the big ones arrive), a single profile's members by size."""
    from .spec import profile_parts, resolve_profile
    members = list(index.get("members", []))
    try:
        wanted = set(resolve_profile(spec, profile).include)
    except ValueError:
        return []
    if "*" in wanted:
        return sorted(members, key=lambda m: int(m.get("bytes") or 0))
    parts = profile_parts(profile)
    order = {p: i for i, p in enumerate(parts)}

    def selected(m: dict) -> bool:
        incl = set(m.get("include") or ())
        return bool(incl & wanted) or (m.get("slice") in order)
    chosen = [m for m in members if selected(m)]
    chosen.sort(key=lambda m: (order.get(m.get("slice"), len(order)), int(m.get("bytes") or 0)))
    return chosen


def offered_slices(index: dict) -> list[dict]:
    """The grow dialog's options for a collection that fetches from an
    index: what the publisher actually offers, with sizes."""
    return [{"name": m.get("slice") or m.get("name"), "bytes": int(m.get("bytes") or 0),
             "files": int(m.get("files") or 0)}
            for m in sorted(index.get("members", []), key=lambda m: int(m.get("bytes") or 0))
            if m.get("slice")]


# ---------------- cache ----------------

def _cached(con, source: str) -> dict | None:
    raw = db.get_control(con, PEER_INDEX_KEY)
    if not raw:
        return None
    try:
        table = json.loads(raw)
    except (ValueError, TypeError):
        return None
    entry = table.get(source) if isinstance(table, dict) else None
    if not isinstance(entry, dict):
        return None
    if time.time() - float(entry.get("at", 0)) > _RESOLVE_STALE_S and entry.get("index") is None:
        return None            # a "plain torrent" verdict is re-checked daily
    return entry


def _remember(con, source: str, index: dict | None) -> None:
    raw = db.get_control(con, PEER_INDEX_KEY)
    try:
        table = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        table = {}
    if not isinstance(table, dict):
        table = {}
    table[source] = {"at": time.time(), "index": index}
    db.set_control(con, PEER_INDEX_KEY, json.dumps(table))


def _hash_of(info) -> str:
    from .torrent_publish import _info_hash
    return _info_hash(info)

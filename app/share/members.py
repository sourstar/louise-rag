"""Members as torrents, and the keys they are found under (09 §9.16; 04 §4.2,
§4.3). Migration step 9a.

Pure functions, no session and no network: a peer computes every infohash
and every DHT key from the snapshot and the Codex alone, so two honest
publishers of one file land in one swarm and a newcomer months later finds
a collection by computing its key, not by asking anyone.

- A file's torrent is a function of its SHARD-relative path, its length and
  its pieces root, and of nothing else: the info dictionary holds exactly
  `meta version`, `name` (the member), `piece length` (16 MiB) and the
  `file tree` with that one file. The infohash is SHA-256 of its bencoding.
- `H(a ‖ b)` is SHA-256 over the canonical JSON of `[a, b]` (09 §9.1); the
  DHT needs 20 bytes, so a key is the first 20. `H` of one identity alone
  (an encoder's weights, an engine's image) is SHA-256 over the identity's
  own UTF-8 bytes.
"""
from __future__ import annotations

import json

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.hold import shard as shard_mod
from app.hold.codex import CodexError, canonical_json
from app.hold.shard import MemberFile, member_root

PIECE_LENGTH = shard_mod.PIECE_LENGTH          # 16 MiB (09 §9.16)
META_VERSION = 2
DHT_KEY_BYTES = 20


class MemberError(CodexError):
    """A member, a path or an identity that 09 §9.16 cannot turn into a torrent."""


# ---- bencoding ------------------------------------------------------------------

def bencode(value: Any) -> bytes:
    """BEP 3's encoding, the one bytes-level form a torrent has: integers,
    byte strings (str as UTF-8), lists, and dictionaries with their keys
    sorted as bytes. No booleans, no floats, no None — a torrent has none."""
    if isinstance(value, bool):
        raise MemberError("bencode: a torrent has no booleans")
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, (bytes, bytearray)):
        return b"%d:" % len(value) + bytes(value)
    if isinstance(value, (list, tuple)):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        items = []
        for k, v in value.items():
            kb = k.encode("utf-8") if isinstance(k, str) else bytes(k)
            items.append((kb, v))
        items.sort(key=lambda kv: kv[0])
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise MemberError(f"bencode: not a torrent value: {type(value).__name__}")


# ---- one file, one torrent --------------------------------------------------------

def raw_digest(identity: str) -> bytes:
    """The 32 bytes of a `sha256:…` identity (09 §9.1)."""
    if not (isinstance(identity, str) and identity.startswith("sha256:") and len(identity) == 71):
        raise MemberError(f"not a sha256: identity: {identity!r}")
    try:
        return bytes.fromhex(identity[7:])
    except ValueError:
        raise MemberError(f"not a sha256: identity: {identity!r}") from None


def info_dict(member: str, path: str, length: int, root: str) -> dict[str, Any]:
    """Exactly 09 §9.16's info dictionary for one file of a member: `meta
    version: 2`, `name` = the member, `piece length`, and a `file tree` with
    the one file at its SHARD-relative path carrying `length` and `pieces
    root`. Nothing else — no private flag, no comment, no date, no creator,
    no announce — so the infohash is a function of (path, length, root)."""
    parts = path.split("/")
    if not path or any(p in ("", ".", "..") for p in parts) or path.startswith("/"):
        raise MemberError(f"a member file's path is SHARD-relative and plain, got {path!r}")
    if not isinstance(length, int) or isinstance(length, bool) or length < 0:
        raise MemberError(f"{path}: length is a non-negative integer, got {length!r}")
    tree: dict[str, Any] = {"": {"length": length, "pieces root": raw_digest(root)}}
    for part in reversed(parts):
        tree = {part: tree}
    return {"file tree": tree, "meta version": META_VERSION, "name": member,
            "piece length": PIECE_LENGTH}


def infohash(info: dict[str, Any]) -> str:
    """The v2 infohash: SHA-256 of the bencoded info dictionary, as an identity."""
    return "sha256:" + hashlib.sha256(bencode(info)).hexdigest()


def dht_id(identity: str) -> bytes:
    """The 20 bytes the DHT uses of a 32-byte identity (an infohash)."""
    return raw_digest(identity)[:DHT_KEY_BYTES]


# ---- the keys (04 §4.2; 09 §9.16) -------------------------------------------------

def key(*identities: str) -> bytes:
    """The first 20 bytes of `H(a ‖ b ‖ …)` — SHA-256 over the canonical JSON
    of the list of strings (09 §9.1) — or, for one identity alone, of
    SHA-256 over its own UTF-8 bytes."""
    if not identities or not all(isinstance(i, str) and i for i in identities):
        raise MemberError("a key is made of one or more non-empty identities")
    if len(identities) == 1:
        h = hashlib.sha256(identities[0].encode("utf-8")).digest()
    else:
        h = hashlib.sha256(canonical_json(list(identities))).digest()
    return h[:DHT_KEY_BYTES]


def collection_key(collection_id: str, snapshot_id: str) -> bytes:
    """Where the sharers of (collection, snapshot) are found."""
    return key(collection_id, snapshot_id)


def lineage_key(lineage_id: str, snapshot_id: str) -> bytes:
    """Where the sharers of the lineage's current daughter are found."""
    return key(lineage_id, snapshot_id)


def swarm_key(collection_id: str, snapshot_id: str) -> bytes:
    """Where the swarms serving (collection, snapshot)'s members are found,
    announced with the swarm's port (04 §4.2). A member is never announced
    by its own infohash: thousands of parts were thousands of announcements
    and filled a router's NAT table (docs/notes/dht-flood.md)."""
    return key(collection_id, snapshot_id, "swarm")


def weights_key(weights: str) -> bytes:
    """Where any peer holding an encoder's weights announces (04 §4.3)."""
    return key(weights)


def image_key(image: str) -> bytes:
    """Where any peer holding an engine's image announces."""
    return key(image)


# ---- members -----------------------------------------------------------------------

@dataclass(frozen=True)
class Torrent:
    path: str                  # SHARD-relative
    length: int
    root: str
    info: dict[str, Any]
    infohash: str


@dataclass(frozen=True)
class Member:
    """One unit a peer fetches or shares (02 §2.6): a name of 09 §9.16's
    table and the files it is made of. Its root is the file's pieces root,
    or `H` over `[[path, root, bytes], …]` for several (shard_mod.member_root).
    `base` is the directory the files' paths are relative to when it is not
    the SHARD: a bundle's own directory (09 §9.7, §9.16)."""
    name: str
    files: tuple[MemberFile, ...]
    base: Path | None = None

    @property
    def root(self) -> str:
        return member_root(self.files)

    @property
    def bytes(self) -> int:
        return sum(f.bytes for f in self.files)

    def torrents(self) -> tuple[Torrent, ...]:
        """One torrent per file, in path order."""
        out = []
        for f in sorted(self.files, key=lambda f: f.path.encode("utf-8")):
            info = info_dict(self.name, f.path, f.bytes, f.root)
            out.append(Torrent(f.path, f.bytes, f.root, info, infohash(info)))
        return tuple(out)

    @property
    def listed_by_a_snapshot(self) -> bool:
        """Record, relations, figures and whole-collection members are found
        in the snapshot, file by file; a bundle or a text index is not."""
        return not self.name.startswith(("bundle-", "index-"))

    def to_dict(self) -> dict[str, Any]:
        """The digest's `members` entry (09 §9.9): name, root, bytes — and,
        for a member no snapshot lists, its files as `[path, root, bytes]` in
        path order, from which a fetcher computes each torrent (§9.16)."""
        out = {"name": self.name, "root": self.root, "bytes": self.bytes}
        if not self.listed_by_a_snapshot:
            out["files"] = [f.to_list() for f in sorted(self.files, key=lambda f: f.path.encode("utf-8"))]
        return out


def _whole_collection_members(files: dict[str, Any]) -> list[Member]:
    """`validation`, `terms`, `slicing`, `calibration-<set>` and
    `seals-<id>` from the snapshot's `files` (09 §9.4); a null entry (a
    green collection's terms) is no member."""
    out: list[Member] = []
    plain = {shard_mod.VALIDATION_PATH: "validation", shard_mod.TERMS_PATH: "terms"}
    calibration: dict[str, list[MemberFile]] = {}
    seals: dict[str, list[MemberFile]] = {}
    lists: list[MemberFile] = []
    for path in sorted(files, key=lambda p: p.encode("utf-8")):
        entry = files[path]
        if entry is None:
            continue
        mf = MemberFile(path, entry["root"], int(entry["bytes"]))
        if path in plain:
            out.append(Member(plain[path], (mf,)))
        elif path.startswith("slicing/") and path.endswith(".json"):
            # the family map a slicing names (09 §9.4, §9.16, amended 2026-10-24)
            out.append(Member("slicing", (mf,)))
        elif path.startswith("calibration/"):
            stem = Path(path).name.split(".", 1)[0]
            calibration.setdefault(f"calibration-{stem}", []).append(mf)
        elif path.startswith("seals/lists/") and path.endswith(".xml"):
            lists.append(mf)                  # the trusted lists travel with the seals (09 §9.16)
        elif path.startswith("seals/") and path.endswith(".json"):
            seals.setdefault(shard_mod.seals_member(Path(path).stem), []).append(mf)
        else:
            raise MemberError(f"{path}: not a whole-collection member 09 §9.16 names")
    for name in sorted(calibration):
        out.append(Member(name, tuple(calibration[name])))
    if lists and not seals:
        raise MemberError("trusted lists in `files` with no seals file to travel with")
    for name in sorted(seals):
        out.append(Member(name, tuple(seals[name] + lists)))
    return out


BUNDLE_FILES = ("MANIFEST.sig", "header.json", "possession.json", "vectors.parquet")


def bundle_member(space: str, bundle_dir: Path) -> Member:
    """`bundle-<space>-<bundle_id hex>` (09 §9.16): the four files, their
    paths relative to the bundle directory, so the member root is a function
    of the bundle alone."""
    bundle_dir = Path(bundle_dir)
    files = tuple(MemberFile(f, shard_mod.pieces_root_of(bundle_dir / f), (bundle_dir / f).stat().st_size)
                  for f in BUNDLE_FILES)
    return Member(f"bundle-{shard_mod.check_space(space)}-{bundle_dir.name}", files, base=bundle_dir)


def bundle_dirs(state_dir: Path, space: str) -> list[Path]:
    """The directory of every whole bundle this peer holds for the space,
    `state/bundles/<space>/<slice>/<NNNNN>/<bundle_id hex>/` (09 §9.7) —
    a listing and a stat per file, no hashing; a directory missing one of
    the four files is no bundle."""
    base = Path(state_dir) / "bundles" / space
    if not base.is_dir():
        return []
    return [d for d in sorted(p for p in base.glob("*/*/*") if p.is_dir())
            if all((d / f).is_file() for f in BUNDLE_FILES)]


#: under `state/share/`: the roots of every held bundle's files, keyed on
#: each file's physical identity — path, size, mtime_ns (CLAUDE.md: a cache
#: keys on physical identity, never on a name). A bundle directory is
#: immutable and named by its id (09 §9.7), so a file is hashed ONCE per
#: bundle: without this every digest and every publish hashed every held
#: bundle — 385 bundles, 10.7 GiB, 8 s per call on the node (2026-10-19),
#: heading for 55 GB and 40 s a minute while contribute produced them
ROOTS_CACHE = "bundle-roots.json"


def _roots_cache_path(state_dir: Path) -> Path:
    return Path(state_dir) / "share" / ROOTS_CACHE


def _read_roots_cache(path: Path) -> dict[str, Any]:
    try:
        got = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _write_roots_cache(path: Path, cache: dict[str, Any]) -> None:
    """Whole, through a temporary file of this process and thread: two
    threads (the digest's, the seeder's) may write at once, and the last
    whole file wins — both are right."""
    import os
    import threading
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{ROOTS_CACHE}.{os.getpid()}.{threading.get_ident()}")
        tmp.write_bytes(json.dumps(cache, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        tmp.replace(path)
    except OSError:
        pass                                   # a cache that cannot be written is recomputed


def bundle_members(state_dir: Path, space: str) -> list[Member]:
    """Every bundle this peer holds for the space, as members with their
    roots — hashed once per bundle and kept in `ROOTS_CACHE`; a file whose
    size or mtime moved is hashed again, a directory gone leaves the cache."""
    state_dir = Path(state_dir)
    dirs = bundle_dirs(state_dir, space)
    cache_path = _roots_cache_path(state_dir)
    cache = _read_roots_cache(cache_path)
    base = state_dir / "bundles" / space
    out: list[Member] = []
    live: set[str] = set()
    changed = False
    for d in dirs:
        key = f"{space}/{d.relative_to(base).as_posix()}"
        live.add(key)
        try:
            stats = [[f, (d / f).stat().st_size, (d / f).stat().st_mtime_ns] for f in BUNDLE_FILES]
        except OSError:
            continue                           # vanished between the listing and the stat
        hit = cache.get(key)
        if isinstance(hit, dict) and hit.get("stat") == stats and isinstance(hit.get("files"), list) \
                and len(hit["files"]) == len(BUNDLE_FILES):
            try:
                files = tuple(MemberFile(str(p), str(r), int(b)) for p, r, b in hit["files"])
                out.append(Member(f"bundle-{shard_mod.check_space(space)}-{d.name}", files, base=d))
                continue
            except (TypeError, ValueError):
                pass                           # a cache entry of another shape: hashed again
        member = bundle_member(space, d)
        out.append(member)
        cache[key] = {"stat": stats, "files": [f.to_list() for f in member.files]}
        changed = True
    stale = [k for k in cache if k.startswith(f"{space}/") and k not in live]
    for k in stale:
        del cache[k]
    if changed or stale:
        _write_roots_cache(cache_path, cache)
    return out


def members_of(snapshot, *, shard_dir: Path | None = None) -> tuple[Member, ...]:
    """The members a snapshot implies, with their files, roots and sizes —
    what a sharer serves and announces and a fetcher asks for: the record
    shards per slice, the relations and figures shards per slice, the
    whole-collection members, and — from the SHARD on disk, when given — the
    text-index members. Vectors are not here: they reach a peer only as
    bundles (04 §4.3)."""
    out: list[Member] = []
    by_slice: dict[str, list[MemberFile]] = {}
    for s in snapshot.shards:
        by_slice.setdefault(s.slice, []).append(MemberFile(s.path, s.root, s.bytes))
    for sl in sorted(by_slice, key=lambda s: s.encode("utf-8")):
        out.append(Member(shard_mod.records_member(sl), tuple(by_slice[sl])))
    rel: dict[str, list[MemberFile]] = {}
    for r in snapshot.relations:
        rel.setdefault(r.slice, []).append(MemberFile(r.path, r.root, r.bytes))
    for sl in sorted(rel, key=lambda s: s.encode("utf-8")):
        out.append(Member(shard_mod.relations_member(sl), tuple(rel[sl])))
    fig: dict[str, list[MemberFile]] = {}
    for f in snapshot.figures:
        fig.setdefault(f.slice, []).append(MemberFile(f.path, f.root, f.bytes))
    for sl in sorted(fig, key=lambda s: s.encode("utf-8")):
        out.append(Member(f"figures-{shard_mod.check_slice(sl)}", tuple(fig[sl])))
    out += _whole_collection_members(snapshot.files or {})
    return tuple(out)

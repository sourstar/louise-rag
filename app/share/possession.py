"""Coverage BY POSSESSION (04 §4.5, amended 2026-10-22).

A record of a part is covered when some accepted bundle held here holds its
uid with the record's CURRENT content_hash — this collection's bundles and
those of the collections it was re-minted from in the same space (the carry
predecessors, `contribute.predecessors`), whatever the shard key a bundle
was filed under. 03 §3.5: a bundle is admissible by what it was computed
FROM (space and per-record content_hash); "the collection_id and
snapshot_id in the header are provenance … and gate nothing".

Why (gutenberg, parser 9 → 10, 2026-10-22): the re-mint re-cut the parts
(222 of 1,639 identical; a bundle may span two new ones), the carry cleared
the predecessor's coverage, and the node's held path bundled and signed as
its own 117 parts whose every record the predecessor's bundles already held
— 259 of them only in another producer's. Counting by the bundle's shard key
could not see the predecessor's 2,976 bundles at all; counting by
possession sees 75,063 of the 79,658 records covered, and the 4,595 left are
the changed (133), the new and the never-bundled — the only work there is.

Cost: the index is rebuilt only when the set of accepted bundles changes
(fingerprinted by one aggregate query), each part's uids and hashes are read
once per part ROOT (a part never changes under its root), and the chunks per
record come from the manifest. A round reads none of the bundle files again.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

INDEX = "possession-index.json"
PARTS = "part-uids"

#: (collection dir) -> (fingerprint, Possession) for this process
_memo: dict[str, tuple[str, "Possession"]] = {}
#: (collection dir, fingerprint, snapshot) -> covered chunks per part, the last one
_covered_memo: dict[tuple[str, str, str], dict[str, int]] = {}
#: (collection dir) -> index rebuilds in this process
REBUILDS: dict[str, int] = {}


class Possession(dict):
    """uid -> [content hashes] the accepted bundles held here hold for it
    (a dict, so `covered_uids` reads it as it always has), with:
    `bundles`: (uid, hash) -> bundle ids holding it (the digest's listing);
    `producers`: bundle id -> producer (own work, by key);
    `fileless`: slice -> [(uid_first, uid_last, producer)] of the
    PREDECESSORS' accepted rows with no files here (the compare path)."""

    def __init__(self, held=(), bundles=None, producers=None, fileless=None, shards=None):
        super().__init__(held)
        self.bundles: dict[str, dict[str, list[str]]] = bundles or {}
        self.producers: dict[str, str] = producers or {}
        self.fileless: dict[str, list[list[str]]] = fileless or {}
        #: bundle id -> the shard it is filed under (`contribute.bundle_dir`)
        self.shards: dict[str, str] = shards or {}

    def to_json(self) -> dict[str, Any]:
        return {"held": dict(self), "bundles": self.bundles, "producers": self.producers,
                "fileless": self.fileless, "shards": self.shards}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Possession":
        return cls(d["held"], d.get("bundles") or {}, d.get("producers") or {}, d.get("fileless") or {},
                   d.get("shards") or {})


def _share_dir(root: Path, name: str) -> Path:
    return Path(root) / name / "state" / "share"


def prefixes(root: Path, name: str, codex) -> list[str]:
    """The shard-id prefixes whose rows count: this collection's and its
    same-space predecessors' (unkeyed rows, app/hold/carry.py; a fresh
    peer's foreign-headed bundles, app/share/accept.py)."""
    from app.share import contribute
    return [f"{cid}/{codex.space_segment}/" for cid in (codex.collection_id, *contribute.predecessors(root, name))]


def _fingerprint(con, pre: Sequence[str], segments: Sequence[str] = ()) -> str:
    """ONE aggregate over the coverage table, whatever the number of
    predecessors (a per-round cost must not grow with a list): the rows'
    count, the highest rowid, the latest acceptance; and the prefixes and
    segments the index reads by."""
    h = hashlib.sha256(("|".join(segments) + "#" + "|".join(pre)).encode())
    n, top, last, accepted = con.execute(
        "SELECT COUNT(*), MAX(rowid), MAX(accepted_at), COUNT(accepted_at) FROM coverage").fetchone()
    h.update(f"{n}|{top}|{last}|{accepted}".encode())
    return h.hexdigest()


def index(root: Path, name: str, codex, con) -> "Possession":
    """What the accepted bundles held here hold (`Possession`), read through
    `contribute.bundle_dir` — every admitted space segment (the A1f twin
    filed 1,743 of the node's bundles under 5ac3c581, and a fixed path read
    them as missing). Cached on disk and in the process; rebuilt when the
    coverage table changes."""
    from app.share import contribute
    pre = prefixes(root, name, codex)
    own = pre[0]
    segs = tuple(getattr(codex, "admitted_segments", ()) or ()) or (codex.space_segment,)
    fp = _fingerprint(con, pre, segs)
    key = str(Path(root) / name)
    got = _memo.get(key)
    if got and got[0] == fp:
        return got[1]
    path = _share_dir(root, name) / INDEX
    try:
        cached = json.loads(path.read_bytes())
        if cached.get("fingerprint") == fp and "shards" in (cached.get("index") or {}):
            result = Possession.from_json(cached["index"])
            _memo[key] = (fp, result)
            return result
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        pass
    t0 = time.perf_counter()
    held: dict[str, set[str]] = {}
    bundles: dict[str, dict[str, list[str]]] = {}
    producers: dict[str, str] = {}
    fileless: dict[str, list[list[str]]] = {}
    shards: dict[str, str] = {}
    by_cid = {p.split("/", 1)[0]: p for p in pre}
    for bundle_id, shard, producer, first, last in con.execute(
            "SELECT bundle_id, shard, producer, uid_first, uid_last FROM coverage WHERE accepted_at IS NOT NULL"):
        p = by_cid.get(shard.split("/", 1)[0])
        if p is None or not shard.startswith(p):
            continue                           # another collection's, not a predecessor: nothing here
        sid = shard[len(p):]
        try:
            got_ = json.loads((contribute.bundle_dir(root, name, codex, sid, bundle_id)
                               / "possession.json").read_bytes())
        except (OSError, ValueError):
            if p != own and first and last:
                fileless.setdefault(sid.rsplit("/", 1)[0], []).append([first, last, producer or ""])
            continue                           # no files here: covers nothing
        producers[bundle_id] = producer or ""
        shards[bundle_id] = sid
        for uid, h in got_.items():
            held.setdefault(uid, set()).add(h)
            bundles.setdefault(uid, {}).setdefault(h, []).append(bundle_id)
    result = Possession({u: sorted(hs) for u, hs in held.items()}, bundles, producers, fileless, shards)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_bytes(json.dumps({"fingerprint": fp, "index": result.to_json()}).encode())
    os.replace(tmp, path)
    _memo[key] = (fp, result)
    # every rebuild said, with its cost: a fetch beat that accepts bundles
    # one by one rebuilds once per bundle (review, 2026-10-22), and this
    # line is what shows whether that costs anything
    REBUILDS[key] = REBUILDS.get(key, 0) + 1
    print(f"{time.strftime('%H:%M:%S')} [{name} info:share] possession index rebuilt "
          f"(#{REBUILDS[key]} in this process): {len(producers)} bundles held, "
          f"{len(result)} uids, in {(time.perf_counter() - t0) * 1000:.0f} ms", flush=True)
    return result


def part_records(root: Path, name: str, snapshot, shard) -> list[tuple[str, str]]:
    """(uid, content_hash) of a part's records, read once per part ROOT."""
    from app.hold import land as landing
    p = _share_dir(root, name) / PARTS / f"{str(shard.root).split(':')[-1]}.json"
    try:
        return [tuple(x) for x in json.loads(p.read_bytes())]
    except (OSError, ValueError):
        pass
    got = [(r["uid"], r.get("content_hash")) for r in
           landing.records_for(Path(root), name, snapshot, f"{shard.slice}/{shard.n:05d}", None)]
    p.parent.mkdir(parents=True, exist_ok=True)
    # a temp file of its OWN (tempfile.mkstemp): the worker's round and a CLI
    # `hold vectors show` fill the same missing entry at once, and one shared
    # `.json.tmp` let the second rename find nothing — the exception sent
    # `counting.view_kwargs` to its row-count fallback, 114,154 of 10.47 M
    # covered for one call (Perugia, 2026-10-25)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(got).encode())
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return got


def covered_uids(held: dict[str, list[str]], records: Iterable[tuple[str, str]]) -> set[str]:
    """The records whose uid a held bundle holds with the same content hash."""
    return {u for u, h in records if h is not None and h in (held.get(u) or ())}


def covered(root: Path, name: str, codex, con, snapshot=None) -> dict[str, int]:
    """shard id -> chunks covered by possession, for EVERY part of the
    snapshot (0 included): what `coverage.shards_view` takes as `union`."""
    from app.hold import seed
    from app.share import coverage as cov
    snapshot = snapshot or seed.latest_snapshot(Path(root), name)
    held = index(root, name, codex, con)
    # a round's view costs the fingerprint query alone while nothing changed
    memo_key = (str(Path(root) / name), _memo[str(Path(root) / name)][0], snapshot.snapshot_id)
    if memo_key in _covered_memo:
        return _covered_memo[memo_key]
    out: dict[str, int] = {}
    for s in snapshot.shards:
        recs = part_records(root, name, snapshot, s)
        mine = covered_uids(held, recs)
        if not mine:
            out[cov.shard_id(codex, f"{s.slice}/{s.n:05d}")] = 0
            continue
        ids = sorted(mine)
        n = 0
        for i in range(0, len(ids), 900):
            part = ids[i:i + 900]
            n += con.execute(f"SELECT COALESCE(SUM(chunks), 0) FROM documents WHERE uid IN "
                             f"({','.join('?' * len(part))})", part).fetchone()[0]
        out[cov.shard_id(codex, f"{s.slice}/{s.n:05d}")] = min(int(s.chunks), int(n))
    _covered_memo.clear()
    _covered_memo[memo_key] = out
    return out


def covered_by_others(root: Path, name: str, codex, con, *, own_key: str) -> list[dict[str, Any]]:
    """This producer's accepted bundles of THIS collection whose every
    (uid, content_hash) is held by an accepted bundle that is not one of
    them — another producer's, or a same-space predecessor's of anyone: the
    copies the node's held path made after the parser-10 re-mint, every
    record already in a d913 bundle (2026-10-22). Candidates never cover one
    another, so withdrawing them all loses no coverage."""
    from app.share import contribute
    own_prefix = f"{codex.collection_id}/{codex.space_segment}/"
    holders: dict[tuple[str, str], int] = {}
    candidates: list[tuple[str, str, dict[str, str]]] = []
    for p in prefixes(root, name, codex):
        for bundle_id, shard, producer in con.execute(
                "SELECT bundle_id, shard, producer FROM coverage WHERE accepted_at IS NOT NULL "
                "AND shard >= ? AND shard < ?", (p, p[:-1] + "0")):
            sid = shard[len(p):]
            try:
                held = json.loads((contribute.bundle_dir(root, name, codex, sid, bundle_id)
                                   / "possession.json").read_bytes())
            except (OSError, ValueError):
                continue
            if p == own_prefix and producer == own_key:
                candidates.append((bundle_id, shard, held))
                continue
            for pair in held.items():
                holders[pair] = holders.get(pair, 0) + 1
    return [{"bundle_id": b, "shard": s, "uids": len(held)}
            for b, s, held in candidates if held and all(pair in holders for pair in held.items())]


#: (collection dir, fingerprint, snapshot) -> the predecessors' bundles per part, the last one
_offered_memo: dict[tuple[str, str, str], dict[str, list[str]]] = {}


def offered_by_part(root: Path, name: str, codex, con, snapshot=None) -> dict[str, list[str]]:
    """shard id of THIS collection's part -> the PREDECESSORS' accepted
    bundles held here whose possession holds one of its records with the
    record's current content_hash (a bundle spanning two re-cut parts is
    listed under both): what the digest's `coverage` offers for the part
    (09 §9.9), so a fetcher of this collection takes them, admitted by what
    they were computed from (03 §3.5). From the possession cache and the
    per-part uid files; memoised per fingerprint and snapshot."""
    from app.hold import seed
    from app.share import coverage as cov
    snapshot = snapshot or seed.latest_snapshot(Path(root), name)
    held = index(root, name, codex, con)
    key = (str(Path(root) / name), _memo[str(Path(root) / name)][0], snapshot.snapshot_id)
    if key in _offered_memo:
        return _offered_memo[key]
    own = f"{codex.collection_id}/{codex.space_segment}/"
    own_ids = {b for (b,) in con.execute(
        "SELECT bundle_id FROM coverage WHERE shard >= ? AND shard < ?", (own, own[:-1] + "0"))}
    out: dict[str, list[str]] = {}
    for s in snapshot.shards:
        ids: set[str] = set()
        for u, h in part_records(root, name, snapshot, s):
            for b in (held.bundles.get(u) or {}).get(h, ()):
                if b not in own_ids:
                    ids.add(b)
        if ids:
            out[cov.shard_id(codex, f"{s.slice}/{s.n:05d}")] = sorted(ids)
    _offered_memo.clear()
    _offered_memo[key] = out
    return out


# ---- the embed stage's reuse: a record held in a bundle FILE is not embedded ----

#: (collection dir, snapshot id) -> uid -> the record's content_hash, the last one
_hash_memo: dict[tuple[str, str], dict[str, str]] = {}


def record_hashes(root: Path, name: str) -> dict[str, str]:
    """uid -> content_hash of the CURRENT snapshot's records — the hash
    possession is judged by (`covered`, `offered_by_part`), read from the
    per-part files those already keep (`part_records`), once per snapshot
    in this process. Perugia, 2026-10-23: the manifest's `content_hash`
    column was NULL on all 79,659 rows there, so a lookup by it matched
    nothing and 921 chunks a node bundle held for fi/00008 were embedded."""
    from app.hold import seed
    try:
        snap = seed.latest_snapshot(Path(root), name)
    except Exception:   # noqa: BLE001 — no SHARD here: nothing is known
        return {}
    if snap is None:
        return {}
    key = (str(Path(root) / name), snap.snapshot_id)
    hit = _hash_memo.get(key)
    if hit is not None:
        return hit
    out: dict[str, str] = {}
    for s in snap.shards:
        for u, h in part_records(root, name, snap, s):
            if h:
                out[u] = h
    _hash_memo.clear()
    _hash_memo[key] = out
    return out

#: the vectors of the last bundles read, by bundle id (documents come part by
#: part, so a part's bundle is read once for its documents, not once each)
_vectors_cache: dict[str, dict[str, Any]] = {}
_VECTORS_KEEP = 2


def _bundle_vectors(root: Path, name: str, codex, shard: str, bundle_id: str) -> dict[str, Any] | None:
    """chunk_id -> vector of one held bundle (challenge rows left out);
    None when its files do not read."""
    hit = _vectors_cache.get(bundle_id)
    if hit is not None:
        return hit
    from app.share import contribute
    try:
        import pyarrow.parquet as pq
        d = contribute.bundle_dir(root, name, codex, shard, bundle_id)
        header = json.loads((d / "header.json").read_bytes())
        n = int((header.get("challenge") or {}).get("n") or 0)
        t = pq.read_table(d / "vectors.parquet", columns=["chunk_id", "vector"])
        ids = t.column("chunk_id").to_pylist()[n:]
        vecs = t.column("vector").to_pylist()[n:]
    except Exception:   # noqa: BLE001 — not here, or not a bundle: nothing reused from it
        return None
    got = dict(zip(ids, vecs))
    while len(_vectors_cache) >= _VECTORS_KEEP:
        _vectors_cache.pop(next(iter(_vectors_cache)))
    _vectors_cache[bundle_id] = got
    return got


def held_vectors(root: Path, name: str, codex, con, uid: str, content_hash: str | None,
                 chunk_ids: Sequence[str]) -> dict[str, Any]:
    """chunk_id -> vector, from an accepted bundle held here that holds
    `uid` with this `content_hash` — the record as it is now — for those of
    `chunk_ids` it carries; {} when none does (Perugia, 2026-10-23: 12,921
    chunks the node's predecessor bundles held were embedded again, because
    the embed stage asked only its vector table, and those vectors live in
    the bundle files). The bundle was judged by the ladder when it was
    accepted (03 §3.5: admissible by what it was computed from); chunking is
    a pure function of the record (invariant 3), so the same record gives
    the same chunk ids and texts."""
    if not content_hash:
        return {}
    held = index(root, name, codex, con)
    if content_hash not in (held.get(uid) or ()):
        return {}
    wanted = set(chunk_ids)
    out: dict[str, Any] = {}
    for b in (held.bundles.get(uid) or {}).get(content_hash, ()):
        shard = held.shards.get(b)
        vecs = _bundle_vectors(root, name, codex, shard, b) if shard else None
        if not vecs:
            continue
        for cid in wanted - set(out):
            v = vecs.get(cid)
            if v is not None:
                out[cid] = v
        if len(out) == len(wanted):
            break
    return out

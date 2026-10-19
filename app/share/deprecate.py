"""Deprecating one's own REDUNDANT bundles (operator, 2026-10-19) — beside
03 §3.6's `withdraw`, which is for a FAULTY bundle (peers drop it and its
vectors). A deprecated copy is valid, only redundant: the holder pass
bundled 418 partially held parts again after each restart, a new id each
time, the same `content_digest`. The operator's rules:

- among a producer's equivalent copies of a part exactly one is CURRENT;
  the rest carry the deprecated tag — one `deprecate` attestation
  (`check` and `result` `deprecate`, `detail.bundles = {deprecated_id:
  current_id}`, 09 §9.8), signed by the producer, travelling as every
  attestation does; peers derive "deprecated" from the attestations they
  hold (`deprecated`), nothing is written to a schema;
- a seeder KEEPS SERVING deprecated copies for `KEEP_S` — a leecher
  mid-fetch completes or drops — and lists them in its digest under
  `deprecated_members`, not `members` (09 §9.9); after the window a holder
  removes a deprecated copy only where it also holds the current one
  (`expire`); a peer whose only copy of the part is deprecated keeps it
  and serves it as deprecated: it is its coverage, and the fallback;
- a leecher IGNORES deprecated copies for new fetches (`accept.listed_
  bundles`), completes a fetch in flight when at least half its bytes
  landed (`accept.take`), and falls back to a deprecated copy when no live
  neighbour offers its current equivalent or the current came back
  nobody-served (the operator's corner case);
- two equivalent copies held together count a part's coverage once
  (`redundant_here`; `coverage.shards_view(redundant=)`): the sum of two
  equal copies of a PARTIAL part read as the whole part, and its leader
  would never have embedded the rest.
Only the producer's word deprecates its bundle (`by_producer`); the open
points are in docs/notes/p2p-review-open-issues.md §11.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from app.hold import envelope as env
from app.hold.codex import canonical_json
from app.share import attest, contribute, coverage as cov, messages

CHECK = "deprecate"
#: a deprecated copy is served this long after its deprecation where its
#: current equivalent is held too — one day (operator, 2026-10-19: "we have
#: a lot of deprecated copies to get rid of"; every peer pulls attestations
#: hourly and has heard long before)
KEEP_S = 86400.0

Info = tuple[str, float]           # (current_id, deprecated_at)
Map = dict[str, dict[str, Info]]    # deprecated id -> signer -> its statement


def _id_of(member_name: str, space: str) -> str | None:
    prefix = f"bundle-{space}-"
    if not member_name.startswith(prefix) or len(member_name) != len(prefix) + 64:
        return None
    return "sha256:" + member_name[len(prefix):]


def _sid(shard_id: str) -> str:
    return "/".join(shard_id.rsplit("/", 2)[1:])


def attestation_body(codex, snapshot, pairs: Mapping[str, str], shards: Iterable[str], *, own_key: str,
                     platform_image: str, now: float) -> dict[str, Any]:
    """09 §9.8's attestation, `check` and `result` `deprecate`, the pairs
    under `detail.bundles`; `shards` the parts they cover."""
    return {"kind": "louise-attestation", "version": 3, "collection_id": codex.collection_id,
            "snapshot_id": snapshot.snapshot_id, "snapshot_hash": snapshot.hash, "check": CHECK,
            "seed": None, "shards": sorted(set(shards)), "result": CHECK,
            "detail": {"bundles": {k: pairs[k] for k in sorted(pairs)}}, "platform_image": platform_image,
            "encoder_engine": None, "encoder_weights": None, "key": own_key,
            "time": messages.time_text(now)}


def publish(root: Path, name: str, codex, snapshot, pairs: Mapping[str, str], *, key, own_key: str, con, node_con,
            platform_image: str, now: float | None = None,
            log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """The attestation written at its path and published as an event for
    the beat to forward. NOTHING is removed: the deprecated copies stay
    served for `KEEP_S` (the seeder's side) and go through `expire`. Every
    id named must be this producer's accepted bundle here, each pair of
    one part."""
    root = Path(root)
    now = time.time() if now is None else now
    shards: list[str] = []
    for dep, cur in pairs.items():
        if dep == cur:
            raise ValueError(f"{dep[:23]}…: a bundle cannot be its own current equivalent")
        rows = {b: cov.row(con, b) for b in (dep, cur)}
        if rows[dep] is None or rows[dep]["producer"] != own_key or not rows[dep]["accepted_at"]:
            raise ValueError(f"{dep[:23]}…: not an accepted bundle of this peer's — a producer deprecates only "
                             f"its own (09 §9.8)")
        if rows[cur] is None or not rows[cur]["accepted_at"]:
            # the current copy may be ANOTHER producer's: a peer that re-signed
            # rows it landed deprecates its copy in favour of the original
            raise ValueError(f"{cur[:23]}…: not a bundle accepted here — the current copy must be held")
        if rows[dep]["shard"] != rows[cur]["shard"]:
            raise ValueError(f"{dep[:23]}… and {cur[:23]}… cover different parts")
        shards.append(rows[dep]["shard"])
    body = attestation_body(codex, snapshot, pairs, shards, own_key=own_key, platform_image=platform_image, now=now)
    envelope = env.sign(env.ATTESTATION, body, key)
    aid = env.body_id(body)
    p = attest.attestation_path(root, name, codex, aid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.deprecate")
    tmp.write_bytes(canonical_json(envelope))
    tmp.replace(p)
    published = attest.publish(root, name, codex, [aid], key=key, own_key=own_key, con=node_con, now=now, log=log)
    if log:
        log(f"[{name}] deprecate: attestation {aid[:23]}… marks {len(pairs)} copy(ies) over {len(set(shards))} "
            f"part(s) deprecated; served until {messages.time_text(now + KEEP_S)}, never offered for a new fetch")
    return {"attestation": aid, "bundles": sorted(pairs), "shards": sorted(set(shards)),
            "published": published, "keep_until": messages.time_text(now + KEEP_S)}


def deprecated(root: Path, name: str, codex) -> Map:
    """deprecated bundle id -> signer -> (current id, when), over every
    `deprecate` attestation held for the collection; a signer's earliest
    statement about an id stands. Whose word counts is `by_producer`'s
    question. Cached with the attestations (copy_state)."""
    from app import copy_state
    out: Map = {}
    if not getattr(codex, "collection_id", None):
        return {}
    for b in copy_state.attestations(Path(root), name, codex):
        if b.get("check") != CHECK or b.get("result") != CHECK:
            continue
        signer = str(b.get("key") or "")
        pairs = (b.get("detail") or {}).get("bundles") or {}
        try:
            when = messages.parse_time(str(b.get("time")))
        except Exception:   # noqa: BLE001 — an attestation without a time deprecates nothing
            continue
        if not signer or not isinstance(pairs, dict):
            continue
        for dep, cur in pairs.items():
            if not isinstance(dep, str) or not isinstance(cur, str):
                continue
            mine = out.setdefault(dep, {})
            if signer not in mine or when < mine[signer][1]:
                mine[signer] = (cur, when)
    return out


def by_producer(ids: Map, bundle_id: str, producer: str | None) -> bool:
    """Whether the bundle's PRODUCER deprecated it — anyone else's word
    deprecates nothing; an unknown producer decides nothing."""
    return bool(producer) and producer in ids.get(bundle_id, {})


def statement(ids: Map, bundle_id: str, producer: str | None = None) -> Info | None:
    """The producer's statement about the bundle when the producer is known
    and made one; else — a leecher that has not read the header yet — the
    earliest statement by any signer, which decides only whether a fetch
    is started, never what is dropped."""
    mine = ids.get(bundle_id) or {}
    if not mine:
        return None
    if producer:
        return mine.get(producer)
    return min(mine.values(), key=lambda i: i[1])


def _rows_for(con, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """The coverage rows of exactly these ids, in a few queries — never the
    whole map."""
    wanted = sorted(set(ids))
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(wanted), 400):
        chunk = wanted[i:i + 400]
        got = con.execute(f"SELECT * FROM coverage WHERE bundle_id IN ({','.join('?' * len(chunk))})",
                          chunk).fetchall()
        for r in got:
            row = cov._row(r)
            out[row["bundle_id"]] = row
    return out


def _currents(ids: Map) -> list[str]:
    return [i[0] for by in ids.values() for i in by.values()]


def redundant_here(root: Path, name: str, codex, con, ids: Map | None = None) -> frozenset[str]:
    """The deprecated bundles accepted here whose current equivalent is
    accepted here too — the copies a coverage count leaves out, lest two
    equal copies of a partial part read as the whole part."""
    ids = deprecated(root, name, codex) if ids is None else ids
    if not ids:
        return frozenset()
    rows = _rows_for(con, list(ids) + _currents(ids))
    out = set()
    for dep in ids:
        r = rows.get(dep)
        if not r or not r["accepted_at"] or not by_producer(ids, dep, r["producer"]):
            continue
        cur = ids[dep][r["producer"]][0]
        c = rows.get(cur)
        if c and c["accepted_at"]:
            out.add(dep)
    return frozenset(out)


def split_members(root: Path, name: str, codex, members, con=None):
    """`(current, deprecated)` for the digest: a held bundle its producer
    deprecated is offered under `deprecated_members`, never `members`
    (09 §9.9). Costs nothing when nothing is deprecated."""
    ids = deprecated(root, name, codex)
    if not ids:
        return list(members), []
    space = codex.space_segment
    candidates = {m.name: _id_of(m.name, space) for m in members}
    hit = {n: b for n, b in candidates.items() if b in ids}
    if not hit:
        return list(members), []
    if con is None:
        from contextlib import closing
        with closing(cov.manifest(root, name)) as mcon:
            rows = _rows_for(mcon, hit.values())
    else:
        rows = _rows_for(con, hit.values())
    current, gone = [], []
    for m in members:
        b = hit.get(m.name)
        r = rows.get(b) if b else None
        producer = (r or {}).get("producer")
        if b and by_producer(ids, b, producer):
            gone.append(m)
        else:
            current.append(m)
    return current, gone


def expire(root: Path, name: str, codex, con, *, now: float | None = None, keep_s: float = KEEP_S,
           log: Callable[[str], None] | None = None) -> dict[str, int]:
    """A holder's beat: a deprecated copy accepted here, past its keep
    window, whose current equivalent is accepted here too, loses its
    directory and its row — the vectors stay, the current copy covers
    them. One whose current is not held stays whatever the clock says: it
    is this peer's copy of the part, and the network's fallback."""
    root = Path(root)
    now = time.time() if now is None else now
    out = {"expired": 0, "kept": 0}
    ids = deprecated(root, name, codex)
    if not ids:
        return out
    rows = _rows_for(con, list(ids) + _currents(ids))
    for dep in ids:
        r = rows.get(dep)
        if r is None or not r["accepted_at"] or not by_producer(ids, dep, r["producer"]):
            continue
        cur, when = ids[dep][r["producer"]]
        c = rows.get(cur)
        if now < when + keep_s or c is None or not c["accepted_at"]:
            out["kept"] += 1
            continue
        d = contribute.bundle_dir(root, name, codex, _sid(r["shard"]), dep)
        shutil.rmtree(d, ignore_errors=True)
        con.execute("DELETE FROM coverage WHERE bundle_id=?", (dep,))
        out["expired"] += 1
        if log:
            log(f"[{name}] deprecate: copy {dep[:23]}… of {_sid(r['shard'])} removed after its keep window; the "
                f"current copy {cur[:23]}… is held here (vectors untouched)")
    return out


def rows_digest(bundle_dir: Path) -> str | None:
    """The digest of a held bundle's CHUNK rows alone — `content_digest`
    (09 §9.7) covers the challenge rows too, which every producer embeds
    itself, so two producers' copies of the same vectors never share it.
    Read through `vectors.read` (the files verified) ; None when not here."""
    from app.hold import bundle as bd, vectors as vec
    try:
        header, rows, _possession, _bid = vec.read(bundle_dir)
        n = int(header["challenge"]["n"])
        return bd.content_digest(int(header["dim"]), vec.chunks_of(rows, n), header["dtype"])
    except Exception:   # noqa: BLE001 — not here, or not a bundle
        return None


def foreign_duplicates(root: Path, name: str, codex, con, *, own_key: str,
                       log: Callable[[str], None] | None = None) -> dict[str, str]:
    """`{my_bundle: their_bundle}`: this peer's accepted bundles whose chunk
    rows equal those of an accepted bundle of ANOTHER producer for the same
    part, made earlier — rows landed from that bundle and re-signed as this
    peer's (Perugia, 2026-10-19: 44 bundles of the node's rows; the held
    path excluded only its own bundles' uids), not independent evidence.
    The cheap test first (identical `possession.json`), the rows' digest
    then; a one-off's cost, never a round's."""
    root = Path(root)
    by_shard: dict[str, list[dict[str, Any]]] = {}
    for r in cov.rows(con):
        if r["accepted_at"]:
            by_shard.setdefault(r["shard"], []).append(r)
    out: dict[str, str] = {}
    for shard, rs in by_shard.items():
        mine = [r for r in rs if r["producer"] == own_key]
        theirs = [r for r in rs if r["producer"] and r["producer"] != own_key]
        if not mine or not theirs:
            continue
        sid = _sid(shard)

        def _possession(bid: str) -> bytes | None:
            try:
                return (contribute.bundle_dir(root, name, codex, sid, bid) / "possession.json").read_bytes()
            except OSError:
                return None

        def _created(bid: str) -> int:
            try:
                return int(json.loads((contribute.bundle_dir(root, name, codex, sid, bid) / "header.json")
                                      .read_bytes()).get("created_at") or 0)
            except (OSError, ValueError, AttributeError):
                return 0
        for m in mine:
            pm = _possession(m["bundle_id"])
            if pm is None:
                continue
            dm = None
            for t in sorted(theirs, key=lambda r: _created(r["bundle_id"])):
                if _possession(t["bundle_id"]) != pm or _created(t["bundle_id"]) >= _created(m["bundle_id"]):
                    continue
                dm = rows_digest(contribute.bundle_dir(root, name, codex, sid, m["bundle_id"])) if dm is None else dm
                if dm is not None and dm == rows_digest(contribute.bundle_dir(root, name, codex, sid, t["bundle_id"])):
                    out[m["bundle_id"]] = t["bundle_id"]
                    if log:
                        log(f"[{name}] deprecate: {sid}: this peer's {m['bundle_id'][:23]}… carries the chunk rows "
                            f"of {t['producer'][:23]}…'s earlier {t['bundle_id'][:23]}…")
                    break
    return out


def staged_fraction(staged_dir: Path, member) -> float:
    """How much of a member's bytes are on disk under staging — libtorrent
    sizes a file before filling it, so this is an upper bound; it decides
    only whether a fetch in flight is worth finishing."""
    import os
    total = int(member.bytes) or 1
    landed = 0
    for f in member.files:
        try:
            landed += min(int(f.bytes), os.path.getsize(staged_dir / f.path))
        except OSError:
            continue
    return landed / total


def header_pairs(root: Path, name: str, codex, con, *, own_key: str) -> dict[str, str]:
    """`{deprecated_id: current_id}` for this producer's duplicates — the
    later copies of each `content_digest` group (`withdraw.duplicates`) —
    less the copies this producer deprecated already: a second run states
    nothing twice (the copies stay on disk until their window ends)."""
    from app.share import withdraw
    already = deprecated(root, name, codex)
    out: dict[str, str] = {}
    for d in withdraw.duplicates(root, name, codex, con, own_key=own_key):
        for b in d["withdraw"]:
            if not by_producer(already, b, own_key):
                out[b] = d["keep"]
    return out

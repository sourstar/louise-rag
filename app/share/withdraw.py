"""Withdrawing one's own bundles (03 §3.6; 09 §9.7, §9.8): a producer that
finds a bundle it made should not stand publishes a `withdraw` attestation
naming the ids; peers drop those bundles, and no key is burned. The spec
wrote it for a faulty build that wrote garbage for a week; its first use is
a DUPLICATE (operator, 2026-10-19): the holder pass bundled 418 partially
held parts again after every restart, a new id each time (`created_at` is
in the signed header) — 2,491,650 redundant chunks, 12.1 GiB on tank, and
every peer taking the node's offers would have fetched both copies to find
them identical. The operator's rule is minimum redundancy.

- `duplicates`: per part, this producer's accepted bundles sharing one
  `content_digest` (09 §9.7: the container-independent digest of the rows);
  the earliest `created_at` stays, the rest are withdrawn.
- `publish`: one attestation (`check: "withdraw"`, `result: "withdraw"`,
  `detail.bundles`), signed, kept at 09 §9.8's path, published as an event
  for the beat to forward; the withdrawn directories and coverage rows go
  from this peer. Its vectors stay — the kept bundle covers them; a faulty
  producer drops its own with `hold vectors check --drop` (07 §7.3).
- `withdrawn`: the ids every held withdraw attestation names, by signer —
  derived from what the peer holds, written nowhere (as copy state is).
- `drop_withdrawn`: the receiving side, at the worker's beat: a withdrawn
  bundle held here whose producer is the signer loses its coverage row and
  its directory; its chunks' vectors go only where no other accepted bundle
  of the part claims their records (a duplicate's stay; a faulty bundle's
  "return to without vectors", 03 §3.6). A withdraw signed by anyone but
  the producer withdraws nothing.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from app.hold import envelope as env
from app.hold.codex import canonical_json
from app.share import attest, contribute, coverage as cov, messages

CHECK = "withdraw"
#: strangers' withdraws of bundles held here, already logged (the beat runs often)
_said: set[tuple[str, frozenset[str]]] = set()


def _sid(shard_id: str) -> str:
    """`"<slice>/<n>"` from 04 §4.5's four-segment shard id."""
    return "/".join(shard_id.rsplit("/", 2)[1:])


def _header(root: Path, name: str, codex, shard_id: str, bundle_id: str) -> dict[str, Any] | None:
    p = contribute.bundle_dir(root, name, codex, _sid(shard_id), bundle_id) / "header.json"
    try:
        got = json.loads(p.read_bytes())
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def duplicates(root: Path, name: str, codex, con, *, own_key: str) -> list[dict[str, Any]]:
    """Per part, this producer's accepted bundles that share a `content_digest`:
    `{shard, content_digest, keep, withdraw: [ids], chunks}`, `keep` the one
    with the earliest `created_at` (then the lowest id)."""
    root = Path(root)
    by_shard: dict[str, dict[str, list[tuple[int, str, int]]]] = {}
    for r in cov.rows(con):
        if r["producer"] != own_key or not r["accepted_at"]:
            continue
        header = _header(root, name, codex, r["shard"], r["bundle_id"])
        if header is None or not header.get("content_digest"):
            continue
        by_shard.setdefault(r["shard"], {}).setdefault(str(header["content_digest"]), []).append(
            (int(header.get("created_at") or 0), r["bundle_id"], int(r["chunks"] or 0)))
    out: list[dict[str, Any]] = []
    for shard, groups in sorted(by_shard.items()):
        for digest, items in sorted(groups.items()):
            if len(items) < 2:
                continue
            items.sort()
            out.append({"shard": shard, "content_digest": digest, "keep": items[0][1],
                        "withdraw": [i[1] for i in items[1:]], "chunks": sum(i[2] for i in items[1:])})
    return out


def attestation_body(codex, snapshot, bundle_ids: list[str], shards: list[str], *, own_key: str,
                     platform_image: str, now: float) -> dict[str, Any]:
    """09 §9.8's attestation with `check` and `result` both `withdraw` and the
    ids under `detail.bundles`; `shards` the parts they covered."""
    return {"kind": "louise-attestation", "version": 3, "collection_id": codex.collection_id,
            "snapshot_id": snapshot.snapshot_id, "snapshot_hash": snapshot.hash, "check": CHECK,
            "seed": None, "shards": sorted(set(shards)), "result": CHECK,
            "detail": {"bundles": sorted(set(bundle_ids))}, "platform_image": platform_image,
            "encoder_engine": None, "encoder_weights": None, "key": own_key,
            "time": messages.time_text(now)}


def drop_locally(root: Path, name: str, codex, con, bundle_ids: list[str], *,
                 log: Callable[[str], None] | None = None) -> int:
    """The producer's own side of a withdraw: each bundle's directory and
    coverage row go; the vectors stay (the kept bundle covers them, or
    `hold vectors check --drop` is the tool for a faulty one)."""
    root = Path(root)
    gone = 0
    for bid in bundle_ids:
        row = cov.row(con, bid)
        if row is not None:
            shutil.rmtree(contribute.bundle_dir(root, name, codex, _sid(row["shard"]), bid), ignore_errors=True)
            con.execute("DELETE FROM coverage WHERE bundle_id=?", (bid,))
            gone += 1
    if log and gone:
        log(f"[{name}] withdraw: {gone} bundle(s) of this peer's gone from state/bundles and the coverage map")
    return gone


def publish(root: Path, name: str, codex, snapshot, bundle_ids: list[str], *, key, own_key: str, con, node_con,
            platform_image: str, now: float | None = None,
            log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """The attestation written, published as an event, and the bundles
    dropped here. Only this producer's own accepted bundles are withdrawn:
    an id that is not one is refused before anything is written."""
    root = Path(root)
    now = time.time() if now is None else now
    shards: list[str] = []
    for bid in bundle_ids:
        row = cov.row(con, bid)
        if row is None or row["producer"] != own_key or not row["accepted_at"]:
            raise ValueError(f"{bid}: not an accepted bundle of this peer's — a producer withdraws only its own "
                             f"(09 §9.8)")
        shards.append(row["shard"])
    body = attestation_body(codex, snapshot, list(bundle_ids), shards, own_key=own_key,
                            platform_image=platform_image, now=now)
    envelope = env.sign(env.ATTESTATION, body, key)
    aid = env.body_id(body)
    p = attest.attestation_path(root, name, codex, aid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.withdraw")
    tmp.write_bytes(canonical_json(envelope))
    tmp.replace(p)
    published = attest.publish(root, name, codex, [aid], key=key, own_key=own_key, con=node_con, now=now, log=log)
    gone = drop_locally(root, name, codex, con, list(bundle_ids), log=log)
    if log:
        log(f"[{name}] withdraw: attestation {aid[:23]}… names {len(set(bundle_ids))} bundle(s) over "
            f"{len(set(shards))} part(s); published for the beat to forward (03 §3.6)")
    return {"attestation": aid, "bundles": sorted(set(bundle_ids)), "shards": sorted(set(shards)),
            "dropped_here": gone, "published": published}


def withdrawn(root: Path, name: str, codex) -> dict[str, frozenset[str]]:
    """bundle id -> the keys whose held withdraw attestations name it (09
    §9.8; cached with the attestations). Whose word counts is `by_producer`'s
    question: only the bundle's producer withdraws it."""
    from app import copy_state
    out: dict[str, set[str]] = {}
    if not getattr(codex, "collection_id", None):
        return {}
    for b in copy_state.attestations(Path(root), name, codex):
        if b.get("check") != CHECK or b.get("result") != CHECK:
            continue
        signer = str(b.get("key") or "")
        for bid in (b.get("detail") or {}).get("bundles") or []:
            if isinstance(bid, str) and signer:
                out.setdefault(bid, set()).add(signer)
    return {k: frozenset(v) for k, v in out.items()}


def by_producer(ids: Mapping[str, frozenset[str]], bundle_id: str, producer: str | None) -> bool:
    """Whether the bundle's PRODUCER withdrew it — a withdraw signed by anyone
    else withdraws nothing, and an unknown producer (a bundle not fetched
    yet) decides nothing: `take` reads the header first."""
    return bool(producer) and producer in ids.get(bundle_id, ())


def _chunk_ids(bundle_dir: Path, uids: set[str]) -> list[str]:
    """The chunk ids of `vectors.parquet` whose record is one of `uids`
    (a chunk id is `<uid>:<seq>:<version>`); the challenge rows are no
    record's and never match."""
    try:
        import pyarrow.parquet as pq
        column = pq.read_table(bundle_dir / "vectors.parquet", columns=["chunk_id"]).column("chunk_id").to_pylist()
    except Exception:   # noqa: BLE001 — a bundle whose rows cannot be read names no vectors to drop
        return []
    return [c for c in column if isinstance(c, str) and c.rsplit(":", 2)[0] in uids]


def drop_withdrawn(root: Path, name: str, codex, con, *, delete_chunks: Callable[[list[str]], Any] | None = None,
                   log: Callable[[str], None] | None = None) -> dict[str, int]:
    """The receiving side: every withdrawn bundle HELD here (accepted, or its
    directory present) whose producer signed the withdraw loses its coverage
    row and its directory; its chunks' vectors go (`delete_chunks`) only
    where no other accepted bundle of the part claims their records. A claim
    row for a bundle never fetched is left: `listed_bundles` and `take`
    decide whether it is asked for. Cheap when nothing is withdrawn: one
    cached read of the attestations."""
    root = Path(root)
    out = {"dropped": 0, "vectors": 0, "not_the_producer": 0}
    ids = withdrawn(root, name, codex)
    if not ids:
        return out
    held: list[dict[str, Any]] = []
    wanted = sorted(ids)
    for i in range(0, len(wanted), 400):             # per beat: the withdrawn ids, never the whole map
        chunk = wanted[i:i + 400]
        got = con.execute(f"SELECT * FROM coverage WHERE bundle_id IN ({','.join('?' * len(chunk))})",
                          chunk).fetchall()
        held.extend(cov._row(r) for r in got)
    for r in held:
        bid = r["bundle_id"]
        sid = _sid(r["shard"])
        d = contribute.bundle_dir(root, name, codex, sid, bid)
        if not r["accepted_at"] and not d.is_dir():
            continue                                 # a claim, not a holding: the fetch's question
        header = _header(root, name, codex, r["shard"], bid)
        producer = str((header or {}).get("producer_pubkey") or r["producer"] or "") or None
        if not by_producer(ids, bid, producer):
            out["not_the_producer"] += 1
            if log and (bid, ids[bid]) not in _said:          # once a process, not once a beat
                _said.add((bid, ids[bid]))
                log(f"[{name}] withdraw of {bid[:23]}… signed by "
                    f"{', '.join(k[:23] + '…' for k in sorted(ids[bid]))}, not its producer "
                    f"{(producer or '?')[:23]}…: ignored (09 §9.8)")
            continue
        if d.is_dir() and delete_chunks is not None and r["accepted_at"]:
            try:
                mine = set(json.loads((d / "possession.json").read_bytes()).keys())
            except (OSError, ValueError, AttributeError):
                mine = set()
            others: set[str] = set()
            for o in cov.rows(con, shard=r["shard"]):
                if o["bundle_id"] == bid or not o["accepted_at"] or by_producer(ids, o["bundle_id"], o["producer"]):
                    continue
                od = contribute.bundle_dir(root, name, codex, sid, o["bundle_id"]) / "possession.json"
                try:
                    others.update(json.loads(od.read_bytes()).keys())
                except (OSError, ValueError, AttributeError):
                    continue
            orphan = mine - others
            if orphan:
                chunk_ids = _chunk_ids(d, orphan)
                if chunk_ids:
                    delete_chunks(chunk_ids)
                    out["vectors"] += len(chunk_ids)
        shutil.rmtree(d, ignore_errors=True)
        con.execute("DELETE FROM coverage WHERE bundle_id=?", (bid,))
        out["dropped"] += 1
        if log:
            log(f"[{name}] withdraw: bundle {bid[:23]}… dropped as its producer asked (03 §3.6)"
                + (f"; {out['vectors']} vector(s) of records no other bundle claims gone" if out["vectors"] else ""))
    return out

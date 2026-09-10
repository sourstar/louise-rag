"""`hold vectors export` — one shard's vectors as 09 §9.7's bundle.

Migration plan step 4d. `app/hold/vectors.py` owns the four files and
`app/hold/bundle.py` owns their bytes; this owns the GATHERING — which
records are in the shard, which chunks they cut into, which of those this
peer actually holds a vector for, and what the header may therefore say.

WHAT IT DOES NOT KNOW. Where vectors are stored. They live in a local
LanceDB table today (invariant 6: "Lance holds vectors locally — a bundle
travels as Parquet"), and that is the app's business, not this module's:
`app/hold/` stays free of the corpus machinery so a peer checking a
bundle needs none of it. So the caller injects `vectors_for`, exactly as
`hold collection create` injects `embed`. It keeps this testable without
a Lance table and keeps one storage decision in one place.

ONE SHARD PER BUNDLE, which is 09 §9.7's constraint and not a choice
here: `shard` is a required header field with a single value, so a bundle
spanning two shards cannot be described by a conformant header.

WHAT GOES IN, precisely, because the header is signed and three of these
are easy to get subtly wrong:

  * `uids` is the uids this bundle actually carries a vector for — NOT
    every record in the shard. `possession` must name exactly the same
    set (§9.7, `check_possession_covers`), so a record whose chunks are
    all unembedded belongs in neither. A bundle is what this peer
    produced, and a peer that has embedded half a shard says so by
    carrying half a shard.
  * `possession` maps each of those uids to the record's `content_hash`
    as the drag holds it. That is what makes a bundle admissible against
    a peer's own copy (03 §3.5: admissibility is `space_id` plus
    per-record `content_hash`), and it is read from the shard rather than
    recomputed, because the shard is what the snapshot's roots cover.
  * the CHALLENGE rows are the calibration set's first sixteen texts
    embedded by this peer, now (§9.7, T2). They are rows in the same
    parquet, before the chunk rows, and `chunk_count` counts only the
    chunks — `vectors.write` derives that, so it cannot disagree.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import layers as layers_mod
from . import bundle as bd
from . import calibration as cal
from . import collection as col
from . import drag as drag_mod
from . import snapshot as snap
from . import vectors as vec
from .codex import CodexError

#: 09 §9.7's T2 challenge: the calibration set's first sixteen texts.
CHALLENGE_N = 16

#: HOW BIG A BUNDLE IS — operator decision, 2026-09-10, and the two
#: documents disagreed. 04 §4.5 says a contributor emits one "every few
#: hundred chunks"; 09 §9.10's worked example shows the same shard covered
#: by two bundles of 14,800 and 15,200. The numbers decided it: at 300
#: chunks a fully covered PubMed has ~120,000 coverage rows and ~120,000
#: members to announce, and the digest's `coverage` and `members` lists
#: become megabytes; at 15,000 it has ~2,400. T3's sixteen recomputes are
#: 5.3 % of production at 300 and 0.16 % at 15,000.
#:
#: The argument FOR small bundles was crash granularity, and it is gone:
#: `hold vectors build` commits to the local store every
#: `EMBED_SLICE_CHUNKS` chunks, so nothing is lost by a bundle that spans
#: an hour of work. A bundle is now purely a unit of SHARING.
#:
#: A floor, from step 4e: under 32 rows T0's envelope is not applied at
#: all (a sample deviation's relative standard error is ~1/sqrt(2n)), so a
#: bundle sized in the tens buys a weaker admission than one sized in the
#: hundreds.
BUNDLE_CHUNKS = 15_000


class ExportError(CodexError):
    """The shard cannot be exported as a conformant bundle."""


def _shard_of(snapshot: snap.Snapshot, shard: str) -> snap.Shard:
    """The named shard entry, `"<slice>/<n>"`, as `Shard.path` spells it."""
    want = str(shard).strip()
    for s in snapshot.shards:
        if f"{s.slice}/{s.n:05d}" == want or f"{s.slice}/{s.n}" == want:
            return s
    have = ", ".join(f"{s.slice}/{s.n:05d}" for s in snapshot.shards[:6])
    raise ExportError(
        f"snapshot {snapshot.snapshot_id} has no shard {want!r}; it has "
        f"{len(snapshot.shards)} ({have}{'…' if len(snapshot.shards) > 6 else ''})")


def _records(drag_dir: Path, shard: snap.Shard) -> list[dict]:
    from app.pack import records as pack_records
    path = Path(drag_dir) / shard.path
    if not path.is_file():
        raise ExportError(
            f"{path} is not in this copy; a bundle is exported from the drag, "
            f"and this peer does not hold that shard's records")
    return list(pack_records.iter_records(path))


def rows_for(codex, records: Sequence[dict],
             vectors_for: Callable[[list[str]], dict[str, list]],
             ) -> tuple[list[tuple[str, list]], dict[str, str]]:
    """`(chunk_rows, possession)` for the records this peer has vectors for.

    Chunks are derived here and never read from a record — a record
    carries none (02 §2.3), and deriving them through
    `layers.chunks_for_document` is what makes a wrong layer a caught
    error rather than a reproduced one (06 §6.3).
    """
    chunk_rows: list[tuple[str, list]] = []
    possession: dict[str, str] = {}
    for uid, content_hash, rows in grouped(codex, records, vectors_for):
        chunk_rows.extend(rows)
        possession[uid] = content_hash
    return chunk_rows, possession


def grouped(codex, records: Sequence[dict],
            vectors_for: Callable[[list[str]], dict[str, list]],
            ) -> list[tuple[str, str, list[tuple[str, list]]]]:
    """`(uid, content_hash, its held chunk rows)`, one entry per record.

    The grouping exists because a bundle is SPLIT on record boundaries
    (`parts` below) and not on a row count: `possession.json` maps a uid
    to the content hash the producer embedded against, so a record whose
    rows straddled two bundles would be claimed by both — and a re-issued
    record would then have to be refused twice, out of two bundles that
    might not arrive together.
    """
    wanted: list[str] = []
    per_uid: dict[str, list[str]] = {}
    for rec in records:
        uid = rec["uid"]
        sections = [tuple(s) for s in rec.get("sections") or []]
        row = {"uid": uid, "native_id": rec.get("native_id", 0)}
        ids = [c["id"] for c in layers_mod.chunks_for_document(codex, row, sections)]
        per_uid[uid] = ids
        wanted.extend(ids)

    held = vectors_for(wanted) or {}
    by_uid = {r["uid"]: r for r in records}
    out = []
    for uid, ids in per_uid.items():
        mine = [(i, held[i]) for i in ids if i in held]
        if not mine:
            continue                      # not embedded here: not in this bundle
        out.append((uid, by_uid[uid]["content_hash"], mine))
    return out


def parts(entries, max_chunks: int = BUNDLE_CHUNKS):
    """Split a shard's records into bundles of about `max_chunks` chunks.

    ON RECORD BOUNDARIES, for the reason `grouped` gives, and never
    mid-record even when one record alone exceeds the target: a bundle
    over the size is a bundle, and a record split across two is a
    possession map that lies in both.

    A record is placed in the current part unless doing so would take it
    over — so the parts are at most one record's worth over the target,
    and the last one may be much smaller. §9.10's own example is two
    bundles of 14,800 and 15,200 for one shard, so uneven parts are the
    documented shape and not a defect.
    """
    if max_chunks < 1:
        raise ExportError("a bundle holds at least one chunk")
    out, current, n = [], [], 0
    for entry in entries:
        size = len(entry[2])
        if current and n + size > max_chunks:
            out.append(current)
            current, n = [], 0
        current.append(entry)
        n += size
    if current:
        out.append(current)
    return out


def challenge_rows(codex, embed) -> list[tuple[str, list]]:
    """The first sixteen calibration texts, embedded by this peer now.

    `verify` rather than `load`: a platform whose set hashes otherwise is
    not carrying the set this Codex names, and a challenge computed over
    other texts would fail every importer for a reason none of them could
    diagnose.
    """
    texts = cal.verify(codex.modality, codex.space.calibration.set)
    got = embed(list(texts[:CHALLENGE_N]))
    if len(got) != CHALLENGE_N:
        raise ExportError(
            f"the encoder returned {len(got)} vectors for {CHALLENGE_N} "
            f"challenge texts")
    return [(bd.challenge_id(i), list(v)) for i, v in enumerate(got)]


def export(root, ref: str, *, shard: str, dest, vectors_for, embed, key,
           snapshot_id: str | None = None, created_at: int | None = None,
           max_chunks: int = BUNDLE_CHUNKS) -> dict[str, Any]:
    """Write one shard's bundles into `dest` (a DIRECTORY, 09 §9.7).

    ONE BUNDLE PER SHARD is the common case and `dest` is then the bundle
    itself. A shard with more than `max_chunks` chunks becomes several,
    at `dest/00000`, `dest/00001`, … — 09 §9.10's own worked example is
    one shard covered by two bundles, and 04 §4.5 says a contributor
    emits them rather than waiting for the end of a shard. The numbering
    is not in any format: a bundle is named by its id, and these are only
    where this command put the files.

    `vectors_for(chunk_ids) -> {chunk_id: vector}` is this peer's own
    store; `embed(texts) -> vectors` is its encoder. Both are injected —
    see the module docstring.
    """
    name, codex = col.resolve(root, ref)
    drag_dir = col.drag_of(root, name)

    if snapshot_id:
        path = drag_dir / drag_mod.snapshot_path(snapshot_id)
        if not path.is_file():
            raise ExportError(f"no snapshot {snapshot_id!r} in this copy")
    else:
        # Snapshot ids sort chronologically by construction (09 §9.4:
        # "2026-08", "2026-W32"), so the last name IS the latest — no need
        # to open every file to find out.
        d = drag_dir / "snapshots"
        ids = sorted(p.stem for p in d.glob("*.json")
                     if not p.name.endswith(".sig.json")) if d.is_dir() else []
        if not ids:
            raise ExportError(
                f"{drag_dir} holds no snapshot; a bundle names the snapshot "
                f"its records come from (09 §9.7) and there is not one yet")
        path = d / f"{ids[-1]}.json"
    snapshot = snap.load(path)

    entry = _shard_of(snapshot, shard)
    records = _records(drag_dir, entry)
    entries = grouped(codex, records, vectors_for)
    if not entries:
        raise ExportError(
            f"this peer holds no vectors for any record of {shard}; there is "
            f"nothing to export. A bundle carries what a peer PRODUCED, so an "
            f"empty one is not a smaller bundle but an absent one")

    split = parts(entries, max_chunks)
    # The challenge is embedded ONCE and the same sixteen rows go in every
    # bundle of this shard: they answer "is the producer's oven this
    # space's", which is a fact about the producer and not about the rows
    # beside them. Sixteen recomputes per split would also be sixteen more
    # encoder calls for nothing.
    challenge = challenge_rows(codex, embed)
    now = int(created_at if created_at is not None else time.time())

    out = []
    for i, part in enumerate(split):
        chunk_rows = [row for _uid, _h, rows in part for row in rows]
        possession = {uid: h for uid, h, _rows in part}
        where = Path(dest) if len(split) == 1 else Path(dest) / f"{i:05d}"
        header = {
            "version": 1,
            "collection": codex.collection_id,
            "space_id": codex.space_id,
            "snapshot_id": snapshot.snapshot_id,
            "shard": f"{entry.slice}/{entry.n:05d}",
            # Sorted, and the same set possession names — `vectors.write`
            # does not police that, but §9.7's reader does, on every import.
            "uids": sorted(possession),
            "dim": codex.space.dim,
            "dtype": codex.space.dtype,
            "producer_pubkey": "ed25519:" + bytes(key.verify_key).hex(),
            "encoder_image": codex.space.encoder.image,
            "encoder_weights": codex.space.encoder.weights,
            "created_at": now,
            "digest_alg": "sha256",
            "sig_alg": "ed25519",
            "challenge": {"set": codex.space.calibration.set, "n": CHALLENGE_N},
        }
        bundle_id = vec.write(where, header=header, challenge_rows=challenge,
                              chunk_rows=chunk_rows, possession=possession,
                              key=key)
        out.append({"bundle_id": bundle_id, "path": str(where),
                    "uids": len(possession), "chunks": len(chunk_rows)})

    return {
        # The first bundle's, for the common case where there is one. Every
        # bundle is in `bundles`, always.
        "bundle_id": out[0]["bundle_id"],
        "path": out[0]["path"],
        "bundles": out,
        "shard": f"{entry.slice}/{entry.n:05d}",
        "snapshot_id": snapshot.snapshot_id,
        "uids": sum(b["uids"] for b in out),
        "chunks": sum(b["chunks"] for b in out),
        "records_in_shard": len(records),
        "dim": codex.space.dim,
        "dtype": codex.space.dtype,
    }

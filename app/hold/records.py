"""Records — the roots a snapshot publishes over a shard, and the rules a
build must not break. docs/02-collections.md §2.2, §2.3; docs/09-formats.md
§9.4 (the shard entry), §9.5 (the record line and its hashes), §9.14 (the
relations shard).

Pure functions over bytes. The shard WRITER lives in `app/pack/records.py`
and is not duplicated here — this module computes what 09 §9.4 asks a
snapshot to carry about a shard the writer produced, and refuses the two
things a build must never do.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Sequence

from .codex import CodexError

#: 09 §9.5's chunk id: `<uid>:<seq>:<chunker version>`.
CHUNK_ID = "{uid}:{seq}:{version}"


class RecordsError(CodexError):
    """A build broke a rule the format depends on."""


# --------------------------------------------------------------------------
# the two roots a snapshot publishes over a shard

def content_hash(title: str, sections: Sequence[Sequence[str]]) -> str:
    """09 §9.5: BLAKE2b-128 of the canonical JSON of `[title, [[heading,
    text], …]]` — the title and the sections exactly as the chunker sees
    them, so the same hash means the same chunks and the same vectors
    whatever the origin's own revision fields say.

    Nothing else may enter it. A figure, an `extra` key, a locator: none of
    them are what the chunker reads, and `possession.json` (09 §9.7) binds a
    bundle to this value, so moving it orphans that record's vectors on
    every peer that holds them.
    """
    blob = json.dumps([title or "", [[h or "", t or ""] for h, t in sections]],
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.blake2b(blob, digest_size=16).hexdigest()


def records_root(content_hashes: Iterable[str]) -> str:
    """09 §9.4: SHA-256 over the shard's `content_hash` values as raw 16-byte
    digests concatenated in record order.

    This is what equivalence (03 §3.4), inheritance (02 §2.7) and
    `records-vs-origin` compare, and it does not depend on the compressor —
    which is why they compare it rather than `root`.
    """
    h = hashlib.sha256()
    n = 0
    for ch in content_hashes:
        raw = bytes.fromhex(ch)
        if len(raw) != 16:
            raise RecordsError(f"content_hash is BLAKE2b-128, 32 hex characters; "
                               f"got {ch!r}")
        h.update(raw)
        n += 1
    if n == 0:
        raise RecordsError("a shard holds at least one record")
    return "sha256:" + h.hexdigest()


def chunks_root(chunks: Iterable[tuple[str, str]]) -> str:
    """09 §9.4: SHA-256 over, in chunk order, each chunk's id as UTF-8 bytes
    followed by the raw 16-byte BLAKE2b-128 of its text.

    `chunks` yields `(chunk_id, text)`. It depends on the records and the
    Codex's layers alone — exactly as the `chunks` count does — so the
    snapshot stays records-only (02 §2.2). Without it the `chunks` check
    could compare only a count, and a chunker that moved every boundary
    without changing the number would pass (09 §9.5).
    """
    h = hashlib.sha256()
    for chunk_id, text in chunks:
        h.update(chunk_id.encode("utf-8"))
        h.update(hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest())
    return "sha256:" + h.hexdigest()


def chunk_id(uid: str, seq: int, chunker_version: int) -> str:
    return CHUNK_ID.format(uid=uid, seq=seq, version=chunker_version)


# --------------------------------------------------------------------------
# what a build refuses

def order_key(native_id: str, order: str):
    """How a source's ids sort within a slice (02 §2.2, 09 §9.3's `order`).

    `"numeric"` is integer order of the id; `"bytes"` is UTF-8 byte order.
    A numeric source whose id is not an integer is a build error and not a
    silent fallback to bytes: the two orders disagree ("10" sorts before
    "9" in bytes and after it in numbers), and a fallback would cut two
    peers' shards differently.
    """
    if order == "bytes":
        return native_id.encode("utf-8")
    if order == "numeric":
        try:
            return int(native_id)
        except ValueError:
            raise RecordsError(
                f"native_id {native_id!r} is not an integer, but the "
                f"collection's slicing.order is \"numeric\"; the two orders "
                f"disagree and a fallback would cut two peers' shards "
                f"differently (09 §9.3)") from None
    raise RecordsError(f"slicing.order must be bytes|numeric, got {order!r}")


def check_in_order(native_ids: Sequence[str], order: str) -> None:
    """Records within a slice sort by `native_id` (02 §2.2)."""
    keys = [order_key(i, order) for i in native_ids]
    for a, b, ka, kb in zip(native_ids, native_ids[1:], keys, keys[1:]):
        if kb < ka:
            raise RecordsError(f"records are cut in `native_id` order and "
                               f"{b!r} sorts before {a!r} (09 §9.3's order "
                               f"is {order!r})")


def refuse_out_of_order(native_id: str, open_first_id: str | None,
                        order: str, *, slice_: str, n: int) -> None:
    """02 §2.2: `hold records build` REFUSES a record whose `native_id` sorts
    before the open shard's `first_id`, naming the record and the shard,
    rather than placing it out of order.

    This is what makes shard ranges a PARTITION of the id space, which is
    what 05 §5.5 needs to resolve a uid to a shard from a snapshot alone,
    and what stops two honest peers cutting one cutoff into different shards
    — one re-cutting the slice, one appending. A source that cannot promise
    it needs a different `native_id` rule or a different slicing key, not a
    different shard.
    """
    if open_first_id is None:
        return
    if order_key(native_id, order) < order_key(open_first_id, order):
        raise RecordsError(
            f"{native_id!r} sorts before {open_first_id!r}, the first id of "
            f"the open shard {slice_}/{n:05d}. Shard ranges must partition "
            f"the id space (02 §2.2, 05 §5.5), so this record is refused "
            f"rather than placed out of order — the source's native_id_order "
            f"does not hold, and it needs a different id rule or a different "
            f"slicing key")


def refuse_without_origin_hash(uid: str, origin_hash: str | None) -> None:
    """CLAUDE.md invariant 11, and 02 §2.3: a record with no `origin_hash` is
    REFUSED at build, never warned about. A pack whose records all verify
    internally while being permanently unverifiable against the source is
    the worst artefact we could ship, and it is published once and adopted
    by peers who cannot repair it."""
    if not origin_hash:
        raise RecordsError(
            f"{uid}: no origin_hash. A record carries the hash of the origin "
            f"unit it was parsed from, or it cannot be verified against the "
            f"source ever again (02 §2.3, 06 §6.4)")
    if len(origin_hash) != 32 or not all(c in "0123456789abcdef" for c in origin_hash):
        raise RecordsError(f"{uid}: origin_hash is BLAKE2b-128, 32 lowercase "
                           f"hex characters, no prefix; got {origin_hash!r}")


# --------------------------------------------------------------------------
# relations (09 §9.14)

EDGE_KEYS = ("from", "to", "type", "snapshot")


def edge_line(from_uid: str, to: str, type_: str, snapshot_id: str) -> bytes:
    """09 §9.14's edge line: compact JSON, keys in this order,
    `\\n`-terminated."""
    if ":" not in type_:
        raise RecordsError(f"edge type {type_!r} is `<plugin>:<name>`, "
                           f"registered by the plugin (09 §9.14)")
    obj = {"from": from_uid, "to": to, "type": type_, "snapshot": snapshot_id}
    ordered = {k: obj[k] for k in EDGE_KEYS}
    return json.dumps(ordered, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8") + b"\n"


def edges_of(source, doc, *, from_uid: str, snapshot_id: str) -> list[bytes]:
    """The edge lines a plugin's optional `relations()` yields for one
    document (06 §6.1). The platform supplies `from` and `snapshot`, which
    the plugin cannot know; the plugin supplies `to` and `type`.

    A plugin that does not implement it yields nothing, and the collection
    has no relations member.
    """
    fn = getattr(source, "relations", None)
    if fn is None:
        return []
    return [edge_line(from_uid, e.to, e.type, snapshot_id) for e in fn(doc)]

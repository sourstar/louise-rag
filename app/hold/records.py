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
    them are what the chunker reads. A figure is the case worth naming,
    because it is the one that keeps being proposed: it is a MEMBER, named
    in the record by hash and carried under `figures/<slice>/`, while its
    caption stays in `sections` and stays indexed (operator decision,
    2026-09-08).

    `possession.json` (09 §9.7) binds a bundle to this value, so moving it
    costs that record's rows on every peer holding them — its rows, not the
    bundle's, since §9.7 was amended to 03 §3.5 and 07 §7.3's per-record
    refusal. That is two to three orders of magnitude cheaper than the
    older reading and it is still not free, and it is not the reason
    anyway: a figure stays out because it is not text, not because moving
    it would be expensive.
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


# --------------------------------------------------------------------------
# computing chunks_root where the chunks actually are

def chunk_contribution(chunk_id_: str, text: str) -> bytes:
    """One chunk's contribution to `chunks_root`: its id as UTF-8 bytes
    followed by the raw 16-byte BLAKE2b-128 of its text (09 §9.4).

    It exists as its own function because the chunks are produced in the
    parse pool's children, one document at a time, and re-chunking in the
    parent to compute the root would double the cost of a build — parse,
    chunk and hash per document IS the cost of a pack pass. A child folds
    each chunk into a blob as it goes; the parent concatenates the blobs in
    record order and hashes once.
    """
    return (chunk_id_.encode("utf-8")
            + hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest())


def chunks_root_of(blob: bytes) -> str:
    """`chunks_root` over an already-concatenated sequence of
    `chunk_contribution` values, in chunk order."""
    return "sha256:" + hashlib.sha256(blob).hexdigest()


# --------------------------------------------------------------------------
# the build driver (07 §7.4: `hold.records.build` is what the command calls)
#
# Everything above this line is a pure function over bytes. Below it is the
# one thing that touches disk: driving the existing pack builder into a
# collection's SHARD. It is a WRAPPER — the shard cutting, the parse pool,
# the resumability and the cutoff rule all stay in `app/pack/build.py`,
# which has been carrying real corpora for months.

STATE = "state"
BUILD = "build"


def _paths(root, name: str):
    from pathlib import Path
    base = Path(root) / name
    return base / "shard", base / STATE, base / STATE / BUILD


def build(root, ref: str, *, corpus_dir=None, snapshot_id: str | None = None,
          records_per_shard: int | None = None, log=print, stop=None):
    """`louise hold records build <collection>` — parse, slice, shard, hash.

    The build runs into a STAGING directory under the collection's state,
    and its records are promoted into the SHARD in one step. That is how a
    fetch already works (04 §4.3, "Fetched shards land in staging and are
    promoted into the copy in one step"), and it is what keeps the SHARD
    holding only what the Codex or a snapshot names (07 §7.8) — the legacy
    builder also writes a `manifest.json`, a `TERMS.txt` and a
    `validation.json` of its own shape, none of which are SHARD members
    under a Codex.

    Returns the underlying `BuildResult`. The shard entries 09 §9.4 needs
    are in the staging build database, where `hold snapshot create` reads
    them; nothing is written to the SHARD but the record shards themselves.
    """
    from dataclasses import replace as _dc_replace
    from pathlib import Path

    from app import collections as collections_mod, db, discovery
    from app.pack import build as pack_build

    from . import collection as col

    name, codex = col.resolve(Path(root), ref)
    shard_dir, state_dir, staging = _paths(root, name)
    state_dir.mkdir(parents=True, exist_ok=True)

    held = collections_mod.by_name(name) if Path(root).resolve() == (collections_mod.louise_root()
                                                                    / "collections").resolve() else None
    if held is not None and getattr(held, "source", "origin") == "shard":
        raise RecordsError(
            f"{name}: its records are HELD, not built — this peer reads them from the shards "
            f"`share fetch` landed (source = \"shard\", 07 §7.8); the producer builds, a fetcher does not")

    if corpus_dir is None:
        raise RecordsError(
            f"{name}: no corpus directory. `records build` parses what is on "
            f"disk; `records fetch` is what puts it there (07 §7.3), and "
            f"until that lands the corpus is named with --corpus")

    src = local_collection(root, name, codex, corpus_dir=corpus_dir)

    # The manifest is the list of documents the corpus holds. `records
    # fetch` will fill it from the acquisition spec; for a corpus already on
    # disk, scanning it is the same act, so an empty manifest is scanned
    # rather than refused.
    con = db.connect(src)
    try:
        held = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        if not held:
            log(f"[records build] {name}: manifest is empty, scanning {corpus_dir}")
            discovery.sync_collection(con, src)
    finally:
        con.close()

    _claim_staging(staging, codex)
    result = pack_build.build(
        src, staging, snapshot=snapshot_id, name=name, sign=False,
        records_per_shard=records_per_shard, log=log, stop=stop)
    if result.interrupted:
        # A stopped build promotes NOTHING: what it cut stays in staging,
        # recorded, and the next run resumes it. Promoting the finished part
        # put a partial record set into the SHARD, beside a snapshot that
        # names the whole (found 2026-09-21).
        log(f"[records build] {name}: stopped after {result.shards} shard(s); "
            f"staged, not promoted — the next run resumes")
        return result

    promoted = _promote_records(staging, shard_dir)
    log(f"[records build] {name}: {result.records} records in {result.shards} "
        f"shard(s), {promoted} promoted into the SHARD")
    return result


#: What a staging build was started FOR, beside its build database. `pack
#: build` resumes whatever that database holds and checks only the plugin,
#: the cutoff and the shard size, so a build begun under one Codex or parser
#: is resumed under the next as if it were the same work: every shard it had
#: already cut counts as done and is promoted unchanged. On 2026-09-21
#: pubmed's staging still held a parser-4 build, and a new Codex's build
#: would have resumed it and shipped parser-4 records under parser 8.
STAGING_CLAIM = ".codex.json"


def _claim_staging(staging, codex) -> None:
    import json
    from pathlib import Path

    staging = Path(staging)
    claim = {"collection_id": codex.collection_id, "parser": codex.parser.to_dict()}
    path = staging / STAGING_CLAIM
    if path.is_file():
        held = json.loads(path.read_text(encoding="utf-8"))
        if held != claim:
            raise RecordsError(
                f"{staging} holds a build of {held.get('collection_id')} with "
                f"parser {held.get('parser')}, and this is {codex.collection_id} "
                f"with parser {claim['parser']}. Resuming it would count that "
                f"build's shards as this one's. Move it aside and build again")
        return
    if (staging / ".build").exists():
        raise RecordsError(
            f"{staging} holds a build with no record of which Codex or parser "
            f"it was started for, and resuming it would count its shards as "
            f"this one's. Move it aside and build again")
    staging.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(claim, sort_keys=True), encoding="utf-8")


def local_collection(root, name: str, codex, *, corpus_dir=None):
    """This peer's local side of a Codex collection, for a stage that USES
    its data — so it is gated here, once, for every such stage (invariant
    9): records build before its scan, vectors export/import/build/check,
    archive import, check, snapshot create.

    `_ack_of` below only asks whether an acknowledgement FILE exists; a
    file whose hash no longer matches the terms passed it. THE resolution
    (`licensing.require_ack`) compares the hash (audit, 2026-09-13).
    """
    local = _local_collection(root, name, codex, corpus_dir=corpus_dir)
    from app import licensing
    licensing.require_ack(local, use="its data is used here")
    return local


def _local_collection(root, name: str, codex, *, corpus_dir=None):
    """This peer's LOCAL side of a Codex collection, as 07 §7.8 lays it out.

    A Codex says what a collection IS; the manifest, the text index and the
    vector table are what this peer keeps about it, and they live beside
    the SHARD rather than in it — `collections/<name>/state/`, `/index/`,
    `/lance/`, none of which `share` serves.

    Every stage that needs them builds this the same way, from here, and
    not each for itself: `records build` fills the manifest, `vectors
    export` reads the vector table, and a disagreement between two such
    constructions would be one of them writing to a directory the other
    never reads.
    """
    from pathlib import Path

    from app import collections as collections_mod

    from app import layers as layers_mod

    _, state_dir, _ = _paths(root, name)
    return collections_mod.Collection(
        name=name, plugin=codex.plugin, profile=None, embed_mode_default=None,
        corpus_dir=Path(corpus_dir) if corpus_dir is not None else None,
        manifest_path=state_dir / "manifest.sqlite",
        fts_dir=Path(root) / name / "index",
        lance_dir=Path(root) / name / "lance",
        acquire_enabled=False,
        license_ack=_ack_of(codex, state_dir),
        # THE CODEX'S LAYERS, and the Codex itself — the worker's loader
        # passes both (app/collections.py) and this did not, so every stage
        # reaching a collection through here cut documents with the
        # PLUGIN's implicit chunker instead of the declared layer list.
        # For a one-layer collection whose plugin chunker happens to equal
        # it — wikivec's `verbatim`, pubmed's `abstract` — that is
        # accidentally right and silent. For a collection that declares
        # layers the plugin does not have, it writes vectors under a
        # `space_id` that promises a different cut: invariant 1's failure
        # mode, and undetectable from outside because both halves are
        # internally consistent.
        layers=layers_mod.layers_for(codex),
        codex=codex,
    )


def _ack_of(codex, state_dir):
    """A yellow or red collection is not built until its acknowledgement
    exists (invariant 9; 04 §4.4). The ack lives in the collection's state
    directory (09 §9.13), and `app/licensing.py` is THE resolution — this
    only carries what it wrote, and never decides."""
    import json
    if codex.license.cls == "green":
        return None
    p = state_dir / "ack.json"
    if not p.is_file():
        raise RecordsError(
            f"{codex.license.cls}-class: no acknowledgement recorded. "
            f"`hold collection ack` prints the terms and records it "
            f"(04 §4.4); nothing below it runs until it exists")
    ack = json.loads(p.read_text(encoding="utf-8"))
    return f"{ack['time']}/{ack['terms_sha256'].replace('sha256:', '')}"


#: What a build produces that belongs in the SHARD: the directory and the
#: suffix its files carry. `figures` joined it with the plates amendment —
#: a container is written beside its shard by `pack build` and is useless
#: in staging, where nothing serves or checks it. The two are promoted in
#: ONE pass and containers first, so a crash between them leaves a
#: container with no shard (inert: nothing names it) rather than a shard
#: whose plates are still in staging (published records naming plates the
#: SHARD does not hold).
_PROMOTED = (("figures", "*.fig.zst"), ("records", "*.jsonl.zst"))


def _promote_records(staging, shard_dir) -> int:
    """Move what the build produced into the SHARD, atomically per file.

    Returns the RECORD shards promoted — the number the log reports — not
    the containers beside them.
    """
    from pathlib import Path

    from . import shard as shard_mod
    n = 0
    for kind, suffix in _PROMOTED:
        src = Path(staging) / kind
        if not src.is_dir():
            continue
        dst = Path(shard_dir) / kind
        for f in sorted(src.rglob(suffix)):
            target = dst / f.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shard_mod.promote(f, target, shard_dir=Path(shard_dir))
            if kind == "records":
                n += 1
    return n


def write_validation(root, ref: str) -> int:
    """`validation.json` at the SHARD root is the Codex's `validation` block
    written out in canonical JSON for tools; `hold check` reads it and
    refuses to run when its bytes differ from the block in the Codex
    (02 §2.6, 09 §9.3). It is a member like any other and waits for the
    acknowledgement with the rest."""
    from pathlib import Path
    from .codex import canonical_json
    from . import collection as col, shard as shard_mod

    name, codex = col.resolve(Path(root), ref)
    shard_tree = Path(root) / name / "shard"
    shard_tree.mkdir(parents=True, exist_ok=True)
    raw = canonical_json(codex.validation.to_dict())
    (shard_tree / shard_mod.VALIDATION_PATH).write_bytes(raw)
    return len(raw)

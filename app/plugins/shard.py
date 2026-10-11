"""The SHARD as a corpus (07 §7.3 `index build`; 02 §2.3): a peer that holds
a collection's record shards and not its origin reads its documents FROM
THE SHARDS — the same `discover`, `fetch`, `parse` the pipeline runs over
an origin, so the rdf, fts and embed stages, the catalogue, the facets and
`ask` work unchanged over what `share fetch` landed. Migration step 9f.

`ShardSource` wraps the Codex's own plugin: every attribute the pipeline
reads — licence class, provider, defaults, erasure, the chunking profile
— is the plugin's, and the three methods read the SHARD instead. A record
is one 09 §9.5 line: `fetch` returns it as the raw bytes with the
record's own `origin_hash` (the origin unit's hash, as the producer saw
it), `parse` gives it back as the canonical document the producer parsed
— same title, same sections, so the chunks and their ids are the
producer's (invariant 3) and a bundle's vectors land on them.

A superseded shard (`NNNNN.<snapshot id>.jsonl.zst`, 02 §2.3) is not read:
the current snapshot's records are the collection. The scan is resumable
per shard file through the plugin cursor, like any container source.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterator

from app.pack import records as pack_records
from app.plugins import cursor
from app.plugins.base import CanonicalDoc, DiscoverState, DocRef, RawDoc

SEP = "#"                      # <shard path>#<uid>; 09 §9.5's own member separator
SOURCE = "shard"                # the collection file's `source` value (07 §7.8, amended 2026-10-07)


def shard_files(shard_dir: Path) -> list[Path]:
    """Every current record shard held, in slice and number order; a
    superseded version (a second dot in its name) is not one."""
    d = Path(shard_dir) / "records"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*/*.jsonl.zst")):
        stem = p.name[: -len(".jsonl.zst")]
        if "." in stem:
            continue
        out.append(p)
    return out


def split_locator(locator: str) -> tuple[Path, str]:
    path, _, uid = str(locator).rpartition(SEP)
    if not path or not uid:
        raise ValueError(f"a SHARD locator is <shard path>#<uid>, got {locator!r}")
    return Path(path), uid


class ShardSource:
    """The Codex's plugin, reading the records this peer holds."""

    def __init__(self, inner, shard_dir: Path, skip: frozenset = frozenset()) -> None:
        self._inner = inner
        self.shard_dir = Path(shard_dir)
        self.source_id = inner.source_id
        #: SHARD-relative shard paths not to read: 03 §3.8's integrity
        #: violations of a certified manifest-only copy (app/hold/integrity.py)
        self.skip = frozenset(skip)

    #: The inner plugin's readers of ITS corpus — a book directory, a
    #: container, the origin's live form — which a peer holding only the
    #: shards does not have. Hidden, so app/plugin_io.py falls back to the
    #: protocol (`fetch`/`parse` below, reading the records) and freshness
    #: answers "unavailable" (05 §5.3). Forwarded, gutenberg's `read_text`
    #: looked for an epub in a directory that does not exist and wrote "not
    #: indexable" onto every fetched document, and `read_metadata` gave the
    #: catalogue empty titles without a word (second peer, 2026-10-07).
    CORPUS_READERS = frozenset({"read_metadata", "read_text", "read_chapters", "read_figures",
                                "prepare", "prepare_key", "current"})

    def __getattr__(self, name: str):
        if name in ShardSource.CORPUS_READERS:
            raise AttributeError(f"{name}: a fetched collection reads its records, not the origin's corpus")
        return getattr(self.__dict__["_inner"], name)

    def __reduce__(self):
        # the parse pool rebuilds the plugin in its children from the
        # collection; a ShardSource travels as what makes it
        return (ShardSource, (self._inner, self.shard_dir, self.skip))

    # ---- the three methods --------------------------------------------------

    def discover(self, state: DiscoverState) -> Iterator[DocRef]:
        progress = cursor.Progress(state, self.source_id + "@shard")
        for shard in shard_files(self.shard_dir):
            if self.skip and shard.relative_to(self.shard_dir).as_posix() in self.skip:
                continue                   # listed by no signed snapshot: never read (03 §3.8)
            if progress.unchanged(shard):
                continue
            st = shard.stat()
            for record in pack_records.iter_records(shard):
                uid = str(record.get("uid") or "")
                native_id = str(record.get("native_id") or uid.rsplit(":", 1)[-1])
                if not uid or not native_id:
                    continue
                yield DocRef(source_id=self.source_id, native_id=native_id,
                             locator=f"{shard}{SEP}{uid}", mtime=int(st.st_mtime), size=st.st_size,
                             extra={"lang": record.get("lang"), "shard": shard.name})
            # staged when its last ref has been taken; promoted into the saved
            # cursor only once the orchestrator has committed that far
            progress.stage(shard)
        progress.flush()

    def fetch(self, ref: DocRef) -> RawDoc:
        path, uid = split_locator(ref.locator)
        record = pack_records.read_record(path, uid)
        data = pack_records.canonical_line(record, record_line=2 if "figures" in record else 1)
        # the record's own origin_hash: the unit the PRODUCER parsed, which
        # is what a record rebuilt from this reader would carry too
        raw_hash = str(record.get("origin_hash") or hashlib.blake2b(data, digest_size=16).hexdigest())
        return RawDoc(ref=ref, raw_hash=raw_hash, data=data)

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        import json
        record = json.loads(raw.data.decode("utf-8"))
        sections = [(str(n or ""), str(b or "")) for n, b in (record.get("sections") or [])]
        extra = dict(record.get("extra") or {})
        return CanonicalDoc(
            uid=str(record.get("uid") or f"{self.source_id}:{raw.ref.native_id}"),
            title=str(record.get("title") or ""),
            creators=[str(c) for c in (record.get("creators") or [])],
            language=str(record.get("lang") or "und"),
            date=record.get("date"),
            subjects=[str(s) for s in (record.get("subjects") or [])],
            keys=[str(k) for k in (record.get("keys") or [])],
            extra=extra,
            sections=sections,
            raw_hash=raw.raw_hash,
        )

    def relations(self, doc: CanonicalDoc) -> Iterator[Any]:
        # the edges travel as the relations member (09 §9.14); a reader of
        # the records emits none
        return iter(())

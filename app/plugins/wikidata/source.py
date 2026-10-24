"""The Wikidata plugin (docs/notes/wikidata-build-plan.md; 06 §6.1): the
official JSON entity dump (D1) read through its pack (pack.py), one entity
one record, the text per language by the vendored textifier over the
dump's own label table (textify.py, format.py; D4 revised), scholarly
articles pruned, everything else the dump states kept (invariant 11).

Ids (ids.py): an item's native id is its number, a property's is
`PROPERTY_BASE + n`; `uid = wikidata:<native_id>`; the canonical id
`wikidata:Q42` goes in `keys` with the ids of its `P31`/`P279` values.
`origin` is `<dump rel path>#<entity index>`; `origin_hash` covers the
entity's own line bytes (the unit a query returns); a verifier needs the
same dump's label table, rebuilt from the dump (06 §6.4).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Iterator

from app.plugins import cursor
from app.plugins.base import (CanonicalDoc, DiscoverState, DocRef, Erasure, LicenseClass, Modality, Mutability,
                              PROTOCOL_VERSION, PrivacyClass, RawDoc, UnindexableDocument)
from app.plugins.wikidata import ids, pack, textify

_LOCATOR_SEP = "#"
_DUMP_RE = re.compile(r"^wikidata-(\d{8})-all\.json\.(bz2|gz)$")
#: what `keys` names besides the entity itself: what it is an instance or a subclass of
_KEY_PROPERTIES = ("P31", "P279")


def split_locator(locator: str) -> tuple[Path, int]:
    path, sep, index = locator.rpartition(_LOCATOR_SEP)
    if not sep or not index.isdigit():
        raise ValueError(f"wikidata locator carries no entity index: {locator!r}")
    return Path(path), int(index)


def find_dumps(corpus_root: Path, snapshot: str | None = None) -> list[Path]:
    """The entity dumps under the corpus, newest first by date; the pinned
    `snapshot` (a dump date, `YYYYMMDD`) alone when given."""
    root = Path(corpus_root)
    found = []
    for p in root.rglob("wikidata-*-all.json.*"):
        # A DUMP IS A FILE, and never one inside a pack (2026-10-21): the pack
        # beside a dump is a directory named after it (`.pack/<dump name>`),
        # and taken for the newest dump it was opened as a bz2 stream, read
        # nothing and said "the dump ended before its closing bracket
        # (truncated download?)" — discovery stopped listing the collection
        if not p.is_file() or pack.PACK_DIRNAME in p.relative_to(root).parts:
            continue
        m = _DUMP_RE.match(p.name)
        if m and (snapshot is None or m.group(1) == snapshot):
            found.append((m.group(1), p))
    found.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in found]


class WikidataSource:
    source_id = "wikidata"
    license_class = LicenseClass.GREEN          # CC0
    corpus_provider = "Wikidata contributors (Wikimedia Foundation)"
    corpus_license = "CC0-1.0"
    corpus_attribution = "Wikidata, Wikimedia Foundation and contributors, CC0 1.0"
    privacy_class = PrivacyClass.PUBLIC
    chunking_profile = "wikidata"
    #: entities change between dumps; a dated dump is immutable (D1)
    mutability = Mutability.MUTABLE
    protocol_version = PROTOCOL_VERSION
    #: 1: the first parser (2026-10-19) — the dump's entity kept whole in
    #: `extra`, one text section per language of `languages` (textify.py,
    #: format.py), scholarly articles pruned (pack.PRUNED_CLASSES).
    #: 2 (2026-10-24): a referenced entity's label falls back to `mul` before
    #: the fallback language, as upstream's — parser 1 wrote its id instead
    PARSER_VERSION = 2
    LOCATOR_SEP = _LOCATOR_SEP
    LOCATOR_MEMBER_FIELDS = 1
    modality = Modality.TEXT
    erasure = Erasure.RETRACTABLE              # entities are deleted; the tombstone stays
    native_id_order = "numeric"
    default_facets: list = []
    #: THE LANGUAGE LIST (D3): one section per language, in this order; the
    #: first with a label gives `title`. A language added is a daughter
    #: collection (02 §2.7). The chunker `wikidata-text` carries the same
    #: list as a parameter; both come from here until the Codex binds them.
    languages: tuple[str, ...] = ("en",)
    fallback_language = "en"
    #: 06 §6.3: the layer's chunker and its parameters enter `space_id`. The
    #: tokenizer is named by the sha256 of its tokenizer.json, which only a
    #: recipe can state (the plugin pins no file): a recipe for this plugin
    #: carries `[[layers]]` with `params.tokenizer`; `create` refuses without.
    default_layers = [
        {"chunker": {"id": "wikidata-text", "version": 1, "params": {"max_tokens": 1024}},
         "rule": "default"},
    ]
    #: the slicing is the scheme's (app/pack/slicing.py "wikidata"): one declaration

    def __init__(self, languages: tuple[str, ...] | None = None):
        if languages:
            self.languages = tuple(languages)

    # ---- discover ---------------------------------------------------------------------------

    def discover(self, state: DiscoverState) -> Iterator[DocRef]:
        """Every entity of the pinned dump (or the newest held) that is not
        pruned, from the pack's index columns; the pack is built first when
        it is not complete for this dump — the one streaming pass, resumable
        at a chunk (pack.py). A dump whose pack cannot be built is one
        container-scope error, never a document."""
        dumps = find_dumps(state.corpus_root, state.snapshot)
        if state.snapshot is None and len(dumps) > 1:
            dumps = dumps[:1]                     # one dump at a time: two dumps mint the same ids
        progress = cursor.Progress(state, self.source_id)
        for dump in dumps:
            if progress.unchanged(dump):
                continue
            try:
                if not pack.is_complete(dump, self.languages):
                    meta = pack.build(dump, languages=self.languages)
                    if not meta.get("complete"):
                        yield DocRef(source_id=self.source_id, native_id=dump.name, locator=str(dump),
                                     scope="container", error="pack build stopped before the end; resumes next scan")
                        continue
            except Exception as e:   # noqa: BLE001 — a truncated download, a corrupt stream
                yield DocRef(source_id=self.source_id, native_id=dump.name, locator=str(dump), scope="container",
                             error=f"wikidata dump not usable: {type(e).__name__}: {e}"[:300])
                continue
            last_index = -1
            for path in pack.chunk_files(dump):
                t = pack.index_columns(path)
                for index, native_id, pruned, modified in zip(t.column("index").to_pylist(),
                                                               t.column("native_id").to_pylist(),
                                                               t.column("pruned").to_pylist(),
                                                               t.column("modified").to_pylist()):
                    last_index = index
                    if pruned:
                        continue
                    yield DocRef(source_id=self.source_id, native_id=native_id,
                                 locator=f"{dump}{_LOCATOR_SEP}{index}", extra={"modified": modified})
            progress.stage(dump, complete_at=state.received)
        progress.flush()

    # ---- fetch / parse ----------------------------------------------------------------------

    def fetch(self, ref: DocRef) -> RawDoc:
        dump, index = split_locator(ref.locator)
        try:
            data = pack.line(dump, index)
        except pack.PackError as e:
            raise UnindexableDocument(f"entity {index} of {dump.name} not in its pack: {e}") from e
        except OSError as e:
            # the pack's FILE is not where it is read from — a cache being
            # carried, a drive not mounted: the node's condition, never the
            # entity's (2026-10-20). Deferred, not marked (CLAUDE.md)
            from app.chunking import PlatformMissing
            raise PlatformMissing(f"the pack of {dump.name} cannot be read at "
                                  f"{pack.pack_dir(dump)}: {e}") from e
        return RawDoc(ref=ref, raw_hash=hashlib.blake2b(data, digest_size=16).hexdigest(), data=data)

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        dump, _index = split_locator(raw.ref.locator)
        obj = json.loads(raw.data)
        eid = str(obj.get("id") or "")
        if not ids.is_entity_id(eid):
            raise UnindexableDocument(f"not an item or property: {eid!r}")
        labels = pack.labels(dump)
        sections = [(lang, textify.entity_text(obj, lang, labels, fallback=self.fallback_language))
                    for lang in self.languages]
        title = next((obj["labels"][lang]["value"] for lang in (*self.languages, "mul", self.fallback_language)
                      if isinstance((obj.get("labels") or {}).get(lang), dict)
                      and (obj["labels"][lang] or {}).get("value")), eid)
        keys = [ids.canonical(eid)]
        for prop in _KEY_PROPERTIES:
            for st in (obj.get("claims") or {}).get(prop) or ():
                dv = (((st or {}).get("mainsnak") or {}).get("datavalue") or {}).get("value")
                if isinstance(dv, dict) and ids.is_entity_id(dv.get("id")):
                    key = ids.canonical(dv["id"])
                    if key not in keys:
                        keys.append(key)
        # EVERYTHING THE DUMP STATES (invariant 11), minus nothing: the text
        # in `sections` is derived from these and from other entities' labels
        extra = {k: v for k, v in obj.items() if v not in (None, "", [], {})}
        return CanonicalDoc(
            uid=f"{self.source_id}:{ids.native_id_of(eid)}",
            title=title,
            creators=[],
            language="mul",
            date=None,
            subjects=[],
            keys=keys,
            extra=extra,
            sections=sections,
            raw_hash=raw.raw_hash,
        )


#: per entity, the names indexed at most (an entity's aliases can run long)
INDEX_NAMES_MAX = 2000


def index_names(extra: dict) -> tuple[list[tuple[str, str]], int]:
    """What the text index finds an entity BY NAME (operator, 2026-10-24:
    "names only"): every label and alias the dump gives it, in EVERY
    language, as `(language, name)`, and its popularity — how many wiki
    pages link it (`sitelinks`), Wikidata's own tie-break between entities
    of one name (WikibaseCirrusSearch rescores by sitelinks). A pure
    function of the record's `extra`, which keeps the whole entity
    (invariant 11), so a peer that fetched the records indexes the same
    names; nothing here enters `sections`, `content_hash` or a vector."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(lang, value) -> None:
        if isinstance(lang, str) and isinstance(value, str) and value.strip():
            key = (lang, value.strip())
            if key not in seen and len(out) < INDEX_NAMES_MAX:
                seen.add(key)
                out.append(key)
    for lang, v in sorted((extra.get("labels") or {}).items()):
        add(lang, (v or {}).get("value") if isinstance(v, dict) else None)
    for lang, vs in sorted((extra.get("aliases") or {}).items()):
        for v in vs or ():
            add(lang, (v or {}).get("value") if isinstance(v, dict) else None)
    sitelinks = extra.get("sitelinks")
    return out, len(sitelinks) if isinstance(sitelinks, dict) else 0


WikidataSource.index_names = staticmethod(index_names)

SOURCE = WikidataSource()

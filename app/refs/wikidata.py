"""Wikidata's entities as a REFERENCE SOURCE (06 §6.5; docs/notes/
wikidata-build-plan.md §2 "Reference source"): the namespace `wikidata`
— QIDs and PIDs, `wikidata:Q42`, `wikidata:P31` — registered, so that a
record of the `wikidata` collection may write its own id and the ids of
what it is an instance or subclass of into `keys`, and so that a
gutenberg-graph mention may point at an entity by canonical id (05 §5.2:
a plugin writes only ids a reference source registers; 09 §9.14: an edge
may end at `<namespace>:<id>` left unresolved).

One table, `wikidata_entity`, one row per entity: the id (the primary
key, so `ask lookup wikidata:Q42` is one `get`), its kind (`item` or
`property`), and ONE label with its language — the first of the pack's
languages that has one, `mul` last, the same preference the plugin's
`title` follows. It is synced from the label table the wikidata plugin
derives from the pinned dump (`pack.py`: `labels.sqlite` beside the
dump, `label(id, lang, label)` for the Codex's languages), the newest
dump under `<ref_root>/wikidata` whose pack is COMPLETE — the corpus as
acquisition lays it out, as `normattiva-refs` reads `<ref_root>/normattiva`
— so `louise dev refs-sync --src <collections root>` serves both. Node-
local as every reference source is; nothing here is a member.

No resolver yet: a label typed in a question is not resolved to an
entity here. At the dump's scale (~10⁸ entities) the dictionary the gene
resolver builds in memory does not exist, and an exact-label index is a
decision about several GB on /data (wikidata-build-plan.md §2). Lookup by
id and the `keys` restriction (05 §5.2) are what this table serves today.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

from ..plugins.base import RefColumn, RefLink, RefRecordBatch, RefSchema, RefSyncState, RefTable

SOURCE_ID = "wikidata-refs"
NAMESPACES = ("wikidata",)
#: the plugin's corpus under the sync root, as acquisition lays it out
CORPUS_DIR = "wikidata"
BATCH = 50_000

ENTITY = RefTable(
    name="wikidata_entity",
    columns=(
        RefColumn("id", "TEXT"),          # Q42, P31 — the primary key
        RefColumn("kind", "TEXT"),        # item | property
        RefColumn("lang", "TEXT"),        # the language `label` is in
        RefColumn("label", "TEXT"),
    ),
    primary_key="id",
)
LINKS: tuple[RefLink, ...] = ()


def canonical_id(entity_id: str) -> str:
    """`Q42` → `wikidata:Q42`; refuses what is not a Wikidata id."""
    if not is_entity_id(entity_id):
        raise ValueError(f"{entity_id!r} is not a Wikidata entity id (Q<n> or P<n>)")
    return f"wikidata:{entity_id}"


def is_entity_id(s: str) -> bool:
    return isinstance(s, str) and len(s) > 1 and s[0] in "QP" and s[1:].isdigit() and s[1] != "0"


def kind_of(entity_id: str) -> str:
    return "item" if entity_id[0] == "Q" else "property"


def current_pack(ref_root: Path) -> tuple[Path, dict] | None:
    """(dump, its pack meta) of the newest dump under `<ref_root>/wikidata`
    whose pack is complete for the dump as it is now; None when there is
    none — a dump still downloading, or a pack still building, is not
    synced from, and nothing is an error."""
    from ..plugins.wikidata import pack
    from ..plugins.wikidata.source import find_dumps
    root = Path(ref_root) / CORPUS_DIR
    if not root.is_dir():
        return None
    for dump in find_dumps(root):
        meta = pack.read_meta(dump)
        if meta and pack.is_complete(dump, tuple(meta.get("languages") or ())):
            return dump, meta
    return None


def label_table(dump: Path) -> Path:
    from ..plugins.wikidata import pack
    return pack.pack_dir(dump) / "labels.sqlite"


def pick_label(labels: dict[str, str], languages: tuple[str, ...]) -> tuple[str, str] | None:
    """The first of the pack's languages that has a label, `mul` last."""
    for lang in (*languages, "mul"):
        if labels.get(lang):
            return lang, labels[lang]
    return None


class WikidataRefSource:
    source_id = SOURCE_ID
    #: 2: one row per entity keyed by its id (was one per (id, language),
    #: keyed `Q42|en`, which no lookup by canonical id could reach)
    ref_version = 2
    namespaces = NAMESPACES
    protocol_version = 1

    def schema(self) -> RefSchema:
        return RefSchema(source_id=self.source_id, tables=(ENTITY,), links=LINKS)

    def input_mtime(self, state: RefSyncState) -> float:
        """When the pack this would sync from was last written — its label
        table or its meta, whichever is newer; 0 when there is none. The
        store re-syncs when this is newer than the last sync (a new dump's
        pack completed), and skips otherwise."""
        found = current_pack(state.ref_root)
        if found is None:
            return 0.0
        from ..plugins.wikidata import pack
        dump, _ = found
        paths = (label_table(dump), pack.pack_dir(dump) / "meta.json")
        return max((p.stat().st_mtime for p in paths if p.is_file()), default=0.0)

    def sync(self, state: RefSyncState) -> Iterator[RefRecordBatch]:
        """The label table of the current pack, read in id order so an
        entity's languages arrive together; one row per entity, in batches
        of `BATCH`. An id that is not Q<n> or P<n> is skipped, never stored."""
        found = current_pack(state.ref_root)
        if found is None:
            return
        dump, meta = found
        languages = tuple(meta.get("languages") or ())
        path = label_table(dump)
        if not path.is_file():
            return
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows: list[dict] = []
            current: str | None = None
            labels: dict[str, str] = {}

            def flush() -> dict | None:
                if current is None or not is_entity_id(current):
                    return None
                picked = pick_label(labels, languages)
                if picked is None:
                    return None
                lang, label = picked
                return {"id": current, "kind": kind_of(current), "lang": lang, "label": label}

            for eid, lang, label in con.execute("SELECT id, lang, label FROM label ORDER BY id, lang"):
                if eid != current:
                    row = flush()
                    if row is not None:
                        rows.append(row)
                    current, labels = eid, {}
                    if len(rows) >= BATCH:
                        yield RefRecordBatch(table=ENTITY.name, rows=tuple(rows))
                        rows = []
                labels[lang] = label
            row = flush()
            if row is not None:
                rows.append(row)
            if rows:
                yield RefRecordBatch(table=ENTITY.name, rows=tuple(rows))
        finally:
            con.close()

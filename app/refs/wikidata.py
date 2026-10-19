"""Wikidata's entity ids as a REFERENCE SOURCE (06 §6.5; docs/notes/
wikidata-build-plan.md §2 "Reference source"): the namespace `wikidata`
— QIDs and PIDs, `wikidata:Q42`, `wikidata:P31` — registered, so that a
record of the `wikidata` collection may write its own id and the ids of
what it is an instance or subclass of into `keys`, and so that a
gutenberg-graph mention may point at an entity by canonical id (05 §5.2:
a plugin writes only ids a reference source registers; 09 §9.14: an edge
may end at `<namespace>:<id>` left unresolved).

One table, `entity`: the id, its kind (`item` or `property`) and one label
per row language — the languages of the collection's Codex — synced from
the label table the wikidata plugin derives from the pinned dump
(`labels.tsv`: id, type, lang, label), node-local as every reference
source is. The resolver is the dictionary resolver over labels, as for
genes: a label typed in a question resolves to its entity as a fact,
never ranked (invariant 4).
"""
from __future__ import annotations

import csv
from typing import Iterator

from ..plugins.base import RefColumn, RefLink, RefRecordBatch, RefSchema, RefSyncState, RefTable

SOURCE_ID = "wikidata-refs"
NAMESPACES = ("wikidata",)
LABELS_FILE = "wikidata-labels.tsv"

ENTITY = RefTable(
    name="wikidata_entity",
    columns=(
        RefColumn("id", "TEXT"),          # Q42, P31
        RefColumn("kind", "TEXT"),        # item | property
        RefColumn("lang", "TEXT"),
        RefColumn("label", "TEXT"),
        RefColumn("key", "TEXT"),         # "Q42|en": the primary key, one row per (id, lang)
    ),
    primary_key="key",
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


class WikidataRefSource:
    source_id = SOURCE_ID
    ref_version = 1
    namespaces = NAMESPACES
    protocol_version = 1

    def schema(self) -> RefSchema:
        return RefSchema(source_id=self.source_id, tables=(ENTITY,), links=LINKS)

    def sync(self, state: RefSyncState) -> Iterator[RefRecordBatch]:
        """`<ref_root>/wikidata-labels.tsv` — `id<TAB>type<TAB>lang<TAB>label`,
        as the plugin's label pass writes it — in batches of 50,000 rows;
        an id that is not Q<n> or P<n> is skipped, never stored."""
        path = state.ref_root / LABELS_FILE
        if not path.is_file():
            return
        rows: list[dict] = []
        with open(path, newline="", encoding="utf-8") as f:
            for parts in csv.reader(f, delimiter="\t"):
                if len(parts) < 4 or not is_entity_id(parts[0]):
                    continue
                eid, kind, lang, label = parts[0], parts[1] or kind_of(parts[0]), parts[2], parts[3]
                rows.append({"id": eid, "kind": kind, "lang": lang, "label": label, "key": f"{eid}|{lang}"})
                if len(rows) >= 50_000:
                    yield RefRecordBatch(table=ENTITY.name, rows=tuple(rows))
                    rows = []
        if rows:
            yield RefRecordBatch(table=ENTITY.name, rows=tuple(rows))

"""tests/test_refs_wikidata.py — the `wikidata` namespace registered by a
reference source (06 §6.5; docs/notes/wikidata-build-plan.md §2): QIDs
and PIDs as canonical ids, the entity table synced from the plugin's
label file, one row per (id, language)."""
import pytest

from app import refs
from app.plugins.base import RefSyncState
from app.refs import wikidata as wref, store as refs_store


def test_the_namespace_and_the_canonical_id_rule():
    src = refs.REFS_REGISTRY["wikidata-refs"]
    assert src.namespaces == ("wikidata",) and src.source_id == "wikidata-refs"
    assert wref.canonical_id("Q42") == "wikidata:Q42" and wref.canonical_id("P31") == "wikidata:P31"
    for bad in ("42", "q42", "Q", "Q0", "L123", "Q42x", ""):
        assert not wref.is_entity_id(bad)
        with pytest.raises(ValueError):
            wref.canonical_id(bad)
    assert wref.kind_of("Q42") == "item" and wref.kind_of("P31") == "property"
    assert {t.name for t in src.schema().tables} == {"wikidata_entity"}
    assert src.schema().tables[0].primary_key == "key"


def test_sync_reads_the_label_file_one_row_per_id_and_language(tmp_path):
    (tmp_path / wref.LABELS_FILE).write_text(
        "Q42\titem\ten\tDouglas Adams\n"
        "Q42\titem\tfr\tDouglas Adams\n"
        "P31\tproperty\ten\tinstance of\n"
        "P31\t\tde\tist ein(e)\n"           # a missing kind is derived from the id
        "L123\tlexeme\ten\tnot an entity id\n"
        "broken line\n",
        encoding="utf-8")
    src = wref.WikidataRefSource()
    batches = list(src.sync(RefSyncState(ref_root=tmp_path)))
    assert len(batches) == 1 and batches[0].table == "wikidata_entity"
    rows = {r["key"]: r for r in batches[0].rows}
    assert set(rows) == {"Q42|en", "Q42|fr", "P31|en", "P31|de"}
    assert rows["P31|de"]["kind"] == "property" and rows["Q42|fr"]["label"] == "Douglas Adams"
    # into a store, upserted by the primary key: a second sync changes nothing
    con = refs_store.connect(tmp_path / "refs.db")
    schema = src.schema()
    refs_store.ensure_schema(con, schema)
    store = refs_store.RefStore(con, [schema])
    state = RefSyncState(ref_root=tmp_path)
    refs_store.sync_source(con, src, state)
    refs_store.sync_source(con, src, state)
    assert store.count("wikidata_entity") == 4
    assert store.get("wikidata_entity", "Q42|en")["label"] == "Douglas Adams"
    assert list(src.sync(RefSyncState(ref_root=tmp_path / "nowhere"))) == []

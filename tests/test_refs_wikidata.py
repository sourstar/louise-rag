"""tests/test_refs_wikidata.py — the `wikidata` namespace as a reference
source (06 §6.5; docs/notes/wikidata-build-plan.md §2): QIDs and PIDs as
canonical ids; one row per entity, synced from the plugin's own label
table of the newest COMPLETE pack under `<ref_root>/wikidata`; a fact by
canonical id; re-synced when a newer pack completes and skipped otherwise."""
import os
import sqlite3
import time

import pytest

from app import refs
from app.ask import lookup
from app.plugins.base import RefSyncState
from app.plugins.wikidata import pack
from app.refs import store as refs_store, wikidata as wref

from tests.test_plugins_wikidata_source import a_dump, entity_lines


@pytest.fixture(autouse=True)
def _fresh():
    pack.reset_caches()
    yield
    pack.reset_caches()


def a_store(tmp_path):
    con = refs_store.connect(tmp_path / "refs.db")
    schema = wref.WikidataRefSource().schema()
    refs_store.ensure_schema(con, schema)
    return con, refs_store.RefStore(con, [schema])


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
    assert src.schema().tables[0].primary_key == "id", "a lookup by canonical id is one get"


def test_sync_reads_the_packs_label_table_one_row_per_entity(tmp_path):
    """The corpus as acquisition lays it out, under `<ref_root>/wikidata`; the
    pack built for (fr, en): the label is the first of the pack's languages
    that has one, so Q5 is `être humain`, and the scholarly article — pruned
    from the collection, but a thing a key may name — keeps its English."""
    dump = a_dump(tmp_path / "wikidata")
    pack.build(dump, languages=("fr", "en"), chunk=4)
    con, store = a_store(tmp_path)
    src = wref.WikidataRefSource()
    r = refs_store.sync_source(con, src, RefSyncState(ref_root=tmp_path))
    assert r == {"skipped": False, "rows": 6}
    rows = {r["id"]: r for r in store.rows("wikidata_entity")}
    assert set(rows) == {"Q42", "Q5", "Q33010486", "P31", "Q2711", "Q3331"}
    assert (rows["Q5"]["label"], rows["Q5"]["lang"]) == ("être humain", "fr")
    assert (rows["P31"]["label"], rows["P31"]["kind"]) == ("nature de l'élément", "property")
    assert (rows["Q33010486"]["label"], rows["Q33010486"]["lang"]) == ("A scholarly article about nothing", "en")
    assert rows["Q42"]["kind"] == "item"
    # a fact by canonical id (05 §5.2), every column, the label named
    out = lookup.lookup("wikidata:Q42", store=store)
    assert out["unresolved"] == [] and len(out["facts"]) == 1
    fact = out["facts"][0]
    assert (fact["entity_id"], fact["kind"], fact["source_id"], fact["label"]) == \
        ("wikidata:Q42", "wikidata_entity", "wikidata-refs", "Douglas Adams"), \
        "the source that REGISTERS the namespace owns the fact"
    assert fact["ref_version"] == "2"
    assert fact["fields"]["lang"] == "fr"
    assert lookup.lookup("wikidata:Q1", store=store) == {"facts": [], "unresolved": ["wikidata:Q1"]}
    # synced once: the pack has not moved
    assert refs_store.sync_source(con, src, RefSyncState(ref_root=tmp_path)) == {"skipped": True, "rows": 0}


def test_a_newer_complete_pack_is_synced_again_and_nothing_else_is(tmp_path):
    dump = a_dump(tmp_path / "wikidata")
    pack.build(dump, languages=("en",), chunk=4)
    con, store = a_store(tmp_path)
    src = wref.WikidataRefSource()
    state = RefSyncState(ref_root=tmp_path)
    assert refs_store.sync_source(con, src, state)["rows"] == 6
    before = src.input_mtime(state)
    assert before > 0
    assert refs_store.sync_source(con, src, state)["skipped"] is True
    # the pack's meta rewritten later (a rebuild, a newer dump's pack): newer than the sync
    meta = pack.pack_dir(dump) / "meta.json"
    later = time.time() + 5
    os.utime(meta, (later, later))
    assert src.input_mtime(state) > before
    assert refs_store.sync_source(con, src, state) == {"skipped": False, "rows": 6}
    assert store.count("wikidata_entity") == 6, "upserted by id: no duplicates"


def test_no_corpus_or_no_complete_pack_syncs_nothing_and_is_not_an_error(tmp_path):
    src = wref.WikidataRefSource()
    state = RefSyncState(ref_root=tmp_path)
    assert wref.current_pack(tmp_path) is None and src.input_mtime(state) == 0.0
    assert list(src.sync(state)) == []
    # a pack still building (stopped after one chunk) is not complete: not synced from
    dump = a_dump(tmp_path / "wikidata")
    pack.build(dump, languages=("en",), chunk=2, stop=lambda: True)
    assert not pack.is_complete(dump, ("en",))
    assert wref.current_pack(tmp_path) is None
    assert list(src.sync(state)) == []
    con, store = a_store(tmp_path)
    assert refs_store.sync_source(con, src, state) == {"skipped": False, "rows": 0}
    assert store.count("wikidata_entity") == 0


def test_the_newest_dump_whose_pack_is_complete_is_the_current_one(tmp_path):
    older = a_dump(tmp_path / "wikidata", date="20261001", lines=entity_lines()[:2])     # Q42, Q5
    newer = a_dump(tmp_path / "wikidata", date="20261012")
    pack.build(older, languages=("en",), chunk=4)
    pack.build(newer, languages=("en",), chunk=2, stop=lambda: True)                      # not complete
    found = wref.current_pack(tmp_path)
    assert found is not None and found[0] == older, "the newer dump's pack is not complete yet"
    src = wref.WikidataRefSource()
    con, store = a_store(tmp_path)
    assert refs_store.sync_source(con, src, RefSyncState(ref_root=tmp_path))["rows"] == 2
    pack.build(newer, languages=("en",), chunk=2)                                          # completes
    assert wref.current_pack(tmp_path)[0] == newer
    assert refs_store.sync_source(con, src, RefSyncState(ref_root=tmp_path))["rows"] == 6


def test_an_id_that_is_not_an_entity_and_an_entity_without_a_label_are_skipped(tmp_path):
    dump = a_dump(tmp_path / "wikidata", lines=entity_lines()[:2])
    pack.build(dump, languages=("en",), chunk=4)
    with sqlite3.connect(wref.label_table(dump)) as con:
        con.execute("INSERT INTO label (id, lang, label) VALUES ('L123', 'en', 'a lexeme')")
        con.execute("INSERT INTO label (id, lang, label) VALUES ('Q77', 'de', 'nur deutsch')")   # not a pack language
        con.execute("INSERT INTO label (id, lang, label) VALUES ('Q78', 'mul', 'Mul')")        # mul: the last resort
    rows = {r["id"]: r for b in wref.WikidataRefSource().sync(RefSyncState(ref_root=tmp_path)) for r in b.rows}
    assert set(rows) == {"Q42", "Q5", "Q78"}
    assert (rows["Q78"]["lang"], rows["Q78"]["label"]) == ("mul", "Mul")


def test_pick_label_prefers_the_packs_order_then_mul():
    assert wref.pick_label({"en": "human", "fr": "être humain"}, ("fr", "en")) == ("fr", "être humain")
    assert wref.pick_label({"en": "human", "mul": "x"}, ("fr", "en")) == ("en", "human")
    assert wref.pick_label({"mul": "x"}, ("fr", "en")) == ("mul", "x")
    assert wref.pick_label({"de": "Mensch", "en": ""}, ("en",)) is None

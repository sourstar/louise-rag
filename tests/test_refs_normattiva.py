"""The Italian acts as a reference source (app/refs/normattiva.py; 06 §6.5;
review §3.12): acts and versions synced from the members' heads, citations
resolved to acts by key, facts pinned and never ranked, and the record
writing its registered ids into `keys` (05 §5.2)."""
import base64
import zipfile
from pathlib import Path

import pytest

from app import entities, refs
from app.plugins.base import Entity, RefSyncState
from app.refs import normattiva as nref, store as refs_store

from tests.test_normattiva import L306, LC67, LC_O, M306, SARD, SARD_MEMBERS, _nid, _zip

FIXTURE_REFS = Path(__file__).parent / "fixtures" / "refs-mini"
URN_306 = "urn:nir:stato:legge:2003-10-31;306"
URN_SARD = "urn:nir:stato:legge.costituzionale:1948-02-26;3"


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "refs"
    _zip(root / "normattiva/akn/M/leggi-costituzionali.zip", {**SARD_MEMBERS, LC_O: LC67})
    _zip(root / "normattiva/akn/M/codici.zip", {M306: base64.b64encode(L306)})
    for p in FIXTURE_REFS.iterdir():                 # the gene tables beside the acts
        (root / p.name).write_bytes(p.read_bytes())
    con = refs.connect(tmp_path / "refs.db")
    refs_store.sync_all(con, root)
    nref.invalidate_cache()
    return refs.RefStore(con, [s.schema() for s in refs.REFS_REGISTRY.values()])


def test_the_acts_and_their_versions_are_synced_from_the_members_heads(store):
    atti = {r["urn"]: r for r in store.rows("atto")}
    assert set(atti) == {URN_306, URN_SARD, "urn:nir:stato:legge.costituzionale:1967-06-21;1"}
    a = atti[URN_306]
    assert (a["tipo"], a["anno"], a["numero"], a["data"]) == ("legge", 2003, "306", "2003-10-31")
    assert a["titolo"].startswith("Disposizioni per l'adempimento di obblighi")
    assert a["denominazione"] == "LEGGE 31 ottobre 2003, n. 306"
    assert a["eli"] == "http://www.normattiva.it/eli/id/2003/11/15/003G0333", "the work, versionless"
    assert (a["codice"], a["pubblicazione"]) == ("003G0333", "2003-11-15")
    assert a["akn"] == "akn:it/act/legge/stato/2003-10-31/306", "the work path, the id its cross-references use"
    s = atti[URN_SARD]
    assert (s["tipo"], s["numero"], s["anno"]) == ("legge.costituzionale", "3", 1948)
    versions = store.rows("versione", {"urn": URN_SARD}, order_by="valid_from")
    assert [v["valid_from"] for v in versions] == ["1948-03-09", "2001-02-01", "2001-02-16"]
    assert [v["expression"] for v in versions] == ["ORIGINAL", "CONSOLIDATED/20010201", "CONSOLIDATED/20010216"]
    assert versions[1]["uid"] == f"normattiva:{_nid('1948-03-09', '048C0003', '2001-02-01', 'CONSOLIDATED/20010201')}"
    assert versions[0]["member"] == "akn/M/leggi-costituzionali.zip#" + SARD + "ORIGINALE_V0.xml"
    assert store.rows("versione", {"urn": URN_306})[0]["member"].endswith(M306), "a base64 member, read decoded"
    assert store.owner_of("atto") == "normattiva-refs" and store.pk_of("versione") == "uid"


@pytest.mark.parametrize("text", [
    "l. 306/2003", "legge 306/2003", "legge n. 306 del 2003", "la legge 31 ottobre 2003, n. 306",
    "LEGGE 31 ottobre 2003, n. 306", "art. 5, comma 3, della legge 306/2003",
])
def test_a_citation_in_any_of_its_forms_names_the_act(store, text):
    got = refs.RESOLVER.resolve(text, store)
    assert [e.entity_id for e in got] == [URN_306]
    assert got[0].kind == "atto" and got[0].source_id == "normattiva-refs"
    assert got[0].label.startswith("LEGGE 31 ottobre 2003, n. 306")


def test_an_article_stays_on_the_label_and_an_unknown_act_resolves_to_nothing(store):
    got = refs.RESOLVER.resolve("art. 5, comma 3, della legge 306/2003", store)
    assert got[0].label.endswith("— art. 5, comma 3")
    assert refs.RESOLVER.resolve("d.lgs. 82/2005 e il d.p.r. 445/2000", store) == []
    assert refs.RESOLVER.resolve("legge costituzionale 26 febbraio 1948, n. 3", store)[0].entity_id == URN_SARD
    assert refs.RESOLVER.resolve("legge 306/2004", store) == [], "the year is part of the key"
    assert nref.citations("l.cost. 3/1948 e la L. 26 febbraio 1948 n. 4") == [
        {"tipo": "legge.costituzionale", "numero": "3", "anno": 1948, "data": None, "art": None},
        {"tipo": "legge", "numero": "4", "anno": 1948, "data": "1948-02-26", "art": None}]


@pytest.mark.parametrize("text", [
    # a title between the form and the date (operator, 2026-10-13: "art. 12
    # della legge sull'emigrazione testo unico 13 novembre 1919, n. 2205")
    "art. 12 della legge sugli obblighi comunitari 31 ottobre 2003, n. 306",
    # a form that is not the origin's, or none: the date and number decide
    "decreto-legge 31 ottobre 2003, n. 306",
    "testo unico 31 ottobre 2003, n. 306",
    "art. 2 del t.u. delle norme comunitarie 31 ottobre 2003, n. 306",
])
def test_a_dated_citation_names_the_act_by_its_date_and_number(store, text):
    got = refs.RESOLVER.resolve(text, store)
    assert [e.entity_id for e in got] == [URN_306], text


def test_a_form_that_is_not_the_origin_s_needs_the_date():
    """Without the date, the form is part of the key: "d.l. 306/2003" is
    not the law 306/2003 — two acts of one year may share a number."""
    assert nref.citations("d.l. 306/2003")[0]["tipo"] == "decreto.legge"
    got = nref.citations("art. 12 della legge sull'emigrazione testo unico 13 novembre 1919, n. 2205")
    assert got == [{"tipo": "legge", "numero": "2205", "anno": 1919, "data": "1919-11-13",
                    "art": "art. 12"}]


def test_an_undated_citation_in_another_form_resolves_to_nothing(store):
    assert refs.RESOLVER.resolve("d.l. 306/2003", store) == []
    assert refs.RESOLVER.resolve("decreto-legge 30 ottobre 2003, n. 306", store) == [], \
        "a date that is not the act's names no act"


def test_the_gene_resolver_still_answers_beside_the_citations(store):
    got = refs.RESOLVER.resolve("BRCA2 and the legge 306/2003", store)
    assert {e.entity_id for e in got} == {"hgnc:1101", URN_306}


def test_expanding_an_act_pins_its_versions_as_facts(store):
    act = refs.RESOLVER.resolve("legge costituzionale 3/1948", store)[0]
    reached = refs.RESOLVER.expand_bounded(act, store, 10)
    assert len(reached) == 1 and reached[0].table == "versione" and reached[0].total == 3
    assert {e.kind for e in reached[0].entities} == {"versione"}
    assert all(e.entity_id.startswith("normattiva-refs:normattiva:") for e in reached[0].entities)
    assert len(refs.RESOLVER.expand(act, store)) == 3


def test_tagging_a_record_writes_the_acts_it_names(store, tmp_path):
    from app import collections as collections_mod, db
    c = collections_mod.Collection(
        name="normattiva", plugin="normattiva", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb")
    con = db.connect(c)
    con.execute("INSERT INTO documents(uid,source,native_id,path,updated_at) VALUES(?,?,?,?,0)",
                ("normattiva:1", "normattiva", "1", "/x"))
    ids = entities.tag_document(con, "normattiva:1", "LEGGE 31 ottobre 2003, n. 306 — Disposizioni",
                                ["Art. 1. La legge costituzionale 26 febbraio 1948, n. 3 è modificata."],
                                refs.RESOLVER, store)
    assert ids == sorted([URN_306, URN_SARD])
    assert nref.NAMESPACES == ("urn", "eli", "akn")
    assert "normattiva-refs" in refs.REFS_REGISTRY


def test_the_record_writes_its_registered_ids_into_keys(tmp_path):
    """05 §5.2: `keys` hold only ids a reference source registers — now `urn`
    and `eli` are — sorted by bytes (09 §9.5)."""
    from app import plugin_io
    from app.plugins.base import DiscoverState
    from app.plugins.normattiva import SOURCE
    from app.plugins.normattiva import source as nsrc
    nsrc._zip_cache.clear()
    root = tmp_path / "normattiva"
    _zip(root / "akn/M/codici.zip", {M306: base64.b64encode(L306)})
    ref = next(r for r in SOURCE.discover(DiscoverState(corpus_root=root)) if r.locator.endswith(M306))
    doc = plugin_io.canonical_doc(SOURCE, {"native_id": ref.native_id, "path": ref.locator})
    assert doc.keys == ["akn:it/act/legge/stato/2003-10-31/306", "eli:it:id/2003/11/15/003G0333", URN_306]
    assert doc.keys == sorted(doc.keys) and SOURCE.PARSER_VERSION == 8

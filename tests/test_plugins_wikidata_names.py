"""tests/test_plugins_wikidata_names.py — an entity's NAME where it has no
English and no `mul` label (operator, 2026-10-24: "showing an id is
meaningless for humans … 1. the original name if it can be determined and
if it's in latin characters 2. Any other language in latin characters.
3. The name in the original language if it can be determined"): the rule
(names.py), the record's title and text, the referenced names through the
pack's `name` table, the pack's upgrade from version 1 without a pass over
the dump, and the reference source."""
import bz2
import json
import sqlite3
from pathlib import Path

import pytest

from app.chunking import PlatformMissing
from app.plugins.base import DiscoverState
from app.plugins.wikidata import names, pack, textify
from app.plugins.wikidata.source import WikidataSource

FIXTURES = Path(__file__).parent / "fixtures" / "wikidata-mini"


def mono(eid: str, pid: str, text: str, lang: str, rank: str = "normal") -> dict:
    return {"mainsnak": {"snaktype": "value", "property": pid, "datatype": "monolingualtext",
                         "datavalue": {"value": {"text": text, "language": lang}, "type": "monolingualtext"}},
            "type": "statement", "id": f"{eid}${pid}{lang}{rank}", "rank": rank}


def item_claim(eid: str, pid: str, target: str) -> dict:
    return {"mainsnak": {"snaktype": "value", "property": pid, "datatype": "wikibase-item",
                         "datavalue": {"value": {"entity-type": "item", "numeric-id": int(target[1:]), "id": target},
                                       "type": "wikibase-entityid"}},
            "type": "statement", "id": f"{eid}${pid}{target}", "rank": "normal"}


def entity(eid: str, labels: dict, *, statements: list = (), sitelinks: tuple = (), descriptions: dict = None,
           aliases: dict = None) -> dict:
    claims: dict = {}
    for st in statements:
        claims.setdefault(st["mainsnak"]["property"], []).append(st)
    return {"type": "item", "id": eid,
            "labels": {k: {"language": k, "value": v} for k, v in labels.items()} or [],
            "descriptions": {k: {"language": k, "value": v} for k, v in (descriptions or {}).items()} or [],
            "aliases": {k: [{"language": k, "value": v} for v in vs] for k, vs in (aliases or {}).items()} or [],
            "claims": claims or [], "sitelinks": {s: {"site": s, "title": "t", "badges": []} for s in sitelinks} or [],
            "lastrevid": 1}


def test_latin_is_the_script_of_every_letter():
    assert names.is_latin("Les Roses de la vie") and names.is_latin("Łódź") and names.is_latin("Þingvellir")
    assert names.is_latin("ʻIolani Palace"), "the ʻokina is a modifier letter, of no script"
    assert not names.is_latin("Москва") and not names.is_latin("東京") and not names.is_latin("Tokyo 東京")
    assert not names.is_latin("1984 –"), "no letter, no script"


def test_the_operators_order():
    # 1. the original name, in Latin characters, before every label
    q = entity("Q1", {"de": "Die Rosen des Lebens", "hu": "Az élet rózsái"},
               statements=[mono("Q1", "P1476", "Les Roses de la vie", "fr")])
    assert names.display_name(q) == ("fr", "Les Roses de la vie")
    # 2. an original name in another script gives way to a label in Latin characters
    q = entity("Q2", {"ru": "Люберецкая ОПГ", "fr": "Liouberetskaya"},
               statements=[mono("Q2", "P1705", "Люберецкая ОПГ", "ru")])
    assert names.display_name(q) == ("fr", "Liouberetskaya")
    # 3. no label in Latin characters: the original name in its own script
    q = entity("Q3", {"zh": "北京烤鸭", "ja": "北京ダック"}, statements=[mono("Q3", "P1705", "北京烤鴨", "zh-hant")])
    assert names.display_name(q) == ("zh-hant", "北京烤鴨")
    # the general rule: any label is a name; an id only when there is nothing at all
    assert names.display_name(entity("Q4", {"ru": "Река", "uk": "Річка"})) == ("ru", "Река")
    assert names.display_name(entity("Q5", {})) is None


def test_among_labels_the_entitys_own_wikis_then_the_most_given_then_the_code():
    labels = {"fr": "ligne de Charpal", "nl": "spoorlijn Charpal"}
    assert names.display_name(entity("Q6", labels)) == ("fr", "ligne de Charpal"), "a tie: the smallest code"
    assert names.display_name(entity("Q6", labels, sitelinks=("nlwiki", "commonswiki"))) == \
        ("nl", "spoorlijn Charpal"), "a language of the entity's own wiki pages first"
    q = entity("Q7", {"cy": "Les Oignons", "de": "Die Zwiebeln", "fr": "Les Oignons"})
    assert names.display_name(q) == ("cy", "Les Oignons"), "the value most languages give"


def test_original_names_preferred_first_deprecated_never():
    q = entity("Q8", {}, statements=[mono("Q8", "P1448", "Old Name", "en", "deprecated"),
                                     mono("Q8", "P1448", "Second", "it"),
                                     mono("Q8", "P1448", "First", "it", "preferred")])
    assert names.original_names(q) == [("it", "First"), ("it", "Second")]
    assert names.display_name(q) == ("it", "First")


def test_the_text_names_an_entity_without_english_or_mul():
    q = entity("Q3235171", {"fr": "Les Roses de la vie", "de": "Die Rosen des Lebens", "hu": "Az élet rózsái"},
               sitelinks=("frwiki",))
    assert textify.entity_text(q, "en", lambda eid, lang: None) == "Les Roses de la vie", \
        "the entity's own name, not Q3235171"
    assert textify.entity_text(entity("Q14", {}), "en", lambda eid, lang: None) == "Q14", "nothing at all: the id"


def test_a_referenced_entity_is_named_by_the_label_tables_names():
    q2 = json.loads([l for l in (FIXTURES / "dump-sample.jsonl").read_bytes().split(b"\n")
                     if b'"Q2711"' in l][0].rstrip(b","))
    table = {("P31", "en"): "instance of", ("Q2755432", names.NAME): "algorithme de compression"}
    text = textify.entity_text(q2, "en", lambda eid, lang: table.get((eid, lang)))
    assert "instance of: algorithme de compression" in text


# ---- the pack: `name` rows, the upgrade from version 1, the deferral ------------------------

LINES = [
    {"type": "property", "datatype": "wikibase-item", "id": "P31", "labels": {"en": {"language": "en",
     "value": "instance of"}}, "descriptions": [], "aliases": [], "claims": [], "lastrevid": 1},
    entity("Q10", {"en": "has English"}),
    entity("Q11", {"mul": "Has Mul"}),
    entity("Q12", {"fr": "ligne de Charpal", "nl": "spoorlijn Charpal"}, sitelinks=("frwiki",),
           descriptions={"en": "an English description, no English label"}),
    entity("Q13", {"ru": "Река", "fr": "Rivière"}, aliases={"en": ["River"]}),
    entity("Q14", {}),
    entity("Q15", {"de": "Schlacht"}, statements=[item_claim("Q15", "P31", "Q12")]),
]


def line_of(e: dict) -> bytes:
    """As the dump writes an entity: compact, UTF-8."""
    return json.dumps(e, ensure_ascii=False, separators=(",", ":")).encode()


def a_dump(root: Path) -> Path:
    d = root / "wikidatawiki" / "entities" / "20261012"
    d.mkdir(parents=True)
    p = d / "wikidata-20261012-all.json.bz2"
    body = b"[\n" + b",\n".join(line_of(e) for e in LINES) + b"\n]\n"
    p.write_bytes(bz2.compress(body))
    return p


def rows(dump: Path, table: str) -> list:
    con = sqlite3.connect(pack.pack_dir(dump) / "labels.sqlite")
    try:
        return con.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
    finally:
        con.close()


@pytest.fixture(autouse=True)
def _fresh():
    pack.reset_caches()
    yield
    pack.reset_caches()


def test_the_pack_names_every_entity_without_english_or_mul(tmp_path):
    dump = a_dump(tmp_path)
    assert pack.build(dump, languages=("en",), chunk=4, workers=1)["pack_version"] == 2
    assert rows(dump, "name") == [("Q12", "fr", "ligne de Charpal"), ("Q13", "fr", "Rivière"),
                                  ("Q15", "de", "Schlacht")], \
        "an English description or alias is not a label; Q14 has no name at all"
    table = pack.labels(dump)
    assert table("Q12", names.NAME) == "ligne de Charpal" and table("Q10", names.NAME) is None
    parallel = a_dump(tmp_path / "p")
    pack.build(parallel, languages=("en",), chunk=2, workers=2)
    assert rows(parallel, "name") == rows(dump, "name")


def test_the_byte_check_never_hides_an_unnamed_entity():
    by_id = {e["id"]: e for e in LINES}
    for e in LINES:
        if not pack.maybe_unnamed(line_of(e)):
            assert names.has_label(e, pack.NAMED_UNLESS), e["id"]
    assert not pack.maybe_unnamed(line_of(by_id["Q10"])) and not pack.maybe_unnamed(line_of(by_id["Q11"]))
    assert pack.maybe_unnamed(line_of(by_id["Q12"])), "an English description only: parsed"
    assert pack.maybe_unnamed(line_of(by_id["Q13"])), "an English alias only: parsed"
    assert pack.maybe_unnamed(json.dumps(by_id["Q10"]).encode()), "another spacing: parsed, never skipped"
    assert pack.maybe_unnamed(b'{"type":"item","id":"Q1","claims":[]}'), "no label map: parsed"


def _v1(monkeypatch, dump: Path, **kw) -> dict:
    with monkeypatch.context() as m:
        m.setattr(pack, "PACK_VERSION", 1)
        m.setattr(pack, "_name_of", lambda obj: None)
        return pack.build(dump, languages=("en",), **kw)


def test_a_version_1_pack_is_upgraded_in_place_not_rebuilt(tmp_path, monkeypatch):
    fresh = a_dump(tmp_path / "fresh")
    pack.build(fresh, languages=("en",), chunk=2, workers=1)
    old = a_dump(tmp_path / "old")
    assert _v1(monkeypatch, old, chunk=2, workers=1)["pack_version"] == 1
    files = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in pack.chunk_files(old)}
    labels = rows(old, "label")
    with pytest.raises(PlatformMissing, match="no names yet"):
        pack.labels(old)("Q12", names.NAME)
    pack.reset_caches()
    said = []
    meta = pack.build(old, languages=("en",), chunk=2, workers=1, log=said.append)
    assert meta["complete"] and meta["pack_version"] == 2 and "names_chunks" not in meta
    assert any("no pass over the dump" in s for s in said)
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in pack.chunk_files(old)} == files, \
        "the chunk files untouched"
    assert rows(old, "label") == labels == rows(fresh, "label")
    assert rows(old, "name") == rows(fresh, "name"), "the names a version-2 pass writes"
    assert pack.labels(old)("Q12", names.NAME) == "ligne de Charpal"


def test_a_stopped_upgrade_defers_and_resumes_at_its_chunk(tmp_path, monkeypatch):
    dump = a_dump(tmp_path)
    _v1(monkeypatch, dump, chunk=2, workers=1)
    meta = pack.build(dump, languages=("en",), chunk=2, workers=1, stop=lambda: True)
    assert not meta["complete"], "the caller defers until the names are all there"
    on_disk = pack.read_meta(dump)
    assert on_disk["pack_version"] == 1 and on_disk["names_chunks"] == 1 and on_disk["complete"]
    assert not pack.is_complete(dump, ("en",))
    meta = pack.build(dump, languages=("en",), chunk=2, workers=2)
    assert meta["complete"] and meta["pack_version"] == 2 and pack.is_complete(dump, ("en",))
    assert [r[0] for r in rows(dump, "name")] == ["Q12", "Q13", "Q15"]


def test_the_record_s_title_and_text_through_the_pack(tmp_path):
    dump = a_dump(tmp_path)
    pack.build(dump, languages=("en",), chunk=4, workers=1)
    src = WikidataSource()
    state = DiscoverState(corpus_root=tmp_path, state_dir=tmp_path / "state")
    (tmp_path / "state").mkdir()
    docs = {r.native_id: src.parse(src.fetch(r)) for r in src.discover(state)}
    assert docs["12"].title == "ligne de Charpal" and docs["13"].title == "Rivière"
    assert docs["14"].title == "Q14", "no label, no name statement: the id"
    assert docs["10"].title == "has English" and docs["11"].title == "Has Mul"
    text = dict(docs["15"].sections)["en"]
    assert text.startswith("Schlacht") and "instance of: ligne de Charpal" in text, \
        "the referenced entity named by the pack's name table, not Q12"


def test_discover_defers_while_the_pack_is_upgraded(tmp_path, monkeypatch):
    dump = a_dump(tmp_path)
    _v1(monkeypatch, dump, chunk=2, workers=1)
    monkeypatch.setattr(pack, "processes", lambda: 1)
    calls = []
    real = pack.build

    def stopping(*a, **kw):
        calls.append(1)
        return real(*a, stop=lambda: True, **kw)
    monkeypatch.setattr(pack, "build", stopping)
    src = WikidataSource()
    state = DiscoverState(corpus_root=tmp_path, state_dir=tmp_path / "state")
    (tmp_path / "state").mkdir()
    refs = list(src.discover(state))
    assert calls and [r.scope for r in refs] == ["container"] and "resumes next scan" in refs[0].error


def test_the_reference_source_takes_the_name_last(tmp_path):
    from app.plugins.base import RefSyncState
    from app.refs import store as refs_store, wikidata as wref
    from tests.test_refs_wikidata import a_store

    dump = a_dump(tmp_path / "wikidata")
    pack.build(dump, languages=("en",), chunk=4, workers=1)
    con, store = a_store(tmp_path)
    assert refs_store.sync_source(con, wref.WikidataRefSource(), RefSyncState(ref_root=tmp_path))["rows"] == 6
    got = {r["id"]: (r["lang"], r["label"]) for r in store.rows("wikidata_entity")}
    assert got == {"P31": ("en", "instance of"), "Q10": ("en", "has English"), "Q11": ("mul", "Has Mul"),
                   "Q12": ("fr", "ligne de Charpal"), "Q13": ("fr", "Rivière"), "Q15": ("de", "Schlacht")}, \
        "a label of the pack's languages, then mul, then the name; Q14 has none"

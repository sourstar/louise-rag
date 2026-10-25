"""A collection whose plugin gives NAMES (wikidata; operator, 2026-10-24:
"names only") is also searched by name: every label and alias in every
language, matched whole against the question's word sequences — longest
name first, then the most linked entity (Wikidata's own near match) — and
never part of the record's text or vectors."""
from types import SimpleNamespace

import pytest

from app import collections as collections_mod, fts
from app.plugins.wikidata.source import index_names


@pytest.fixture(autouse=True)
def names_plugin(monkeypatch):
    monkeypatch.setitem(collections_mod.PLUGIN_REGISTRY, "wd-names",
                        SimpleNamespace(languages=("en",), index_names=staticmethod(index_names)))


def _collection(tmp_path, plugin="wd-names"):
    return collections_mod.Collection(
        name="w", plugin=plugin, profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")


def _entity(labels: dict, aliases: dict | None = None, sitelinks: int = 0) -> dict:
    return {"labels": {l: {"language": l, "value": v} for l, v in labels.items()},
            "aliases": {l: [{"language": l, "value": v} for v in vs] for l, vs in (aliases or {}).items()},
            "sitelinks": {f"site{i}": {} for i in range(sitelinks)}}


ENTITIES = {
    956: ("Beijing, capital of the People's Republic of China.",
          _entity({"en": "Beijing", "it": "Pechino", "de": "Peking", "zh": "北京"}, {"it": ["Pekino"]}, 300)),
    235: ("Monaco, sovereign city-state on the French Riviera.",
          _entity({"en": "Monaco", "it": "Monaco"}, sitelinks=280)),
    1726: ("Munich, capital of Bavaria.",
           _entity({"en": "Munich", "it": "Monaco di Baviera", "de": "München"}, sitelinks=250)),
    99999: ("Monaco, a ship.", _entity({"en": "Monaco"}, sitelinks=1)),
}


def _indexed(tmp_path):
    c = _collection(tmp_path)
    fts.index_books([("mul", bid, [{"id": f"wikidata:{bid}:0:1", "chapter": "en", "text": text}], None,
                      index_names(extra))
                     for bid, (text, extra) in ENTITIES.items()], c)
    return c


def _ids(hits):
    return [h["book_id"] for h in hits]


def test_the_plugin_gives_every_label_and_alias_in_every_language_and_its_sitelinks():
    pairs, pop = index_names(ENTITIES[956][1])
    assert ("it", "Pechino") in pairs and ("it", "Pekino") in pairs and ("zh", "北京") in pairs
    assert pop == 300
    assert index_names({}) == ([], 0)


def test_a_name_in_any_language_finds_the_entity_and_its_english_text(tmp_path):
    c = _indexed(tmp_path)
    hits = fts.search_names(c, "Quanti abitanti ha Pechino?", 10)
    assert _ids(hits) == [956]
    assert hits[0]["chunk_id"] == "wikidata:956:0:1" and hits[0]["text"].startswith("Beijing, capital")
    assert _ids(fts.search_names(c, "北京", 10)) == [956], "a name in a script without spaces"
    assert _ids(fts.search_names(c, "PEKINO", 10)) == [956], "case and accents fold"
    # names are index only: the record's text does not hold them
    assert fts.search("en", "Pechino", collection=c) == []
    fts.close_all()


def test_the_longer_name_first_then_the_more_linked(tmp_path):
    c = _indexed(tmp_path)
    assert _ids(fts.search_names(c, "Monaco di Baviera", 10)) == [1726, 235, 99999]
    assert _ids(fts.search_names(c, "Monaco", 10)) == [235, 99999], \
        "the city-state, linked from 280 wikis, before a ship of the same name"
    assert _ids(fts.search_names(c, "Monaco", 1)) == [235]
    fts.close_all()


def test_a_search_limited_to_languages_matches_names_in_those_languages(tmp_path):
    c = _indexed(tmp_path)
    assert _ids(fts.search_names(c, "Pechino", 10, langs=["it"])) == [956]
    assert fts.search_names(c, "Pechino", 10, langs=["de"]) == []
    assert _ids(fts.search_names(c, "Peking", 10, langs=["de"])) == [956]
    assert _ids(fts.search_names(c, "München", 10, langs=["de-at"])) == [1726], "a regional code is its language"
    fts.close_all()


def test_short_words_alone_are_no_name_and_nothing_else_moves(tmp_path):
    c = _indexed(tmp_path)
    assert fts.search_names(c, "di", 10) == [] and fts.search_names(c, "", 10) == []
    plain = _collection(tmp_path / "plain", plugin="gutenberg")
    fts.index_books([("en", 1, [{"id": "g:1:0:1", "chapter": "I", "text": "Monaco is mentioned here"}])], plain)
    assert fts.search_names(plain, "Monaco", 10) == [], "a collection without names"
    assert _ids(fts.search("en", "Monaco", collection=plain)) == [1]
    fts.close_all()


def test_re_indexing_keeps_one_hit_per_record(tmp_path):
    c = _indexed(tmp_path)
    _indexed(tmp_path)
    assert _ids(fts.search_names(c, "Monaco", 10)) == [235, 99999]
    fts.close_all()


def test_the_text_leg_carries_the_by_name_list_with_the_users_own_words(isolated_config, monkeypatch):
    """The by-name list is one more list of the text leg, from the reader's
    own words, limited like the search: `langs` = the asked languages."""
    from app import db, language_plan, llm, retrieval
    c = collections_mod.default_collection()
    monkeypatch.setattr(fts, "existing_groups", lambda collection=None: [])
    monkeypatch.setattr(fts, "names_capable", lambda collection: True)
    asked = []

    def by_name(collection, query, limit, langs=None, **kw):
        asked.append((query, langs))
        return [{"score": 1.0, "book_id": 956, "chunk_id": "wikidata:956:0:1", "chapter": "en",
                 "lang": "en", "text": "Beijing, capital…", "source": "names:en"}]
    monkeypatch.setattr(fts, "search_names", by_name)
    monkeypatch.setattr(llm, "translate_query", lambda q, langs: {})
    monkeypatch.setattr(llm, "language_restriction", lambda q: ([], q))
    out = retrieval._search_one_collection(db.connect(c), c, "Pechino", 5, "fts", False, set(),
                                           language_plan.plan("Pechino", [c], False))
    assert [retrieval.record_of(m) for m in out["merged"]] == ["wikidata:956"]
    monkeypatch.setattr(llm, "language_restriction", lambda q: (["it"], q))
    monkeypatch.setattr(llm, "translate_query", lambda q, langs: {l: q for l in langs})
    retrieval._search_one_collection(db.connect(c), c, "Pechino in italiano", 5, "fts", True, set(),
                                     language_plan.plan("Pechino in italiano", [c], True))
    assert asked == [("Pechino", None), ("Pechino in italiano", ["it"])]


def test_the_worker_indexes_the_names_it_parses_once_with_the_text(tmp_path, monkeypatch):
    """Through the real text stage: the parse pool's one parse gives the
    chunks and the names, and the names land on the record's first chunk."""
    import shutil
    from pathlib import Path
    from app import db, discovery, worker
    fixture = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"
    corpus = tmp_path / "corpus"
    shutil.copytree(fixture, corpus)
    c = collections_mod.Collection(
        name="named", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / "named" / "manifest.db",
        fts_dir=tmp_path / "named" / "fts", lance_dir=tmp_path / "named" / "lancedb")
    monkeypatch.setattr(fts, "names_capable", lambda collection: True)
    monkeypatch.setattr(fts, "index_names_of",
                        lambda collection, extra: ([("it", "Un Nome Inventato")], 7))
    con = db.connect(c)
    discovery.sync_collection(con, c)
    con.execute("UPDATE documents SET rdf_done=1")
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    row = con.execute("SELECT * FROM documents WHERE fts_done=0 ORDER BY native_id LIMIT 1").fetchone()
    assert worker._execute_step(con, c, "fts", row) is True
    assert con.execute("SELECT COUNT(*) FROM documents WHERE fts_done=1").fetchone()[0] == total
    hits = fts.search_names(c, "chi ha scritto un nome inventato", 50)
    assert len(hits) == total, "one hit per record, each on its first chunk"
    assert all(h["chunk_id"].split(":")[-2] == "0" for h in hits)
    fts.close_all()


def test_an_erase_rewrite_refuses_rather_than_dropping_the_names(tmp_path):
    c = _indexed(tmp_path)
    with pytest.raises(RuntimeError, match="rebuilt from its records"):
        fts.rewrite_without("en", c)
    assert _ids(fts.search_names(c, "Pechino", 10)) == [956], "nothing was touched"
    fts.close_all()

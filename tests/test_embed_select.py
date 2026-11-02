"""wikipedia's smart embedding (app/embed_select.py; operator, 2026-11-02:
"download and embed english only, plus all the articles that are either
missing or that are longer in another language; in this case pick only the
longer article in another language … Make it the default option";
"Interpretation A is fine")."""
from __future__ import annotations

import json
import sqlite3
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from app import collections as collections_mod, embed_select as es, slice_scope
from app.plugins.wikipedia import ids

EN, DE, FR, IT = (ids.LANG_ORDINAL[x] for x in ("en", "de", "fr", "it"))


def _sel(rows):
    lang, qid, tokens = zip(*rows)
    return list(es.smart(np.array(lang), np.array(qid), np.array(tokens), english=EN))


def test_english_always_and_the_longest_other_only_where_english_is_shorter():
    assert _sel([(EN, 1, 500), (DE, 1, 400), (FR, 1, 300)]) == [True, False, False]
    # German longer than English: German too, and only German (the longest)
    assert _sel([(EN, 2, 300), (DE, 2, 900), (FR, 2, 600), (IT, 2, 800)]) == [True, True, False, False]


def test_no_english_article_takes_the_longest_other_one():
    assert _sel([(DE, 3, 100), (FR, 3, 700), (IT, 3, 200)]) == [False, True, False]


def test_ties_go_to_the_lower_language_ordinal_then_the_first_document():
    assert _sel([(IT, 4, 500), (DE, 4, 500), (FR, 4, 500)]) == [False, True, False]
    assert _sel([(DE, 5, 500), (DE, 5, 500)]) == [True, False]


def test_nothing_is_declined_on_a_guess():
    # no Wikidata item: kept, whatever its length
    assert _sel([(DE, 0, 900), (FR, 0, 0)]) == [True, True]
    # length not known yet: kept, and an English one of unknown length keeps nothing out
    assert _sel([(EN, 6, 300), (DE, 6, 0)]) == [True, True]
    assert _sel([(EN, 7, 0), (DE, 7, 50), (FR, 7, 40)]) == [True, True, False]
    # an unknown language is kept
    assert _sel([(-1, 8, 10), (EN, 8, 900)]) == [True, True]


def test_ids_decode_both_layers():
    a = ids.native_id_of("de", 1234, archive=True)
    d = ids.native_id_of("fr", 98765)
    lang, page = es.decode(np.array([a, d]))
    assert list(lang) == [DE, FR] and list(page) == [1234, 98765]


def test_lookup_names_the_item_of_each_page():
    pages, qids = np.array([3, 10, 42]), np.array([7, 70, 420])
    assert list(es.lookup(pages, qids, np.array([42, 5, 3, 99]))) == [420, 0, 7, 0]


def test_a_page_props_pack_gives_each_page_its_item(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    rows = [b"\xff(12,'wikibase_item','Q7',NULL)",
            b"(5,'wikibase_item','Q42',NULL),(5,'defaultsort','Adams',NULL)",
            b"(9,'disambiguation','',NULL)"]
    pq.write_table(pa.table({"page": pa.array([12, 5, 9], pa.int64()), "start": [0, 0, 0], "end": [0, 0, 0],
                             "rows": pa.array(rows, pa.binary())}), tmp_path / "p.parquet")
    pages, qids = es.qids_of_pack(tmp_path / "p.parquet")
    assert list(pages) == [5, 12] and list(qids) == [42, 7]


# ---- the pass ---------------------------------------------------------------

def _manifest(rows):
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE documents (uid TEXT, native_id TEXT, tokens INTEGER, embed_done INTEGER)")
    con.execute("CREATE TABLE control (key TEXT PRIMARY KEY, value TEXT)")
    for i, (lang, page, tokens, done) in enumerate(rows):
        con.execute("INSERT INTO documents VALUES (?,?,?,?)",
                    (f"u{i}", str(ids.native_id_of(lang, page)), tokens, done))
    return con


def _coll(tmp_path, **kw):
    return SimpleNamespace(name="wikipedia", plugin="wikipedia", embed_select=kw.get("select"),
                           embed_slices=kw.get("slices"), snapshot="20261001", corpus_dir=tmp_path)


#: page -> item, per language (the packs' answer)
ITEMS = {"en": {1: 100, 2: 200}, "de": {11: 100, 12: 200, 13: 300}, "fr": {21: 300, 22: 0}}


def _qids(collection, langs):
    out = {}
    for lang, m in ITEMS.items():
        pages = np.array(sorted(p for p, q in m.items() if q), dtype=np.int64)
        out[ids.LANG_ORDINAL[lang]] = (pages, np.array([m[p] for p in pages], dtype=np.int64))
    return {o: v for o, v in out.items() if o in langs}


def _done(con):
    return [r[0] for r in con.execute("SELECT embed_done FROM documents ORDER BY rowid")]


def test_the_pass_declines_the_redundant_and_keeps_what_is_embedded(tmp_path):
    con = _manifest([("en", 1, 500, 0),     # item 100, English
                     ("de", 11, 300, 0),    # item 100, shorter than English: declined
                     ("en", 2, 100, 1),     # item 200, English, already embedded
                     ("de", 12, 900, 0),    # item 200, longer than English: embedded
                     ("de", 13, 200, 1),    # item 300, no English, shorter than French: kept (embedded already)
                     ("fr", 21, 400, 0),    # item 300, the longest: embedded
                     ("fr", 22, 50, 0)])    # no item: kept
    c = _coll(tmp_path)
    assert es.policy(c) == "smart" and not es.ready(con, c)
    out = es.apply(con, c, qids_for=_qids)
    assert out["selected"] == 5 and out["declined"] == 2 and out["newly_declined"] == 1
    assert _done(con) == [0, -1, 1, 0, 1, 0, 0], "an embedded document keeps its vectors"
    assert es.ready(con, c)
    assert es.apply(con, c, qids_for=_qids) == {"skipped": "up to date"}
    # a new document: the pass is redone for it
    con.execute("INSERT INTO documents VALUES ('u7', ?, 1000, 0)", (str(ids.native_id_of("it", 5)),))
    assert not es.ready(con, c)
    es.apply(con, c, qids_for=_qids)
    assert es.ready(con, c) and _done(con)[-1] == 0


def test_all_unwinds_the_marks_and_the_file_can_say_so(tmp_path):
    con = _manifest([("en", 1, 500, 0), ("de", 11, 300, 0)])
    es.apply(con, _coll(tmp_path), qids_for=_qids)
    assert _done(con) == [0, -1]
    every = _coll(tmp_path, select="all")
    assert es.policy(every) == "all" and not es.ready(con, every)
    es.apply(con, every)
    assert _done(con) == [0, 0] and es.ready(con, every)
    assert con.execute("SELECT COUNT(*) FROM control").fetchone()[0] == 0


def test_embed_slices_narrow_the_smart_choice_too(tmp_path):
    con = _manifest([("en", 1, 500, 0), ("de", 12, 900, 0), ("fr", 22, 50, 0)])
    c = _coll(tmp_path, slices=("lang=en",))
    assert slice_scope.embedded(c) is None, "the smart pass owns the marks"
    assert slice_scope.embedded(_coll(tmp_path, select="all", slices=("lang=en",))) == ("lang=en",)
    es.apply(con, c, qids_for=_qids)
    assert _done(con) == [0, -1, -1]


def test_a_stop_ends_the_pass_at_a_window_and_it_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(es, "WINDOW", 1)
    con = _manifest([("en", 1, 500, 0), ("de", 11, 300, 0), ("de", 13, 10, 0)])
    stop = threading.Event()
    stop.set()
    out = es.apply(con, _coll(tmp_path), stop=stop, qids_for=_qids)
    assert out["interrupted"] and not es.ready(con, _coll(tmp_path))
    es.apply(con, _coll(tmp_path), qids_for=_qids)
    assert es.ready(con, _coll(tmp_path)) and _done(con) == [0, -1, 0]


def test_a_missing_page_props_defers_the_pass(tmp_path):
    con = _manifest([("en", 1, 500, 0)])
    out = es.apply(con, _coll(tmp_path))
    assert "page_props not on disk" in out["deferred"] and not es.ready(con, _coll(tmp_path))


def test_only_wikipedia_defaults_to_smart_and_another_plugin_is_skipped(tmp_path):
    assert es.policy(SimpleNamespace(plugin="gutenberg", embed_select=None)) == "all"
    con = _manifest([("en", 1, 500, 0)])
    g = SimpleNamespace(name="g", plugin="gutenberg", embed_select="smart", embed_slices=None)
    assert "no such grouping" in es.apply(con, g)["skipped"]


# ---- the collection file ------------------------------------------------------

def test_the_file_states_embed_select_and_refuses_another_value(tmp_path):
    from app import collection_file
    assert collection_file._parse("embed_select", "smart", "f") == "smart"
    assert collection_file._parse("embed_select", "default", "f") is None
    with pytest.raises(collection_file.SetRefused, match="embed_select is one of"):
        collection_file._parse("embed_select", "some", "f")


def test_the_loader_reads_embed_select_and_refuses_smart_where_nothing_groups(tmp_path, isolated_config,
                                                                             monkeypatch):
    from app.hold import collection as col
    from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed, fake_query_embed
    from tests.hold.test_template import recipe
    root = tmp_path / "louise"
    (root / "collections").mkdir(parents=True)
    cx = col.create_from_recipe(root / "collections", "gutenberg", recipe(), platform_image=PLATFORM,
                                embed=fake_embed, embed_query=fake_query_embed, name="gutenberg",
                                key=KEY, now=NOW)
    cfg = tmp_path / "config"
    (cfg / "collections").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOUISE_ROOT", str(root))
    monkeypatch.setenv("CONFIG_DIR", str(cfg))
    monkeypatch.setattr(collections_mod.config, "DATA_DIR", root)
    path = cfg / "collections" / "gutenberg.toml"
    base = (f'codex = "{cx.collection_id}"\nslices = "all"\nkinds = ["records", "vectors"]\n'
            'share = false\n\n[encoder]\nurl = "http://enc:8080"\napi = "openai"\n'
            'embed_model = "m"\nquery_model = "m"\n')
    path.write_text(base.replace("share = false", 'embed_select = "all"\nshare = false'))
    assert collections_mod._load_one(path).embed_select == "all"
    path.write_text(base.replace("share = false", 'embed_select = "smart"\nshare = false'))
    with pytest.raises(ValueError, match="no such grouping"):
        collections_mod._load_one(path)
    path.write_text(base.replace("share = false", 'embed_select = "loud"\nshare = false'))
    with pytest.raises(ValueError, match="embed_select is one of all, smart"):
        collections_mod._load_one(path)


def test_the_card_names_the_languages_held_not_hand_populated():
    from app import web
    from app.acquire.specs import ACQUIRE_REGISTRY
    assert ACQUIRE_REGISTRY["wikipedia"].profile_mode == "additive"
    held = SimpleNamespace(plugin="wikipedia", profile=None, slices=("lang=en", "lang=it"))
    assert web._profile_label(held) == "en,it"
    assert web._profile_held(held) == ["en", "it"]
    # a file naming nothing (the node's `slices = []`) is still said as such
    assert web._profile_label(SimpleNamespace(plugin="wikipedia", profile=None, slices=())) == \
        "(hand-populated)"
    assert web._profile_label(SimpleNamespace(plugin="wikipedia", profile="en,de", slices=None)) == "en,de"

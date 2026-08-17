"""Regression test: documents.native_id is TEXT (generic across plugins), so
apply_scope's ordering must CAST to INTEGER to match the pre-refactor
behavior of ordering by the old int `id` column. Without the cast, "100"
would sort before "3" as a string, silently changing which documents
sample_per_lang/limit select.
"""
from app import collections as collections_mod, db, discovery


def _insert(con, native_id: str, lang: str):
    con.execute(
        "INSERT INTO documents(uid,source,native_id,path,lang,rdf_done,updated_at) "
        "VALUES(?,?,?,?,?,1,0)",
        (f"gutenberg:{native_id}", "gutenberg", native_id, f"/corpus/{native_id}", lang))


def test_sample_per_lang_orders_numerically(isolated_config):
    collection = collections_mod.default_collection()
    con = db.connect(collection)
    for native_id in ("3", "22", "100"):
        _insert(con, native_id, "en")
    for native_id in ("40", "5"):
        _insert(con, native_id, "fr")

    db.set_scope(con, {"sample_per_lang": 1})
    discovery.apply_scope(con)

    in_scope = {r["native_id"] for r in con.execute(
        "SELECT native_id FROM documents WHERE in_scope=1")}
    assert in_scope == {"3", "5"}, (
        "sample_per_lang must pick the numerically lowest native_id per "
        f"language, got {in_scope}")


def test_limit_orders_numerically(isolated_config):
    collection = collections_mod.default_collection()
    con = db.connect(collection)
    for native_id in ("3", "22", "100"):
        _insert(con, native_id, "en")

    db.set_scope(con, {"limit": 2})
    discovery.apply_scope(con)

    in_scope = {r["native_id"] for r in con.execute(
        "SELECT native_id FROM documents WHERE in_scope=1")}
    assert in_scope == {"3", "22"}, (
        f"limit must keep the numerically lowest native_ids, got {in_scope}")

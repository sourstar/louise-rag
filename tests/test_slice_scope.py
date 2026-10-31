"""What a node holds is what it processes (operator, 2026-10-31, on wikidata:
"As for other collections the grow profile should drive what's fetched and
processed"; app/slice_scope.py)."""
import threading
from types import SimpleNamespace

import pytest

from app import collections as collections_mod, db, discovery, slice_scope
from app.acquire import spec as acquire_spec
from app.acquire.specs import ACQUIRE_REGISTRY
from app.pack import slicing

FAMILY = {1: "person", 2: "taxon", 3: "person", 4: "work", 5: "internal", 6: None}


def _col(tmp_path, slices, plugin="wikidata"):
    d = tmp_path / "wd"
    d.mkdir(exist_ok=True)
    return collections_mod.Collection(
        name="wd", plugin=plugin, profile=None, embed_mode_default=None,
        corpus_dir=d / "corpus", manifest_path=d / "manifest.db",
        fts_dir=d / "fts", lance_dir=d / "lancedb", slices=slices)


def _rows(con, ids):
    for i in ids:
        con.execute("INSERT INTO documents(uid, source, native_id, path) VALUES (?,?,?,?)",
                    (f"wd:{i}", "wikidata", str(i), f"dump::{i}"))


def _context(dump="20261012"):
    return SimpleNamespace(fmap=SimpleNamespace(sha256="sha256:" + "a" * 64),
                           dump=SimpleNamespace(name=dump), walked={})


def _hint(plugin, row, context):
    fam = FAMILY.get(int(row["native_id"]))
    return f"family={fam}/range=0" if fam else None


def _apply(con, c, **kw):
    kw.setdefault("context_for", lambda collection, log=None, stop=None: _context())
    return slice_scope.apply(con, c, slice_hint=_hint, **kw)


def _scope(con):
    return {int(r["native_id"]): r["in_scope"]
            for r in con.execute("SELECT native_id, in_scope FROM documents")}


def test_only_the_slices_held_are_in_scope(isolated_config, tmp_path):
    c = _col(tmp_path, ["family=person", "family=work"])
    con = db.connect(c)
    _rows(con, FAMILY)
    assert slice_scope.ready(con, c) is False, "nothing is processed before the pass"
    out = _apply(con, c)
    assert (out["held"], out["not_held"], out["unknown"]) == (3, 3, 1)
    assert _scope(con) == {1: 1, 2: 0, 3: 1, 4: 1, 5: 0, 6: 0}, \
        "a document whose slice is not known yet is not processed"
    assert slice_scope.ready(con, c) is True


def test_every_slice_held_or_another_plugin_narrows_nothing(isolated_config, tmp_path):
    for c in (_col(tmp_path, "all"), _col(tmp_path, ["lang=it"], plugin="wikipedia")):
        con = db.connect(c)
        assert slice_scope.held(c) is None and slice_scope.ready(con, c) is True
        assert "skipped" in slice_scope.apply(con, c)


def test_the_pass_resumes_from_its_cursor_and_takes_only_new_rows(isolated_config, tmp_path,
                                                                  monkeypatch):
    monkeypatch.setattr(slice_scope, "WINDOW", 2)
    c = _col(tmp_path, ["family=person"])
    con = db.connect(c)
    _rows(con, [1, 2, 3, 4])
    windows = {"n": 0}

    class StopAfterOne:
        def is_set(self):
            windows["n"] += 1
            return windows["n"] > 1

    out = _apply(con, c, stop=StopAfterOne())
    assert out["interrupted"] and out["rows"] == 2
    assert slice_scope.ready(con, c) is False
    assert _scope(con) == {1: 1, 2: 0, 3: 1, 4: 1}, "the second window not written yet"
    out = _apply(con, c)
    assert out["rows"] == 2 and _scope(con) == {1: 1, 2: 0, 3: 1, 4: 0}
    assert slice_scope.ready(con, c) is True

    _rows(con, [5])                       # discovery adds a row
    assert slice_scope.ready(con, c) is False, "a row the pass has not seen"
    assert _apply(con, c)["rows"] == 1
    assert slice_scope.ready(con, c) is True


def test_a_holding_that_grows_widens_back_what_it_narrowed(isolated_config, tmp_path):
    c = _col(tmp_path, ["family=person"])
    con = db.connect(c)
    _rows(con, FAMILY)
    _apply(con, c)
    assert _scope(con)[4] == 0
    grown = _col(tmp_path, ["family=person", "family=work"])
    assert slice_scope.ready(con, grown) is False, "the slices changed"
    out = _apply(con, grown)
    assert out["rows"] == len(FAMILY), "the whole manifest again"
    assert _scope(con)[4] == 1 and _scope(con)[2] == 0


def test_a_new_dump_redoes_the_pass(isolated_config, tmp_path):
    c = _col(tmp_path, ["family=person"])
    con = db.connect(c)
    _rows(con, FAMILY)
    _apply(con, c)
    out = _apply(con, c, context_for=lambda collection, log=None, stop=None: _context("20261109"))
    assert out["rows"] == len(FAMILY)


def test_discoverys_widening_sends_the_pass_back_to_the_start(isolated_config, tmp_path):
    c = _col(tmp_path, ["family=person"])
    con = db.connect(c)
    _rows(con, FAMILY)
    _apply(con, c)
    discovery.apply_scope(con)
    assert _scope(con)[2] == 1, "apply_scope widened every row"
    assert slice_scope.ready(con, c) is False, "so the stages wait for the pass again"
    assert _apply(con, c)["rows"] == len(FAMILY)
    assert _scope(con)[2] == 0 and slice_scope.ready(con, c) is True


def test_a_context_not_there_yet_defers_and_the_stages_keep_waiting(isolated_config, tmp_path):
    c = _col(tmp_path, ["family=person"])
    con = db.connect(c)
    _rows(con, FAMILY)

    def unavailable(collection, log=None, stop=None):
        raise slicing.ContextUnavailable("the family codes are being computed")

    out = slice_scope.apply(con, c, context_for=unavailable, slice_hint=_hint)
    assert "being computed" in out["deferred"]
    assert slice_scope.ready(con, c) is False


def test_a_scope_config_that_narrows_is_never_widened_by_the_pass(isolated_config, tmp_path):
    c = _col(tmp_path, ["family=person"])
    con = db.connect(c)
    _rows(con, FAMILY)
    db.set_scope(con, {"limit": 1})
    con.execute("UPDATE documents SET in_scope=0 WHERE native_id='3'")
    _apply(con, c)
    assert _scope(con)[3] == 0, "the operator's own narrowing stands"


# ---------------- the spec: a family is a profile ----------------

def test_each_wikidata_family_is_a_profile_the_library_can_grow_by():
    w = ACQUIRE_REGISTRY["wikidata"]
    assert acquire_spec.everything_profile(w) == "all"
    assert w.profile_mode == "additive"
    assert acquire_spec.profile_for_slices(w, ["family=person", "family=work"]) == "person,work"
    assert acquire_spec.slices_for_profile(w, "person,work") == ["family=person", "family=work"], \
        "by name: every family fetches the one dump, so a filter match would name them all"
    assert acquire_spec.slices_for_profile(w, "all") == "all"
    assert all(f in w.profile_labels for f in w.profiles if f != "all")


def test_wikipedias_profiles_still_resolve_by_what_they_fetch():
    assert acquire_spec.slices_for_profile(ACQUIRE_REGISTRY["wikipedia"], "demo") == ["lang=tr"]


def test_is_held_reads_a_slice_under_a_held_one():
    assert slice_scope.is_held("family=person/range=3", ("family=person",))
    assert not slice_scope.is_held("family=personal/range=3", ("family=person",))
    assert not slice_scope.is_held(None, ("family=person",))


def test_the_hints_are_computed_in_the_parse_pool_when_there_is_one(isolated_config, tmp_path,
                                                                   monkeypatch):
    """The multi-core rule: the first pass ran on one core (954 s for 75.9 M
    rows); with a pool the children place the rows, the parent writes."""
    from app import parallel
    c = _col(tmp_path, ["family=person"])
    con = db.connect(c)
    _rows(con, FAMILY)
    asked = []
    monkeypatch.setattr(parallel, "pool", lambda: object())

    def compute(kind, collection, rows):
        asked.append((kind, len(rows)))
        return [(True, _hint(None, r, None)) if r["native_id"] != "5" else (False, {"type": "X"})
                for r in rows]
    monkeypatch.setattr(parallel, "compute", compute)
    out = slice_scope.apply(con, c, context_for=lambda collection, log=None, stop=None: _context())
    assert asked == [("slice-hint", len(FAMILY))]
    assert _scope(con) == {1: 1, 2: 0, 3: 1, 4: 0, 5: 0, 6: 0}
    assert out["rows"] == len(FAMILY) and out["unknown"] == 1, \
        "row 5 failed in the child and was placed in this process instead"

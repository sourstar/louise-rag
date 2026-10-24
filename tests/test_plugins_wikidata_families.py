"""tests/test_plugins_wikidata_families.py — wikidata sliced by FAMILY under a
map pinned at creation (operator, 2026-10-24: "Go with A";
docs/notes/wikidata-family-slices.md; pack-format.md §2 amended): the map
from a dump's class tree (nearest root, a tie to precedence), an entity's
family, the walk of classes newer than the map, the pack's version-3 passes
(upgraded in place from version 2), the records build's slices through the
Codex's map, the SHARD member and the records check."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.pack import slicing
from app.plugins.wikidata import families, pack
from tests.test_plugins_wikidata_source import a_dump, entity_lines


@pytest.fixture(autouse=True)
def _fresh():
    pack.reset_caches()
    yield
    pack.reset_caches()


def _st(pid: str, q: int, rank: str = "normal", snaktype: str = "value") -> dict:
    snak = {"snaktype": snaktype, "property": pid, "datatype": "wikibase-item"}
    if snaktype == "value":
        snak["datavalue"] = {"value": {"entity-type": "item", "numeric-id": q, "id": f"Q{q}"},
                             "type": "wikibase-entityid"}
    return {"mainsnak": snak, "type": "statement", "rank": rank}


def _item(q: int, label: str, **claims) -> dict:
    return {"type": "item", "id": f"Q{q}", "labels": {"en": {"language": "en", "value": label}},
            "claims": {pid: [_st(pid, v) if isinstance(v, int) else v for v in vs] for pid, vs in claims.items()}}


def _line(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


# ---- the map ---------------------------------------------------------------------------------

def test_a_class_takes_its_nearest_root_and_a_tie_goes_to_the_first_family():
    child, parent = zip(*[
        (100, 486972),               # municipality → human settlement (1 step) …
        (100, 200), (200, 43229),    # … and → local government → organization (2): a place
        (300, 811979), (300, 43229),  # one step to built structure and to organization: heritage, first
        (400, 401), (401, 400), (401, 618123),   # a P279 cycle under geographical feature: settled once
        (500, 13442814), (13442814, 17537576),  # under scholarly article, itself a root under creative work
    ])
    got = families.nearest_roots(child, parent)
    code = {v: k for k, v in families.CODE.items()}
    assert code[got[100]] == "place" and code[got[300]] == "heritage"
    assert code[got[400]] == code[got[401]] == "place"
    assert code[got[13442814]] == code[got[500]] == "scholarly", "a root is its own family's, at distance 0"
    assert code[got[5]] == "person" and 999 not in got


def test_the_map_is_canonical_bytes_named_by_their_hash_and_refused_otherwise():
    raw = families.build_map([100], [486972], max_item=1000, dump="wikidata-20261012-all.json.bz2")
    fmap = families.load_map(raw)
    assert fmap.sha256 == "sha256:" + hashlib.sha256(raw).hexdigest() and fmap.max_item == 1000
    assert fmap.classes[100] == fmap.classes[486972] == families.CODE["place"]
    assert raw == families.build_map([100], [486972], max_item=1000, dump="wikidata-20261012-all.json.bz2")
    d = json.loads(raw)
    with pytest.raises(families.FamilyError, match="canonical"):
        families.load_map(json.dumps(d, indent=1).encode())
    with pytest.raises(families.FamilyError, match="families"):
        families.load_map(families.canonical({**d, "families": list(reversed(d["families"]))}))


def test_an_entity_s_family_from_its_own_statements():
    raw = families.build_map([515, 41176], [486972, 811979], max_item=1000, dump="d")
    fmap = families.load_map(raw)
    assert families.family_code(_item(1, "Rome", P31=[515]), fmap) == families.CODE["place"]
    assert families.family_code(_item(2, "Castle town", P31=[515, 41176]), fmap) == families.CODE["heritage"], \
        "an entity in two families takes the first"
    deprecated = _item(3, "Was a building", P31=[_st("P31", 41176, rank="deprecated"), 515])
    assert families.family_code(deprecated, fmap) == families.CODE["place"], "a deprecated P31 is not read"
    listed = _item(4, "A listed oak", P31=[777], P1435=[900])
    assert families.family_code(listed, fmap) == families.CODE["heritage"], "a heritage designation"
    none = _item(5, "Not listed", P31=[777], P1435=[_st("P1435", 0, snaktype="novalue")])
    assert families.family_code(none, fmap) == families.OTHER, "\"no value\" designates nothing"
    assert families.family_code({"id": "P31"}, fmap) == families.CODE["property"]
    assert families.family_and_class(_item(6, "x", P31=[777, 515]), fmap) == (families.CODE["place"], 515)


def test_a_class_newer_than_the_map_is_walked_to_the_nearest_class_that_existed():
    fmap = families.load_map(families.build_map([515], [486972], max_item=1000, dump="d"))
    # 2001 → 2000 (both new) → 515 (old, place); 3000 → 999 (old, under no root) → other
    walked = families.walk([2001, 2000, 3000, 2002, 2002], [2000, 515, 999, 515, 41176], fmap)
    assert walked == {2001: families.CODE["place"], 2000: families.CODE["place"], 3000: families.OTHER,
                      2002: families.CODE["place"]}, "a tie at one level: the first family (other is last)"
    e = _item(7, "A new kind of town", P31=[2001])
    assert families.family_code(e, fmap) == families.OTHER, "without this dump's walk: other"
    assert families.family_code(e, fmap, walked) == families.CODE["place"]
    assert families.family_code(_item(8, "Unmapped old class", P31=[999]), fmap, walked) == families.OTHER


def test_the_slice_is_the_family_then_the_entity_s_number_in_its_kind():
    assert families.slice_name(families.CODE["heritage"], "Q23456789") == "family=heritage/range=2"
    assert families.slice_name(families.CODE["property"], "P31") == "family=property/range=0"
    with pytest.raises(families.FamilyError):
        families.slice_name(0, "Q1")


# ---- the pack (version 3) --------------------------------------------------------------------

CLASSES = [
    _item(811979, "built structure"),
    _item(2354973, "road tunnel", P279=[811979]),
    _item(486972, "human settlement"),
    _item(515, "city", P279=[486972, 100]),
    _item(100, "local government", P279=[43229]),
    _item(43229, "organization"),
    _item(600, "Bologna", P31=[515]),
    _item(601, "Old oak", P31=[777], P1435=[900]),
]


def _dump(root: Path) -> Path:
    return a_dump(root, lines=entity_lines() + [_line(o) for o in CLASSES])


def test_the_pack_builds_the_class_tree_the_map_and_every_entity_s_code(tmp_path):
    dump = _dump(tmp_path)
    said = []
    meta = pack.build(dump, languages=("en",), chunk=4, workers=1, log=said.append)
    assert meta["complete"] and meta["families_done"] and meta["pack_version"] == pack.PACK_VERSION == 3
    assert meta["max_item"] == 33010486, "the largest item number of the dump, the pruned article included"
    raw = pack.creation_map(dump)
    assert meta["families_map"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    fmap = families.load_map(raw)
    assert fmap.classes[515] == families.CODE["place"] and fmap.classes[2354973] == families.CODE["heritage"]
    ids = [json.loads(l)["id"] for l in entity_lines() + [_line(o) for o in CLASSES]]
    got = {eid: families.FAMILIES[pack.family_code_at(dump, fmap.sha256, i) - 1] for i, eid in enumerate(ids)}
    assert got["Q42"] == "person" and got["Q3331"] == "heritage" and got["P31"] == "property"
    assert got["Q33010486"] == "scholarly", "the pruned article has its code too: the class tree is the dump's"
    assert got["Q600"] == "place" and got["Q601"] == "heritage" and got["Q43229"] == "other"
    report = json.loads(pack.report_path(pack.pack_dir(dump), fmap.sha256).read_text())
    by = {f["family"]: f for f in report["families"]}
    assert by["scholarly"]["records"] == 0 and by["scholarly"]["pruned"] == 1
    assert by["place"]["top_classes"] == [[515, "city", 1]]
    assert any("families under map" in s for s in said)
    assert pack.is_complete(dump, ("en",)) and pack.ready(dump)


def test_a_version_2_pack_is_upgraded_in_place_and_comes_out_as_a_fresh_build(tmp_path, monkeypatch):
    fresh = _dump(tmp_path / "fresh")
    pack.build(fresh, languages=("en",), chunk=4, workers=1)
    old = _dump(tmp_path / "old")
    with monkeypatch.context() as m:              # what version 2 left: no class tree, no families
        m.setattr(pack, "PACK_VERSION", 2)
        real = pack._finish

        def v2_finish(dump, d, meta, **kw):
            m.setattr(pack, "_codes_pass", lambda *a, **k: True)
            m.setattr(pack, "_lines_pass", _only_names(real_lines := pack._lines_pass))
            out = real(dump, d, meta, **kw)
            for k in ("subclass_done", "max_item", "families_map", "families_done", "families_codes"):
                out.pop(k, None)
            pack._write_meta(d, out)
            return out
        m.setattr(pack, "_finish", v2_finish)
        assert pack.build(old, languages=("en",), chunk=4, workers=1)["pack_version"] == 2
    assert pack.ready(old) and not pack.is_complete(old, ("en",)), "parser 2 reads it; discovery upgrades it"
    files = {p.name: p.stat().st_mtime_ns for p in pack.chunk_files(old)}
    pack.reset_caches()
    said = []
    meta = pack.build(old, languages=("en",), chunk=4, workers=1, log=said.append)
    assert meta["pack_version"] == 3 and meta["families_done"]
    assert any("version 2 → 3" in s for s in said)
    assert {p.name: p.stat().st_mtime_ns for p in pack.chunk_files(old)} == files, "the chunk files untouched"
    assert pack.creation_map(old) == pack.creation_map(fresh)
    sha = pack.read_meta(old)["families_map"]
    assert pack.codes_path(pack.pack_dir(old), sha).read_bytes() == \
        pack.codes_path(pack.pack_dir(fresh), sha).read_bytes()


def _only_names(real_lines):
    """Version 2's `_finish` ran the names pass alone."""
    def run(dump, d, meta, con, *, what, **kw):
        return real_lines(dump, d, meta, con, what=what, **kw) if what in ("names", "languages") else True
    return run


def test_a_stopped_families_pass_resumes_at_its_chunk(tmp_path):
    dump = _dump(tmp_path)
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return True
    meta = pack.build(dump, languages=("en",), chunk=4, workers=1, stop=stop)
    while not meta.get("families_done"):
        meta = pack.build(dump, languages=("en",), chunk=4, workers=1, stop=lambda: True)
    assert meta["complete"] and pack.is_complete(dump, ("en",))
    fresh = _dump(tmp_path / "fresh")
    pack.build(fresh, languages=("en",), chunk=4, workers=1)
    sha = meta["families_map"]
    assert pack.codes_path(pack.pack_dir(dump), sha).read_bytes() == \
        pack.codes_path(pack.pack_dir(fresh), sha).read_bytes(), "stopped at every chunk, the same bytes"


def test_codes_under_another_map_walk_this_dump_s_newer_classes(tmp_path):
    dump = _dump(tmp_path)
    pack.build(dump, languages=("en",), chunk=4, workers=1)
    # a map pinned on an OLDER dump: it knew human settlement only, and its
    # largest item was 10,000 — city (515) existed under no root of it, and
    # every class above 10,000 is newer and walked over this dump's links
    older = families.build_map([], [], max_item=10_000, dump="wikidata-20250101-all.json.bz2")
    assert pack.ensure_codes(dump, older, workers=1)
    fmap = families.load_map(older)
    ids = [json.loads(l)["id"] for l in entity_lines() + [_line(o) for o in CLASSES]]
    got = {eid: families.FAMILIES[pack.family_code_at(dump, fmap.sha256, i) - 1] for i, eid in enumerate(ids)}
    assert got["Q600"] == "other", "city was under no root of the pinned map: other, whatever this dump says"
    assert got["Q3331"] == "heritage", "road tunnel is newer than the map: walked to built structure"


# ---- the slicing, the SHARD member, the records check ---------------------------------------

def test_the_slicing_block_names_the_map_and_its_member():
    sha = "sha256:" + "ab" * 32
    block = slicing.family_block(sha)
    assert block["keys"] == ["family", "range"] and slicing.family_map_of(block) == sha
    assert slicing.map_member(sha) == f"slicing/{'ab' * 32}.json"
    assert slicing.family_map_of({"keys": ["range"]}) is None
    with pytest.raises(ValueError):
        slicing.map_member("sha256:short")
    from app.hold import template
    with pytest.raises(template.TemplateError, match="names its map"):
        template._check_slicing({**block, "family": {"field": "extra.claims", "rule": slicing.FAMILY_RULE}})
    assert template._check_slicing(dict(block)) == block


def test_the_hint_and_the_parse_give_the_same_slice(tmp_path):
    from app.plugins.wikidata.source import WikidataSource
    dump = _dump(tmp_path)
    pack.build(dump, languages=("en",), chunk=4, workers=1)
    fmap = families.load_map(pack.creation_map(dump))
    ctx = slicing.FamilyContext(fmap, dump, {})
    src = WikidataSource()
    (tmp_path / "state").mkdir()
    from app.plugins.base import DiscoverState
    for ref in src.discover(DiscoverState(corpus_root=tmp_path, state_dir=tmp_path / "state")):
        row = {"path": ref.locator, "native_id": ref.native_id}
        hint = slicing.slice_hint("wikidata", row, ctx)
        doc = src.parse(src.fetch(ref))
        assert hint == slicing.slice_of(slicing.scheme_for("wikidata"), doc, int(ref.native_id), ctx), ref.native_id
    assert slicing.slice_hint("wikidata", {"path": f"{dump}#0", "native_id": "42"}, ctx) == "family=person/range=0"
    assert slicing.slice_hint("wikidata", {"path": f"{dump}#0", "native_id": "42"}) == "range=0", \
        "without the map: the id ranges a collection created before keeps"


def test_create_writes_the_map_as_a_member_and_the_snapshot_lists_it(tmp_path):
    from app.hold import collection as col, snapshot as sn
    from app.hold.codex import CodexError
    raw = families.build_map([515], [486972], max_item=1000, dump="d")
    sha = "sha256:" + hashlib.sha256(raw).hexdigest()
    codex = SimpleNamespace(slicing=slicing.family_block(sha))
    col.write_slicing_map(tmp_path, codex, raw)
    assert (tmp_path / slicing.map_member(sha)).read_bytes() == raw
    with pytest.raises(CodexError, match="hashes otherwise"):
        col.write_slicing_map(tmp_path, codex, raw + b" ")
    with pytest.raises(CodexError, match="no map was given"):
        col.write_slicing_map(tmp_path, codex, None)
    col.write_slicing_map(tmp_path / "ranges", SimpleNamespace(slicing={"keys": ["range"]}), None)
    assert not (tmp_path / "ranges").exists(), "a slicing that names no map: no member"
    from app.share import members as mem
    got = mem._whole_collection_members({slicing.map_member(sha): {"root": "r" * 64, "bytes": len(raw)}})
    assert [m.name for m in got] == ["slicing"]
    # the snapshot lists it, required: without it the SHARD is unshareable
    calset = "sha256:" + "cd" * 32
    from app.hold import shard as shard_mod
    for p in (shard_mod.VALIDATION_PATH, *shard_mod.calibration_paths(calset)):
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_bytes(b"x")
    cx = SimpleNamespace(slicing=codex.slicing, space=SimpleNamespace(calibration=SimpleNamespace(set=calset)))
    assert slicing.map_member(sha) in sn._files(tmp_path, cx)
    (tmp_path / slicing.map_member(sha)).unlink()
    with pytest.raises(CodexError, match="missing from the SHARD"):
        sn._files(tmp_path, cx)


def test_the_plugin_gives_create_the_block_and_the_map_of_the_pinned_dump(tmp_path, monkeypatch):
    from app import collections as collections_mod
    from app.plugins.wikidata.source import WikidataSource
    dump = _dump(tmp_path / "corpus")
    pack.build(dump, languages=("en",), chunk=4, workers=1)
    held = SimpleNamespace(corpus_dir=tmp_path / "corpus", snapshot="20261012")
    monkeypatch.setattr(collections_mod, "by_name", lambda name: held)
    block, raw = WikidataSource().creation_slicing("wikidata")
    assert raw == pack.creation_map(dump) and slicing.family_map_of(block) == pack.read_meta(dump)["families_map"]
    a_dump(tmp_path / "corpus", date="20261019", lines=entity_lines())
    held.snapshot = None
    with pytest.raises(pack.PackError, match="no `snapshot`"):
        WikidataSource().creation_slicing("wikidata")


def test_the_records_check_reads_the_map_member_and_each_record_s_slice(tmp_path):
    from app.hold import check
    raw = families.build_map([515], [486972], max_item=1000, dump="d")
    sha = "sha256:" + hashlib.sha256(raw).hexdigest()
    ctx = SimpleNamespace(codex=SimpleNamespace(slicing=slicing.family_block(sha)), shard_tree=tmp_path)
    assert check._family_map(ctx) == (None, f"{slicing.map_member(sha)}:missing")
    (tmp_path / "slicing").mkdir()
    (tmp_path / slicing.map_member(sha)).write_bytes(raw + b"\n")
    assert check._family_map(ctx)[1] == f"{slicing.map_member(sha)}:hash"
    (tmp_path / slicing.map_member(sha)).write_bytes(raw)
    fmap, broken = check._family_map(ctx)
    assert broken is None and fmap.sha256 == sha
    assert check._slice_unless_walked(fmap, {"extra": _item(600, "Bologna", P31=[515])}) == "family=place/range=0"
    assert check._slice_unless_walked(fmap, {"extra": _item(602, "New", P31=[5000])}) is None, \
        "a class newer than the map: placed by the dump's links, not checked from the record"
    assert check._family_map(SimpleNamespace(codex=SimpleNamespace(slicing={"keys": ["range"]}),
                                             shard_tree=tmp_path)) == (None, None)


def test_the_records_build_slices_a_family_collection_by_its_codex_s_map(isolated_config, tmp_path):
    """End to end: a wikidata collection whose Codex names the map, the map
    in its SHARD — discovery, then the build's plan pass: every record in
    the slice its family gives it, read from the pack's codes."""
    from dataclasses import replace
    from app import collections as collections_mod, db, discovery
    from app.hold import codex as cx
    from app.hold.codex import License, Parser
    from app.pack import build as pack_build
    from tests.hold.conftest import pubmed
    corpus = tmp_path / "corpus"
    dump = _dump(corpus)
    pack.build(dump, languages=("en",), chunk=4, workers=1)
    raw = pack.creation_map(dump)
    sha = "sha256:" + hashlib.sha256(raw).hexdigest()
    d = tmp_path / "wikidata"
    (d / "shard" / "slicing").mkdir(parents=True)
    codex = pubmed(plugin="wikidata", parser=Parser("wikidata", 2), license=License("green", None),
                   slicing=slicing.family_block(sha))
    (d / "shard" / "codex.json").write_bytes(codex.canonical())
    (d / "shard" / slicing.map_member(sha)).write_bytes(raw)
    c = collections_mod.Collection(name="wikidata", plugin="wikidata", profile=None, embed_mode_default=None,
                                   corpus_dir=corpus, manifest_path=d / "state" / "manifest.sqlite",
                                   fts_dir=d / "index", lance_dir=d / "lance")
    (d / "state").mkdir()
    con = db.connect(c)
    discovery.sync_collection(con, c)
    con.close()
    con_src, con_build = db.connect(c), pack_build._build_db(tmp_path / "build")
    try:
        pack_build.plan(c, con_src, con_build, log=lambda *_: None)
        got = dict(con_build.execute("SELECT uid, slice FROM plan").fetchall())
    finally:
        con_src.close()
        con_build.close()
    assert got["wikidata:42"] == "family=person/range=0"
    assert got["wikidata:3331"] == "family=heritage/range=0"
    assert got["wikidata:600"] == "family=place/range=0" and got["wikidata:601"] == "family=heritage/range=0"
    assert got["wikidata:1000000031"] == "family=property/range=0"
    assert "wikidata:33010486" not in got, "the scholarly article is pruned: not a record (option 3, later)"
    # the same collection whose Codex slices by id range: the ranges, as before
    (d / "shard" / "codex.json").write_bytes(replace(codex, slicing=slicing.SCHEMES["wikidata"].describe
                                                     | {"unknown": "range=unknown", "shard_records": 20000,
                                                        "order": "numeric"}).canonical())
    con_src, con_build = db.connect(c), pack_build._build_db(tmp_path / "build-ranges")
    try:
        pack_build.plan(c, con_src, con_build, log=lambda *_: None)
        assert dict(con_build.execute("SELECT uid, slice FROM plan").fetchall())["wikidata:42"] == "range=0"
    finally:
        con_src.close()
        con_build.close()


def test_a_build_waits_for_the_pack_s_own_families_pass_and_spacing_never_skips_a_parse(tmp_path):
    spaced = json.dumps(_item(602, "Spaced city", P31=[515])).encode()      # `"P31": [`, not `"P31":[`
    dump = a_dump(tmp_path, lines=entity_lines() + [_line(o) for o in CLASSES] + [spaced])
    meta = pack.build(dump, languages=("en",), chunk=4, workers=1)
    sha = meta["families_map"]
    n = len(entity_lines()) + len(CLASSES)
    assert families.FAMILIES[pack.family_code_at(dump, sha, n) - 1] == "place", "parsed, whatever the spacing"
    pack._write_meta(pack.pack_dir(dump), {**meta, "families_done": False,
                                           "families_codes": {}})       # as while the worker's pass runs
    pack.reset_caches()
    with pytest.raises(pack.PackError, match="being computed by the pack's pass"):
        pack.ensure_codes(dump, pack.creation_map(dump), workers=1)

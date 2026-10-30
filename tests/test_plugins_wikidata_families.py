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
        (300, 811979), (300, 43229),  # one step to built structure and to organization: built, first
        (400, 401), (401, 400), (401, 271669),   # a P279 cycle under landform: settled once
        (500, 13442814), (13442814, 17537576),  # under scholarly article, itself a root under creative work
        # a statutory instrument as the node's dump has it (2026-10-25):
        # → statutory instrument → legislation → "Wikidata item that needs to
        # be split" → Wikidata item → Wikimedia internal item, and far under
        # creative work. Legislation is a WORK root, so the instrument is a
        # work; a category one step under internal is internal (no fallback)
        (600, 700), (700, 49371), (49371, 91615393), (91615393, 16222597),
        (16222597, 17442446), (49371, 800), (800, 801), (801, 802), (802, 17537576),
        (601, 17442446), (601, 610), (610, 611), (611, 17537576),
    ])
    got = families.nearest_roots(child, parent)
    code = {v: k for k, v in families.CODE.items()}
    assert code[got[100]] == "place" and code[got[300]] == "built"
    assert code[got[400]] == code[got[401]] == "place"
    assert code[got[13442814]] == code[got[500]] == "scholarly", "a root is its own family's, at distance 0"
    assert code[got[600]] == code[got[49371]] == "work" and code[got[601]] == "internal"
    without = {f: rs for f, rs in families.ROOTS.items()}
    without["work"] = tuple(r for r in without["work"] if r != 49371)
    got = families.nearest_roots(child, parent, without)
    assert code[got[600]] == "internal", "without legislation's root, the one bad edge decides"
    got = families.nearest_roots(child, parent, fallback=("internal",))
    assert code[got[601]] == "work", "as a FALLBACK internal loses even its categories (the second map)"
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
    with pytest.raises(families.FamilyError, match="ends with `other`"):
        families.load_map(families.canonical({**d, "families": list(reversed(d["families"]))}))


def test_an_entity_s_family_from_its_own_statements():
    raw = families.build_map([515, 41176], [486972, 811979], max_item=1000, dump="d")
    fmap = families.load_map(raw)
    assert families.family_code(_item(1, "Rome", P31=[515]), fmap) == families.CODE["place"]
    assert families.family_code(_item(2, "Castle town", P31=[515, 41176]), fmap) == families.CODE["built"], \
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
    assert fmap.classes[515] == families.CODE["place"] and fmap.classes[2354973] == families.CODE["built"]
    ids = [json.loads(l)["id"] for l in entity_lines() + [_line(o) for o in CLASSES]]
    got = {eid: families.FAMILIES[pack.family_code_at(dump, fmap.sha256, i) - 1] for i, eid in enumerate(ids)}
    assert got["Q42"] == "person" and got["Q3331"] == "built" and got["P31"] == "property"
    assert got["Q33010486"] == "scholarly", "the pruned article has its code too: the class tree is the dump's"
    assert got["Q600"] == "place" and got["Q601"] == "heritage" and got["Q43229"] == "other"
    report = json.loads(pack.report_path(pack.pack_dir(dump), fmap.sha256).read_text())
    by = {f["family"]: f for f in report["families"]}
    assert by["scholarly"]["records"] == 0 and by["scholarly"]["pruned"] == 1
    assert by["place"]["top_classes"] == [[515, "city", 1]]
    assert [777, None, 1] not in by["other"]["top_classes"], "Old oak is heritage, by its designation"
    assert by["other"]["top_classes"], "what fills `other` is listed (the first report showed nothing)"
    probes = {q: fam for q, _label, fam in report["probes"]}
    assert probes[515] == "place" and probes[4167836] == "other" and len(probes) == len(families.PROBES)
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
    assert got["Q3331"] == "built", "road tunnel is newer than the map: walked to built structure"


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


def test_where_the_pack_is_is_asked_once_not_once_a_document(tmp_path, monkeypatch):
    """A COST test. The plan asks every document's family code: `pack_dir`
    (a realpath through the /data links and two stats) once a document was
    the whole cost of wikidata's plan — one core, ~11,700 documents/s,
    1 h 48 min for 75.9 M (München, 2026-10-30)."""
    dump = _dump(tmp_path)
    pack.build(dump, languages=("en",), chunk=4, workers=1)
    sha = pack.read_meta(dump)["families_map"]
    monkeypatch.setattr(pack, "_codes_by_dump", {})
    monkeypatch.setattr(pack, "_codes_views", {})
    asked = []
    real = pack.pack_dir
    monkeypatch.setattr(pack, "pack_dir", lambda d: asked.append(d) or real(d))
    n = len(entity_lines()) + len(CLASSES)
    assert pack.family_code_at(dump, sha, 0)
    first = len(asked)                      # where the pack is, and its meta
    codes = [pack.family_code_at(dump, sha, i % n) for i in range(5000)]
    assert all(codes) and len(asked) == first, "nothing more asked, however many documents"


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
    assert got["wikidata:3331"] == "family=built/range=0"
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


def test_create_after_a_rename_reads_the_held_wikidata_s_corpus(tmp_path, monkeypatch):
    """München's sequence (2026-10-24): `wikidata` renamed `wikidata-p1`,
    then `create --name wikidata` — no collection of that name yet, so the
    corpus and the pinned dump are the one held wikidata collection's."""
    from app import collections as collections_mod
    from app.plugins.wikidata.source import WikidataSource
    dump = _dump(tmp_path / "corpora")
    pack.build(dump, languages=("en",), chunk=4, workers=1)
    p1 = SimpleNamespace(name="wikidata-p1", plugin="wikidata", corpus_dir=tmp_path / "corpora", snapshot="20261012")
    other = SimpleNamespace(name="gutenberg", plugin="gutenberg", corpus_dir=tmp_path / "g", snapshot=None)
    monkeypatch.setattr(collections_mod, "by_name", lambda name: None)
    monkeypatch.setattr(collections_mod, "load_all", lambda **kw: ([p1, other], []))
    block, raw = WikidataSource().creation_slicing("wikidata")
    assert raw == pack.creation_map(dump)
    twin = SimpleNamespace(name="wikidata-x", plugin="wikidata", corpus_dir=tmp_path / "elsewhere", snapshot="20261019")
    monkeypatch.setattr(collections_mod, "load_all", lambda **kw: ([p1, twin], []))
    with pytest.raises(pack.PackError, match="read different dumps"):
        WikidataSource().creation_slicing("wikidata")
    monkeypatch.setattr(collections_mod, "load_all", lambda **kw: ([], []))
    monkeypatch.setattr(collections_mod, "origin_dir", lambda name: tmp_path / "corpora")
    assert WikidataSource().creation_slicing("wikidata")[1] == raw, "none held: the name's own origin folder"


def test_a_family_added_later_leaves_every_pinned_map_readable_as_it_was(monkeypatch):
    """A map carries its own family list and is read by it: after `food` is
    added to the code's list, Monday's map — twelve families — slices
    exactly as before, and a new map has thirteen."""
    old = families.build_map([515], [486972], max_item=1000, dump="d")
    bologna = _item(600, "Bologna", P31=[515])
    before = families.slice_name(families.family_code(bologna, families.load_map(old)), "Q600")
    added = families.FAMILIES[:4] + ("food",) + families.FAMILIES[4:]
    monkeypatch.setattr(families, "FAMILIES", added)
    monkeypatch.setattr(families, "CODE", {f: i + 1 for i, f in enumerate(added)})
    fmap = families.load_map(old)
    assert fmap.families[4] == "person" and len(fmap.families) == len(added) - 1, "read by its own list"
    assert families.slice_name(families.family_code(bologna, fmap), "Q600", fmap.families) == before
    new = families.load_map(families.build_map([515, 7000], [486972, 2095], max_item=9000, dump="d2",
                                               roots={**families.ROOTS, "food": (2095,)}, families=added))
    assert new.families == added and new.classes[7000] == new.code("food")
    pizza = _item(601, "Pizza", P31=[7000])
    assert families.slice_name(families.family_code(pizza, new), "Q601", new.families) == "family=food/range=0"
    assert families.family_code(pizza, families.load_map(old)) == families.load_map(old).other
    with pytest.raises(families.FamilyError, match="not in the list"):
        families.build_map([], [], max_item=1, dump="d", roots={"food": (1,)})


# ---- parts cut by content (02 §2.2, amended 2026-10-24) ----------------------------------------

def _cut(uids, rule):
    """The layout the rule gives a slice's records in order."""
    parts, cur = [], []
    for u in uids:
        cur.append(u)
        if slicing.ends_part(u, len(cur), rule):
            parts.append(cur)
            cur = []
    return parts + ([cur] if cur else [])


def test_the_cut_is_a_pure_function_of_the_records_and_a_change_stays_local():
    rule = slicing.parts_rule_of({"parts": slicing.content_parts(100), "shard_records": 100})
    assert rule == (50, 200, 100)
    uids = [f"wikidata:{n}" for n in range(1, 20_001)]
    layout = _cut(uids, rule)
    sizes = [len(p) for p in layout]
    assert all(50 <= n <= 200 for n in sizes[:-1]) and 80 <= sum(sizes) / len(sizes) <= 125
    assert _cut(uids, rule) == layout
    gone = layout[40][7]                                   # a record inside part 41 leaves the slice
    after = _cut([u for u in uids if u != gone], rule)
    changed = [i for i, (a, b) in enumerate(zip(layout, after)) if a != b]
    assert changed and changed[0] == 40 and len(changed) <= 3, "its own part, and at worst the next few"
    assert after[changed[-1] + 1:] == layout[changed[-1] + 1:], "everything after is untouched"


def test_the_records_check_reads_a_content_cut():
    from app.hold import check
    rule = (2, 6, 4)
    uids = [f"wikidata:{n}" for n in range(1, 400)]
    layout = _cut(uids, rule)
    assert all(check._cut_by_content(rule, p, i == len(layout) - 1) for i, p in enumerate(layout))
    merged = layout[3] + layout[4]
    assert not check._cut_by_content(rule, merged, False), "a part that runs past its boundary"
    assert not check._cut_by_content(rule, layout[3][:-1], False), "a part that stops short of it"
    from app.hold import template
    with pytest.raises(template.TemplateError, match="slicing.parts"):
        template._check_slicing({"keys": [], "parts": {"rule": "content", "min": 9, "max": 4},
                                 "shard_records": 5})


def test_a_family_collection_cut_by_content_builds_the_same_parts_from_scratch_and_resumed(
        isolated_config, tmp_path):
    from app import collections as collections_mod, db, discovery
    from app.hold.codex import License, Parser
    from app.pack import build as pack_build, records as pack_records
    from tests.hold.conftest import pubmed
    corpus = tmp_path / "corpus"
    many = [_item(10_000 + i, f"Town {i}", P31=[515]) for i in range(40)]
    dump = a_dump(corpus, lines=entity_lines() + [_line(o) for o in CLASSES + many])
    pack.build(dump, languages=("en",), chunk=8, workers=1)
    raw = pack.creation_map(dump)
    sha = "sha256:" + hashlib.sha256(raw).hexdigest()
    d = tmp_path / "wikidata"
    (d / "shard" / "slicing").mkdir(parents=True)
    codex = pubmed(plugin="wikidata", parser=Parser("wikidata", 2), license=License("green", None),
                   slicing=slicing.family_block(sha, records_per_shard=6))
    (d / "shard" / "codex.json").write_bytes(codex.canonical())
    (d / "shard" / slicing.map_member(sha)).write_bytes(raw)
    c = collections_mod.Collection(name="wikidata", plugin="wikidata", profile=None, embed_mode_default=None,
                                   corpus_dir=corpus, manifest_path=d / "state" / "manifest.sqlite",
                                   fts_dir=d / "index", lance_dir=d / "lance", codex=codex)
    (d / "state").mkdir()
    con = db.connect(c)
    discovery.sync_collection(con, c)
    con.close()

    def layout(pack_dir):
        m = pack_build.manifest_mod.load(pack_dir)
        return {(s["slice"], s["n"]): ([r["uid"] for r in pack_records.iter_records(pack_dir / s["path"])],
                                      s["sha256"]) for s in m["shards"]}
    whole = tmp_path / "whole"
    pack_build.build(c, whole, snapshot="2026-10-26", sign=False, log=lambda *_: None)
    got = layout(whole)
    places = [got[k][0] for k in sorted(got) if k[0] == "family=place/range=0"]
    rule = slicing.parts_rule_of(codex.slicing)
    assert rule == (3, 12, 6) and places == _cut([u for p in places for u in p], rule), "cut by content"
    assert len(places) > 1
    # a build stopped after every part and resumed: the same parts, byte for byte
    import threading
    stopped = tmp_path / "stopped"
    stop = threading.Event()
    real = pack_build.records.write_shard

    def write_then_stop(*a, **k):
        out = real(*a, **k)
        stop.set()
        return out
    pack_build.records.write_shard = write_then_stop
    try:
        while not pack_build.build(c, stopped, snapshot="2026-10-26", sign=False, log=lambda *_: None,
                                   stop=stop).manifest_path:
            stop.clear()
    finally:
        pack_build.records.write_shard = real
    assert layout(stopped) == got


def _family_collection(tmp_path, name, corpus, codex, raw):
    from app import collections as collections_mod, db, discovery
    d = tmp_path / name
    (d / "shard" / "slicing").mkdir(parents=True)
    (d / "shard" / "codex.json").write_bytes(codex.canonical())
    sha = slicing.family_map_of(codex.slicing)
    (d / "shard" / slicing.map_member(sha)).write_bytes(raw)
    c = collections_mod.Collection(name=name, plugin="wikidata", profile=None, embed_mode_default=None,
                                   corpus_dir=corpus, manifest_path=d / "state" / "manifest.sqlite",
                                   fts_dir=d / "index", lance_dir=d / "lance", codex=codex)
    (d / "state").mkdir()
    con = db.connect(c)
    discovery.sync_collection(con, c)
    con.close()
    return c


def test_a_daughter_with_a_new_family_reuses_her_mother_s_records_and_equals_a_build_from_scratch(
        isolated_config, tmp_path, monkeypatch):
    """The operator's requirement (2026-10-24): a family added → a daughter
    that reuses every record of her mother (none parsed) and whose parts are
    those a collection created from scratch with the new map has, byte for
    byte."""
    import sqlite3
    from app.hold.codex import License, Parser
    from app.pack import build as pack_build, records as pack_records
    from tests.hold.conftest import pubmed
    corpus = tmp_path / "corpus"
    towns = [_item(10_000 + i, f"Town {i}", P31=[515]) for i in range(30)]
    dump = a_dump(corpus, lines=entity_lines() + [_line(o) for o in CLASSES + towns])
    pack.build(dump, languages=("en",), chunk=8, workers=1)
    m1 = pack.creation_map(dump)
    con = sqlite3.connect(pack.pack_dir(dump) / "labels.sqlite")
    child, parent = zip(*con.execute("SELECT id, parent FROM subclass").fetchall())
    con.close()
    # the family added: `city`, before `place` in precedence
    fams = families.FAMILIES[:6] + ("city",) + families.FAMILIES[6:]
    m2 = families.build_map(child, parent, max_item=pack.read_meta(dump)["max_item"], dump=dump.name,
                            roots={**families.ROOTS, "city": (515,)}, families=fams)

    def codex_for(raw):
        return pubmed(plugin="wikidata", parser=Parser("wikidata", 2), license=License("green", None),
                      slicing=slicing.family_block("sha256:" + hashlib.sha256(raw).hexdigest(),
                                                   records_per_shard=6))

    def layout(pack_dir):
        m = pack_build.manifest_mod.load(pack_dir)
        return {(s["slice"], s["n"]): ([r["uid"] for r in pack_records.iter_records(pack_dir / s["path"])],
                                      s["sha256"]) for s in m["shards"]}
    mother = _family_collection(tmp_path, "mother", corpus, codex_for(m1), m1)
    pack_build.build(mother, tmp_path / "m", snapshot="2026-10-26", sign=False, log=lambda *_: None)
    m_parts = pack_build.manifest_mod.load(tmp_path / "m")["shards"]
    scratch = _family_collection(tmp_path, "scratch", corpus, codex_for(m2), m2)
    pack_build.build(scratch, tmp_path / "s", snapshot="2026-10-26", sign=False, log=lambda *_: None)
    daughter = _family_collection(tmp_path, "daughter", corpus, codex_for(m2), m2)

    def never(*a, **k):
        raise AssertionError("the daughter parsed a document")
    monkeypatch.setattr(pack_build, "_record_of", never)
    held = pack_build.HeldRecords(tmp_path / "m", tuple(s["path"] for s in m_parts))
    pack_build.build(daughter, tmp_path / "d", snapshot="2026-10-26", sign=False, log=lambda *_: None,
                     held=held)
    assert layout(tmp_path / "d") == layout(tmp_path / "s"), "the parts of a creation from scratch, byte for byte"
    got = layout(tmp_path / "d")
    assert any(k[0] == "family=city/range=0" for k in got) and not any(k[0] == "family=place/range=0" for k in got)
    assert {u for uids, _ in got.values() for u in uids} == \
        {u for uids, _ in layout(tmp_path / "m").values() for u in uids}, "the same records, re-sliced"


def test_a_resliced_daughter_and_carry_are_refused_where_more_than_the_slicing_differs(tmp_path):
    from app.hold import carry as carry_mod, collection as col
    from app.hold.codex import CodexError, Parser
    from tests.hold.conftest import PLATFORM, a_root, pubmed
    root = tmp_path / "root"
    raw = families.build_map([515], [486972], max_item=1000, dump="d")
    sha = "sha256:" + hashlib.sha256(raw).hexdigest()
    mother = pubmed(slicing={**slicing.family_block(sha, records_per_shard=30_000)})
    created = a_root(root, mother, name="m")
    col.write_slicing_map(col.shard_dir_of(root, "m"), created, raw)
    by_count = {k: v for k, v in mother.slicing.items() if k != "parts"}
    with pytest.raises(CodexError, match="cuts her parts by content"):
        col.continue_(root, created.collection_id, platform_image=PLATFORM, continues_through="2026-10",
                      slicing=by_count, slicing_member=raw)
    raw2 = families.build_map([515], [43229], max_item=1000, dump="d")
    d = col.continue_(root, created.collection_id, platform_image=PLATFORM, continues_through="2026-10",
                      slicing=slicing.family_block("sha256:" + hashlib.sha256(raw2).hexdigest(), 30_000),
                      slicing_member=raw2)
    assert d.space_id == created.space_id and d.slicing != created.slicing
    assert (col.shard_dir_of(root, col.name_of(root, d.collection_id))
            / slicing.map_member(slicing.family_map_of(d.slicing))).read_bytes() == raw2
    with pytest.raises(CodexError, match="--rebuilt, --keep or --recut"):
        carry_mod.carry(root, "m", col.name_of(root, d.collection_id), resliced=True, rebuilt=True)


# ---- why a class has its family, and what candidate roots would move (louise dev wikidata-classes)

def test_a_class_s_shortest_path_to_each_family_s_root_explains_the_map():
    """The second report (2026-10-24) put every Wikimedia category under
    `work`: the path the map followed is what a root is chosen on."""
    up = {601: [17442446, 610], 610: [611], 611: [17537576, 610], 700: [701], 701: [700]}
    paths = families.paths_to_roots(lambda q: up.get(q, ()), 601)
    assert paths == {"internal": [601, 17442446], "work": [601, 610, 611, 17537576]}
    assert list(paths) == ["internal", "work"], "nearest first: the order the walk reached them"
    assert families.paths_to_roots(lambda q: up.get(q, ()), 700) == {}, "a cycle under no root reaches none"
    assert families.paths_to_roots(lambda q: up.get(q, ()), 601, depth=1) == {"internal": [601, 17442446]}
    child, parent = zip(*[(c, p) for c, ps in up.items() for p in ps])
    fmap = families.load_map(families.build_map(child, parent, max_item=10**9, dump="d"))
    assert fmap.name(fmap.code_of_class(601)) == "internal", "the nearer root wins"
    fmap = families.load_map(families.build_map(child, parent, max_item=10**9, dump="d", fallback=("internal",)))
    assert fmap.name(fmap.code_of_class(601)) == "work", "as a FALLBACK, the longer path won (the second map)"


def test_a_candidate_s_family_sizes_are_estimated_from_the_classes_that_decided():
    child, parent = (601, 601, 610, 611, 3305213), (17442446, 610, 611, 17537576, 17537576)
    old = families.load_map(families.build_map(child, parent, max_item=10**9, dump="d", fallback=("internal",)))
    new = families.load_map(families.build_map(child, parent, max_item=10**9, dump="d"))
    work, internal, other = old.code("work"), old.code("internal"), old.other
    deciding = {(work, 601): 10, (work, 3305213): 4, (other, 999999): 2}
    sizes, moved = families.preview(deciding, {work: 15, other: 5}, old, new)
    assert sizes["work"] == 5 and sizes["internal"] == 10 and sizes["other"] == 5
    assert moved == [("work", "internal", 601, 10)]
    assert new.code("internal") == internal


def test_the_dev_tool_explains_the_probes_and_previews_a_candidate(tmp_path, capsys):
    from tools import wikidata_classes
    dump = _dump(tmp_path)
    pack.build(dump, languages=("en",), chunk=4, workers=1)
    cand = tmp_path / "roots.json"
    # the code's roots without `place`: city is reached only through local government
    cand.write_text(json.dumps({"roots": {f: rs for f, rs in families.ROOTS.items() if f != "place"}}))
    out = tmp_path / "report.json"
    assert wikidata_classes.main([str(dump), "--candidate", str(cand), "--classes", "Q100", "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    city = next(r for r in report["explained"] if r["class"] == 515)
    assert city["label"] == "city" and city["family"] == "place" and city["candidate"] == "organization"
    assert city["paths"] == {"organization": [[515, "city"], [100, "local government"], [43229, "organization"]]}, \
        "paths to the CANDIDATE's roots: what it is chosen on"
    assert [r for r in report["explained"] if r["class"] == 100], "--classes adds to the probes"
    assert report["preview"]["moved"] == [["place", "organization", 515, "city", 1]]
    assert report["preview"]["entities"]["organization"] == report["preview"]["entities"]["place"] + 1
    printed = capsys.readouterr().out
    assert "Q515 'city': place → organization" in printed and "local government (Q100)" in printed
    assert pack.read_meta(dump)["families_map"], "read-only: the pack's own record is as it was"


def test_a_line_resolves_the_pack_s_place_and_reads_its_meta_once(tmp_path, monkeypatch):
    """A COST test. Each entity's line asked `derived.place` (a realpath
    through the /data links) and parsed meta.json: ~7 % of a records-build
    child on wikidata (München's py-spy, 2026-10-30)."""
    from app import derived
    dump = _dump(tmp_path)
    pack.build(dump, languages=("en",), chunk=4, workers=1)
    pack.reset_caches()
    derived._places.clear()
    placed, read = [], []
    real_place, real_read = derived.place, Path.read_text
    monkeypatch.setattr(derived, "place", lambda kind, origin: placed.append(1) or real_place(kind, origin))
    monkeypatch.setattr(Path, "read_text",
                        lambda self, *a, **k: (read.append(1) if self.name == "meta.json" else None)
                        or real_read(self, *a, **k))
    n = len(entity_lines()) + len(CLASSES)
    lines = [pack.line(dump, i % n) for i in range(2000)]
    assert all(lines) and len(placed) <= 1 and len(read) == 1


def test_regrouping_the_pack_keeps_every_line_and_reads_either_layout(tmp_path, monkeypatch):
    """A sparse family read a 1,000-line row group for each line it kept
    (~29 % of a records-build child, München 2026-10-30). The pack's chunk
    files are rewritten with small groups, file by file, while a reader
    reads: every line is the same bytes, before, between and after."""
    monkeypatch.setattr(pack, "ROW_GROUP", 1000)
    dump = _dump(tmp_path)
    pack.build(dump, languages=("en",), chunk=4, workers=1)
    pack.reset_caches()
    n = len(entity_lines()) + len(CLASSES)
    before = [pack.line(dump, i) for i in range(n)]
    files = pack.chunk_files(dump)
    assert len(files) > 2
    # half the files regrouped — a reader in the middle of the regrouping
    for f in files[: len(files) // 2]:
        assert pack._regroup_file(str(f), 2) is True
    assert [pack.line(dump, i) for i in range(n)] == before
    got = pack.regroup(dump, row_group=2, log=lambda *_: None)
    # the regrouped half is skipped, and so is a file that already fits
    assert got["skipped"] >= len(files) // 2 and got["rewritten"] >= 1
    assert got["skipped"] + got["rewritten"] == len(files)
    assert [pack.line(dump, i) for i in range(n)] == before, "the cache follows the file it read"
    import pyarrow.parquet as pq
    assert all(pq.ParquetFile(f).metadata.row_group(0).num_rows <= 2 for f in files)
    assert pack.regroup(dump, row_group=2, log=lambda *_: None)["rewritten"] == 0, "resumable"

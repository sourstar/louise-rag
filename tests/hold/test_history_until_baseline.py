"""`origin_versions = "history-until-baseline"` (06 §6.2, amended 2026-11-07,
operator: C) and a deleted citation's part (02 §2.2, amended 2026-11-07,
operator: B) — docs/notes/pubmed-updatefiles.md §5.

NLM keeps a baseline year's files (the baseline and every update file
numbered on from it) until it publishes the next baseline, and removes them
then. A superseded part version is kept while those files can verify it and
dropped at the rollover; the current version is never touched. A citation
NLM deletes leaves its part at the next snapshot, tombstoned `retracted`,
and the version that held it is kept under the same rule.

The first half drives the rule over part files written here; the second
drives the real path — `hold records build`, discovery, `hold snapshot
create` — over a corpus in NLM's layout through one baseline year and the
next year's baseline.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.hold import shard as shard_mod, snapshot as sn
from app.pack import records as pack_records
from app.plugins import base
from app.plugins.pubmed import SOURCE


@pytest.fixture(autouse=True)
def _fresh_origin_versions(monkeypatch):
    monkeypatch.setattr(shard_mod, "_origin_versions", {})


# ---------------------------------------------------------------- the rule, over part files

def _line(pmid: int, origin: str | None) -> dict:
    return {"uid": f"pubmed:{pmid}", "native_id": str(pmid), "lang": "en", "title": f"T{pmid}",
            "creators": [], "date": None, "subjects": [], "keys": {},
            "sections": [["abstract", "text"]],
            # an `origin` inside `extra` must never be read for the record's
            "extra": {"origin": "pubmed99n0001.xml.gz#0"},
            "origin": origin, "content_hash": "00" * 16, "origin_hash": "sha256:" + "11" * 32}


def _part(path: Path, *origins: tuple[int, str | None]) -> Path:
    pack_records.write_shard(path, [_line(p, o) for p, o in origins], frame_records=2)
    return path


def _shard(tmp_path: Path, plugin: str = "pubmed") -> Path:
    d = tmp_path / "shard"
    (d / "records" / "x").mkdir(parents=True)
    (d / "codex.json").write_text(json.dumps({"plugin": plugin}))
    return d


def _held(shard_dir: Path) -> dict[str, bytes]:
    return {p.relative_to(shard_dir).as_posix(): p.read_bytes()
            for p in sorted(shard_dir.rglob("*.zst"))}


Y26 = "pubmed26n0001.xml.gz#{}"
U26 = "updatefiles/pubmed26n1400.xml.gz#{}"
Y27 = "pubmed27n0001.xml.gz#{}"


def test_pubmed_declares_the_rule_and_the_period_of_an_origin():
    assert base.origin_versions(SOURCE) == base.ORIGIN_HISTORY_UNTIL_BASELINE
    assert SOURCE.origin_period("pubmed26n0001.xml.gz#3") == 26
    assert SOURCE.origin_period("updatefiles/pubmed26n1401.xml.gz#17") == 26
    assert SOURCE.origin_period("pubmed27n0001.xml.gz#0") == 27
    assert SOURCE.origin_period("something-else.xml#1") is None
    # a plugin that declares the rule without the period keeps everything
    lone = SimpleNamespace(origin_versions=base.ORIGIN_HISTORY_UNTIL_BASELINE)
    assert base.origin_versions(lone) == base.ORIGIN_HISTORY


def test_through_the_year_every_version_is_kept(tmp_path):
    d = _shard(tmp_path)
    assert shard_mod.origin_keeps_history(d), "kept at promotion, as under history"
    cur = _part(d / "records/x/00001.jsonl.zst", (1, U26.format(0)), (3, Y26.format(1)))
    _part(shard_mod.superseded_name(cur, "2026-08"), (1, Y26.format(0)), (2, Y26.format(1)))
    _part(shard_mod.superseded_name(cur, "2026-09"), (1, U26.format(0)), (3, Y26.format(1)))
    before = _held(d)
    assert shard_mod.drop_past_period(cur, d, SOURCE.origin_period) == []
    assert _held(d) == before


def test_at_the_rollover_the_years_versions_go_and_the_current_stays(tmp_path):
    d = _shard(tmp_path)
    cur = _part(d / "records/x/00001.jsonl.zst", (1, Y27.format(0)), (3, Y27.format(1)))
    old = _part(shard_mod.superseded_name(cur, "2026-08"), (1, Y26.format(0)), (2, Y26.format(1)))
    last = _part(shard_mod.superseded_name(cur, "2026-11"), (1, U26.format(0)), (3, Y26.format(1)))
    # a version one record of which names the new year is still checkable: kept
    mixed = _part(shard_mod.superseded_name(cur, "2026-12"), (1, Y26.format(0)), (3, Y27.format(1)))
    # its companions go with a dropped version, and stay with a kept one
    rel = d / shard_mod.relations_path("x", 1)
    fig = d / shard_mod.figures_path("x", 1)
    for p in (rel, fig):
        p.parent.mkdir(parents=True, exist_ok=True)
    gone_rel = shard_mod.superseded_name(rel, "2026-08")
    gone_fig = shard_mod.superseded_name(fig, "2026-11")
    kept_rel = shard_mod.superseded_name(rel, "2026-12")
    for p in (gone_rel, gone_fig, kept_rel):
        p.write_bytes(b"companion")
    current_bytes = cur.read_bytes()
    dropped = shard_mod.drop_past_period(cur, d, SOURCE.origin_period)
    assert sorted(dropped) == sorted([old, last, gone_rel, gone_fig])
    assert cur.read_bytes() == current_bytes, "the current version is never touched"
    assert mixed.is_file() and kept_rel.is_file()


def test_an_origin_the_plugin_cannot_place_is_never_dropped(tmp_path):
    d = _shard(tmp_path)
    cur = _part(d / "records/x/00001.jsonl.zst", (1, Y27.format(0)))
    unknown = _part(shard_mod.superseded_name(cur, "2026-08"), (1, Y26.format(0)), (2, None))
    foreign = _part(shard_mod.superseded_name(cur, "2026-09"), (1, "elsewhere.xml#0"))
    assert shard_mod.drop_past_period(cur, d, SOURCE.origin_period) == []
    assert unknown.is_file() and foreign.is_file()
    # nor anything at a part whose current period cannot be read
    d2 = _shard(tmp_path / "b")
    cur2 = _part(d2 / "records/x/00001.jsonl.zst", (1, None), (2, Y27.format(1)))
    old2 = _part(shard_mod.superseded_name(cur2, "2026-08"), (1, Y26.format(0)))
    assert shard_mod.drop_past_period(cur2, d2, SOURCE.origin_period) == [] and old2.is_file()


def test_a_part_that_cannot_be_read_is_never_judged_and_never_fails_a_promotion(tmp_path):
    d = _shard(tmp_path)
    cur = d / "records/x/00001.jsonl.zst"
    cur.write_bytes(b"not a part")
    old = _part(shard_mod.superseded_name(cur, "2026-08"), (1, Y26.format(0)))
    assert shard_mod.drop_past_period(cur, d, SOURCE.origin_period) == [] and old.is_file()
    good = _part(d / "records/x/00002.jsonl.zst", (1, Y27.format(0)))
    broken = shard_mod.superseded_name(good, "2026-08")
    broken.write_bytes(b"not a part either")
    assert shard_mod.drop_past_period(good, d, SOURCE.origin_period) == [] and broken.is_file()


def test_the_origin_of_a_parquet_part_is_read_from_its_column(tmp_path):
    d = _shard(tmp_path)
    p = _part(d / "records/x/00001.parquet", (1, Y26.format(0)), (2, None), (3, Y27.format(2)))
    assert list(shard_mod._origins(p)) == [Y26.format(0), None, Y27.format(2)]
    j = _part(d / "records/x/00002.jsonl.zst", (1, Y26.format(0)), (2, None), (3, 'a"b#0'))
    assert list(shard_mod._origins(j)) == [Y26.format(0), None, 'a"b#0'], \
        "the top-level key, never `extra`'s, and an escaped quote read back"


def test_the_drop_is_idempotent_and_a_stop_resumes_where_it_was(tmp_path, monkeypatch):
    d = _shard(tmp_path)
    cur = _part(d / "records/x/00001.jsonl.zst", (1, Y27.format(0)))
    for sid in ("2026-08", "2026-09", "2026-10"):
        _part(shard_mod.superseded_name(cur, sid), (1, Y26.format(0)))
    real = Path.unlink
    calls = []

    def stop_after_one(self, *a, **kw):
        if calls:
            raise KeyboardInterrupt("stopped (test)")
        calls.append(self)
        return real(self, *a, **kw)
    monkeypatch.setattr(Path, "unlink", stop_after_one)
    with pytest.raises(KeyboardInterrupt):
        shard_mod.drop_past_period(cur, d, SOURCE.origin_period)
    monkeypatch.setattr(Path, "unlink", real)
    assert len(shard_mod.superseded_versions(cur)) == 2, "one gone, the rest still held"
    assert len(shard_mod.drop_past_period(cur, d, SOURCE.origin_period)) == 2
    assert shard_mod.superseded_versions(cur) == []
    assert shard_mod.drop_past_period(cur, d, SOURCE.origin_period) == [], "applied twice: nothing"


def test_the_freeze_judges_the_parts_it_reissued_and_only_under_the_rule(tmp_path):
    d = _shard(tmp_path)
    a = _part(d / "records/x/00001.jsonl.zst", (1, Y27.format(0)))
    b = _part(d / "records/x/00002.jsonl.zst", (5, Y27.format(0)))
    a_old = _part(shard_mod.superseded_name(a, "2026-12"), (1, Y26.format(0)))
    b_old = _part(shard_mod.superseded_name(b, "2026-12"), (5, Y26.format(0)))
    entry = lambda p, root: SimpleNamespace(path=p.relative_to(d).as_posix(), root=root)  # noqa: E731
    previous = SimpleNamespace(shards=(entry(a, "r-a-old"), entry(b, "r-b")))
    current = SimpleNamespace(shards=(entry(a, "r-a-new"), entry(b, "r-b")))
    assert sn.drop_past_baseline(d, previous, current) == 1
    assert not a_old.exists() and b_old.exists(), "a part not re-issued was judged at its own freeze"
    assert sn.drop_past_baseline(d, None, current) == 0
    g = _shard(tmp_path / "g", plugin="gutenberg")
    g_cur = _part(g / "records/x/00001.jsonl.zst", (1, Y27.format(0)))
    g_old = _part(shard_mod.superseded_name(g_cur, "2026-12"), (1, Y26.format(0)))
    gp = SimpleNamespace(shards=(SimpleNamespace(path="records/x/00001.jsonl.zst", root="o"),))
    gc = SimpleNamespace(shards=(SimpleNamespace(path="records/x/00001.jsonl.zst", root="n"),))
    assert sn.drop_past_baseline(g, gp, gc) == 0 and g_old.is_file(), "only under the rule"


def test_a_peers_promotion_drops_at_once_what_the_new_version_outdates(tmp_path, monkeypatch):
    """A peer keeps no build delta: promoting the first part of a new year
    drops the versions the origin no longer verifies, its companion's with
    it; promoting a part of the same year keeps them."""
    d = _shard(tmp_path)
    target = _part(d / "records/x/00001.jsonl.zst", (1, Y26.format(0)))
    listed = {"records/x/00001.jsonl.zst": ("2026-11", target.stat().st_size)}
    monkeypatch.setattr(shard_mod, "last_listing",
                        lambda sd, rel, other_than=None: (listed[rel][0], "old-root", listed[rel][1])
                        if rel in listed else None)
    same_year = _part(tmp_path / "staged" / "a.jsonl.zst", (1, U26.format(0)))
    shard_mod.promote(same_year, target, shard_dir=d, new_root="r1")
    kept = shard_mod.superseded_name(target, "2026-11")
    assert kept.is_file(), "the same year: kept"
    listed["records/x/00001.jsonl.zst"] = ("2026-12", target.stat().st_size)
    next_year = _part(tmp_path / "staged" / "b.jsonl.zst", (1, Y27.format(0)))
    shard_mod.promote(next_year, target, shard_dir=d, new_root="r2")
    assert shard_mod.superseded_versions(target) == [], "both of 2026's versions dropped"
    assert first_origin(target) == Y27.format(0)

    # the companion arrives after its record part: its record version is gone, so is it
    rel = d / shard_mod.relations_path("x", 1)
    rel.parent.mkdir(parents=True, exist_ok=True)
    rel.write_bytes(b"edges of 2026-12")
    listed[rel.relative_to(d).as_posix()] = ("2026-12", rel.stat().st_size)
    snap_of = {"2026-12": SimpleNamespace(shards=(SimpleNamespace(
        slice="x", n=1, path="records/x/00001.jsonl.zst", bytes=10 ** 9),))}
    monkeypatch.setattr(sn, "load", lambda p: snap_of[Path(p).stem])
    staged = tmp_path / "staged" / "rel"
    staged.write_bytes(b"edges of 2027-01")
    shard_mod.promote(staged, rel, shard_dir=d, new_root="r3")
    assert not shard_mod.superseded_name(rel, "2026-12").exists()
    assert rel.read_bytes() == b"edges of 2027-01"


def first_origin(path: Path) -> str | None:
    return next(iter(shard_mod._origins(path)))


# ---------------------------------------------------------------- the real path, through a rollover

@pytest.fixture
def pubmed_root(tmp_path, isolated_config):
    from app import louise
    from app.acquire.specs import ACQUIRE_REGISTRY
    from app.hold import collection as col
    from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed, fake_query_embed
    from tests.hold.test_template import ORIGIN, SPACE, VALIDATION
    body = ACQUIRE_REGISTRY["pubmed"].terms_text_path.read_bytes()
    recipe = {
        "descriptive": {"title": "PubMed", "summary": "Abstracts."},
        "space": dict(SPACE), "work": {"tokens": 40_000_000},
        "origin": dict(ORIGIN), "validation": dict(VALIDATION),
        "license": {"class": "red", "terms_sha256": "sha256:" + hashlib.sha256(body).hexdigest()},
    }
    root = tmp_path / "collections"
    col.create_from_recipe(root, "pubmed", recipe, platform_image=PLATFORM, embed=fake_embed,
                           embed_query=fake_query_embed, name="pubmed", key=KEY, now=NOW)
    assert louise.main(["hold", "collection", "ack", "pubmed", "--root", str(root), "--yes"]) == 0
    return root


def _build_and_freeze(root, corpus: Path, snapshot_id: str):
    from app.hold import records as rec
    rec.build(root, "pubmed", corpus_dir=corpus, records_per_shard=2, log=lambda *_: None)
    rec.write_validation(root, "pubmed")
    said: list[str] = []
    return sn.create(root, "pubmed", snapshot_id=snapshot_id, log=said.append), said


def _source(root, corpus: Path):
    from app.hold import collection as col, records as rec
    return rec.local_collection(root, "pubmed", col.read(root, "pubmed"), corpus_dir=corpus)


def _scan(root, corpus: Path):
    from app import db, discovery
    src = _source(root, corpus)
    con = db.connect(src)
    try:
        return discovery.sync_collection(con, src)
    finally:
        con.close()


def _index_everything(root, corpus: Path) -> None:
    """What the node's stages would have done since the scan: every record
    in the text index, the catalogue and the vectors."""
    from app import db
    con = db.connect(_source(root, corpus))
    try:
        con.execute("UPDATE documents SET rdf_done=1, fts_done=1, embed_done=1 WHERE error IS NULL")
        con.commit()
    finally:
        con.close()


def _uids(path: Path) -> list[str]:
    return [r["uid"] for r in pack_records.iter_records(path)]


def _lines(path: Path) -> list[bytes]:
    index = pack_records.read_index(path)
    return [line for k in range(len(index.frames)) for line in pack_records.read_frame(path, index, k)]


def test_a_deleted_citation_leaves_its_part_and_its_year_leaves_at_the_next_baseline(
        tmp_path, pubmed_root, monkeypatch):
    from app import louise, unindexable
    from app.hold import collection as col
    from app.plugins.pubmed.source import UPDATES_SUBDIR
    from tests.test_pubmed_updatefiles import _article, _container, _delete
    root, corpus = pubmed_root, tmp_path / "corpus"
    withdrawn: list[tuple[str, object]] = []

    def withdraw_many(con, coll, uids):
        # the row as any other reader sees it at that moment: not yet tombstoned
        import sqlite3
        look = sqlite3.connect(Path(coll.manifest_path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for u in uids:
                withdrawn.append((u, look.execute("SELECT error FROM documents WHERE uid=?",
                                                  (u,)).fetchone()[0]))
        finally:
            look.close()
        return len(uids)
    monkeypatch.setattr(unindexable, "withdraw_many", withdraw_many)
    shard_dir = col.shard_dir_of(root, "pubmed")

    # 2026-08: the 2026 baseline alone
    _container(corpus / "pubmed26n0001.xml.gz", _article(31000001, "Widgets, first version."),
               _article(31000002, "A duplicate citation."))
    _container(corpus / "pubmed26n0002.xml.gz", _article(31000003, "A third record."))
    aug, _ = _build_and_freeze(root, corpus, "2026-08")
    holder = next(e for e in aug.shards if "pubmed:31000002" in _uids(shard_dir / e.path))
    assert _uids(shard_dir / holder.path) == ["pubmed:31000001", "pubmed:31000002"]
    _index_everything(root, corpus)

    # 2026-09: a revision, a new citation and a DeleteCitation of the same year
    _container(corpus / UPDATES_SUBDIR / "pubmed26n0003.xml.gz",
               _article(31000001, "Widgets, revised.", "Corrected."), _article(31000004, "A new citation."))
    _container(corpus / UPDATES_SUBDIR / "pubmed26n0004.xml.gz", _delete(31000002),
               _article(31000005, "Another new citation."))
    _scan(root, corpus)
    assert withdrawn == [("pubmed:31000002", None)], "out of every index BEFORE its tombstone commits"
    from app import db
    con = db.connect(_source(root, corpus))
    try:
        assert con.execute("SELECT error FROM documents WHERE uid='pubmed:31000002'").fetchone()[0] \
            is not None, "and then tombstoned"
    finally:
        con.close()
    sep, _ = _build_and_freeze(root, corpus, "2026-09")
    # 02 §2.2 (amended 2026-11-07, operator: B): it leaves its part at once
    reissued = next(e for e in sep.shards if e.path == holder.path)
    assert reissued.root != holder.root
    assert _uids(shard_dir / reissued.path) == ["pubmed:31000001"]
    assert all("pubmed:31000002" not in _uids(shard_dir / e.path) for e in sep.shards)
    assert [(t.uid, t.reason) for t in sep.delta.tombstoned] == [("pubmed:31000002", "retracted")]
    # the version that held it is kept, and still checks against the origin
    kept = shard_mod.superseded_name(shard_dir / holder.path, "2026-08")
    assert shard_mod.pieces_root_of(kept) == holder.root, "2026-08 can still be served"
    reparse = louise._reparse_from(col.read(root, "pubmed"), "pubmed", corpus)
    for line in _lines(kept):
        record = json.loads(line)
        assert reparse(record["uid"], record["origin"]).rstrip(b"\n") == line, record["uid"]
    # a re-run: the same snapshot, nothing dropped, nothing moved
    before = _held(shard_dir)
    again, _ = _build_and_freeze(root, corpus, "2026-09")
    assert again.hash == sep.hash and _held(shard_dir) == before

    # NLM's 2027 baseline: every citation of the year again, the deleted one not
    _container(corpus / "pubmed27n0001.xml.gz", _article(31000001, "Widgets, revised.", "Corrected."),
               _article(31000003, "A third record."), _article(31000004, "A new citation."),
               _article(31000005, "Another new citation."))
    _scan(root, corpus)
    from app.hold import records as rec
    rec.build(root, "pubmed", corpus_dir=corpus, records_per_shard=2, log=lambda *_: None)
    rec.write_validation(root, "pubmed")
    superseded = sorted(p.name for p in shard_dir.glob("records/*/*.2026-*.jsonl.zst"))
    assert len(superseded) >= 5 and kept.is_file(), "kept through the year, until the freeze"
    octo = sn.create(root, "pubmed", snapshot_id="2026-10", log=lambda *_: None)
    assert list(shard_dir.glob("records/*/*.2026-*.jsonl.zst")) == [], \
        "every version only 2026's files verified is gone"
    for e in octo.shards:      # the current versions are untouched, each its snapshot's bytes
        assert shard_mod.pieces_root_of(shard_dir / e.path) == e.root
        assert {SOURCE.origin_period(json.loads(x)["origin"]) for x in _lines(shard_dir / e.path)} == {27}
    after = _held(shard_dir)
    assert sn.create(root, "pubmed", snapshot_id="2026-10", log=lambda *_: None).hash == octo.hash
    assert _held(shard_dir) == after, "the rollover applied twice changes nothing"

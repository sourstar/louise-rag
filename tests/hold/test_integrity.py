"""tests/hold/test_integrity.py — 03 §3.8's integrity-violated halt
(app/hold/integrity.py): a record shard in a certified manifest-only copy
that no certifier-signed snapshot lists at its size is never read, stops
embedding and contribution for the whole collection, and leaves search
answering from the certified rest — saying what it left out."""
import sqlite3
from types import SimpleNamespace

from app import query_service, worker
from app.ask import results
from app.hold import integrity
from app.share import contribute
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.hold.conftest import a_root, pubmed
from tests.hold.test_anchor import a_signed_shard, certified

SIGNED = "records/year=2023/00001.jsonl.zst"
FOREIGN = "records/year=2023/00002.jsonl.zst"


def a_violated_copy(node):  # noqa: F811
    codex = certified()
    a_root(node, codex)
    name, d, snap, data = a_signed_shard(node, codex)
    return codex, name, d, snap, data


def test_a_shard_no_signed_snapshot_lists_is_a_violation(node, monkeypatch):  # noqa: F811
    codex, name, d, snap, data = a_violated_copy(node)
    monkeypatch.setattr(integrity, "RECHECK_S", 0.0)          # the judgement; the cache is the next test's
    assert integrity.violations(node, name, codex) == (), "every shard signed"
    (d / FOREIGN).write_bytes(b"f" * 100)
    assert integrity.violations(node, name, codex) == (FOREIGN,), "a shard placed by hand"
    (d / "records/year=2023/00003.2026-07.jsonl.zst").write_bytes(b"old")
    assert integrity.violations(node, name, codex) == (FOREIGN,), "a superseded version is not read, not counted"
    (d / FOREIGN).unlink()
    assert integrity.violations(node, name, codex) == (), "the mark clears by itself once the shard is gone"
    (d / SIGNED).write_bytes(data + b"x")
    assert integrity.violations(node, name, codex) == (SIGNED,), "the listed path at another size"
    (d / SIGNED).write_bytes(data)
    (d / "snapshots/2026-08.sig.json").unlink()
    assert integrity.violations(node, name, codex) == (SIGNED,), "a snapshot no certifier signed lists nothing"
    assert integrity.violations(node, name, pubmed()) == (), "an open collection: the rule does not apply"


def test_the_listing_is_cached_by_its_stat_and_redone_on_a_clock(node, monkeypatch):  # noqa: F811
    import os
    codex, name, d, snap, data = a_violated_copy(node)
    assert integrity.violations(node, name, codex) == ()
    (d / FOREIGN).write_bytes(b"f" * 100)
    os.utime(d / "records/year=2023", (1, 1))                # a definite change of the slice directory's stat
    assert integrity.violations(node, name, codex) == (FOREIGN,), "a file added changes the listing's signature"
    (d / FOREIGN).unlink()
    os.utime(d / "records/year=2023", (2, 2))
    assert integrity.violations(node, name, codex) == ()
    # overwritten IN PLACE: no directory's stat moves, the cached answer
    # stands until the clock runs out
    (d / SIGNED).write_bytes(data + b"x")
    os.utime(d / "records/year=2023", (2, 2))
    assert integrity.violations(node, name, codex) == ()
    monkeypatch.setattr(integrity, "RECHECK_S", 0.0)
    assert integrity.violations(node, name, codex) == (SIGNED,)


def test_the_shard_is_never_read(tmp_path):
    from app import collections as collections_mod
    from app.plugins.base import DiscoverState
    from app.plugins.shard import ShardSource
    from tests.test_plugins_shard import a_shard
    d = a_shard(tmp_path)
    (tmp_path / "state").mkdir()
    src = ShardSource(collections_mod.PLUGIN_REGISTRY["pubmed"], d, frozenset({"records/year=2021/00001.jsonl.zst"}))
    refs = list(src.discover(DiscoverState(corpus_root=d, state_dir=tmp_path / "state")))
    assert [r.native_id for r in refs] == ["1", "2"], "none of the violating shard's records enters the manifest"


def test_embedding_and_contribution_stop_and_show_names_it(node, monkeypatch):  # noqa: F811
    codex, name, d, snap, _ = a_violated_copy(node)
    monkeypatch.setattr(integrity, "RECHECK_S", 0.0)
    events = []
    monkeypatch.setattr(worker.db, "log_event", lambda con, level, stage, msg, doc_uid=None:
                        events.append((level, stage, msg)))
    c = SimpleNamespace(name=name, codex=codex, source="shard", corpus_dir=d, manifest_path=d / "m.sqlite")
    con = sqlite3.connect(":memory:")
    assert worker._integrity_halts(con, c) is False and events == []
    (d / FOREIGN).write_bytes(b"f" * 100)
    assert worker._integrity_halts(con, c) is True
    assert worker._integrity_halts(con, c) is True
    assert [e[:2] for e in events] == [("error", "integrity")], "said once, on the change"
    assert "INTEGRITY VIOLATED" in events[0][2] and FOREIGN in events[0][2]
    reasons = contribute.preconditions(node, name, codex, snap, own_key=None)
    assert any("integrity violated" in r and "03 §3.8" in r for r in reasons), "no bundle is published"
    from app import louise
    assert louise._integrity_violations(node, name, codex) == (FOREIGN,)
    (d / FOREIGN).unlink()
    assert worker._integrity_halts(con, c) is False and events[-1][:2] == ("info", "integrity")


def test_search_answers_from_the_certified_rest_and_says_so(node, monkeypatch):  # noqa: F811
    codex, name, d, snap, _ = a_violated_copy(node)
    monkeypatch.setattr(integrity, "RECHECK_S", 0.0)
    (d / FOREIGN).write_bytes(b"f" * 100)
    c = SimpleNamespace(name=name, plugin="pubmed", codex=codex, source="shard", corpus_dir=d)
    monkeypatch.setattr(query_service, "resolve_collections", lambda names, **kw: [c])
    monkeypatch.setattr(results, "_layer", lambda c, h: "abstract")
    shard_of = {"pubmed:1:0:1": SIGNED, "pubmed:2:0:1": FOREIGN}
    monkeypatch.setattr(results, "_from_shard", lambda c, hit: shard_of[hit["chunk_id"]])

    def run(names, query, k, mode, translate, rerank_depth=50, all_languages=False, filters=None):
        return {"hits": [{"chunk_id": cid, "book_id": int(cid.split(":")[1]), "source_collection": name,
                          "leg_ranks": {"text": 1}, "verification": "imported", "producer": "local",
                          "title": "t", "lang": "en"} for cid in ("pubmed:2:0:1", "pubmed:1:0:1")]}
    out = results.search("q", limit=5, translate=False, root=node, run=run)
    assert [h["chunk_id"] for h in out["lists"][0]["hits"]] == ["pubmed:1:0:1"], "the certified rest is answered"
    (ex,) = out["withheld"]
    assert (ex["collection_id"], ex["shards"]) == (codex.collection_id, [FOREIGN]), "named: the shards and why"
    assert "03 §3.8" in ex["reason"] and "1 result(s)" in ex["reason"]


def test_a_hit_is_traced_to_the_shard_its_record_was_read_from(tmp_path):
    from contextlib import closing
    from app import db
    d = tmp_path / "shard"
    c = SimpleNamespace(name="p", plugin="pubmed", corpus_dir=d, manifest_path=tmp_path / "m.db",
                        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")
    with closing(db.connect(c)) as con:
        con.execute("INSERT INTO documents(uid, source, native_id, path) VALUES "
                    "('pubmed:2', 'pubmed', '2', ?), ('pubmed:3', 'pubmed', '3', '/elsewhere/x.xml')",
                    (f"{d / FOREIGN}#pubmed:2",))
    assert results._from_shard(c, {"book_id": 2}) == FOREIGN, "the locator's shard, SHARD-relative"
    assert results._from_shard(c, {"book_id": 3}) is None, "not a SHARD locator"
    assert results._from_shard(c, {"book_id": 9}) is None, "no such document"

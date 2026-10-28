"""tests/ask/test_maintain.py — the worker's index maintainer counts the
facets and indexes the edges once per snapshot (05 §5.5, §5.7), and
`ask facets` then reads the cache."""
import sqlite3
from types import SimpleNamespace

from app import worker
from app.ask import facets, traverse
from app.hold import collection as col, snapshot as sn
from app.hold.codex import Facet, canonical_json
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.hold.conftest import KEY, NOW, a_root, pubmed


def test_the_maintainer_builds_once_per_snapshot_and_facets_read_the_cache(node, monkeypatch):  # noqa: F811
    codex = a_root(node, pubmed(facets=(Facet("lang", "flat"), Facet("subjects", "tree", "/"))))
    name = col.name_of(node, codex.collection_id)
    d = col.shard_dir_of(node, name)
    snap = sn.Snapshot(collection_id=codex.collection_id, snapshot_id="2026-08", previous=None, shards=(),
                       delta=sn.Delta((), (), ()), files={})
    (d / "snapshots").mkdir(exist_ok=True)
    (d / "snapshots/2026-08.json").write_bytes(snap.canonical())
    (d / "snapshots/2026-08.sig.json").write_bytes(canonical_json(sn.sidecar_add(None, snap.hash, KEY, now=NOW)))
    state = node / name / "state"
    state.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(state / "manifest.sqlite", isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE documents (uid TEXT PRIMARY KEY, native_id TEXT NOT NULL, lang TEXT, authors TEXT, subjects TEXT, "
                "in_scope INTEGER DEFAULT 1, content_hash TEXT)")
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts INTEGER, level TEXT, stage TEXT, message TEXT, doc_uid TEXT)")
    con.executemany("INSERT INTO documents(uid, native_id, lang, authors, subjects) VALUES(?,?,?,?,?)",
                    [("pubmed:1", "1", "en", "A", "Neoplasms/genetics"), ("pubmed:2", "2", "en", "B", "Humans")])
    logged = []
    monkeypatch.setattr(worker.db, "log_event", lambda con, level, stage, msg, *a, **k: logged.append((level, stage, msg)))
    monkeypatch.setattr(worker, "_ask_indexed", {})
    c = SimpleNamespace(name=name, codex=codex, manifest_path=state / "manifest.sqlite")
    assert worker._maintain_ask_indexes(con, c) is True
    assert [m for lvl, st, m in logged if lvl == "warn"] == [], "a pass with no failure"
    assert facets.cached(node / name / "index", "lang", snapshot_id="2026-08")["values"] == {"en": 2}
    assert worker._maintain_ask_indexes(con, c) is False, "the same snapshot: nothing to do"
    assert con.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0, "no relation shards: the table exists, empty"
    listing = facets.listing(name, root=node)
    assert [f["field"] for f in listing["fields"]] == ["lang", "subjects"] and listing["snapshot_id"] == "2026-08"
    assert facets.listing(name, "subjects", "Neoplasms", root=node)["values"] == {"Neoplasms": 1, "Neoplasms/genetics": 1}
    assert traverse.traverse("pubmed:1", root=node)["edges"] == []
    assert worker._maintain_ask_indexes(con, SimpleNamespace(name="legacy", codex=None)) is False

    # A RESTART does not recount: the marker under index/facets names the snapshot indexed
    # whole, so a fresh process finds nothing to do (2026-10-28: every deploy recounted
    # pubmed's facets, 16.5 minutes of the index resource)
    index_dir = node / name / "index"
    assert facets.read_marker(index_dir) == ("2026-08", snap.hash)
    calls = []
    real_build = facets.build
    monkeypatch.setattr(facets, "build", lambda *a, **k: calls.append(1) or real_build(*a, **k))
    monkeypatch.setattr(worker, "_ask_indexed", {})
    assert worker._maintain_ask_indexes(con, c) is False and calls == [], "a restart, the same snapshot: no pass"
    # a NEW snapshot is counted, and the marker moves with it
    snap2 = sn.Snapshot(collection_id=codex.collection_id, snapshot_id="2026-09", previous=snap.hash, shards=(),
                        delta=sn.Delta((), (), ()), files={})
    (d / "snapshots/2026-09.json").write_bytes(snap2.canonical())
    assert worker._maintain_ask_indexes(con, c) is True and calls == [1]
    assert facets.read_marker(index_dir) == ("2026-09", snap2.hash)
    # a pass that FAILS leaves the marker where it was: the next process tries again
    snap3 = sn.Snapshot(collection_id=codex.collection_id, snapshot_id="2026-10", previous=snap2.hash, shards=(),
                        delta=sn.Delta((), (), ()), files={})
    (d / "snapshots/2026-10.json").write_bytes(snap3.canonical())
    monkeypatch.setattr(facets, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    worker._maintain_ask_indexes(con, c)
    assert facets.read_marker(index_dir) == ("2026-09", snap2.hash), "not written after a failure"
    assert worker._maintain_ask_indexes(con, c) is False, "this process remembers; it does not retry every minute"
    monkeypatch.setattr(worker, "_ask_indexed", {})
    monkeypatch.setattr(facets, "build", real_build)
    assert worker._maintain_ask_indexes(con, c) is True, "a restart retries the failed snapshot"
    assert facets.read_marker(index_dir) == ("2026-10", snap3.hash)
    con.close()

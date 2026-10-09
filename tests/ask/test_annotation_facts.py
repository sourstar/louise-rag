"""tests/ask/test_annotation_facts.py — the facts an annotations collection
attaches to a search's results (05 §5.3, amended 2026-10-09; 02 §2.10):
the record at a claim's other end, labelled with method, confidence and the
claim's state against the text the answer was read from; nothing attached to
an answer read before the snapshot the claims hold from; a copy withheld is
named; the lists' order and membership untouched (invariant 4); and an
annotations collection's edges followed by `louise_traverse`."""
from pathlib import Path
from types import SimpleNamespace

from app import collections as collections_mod, db
from app.ask import annotations as facts, results, traverse
from app.hold import collection as col
from tests.hold.test_annotations import TARGET_SNAPSHOT, a_claim, a_target, claims_file, make


def setup(tmp_path):
    target, hashes = a_target(tmp_path)
    codex, snap = make(tmp_path, claims=claims_file(
        a_claim(), a_claim("pubmed:3", "pubmed:1", confidence="0.73", type="translation-of")))
    tcoll = collections_mod.Collection(
        name="pubmed", plugin="pubmed", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "pubmed" / "drag", manifest_path=tmp_path / "pubmed" / "state" / "manifest.sqlite",
        fts_dir=tmp_path / "pubmed" / "index", lance_dir=tmp_path / "pubmed" / "lance", codex=target)
    with db.connect(tcoll) as con:
        for i in (1, 2, 3, 4):
            uid = f"pubmed:{i}"
            con.execute("INSERT INTO documents(uid, source, native_id, path, title, lang, content_hash) "
                        "VALUES(?,?,?,?,?,?,?)", (uid, "pubmed", str(i), f"x/{i}", f"T{i}",
                                                  "la" if i == 1 else "en", hashes[uid]))
    held = SimpleNamespace(name="pubmed-same-work", codex=codex, plugin="annotations", license_ack=None,
                           manifest_path=tmp_path / "pubmed-same-work" / "state" / "manifest.sqlite")
    return target, tcoll, held, codex


def a_list(tcoll, target, read_snapshot=TARGET_SNAPSHOT, uids=("pubmed:1", "pubmed:4")):
    hits = [{"uid": u, "rank": i, "text": "…"} for i, u in enumerate(uids, 1)]
    lists = [{"space_id": target.space_id, "question": "relevance", "hits": hits}]
    source_of = {id(h): tcoll for h in hits}
    contexts = {"pubmed": {"collection_id": target.collection_id, "snapshot_id": read_snapshot}}
    return lists, source_of, contexts


def test_a_result_carries_the_record_at_the_other_end_of_each_claim_as_a_fact(tmp_path):
    target, tcoll, held, codex = setup(tmp_path)
    lists, source_of, contexts = a_list(tcoll, target)
    before = [h["uid"] for h in lists[0]["hits"]]
    assert facts.attach(lists, source_of, contexts, root=tmp_path, collections=[held]) == []
    one, four = lists[0]["hits"]
    assert [h["uid"] for h in lists[0]["hits"]] == before, "placed beside, never ranked (invariant 4)"
    assert four["annotations"] == [], "a record no claim names: the field, empty"
    by_other = {f["record"]["uid"]: f for f in one["annotations"]}
    assert set(by_other) == {"pubmed:2", "pubmed:3"}
    same = by_other["pubmed:2"]
    assert same == {
        "collection_id": codex.collection_id, "snapshot_id": "2026-09", "claim": "annotations:1",
        "type": "same-work", "relation": "annotations:same-work", "role": "subject",
        "record": {"uid": "pubmed:2", "title": "T2", "lang": "en", "held": True},
        "subject_lang": "la", "object_lang": "en",
        "method": {"kind": "matcher", "id": "louise-classical-matcher", "version": 1},
        "confidence": "0.95", "contributor": "a test annotator", "state": "current",
        "trust_level": "unverified"}
    tr = by_other["pubmed:3"]
    assert (tr["type"], tr["role"], tr["confidence"]) == ("translation-of", "object", "0.73"), \
        "pubmed:3 translates pubmed:1: from pubmed:1's side it is the object"


def test_a_record_changed_since_the_claim_was_checked_makes_it_stale_and_an_older_snapshot_gets_none(tmp_path):
    target, tcoll, held, _ = setup(tmp_path)
    with db.connect(tcoll) as con:
        con.execute("UPDATE documents SET content_hash=? WHERE uid='pubmed:2'", ("ff" * 16,))
        con.execute("UPDATE documents SET content_hash=NULL WHERE uid='pubmed:3'")
    lists, source_of, contexts = a_list(tcoll, target, read_snapshot="2026-09")
    facts.attach(lists, source_of, contexts, root=tmp_path, collections=[held])
    states = {f["record"]["uid"]: f["state"] for f in lists[0]["hits"][0]["annotations"]}
    assert states == {"pubmed:2": "stale", "pubmed:3": "unknown"}, \
        "later snapshot: still attached, labelled — the claim was made about other text"
    lists, source_of, contexts = a_list(tcoll, target, read_snapshot="2026-07")
    facts.attach(lists, source_of, contexts, root=tmp_path, collections=[held])
    assert lists[0]["hits"][0]["annotations"] == [], "an answer read before from_snapshot: nothing applies"


def test_a_withheld_copy_attaches_nothing_and_is_named(tmp_path, monkeypatch):
    from app import copy_state
    target, tcoll, held, codex = setup(tmp_path)
    monkeypatch.setattr(copy_state, "withholds", lambda c: "quarantine: roots not yet run on this peer")
    lists, source_of, contexts = a_list(tcoll, target)
    out = facts.attach(lists, source_of, contexts, root=tmp_path, collections=[held])
    assert out == [{"collection": "pubmed-same-work", "collection_id": codex.collection_id, "shards": "all",
                    "reason": "annotations: quarantine: roots not yet run on this peer"}]
    assert lists[0]["hits"][0]["annotations"] == []
    other = SimpleNamespace(name="x", codex=SimpleNamespace(annotates=SimpleNamespace(
        collection_id="sha256:" + "99" * 32)))
    assert facts.attach(lists, source_of, contexts, root=tmp_path, collections=[other]) == [], \
        "about a collection this search did not return: not consulted, not named"


def test_the_search_answer_carries_the_field_and_names_a_failure_to_attach(tmp_path, monkeypatch):
    from app import query_service
    target, tcoll, held, _ = setup(tmp_path)
    monkeypatch.setattr(query_service, "resolve_collections", lambda names, **kw: [tcoll])
    monkeypatch.setattr(results, "context_for", lambda c, **kw: {
        "collection_id": target.collection_id, "lineage_id": target.collection_id,
        "snapshot_id": TARGET_SNAPSHOT, "space_id": target.space_id, "trust_level": "unverified",
        "certification": None, "red": False, "figures": None, "codex": target})
    monkeypatch.setattr(results, "_layer", lambda c, h: "abstract")
    monkeypatch.setattr(collections_mod, "load_annotations", lambda: [held])

    def run(names, query, k, mode, translate, rerank_depth=50, all_languages=False, filters=None):
        return {"hits": [{"chunk_id": "pubmed:1:0:1", "book_id": 1, "source_collection": "pubmed",
                          "text": "one", "leg_ranks": {"text": 1}, "title": "T1", "lang": "la"}]}
    out = results.search("one", root=tmp_path, run=run)
    (hit,) = out["lists"][0]["hits"]
    assert {f["record"]["uid"] for f in hit["annotations"]} == {"pubmed:2", "pubmed:3"}
    assert out["withheld"] == []
    monkeypatch.setattr(facts, "attach", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = results.search("one", root=tmp_path, run=run)
    assert out["withheld"][-1]["reason"] == "annotations not attached: RuntimeError: boom", \
        "said, never a silent drop, and the search still answers"
    assert out["lists"][0]["hits"]


def test_traverse_follows_an_annotations_collection_s_edges_resolved_against_its_target(tmp_path):
    target, tcoll, held, codex = setup(tmp_path)
    out = traverse.traverse("pubmed:1", root=tmp_path)
    assert codex.collection_id in out["collections"]
    mine = {(e["from"], e["to"], e["type"]): e for e in out["edges"] if e["collection_id"] == codex.collection_id}
    assert set(mine) == {("pubmed:1", "pubmed:2", "annotations:same-work"),
                         ("pubmed:3", "pubmed:1", "annotations:translation-of")}
    assert all(e["resolved"] for e in mine.values()), "held in the target it annotates"

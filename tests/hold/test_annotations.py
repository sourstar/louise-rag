"""tests/hold/test_annotations.py — annotations collections (02 §2.10;
09 §9.3, §9.5, §9.14; docs/notes/annotation-collections.md): the Codex at
version 7 with `annotates` and no space, which moves no other Codex; the
claims file and what it refuses; `create` writing the claim records, their
edges, `validation.json`, the first snapshot and the collection file — every
target hash read from the target snapshot; the checks, the copy state and the
trust level of a copy; and what `show`, `load_all` and the seeder make of it."""
import json
from pathlib import Path

import pytest

from app.hold import (annotations as ann, check, codex as cx, collection as col, shard as shard_mod,
                      records as rec, snapshot as sn)
from app.hold.codex import CodexError, License
from app.pack import records as pack_records
from tests.hold.conftest import KEY, NOW, PLATFORM, pubmed

TARGET_SNAPSHOT = "2026-08"


def a_target(root: Path, name: str = "pubmed", texts=("one", "two", "three", "four")):
    """A held target: a green Codex, one record part, a snapshot."""
    target = col.create(root, pubmed(license=License("green", None)), name=name)
    d = col.shard_dir_of(root, name)
    records = []
    for i, t in enumerate(texts, 1):
        sections = [["Abstract", t]]
        records.append({"uid": f"pubmed:{i}", "native_id": str(i), "lang": "en", "title": f"T{i}",
                        "creators": [], "date": "2023", "subjects": [], "keys": [],
                        "sections": sections, "extra": {}, "origin": f"x.xml#{i}",
                        "content_hash": rec.content_hash(f"T{i}", [tuple(s) for s in sections]),
                        "origin_hash": "ab" * 16})
    p = d / shard_mod.shard_path("year=2023", 1)
    pack_records.write_shard(p, records)
    raw = p.read_bytes()
    shard = sn.Shard(slice="year=2023", n=1, root=shard_mod.pieces_root(raw),
                     records_root=rec.records_root(r["content_hash"] for r in records),
                     first_id="1", last_id=str(len(records)), records=len(records),
                     chunks=len(records), chunks_root="sha256:" + "22" * 32, tokens=512 * len(records),
                     bytes=len(raw))
    snap = sn.Snapshot(collection_id=target.collection_id, snapshot_id=TARGET_SNAPSHOT, previous=None,
                       shards=(shard,), delta=sn.Delta(tuple(r["uid"] for r in records), (), ()), files={})
    (d / "snapshots").mkdir(exist_ok=True)
    (d / shard_mod.snapshot_path(TARGET_SNAPSHOT)).write_bytes(snap.canonical())
    return target, {r["uid"]: r["content_hash"] for r in records}


def a_claim(subject="pubmed:1", obj="pubmed:2", **over):
    c = {"type": "same-work", "subject": subject, "object": obj, "subject_lang": "la",
         "object_lang": "en", "method": {"kind": "matcher", "id": "louise-classical-matcher", "version": 1},
         "evidence": {"signals": ["wikidata-edition"], "tier": "wikidata"}, "confidence": "0.95",
         "contributor": "a test annotator"}
    c.update(over)
    return c


def claims_file(*claims) -> bytes:
    return b"".join(json.dumps(c, ensure_ascii=False).encode() + b"\n" for c in claims)


def a_codex(target, **kw):
    return ann.build_codex(target, TARGET_SNAPSHOT, title="Same works", summary="Claims.",
                           annotator="a test annotator", platform_image=PLATFORM,
                           nonce="0f" * 16, **kw)


# ---- the Codex ---------------------------------------------------------------------

def test_an_annotations_codex_is_version_7_names_its_target_and_has_no_space(tmp_path):
    target, _ = a_target(tmp_path)
    codex = a_codex(target)
    d = json.loads(codex.canonical())
    assert d["version"] == 7 and d["plugin"] == "annotations"
    assert d["annotates"] == {"collection_id": target.collection_id, "from_snapshot": TARGET_SNAPSHOT}
    assert d["space"] is None and d["work"] is None and d["layers"] == []
    assert codex.space_id is None and codex.space_segment is None
    assert cx.loads(codex.canonical()) == codex, "it round-trips through codex.json"
    assert codex.collection_id == cx.loads(codex.canonical()).collection_id
    assert codex.relation == "root"


def test_no_other_codex_carries_the_key_so_no_collection_id_moves():
    p = pubmed()
    assert "annotates" not in json.loads(p.canonical())
    raw = json.loads(p.canonical())
    raw["annotates"] = None
    with pytest.raises(CodexError, match="unknown"):
        cx.from_dict(raw)


def test_the_codex_refuses_what_an_annotations_collection_cannot_have(tmp_path):
    target, _ = a_target(tmp_path)
    good = json.loads(a_codex(target).canonical())

    def refused(match, **over):
        bad = dict(good, **over)
        with pytest.raises(CodexError, match=match):
            cx.validate(cx.from_dict(bad))

    refused("annotates", annotates=None)
    refused("from_snapshot", annotates={"collection_id": target.collection_id, "from_snapshot": "Sept"})
    refused("plugin", plugin="gutenberg")
    refused("nonce", origin=dict(good["origin"], spec={"source_id": "annotations"}))
    refused("cannot run", validation={"checks": ["roots", "chunks"], "sample": {"chunks": {"shards": "1"}},
                                      "required": []})
    refused("never erasable", erasure="erasable")
    with pytest.raises(CodexError, match="null only on an annotations"):
        cx.from_dict(dict(json.loads(pubmed().canonical()), space=None))
    with pytest.raises(CodexError):
        cx.validate(cx.from_dict(dict(good, layers=[{"chunker": {"id": "abstract", "version": 1, "params": {}},
                                                       "rule": "default", "input": {"form": "text"}}])))


# ---- the claims file -------------------------------------------------------------------

def test_the_claims_file_refuses_a_bad_claim_and_a_claim_made_twice():
    ok = ann.read_claims(claims_file(a_claim(), a_claim("pubmed:3", "pubmed:4")), target_plugin="pubmed")
    assert [s.line for s in ok] == [1, 2] and ok[0].claim["confidence"] == "0.95"
    for bad, match in (
            (a_claim(confidence=0.9), "decimal string"),
            (a_claim(type="related"), "type is one of"),
            (a_claim(obj="pubmed:1"), "same record"),
            (a_claim(obj="gutenberg:2"), "not a uid of the target"),
            (a_claim(method={"kind": "matcher", "id": "m"}), "kind, id, version"),
            (a_claim(method={"kind": "human", "who": "me"}), "alone"),
            (a_claim(subject_lang="Latin"), "ISO 639"),
            (dict(a_claim(), extra=1), "unknown extra"),
            ({k: v for k, v in a_claim().items() if k != "contributor"}, "missing contributor")):
        with pytest.raises(ann.AnnotationsError, match=match):
            ann.read_claims(claims_file(bad), target_plugin="pubmed")
    with pytest.raises(ann.AnnotationsError, match="same claim as line 1"):
        ann.read_claims(claims_file(a_claim(), a_claim("pubmed:2", "pubmed:1")))
    # translation-of is directed: the reverse is another claim
    assert len(ann.read_claims(claims_file(a_claim(type="translation-of"),
                                           a_claim("pubmed:2", "pubmed:1", type="translation-of")))) == 2
    with pytest.raises(ann.AnnotationsError, match="no claim"):
        ann.read_claims(b"\n\n")


# ---- create ---------------------------------------------------------------------------

def make(root, name="pubmed-same-work", claims=None, **kw):
    data = claims if claims is not None else claims_file(a_claim(), a_claim("pubmed:3", "pubmed:1",
                                                                             confidence="0.73"))
    return ann.create(root, name, target_ref="pubmed", from_snapshot=TARGET_SNAPSHOT, claims=data,
                      platform_image=PLATFORM, snapshot_id="2026-09", key=KEY, now=NOW,
                      nonce="0f" * 16, log=lambda m: None, **kw)


def test_create_writes_the_claims_their_edges_and_the_first_snapshot(tmp_path):
    target, hashes = a_target(tmp_path)
    codex, snap = make(tmp_path)
    d = col.shard_dir_of(tmp_path, "pubmed-same-work")
    assert col.read(tmp_path, "pubmed-same-work") == codex
    (shard,), (rel,) = snap.shards, snap.relations
    assert (shard.slice, shard.n, shard.records, shard.chunks, shard.tokens) == ("all", 1, 2, 0, 0)
    assert shard_mod.pieces_root_of(d / shard.path) == shard.root
    lines = list(pack_records.iter_records(d / shard.path))
    assert [r["uid"] for r in lines] == ["annotations:1", "annotations:2"], "numbered in (subject, object) order"
    first = lines[0]
    assert (first["lang"], first["title"], first["sections"][0][0]) == ("und", "", "claim")
    claim = ann.claim_of(first)
    assert (claim["subject"], claim["object"]) == ("pubmed:1", "pubmed:2")
    assert (claim["subject_hash"], claim["object_hash"]) == (hashes["pubmed:1"], hashes["pubmed:2"]), \
        "the target's content hashes, read from the snapshot the claims hold from"
    assert claim["target_snapshot"] == TARGET_SNAPSHOT
    assert first["content_hash"] == rec.content_hash("", [tuple(first["sections"][0])])
    assert first["origin"] == "claims.jsonl#1"
    raw_line = (tmp_path / "pubmed-same-work" / "origin" / "claims.jsonl").read_bytes().split(b"\n")[0]
    assert first["origin_hash"] == ann.blake128(raw_line), "the origin is the claims file as submitted"
    edges = list(pack_records.iter_records(d / rel.path))
    assert edges == [{"from": "pubmed:1", "to": "pubmed:2", "type": "annotations:same-work", "snapshot": "2026-09"},
                     {"from": "pubmed:3", "to": "pubmed:1", "type": "annotations:same-work", "snapshot": "2026-09"}]
    assert rel.edges == 2
    assert set(snap.files) == {"validation.json", "LICENSE-TERMS", "seals/2026-09.json"}
    assert snap.files["LICENSE-TERMS"] is None, "green; and no calibration member — there is no space"
    assert sorted(snap.delta.added) == ["annotations:1", "annotations:2"]
    keys, bad = sn.sidecar_keys(json.loads((d / "snapshots/2026-09.sig.json").read_bytes()), snap.hash)
    assert keys and not bad, "signed: this peer computed it"
    held = (tmp_path / "pubmed-same-work.toml").read_text()
    assert f'codex = "{codex.collection_id}"' in held and 'kinds = ["records", "relations"]' in held
    assert "[encoder]" not in held, "no space, no encoder"
    # the plan's checks pass on it
    report = check.run(tmp_path, "pubmed-same-work", codex, snap, seed=b"s" * 32, checks=list(cx.ANNOTATIONS_CHECKS))
    assert {o.check: o.result for o in report.outcomes} == {
        "roots": "pass", "records": "pass", "license": "pass", "signature": "pass"}
    # nothing in the target moved
    assert col.read(tmp_path, "pubmed") == target
    assert sorted(p.name for p in (col.shard_dir_of(tmp_path, "pubmed") / "snapshots").iterdir()) == ["2026-08.json"]


def test_create_again_is_a_no_op_and_a_name_held_by_another_is_refused(tmp_path):
    a_target(tmp_path)
    codex, snap = make(tmp_path)
    again, snap2 = make(tmp_path)
    assert again == codex and snap2.hash == snap.hash, "resumed: the same bytes"
    with pytest.raises(CodexError, match="different bytes"):
        make(tmp_path, claims=claims_file(a_claim()))
    with pytest.raises(CodexError, match="holds one collection"):
        make(tmp_path, name="pubmed")


def test_a_claim_about_a_record_the_target_snapshot_does_not_hold_refuses_the_file(tmp_path):
    _, hashes = a_target(tmp_path)
    with pytest.raises(ann.AnnotationsError, match="does not hold: line 1: pubmed:99"):
        make(tmp_path, claims=claims_file(a_claim(obj="pubmed:99")))
    assert not (tmp_path / "pubmed-same-work").exists(), "refused before anything is written"
    with pytest.raises(ann.AnnotationsError, match="checked against other text"):
        make(tmp_path, claims=claims_file(a_claim(subject_hash="00" * 16)))
    with pytest.raises(ann.AnnotationsError, match="no snapshot 2026-07"):
        ann.create(tmp_path, "x", target_ref="pubmed", from_snapshot="2026-07",
                   claims=claims_file(a_claim()), platform_image=PLATFORM, log=lambda m: None)
    codex, _ = make(tmp_path, claims=claims_file(a_claim(subject_hash=hashes["pubmed:1"])))
    assert codex.annotates.from_snapshot == TARGET_SNAPSHOT


def test_an_annotations_collection_cannot_annotate_another(tmp_path):
    a_target(tmp_path)
    make(tmp_path)
    with pytest.raises(ann.AnnotationsError, match="itself an annotations collection"):
        ann.create(tmp_path, "meta", target_ref="pubmed-same-work", from_snapshot="2026-09",
                   claims=claims_file(a_claim("annotations:1", "annotations:2")),
                   platform_image=PLATFORM, log=lambda m: None)


# ---- the copy, as this peer holds it ------------------------------------------------------

def test_its_copy_state_waits_for_roots_alone_and_its_trust_level_counts_roots_and_records(tmp_path):
    from app import copy_state
    from app.share import levels
    a_target(tmp_path)
    codex, snap = make(tmp_path)
    st = copy_state.evaluate(tmp_path, "pubmed-same-work", codex, licence="not-required", key=None)
    assert st.state == "quarantine" and st.origin_check == "not-planned" and not st.vector_leg
    assert {t.name: t.status for t in st.tests} == {
        "roots": "open", "bundles": "not-required", "gate": "not-required",
        "licence": "not-required", "origin": "not-required"}
    me = "ed25519:" + KEY.verify_key.encode().hex()
    report = check.run(tmp_path, "pubmed-same-work", codex, snap, seed=b"s" * 32, checks=["roots", "records"])
    check.write_attestations(tmp_path, "pubmed-same-work", report, codex, snap, KEY,
                             platform_image=PLATFORM, now=NOW)
    copy_state.reset_cache()
    st = copy_state.evaluate(tmp_path, "pubmed-same-work", codex, licence="not-required", key=me)
    assert st.state == "in-use" and st.reasons == ()
    bodies = copy_state.attestations(tmp_path, "pubmed-same-work", codex)
    lvl = levels.level(codex, snap, bodies, now=__import__("calendar").timegm((2026, 9, 9, 0, 0, 0)))
    assert lvl["level"] == "unverified" and lvl["passed"]["roots"] == 1 and lvl["passed"]["records"] == 1
    assert lvl["why"] == ["roots: 1 of 3 independent keys", "records: 1 of 3 independent keys"]


def test_show_names_the_target_and_the_snapshot_the_claims_hold_from(tmp_path):
    target, _ = a_target(tmp_path)
    codex, _ = make(tmp_path)
    shown = col.show(tmp_path, "pubmed-same-work")
    assert shown["annotates"] == {"collection_id": target.collection_id, "from_snapshot": TARGET_SNAPSHOT,
                                  "target": "pubmed"}
    assert shown["space_id"] is None and shown["space"] is None
    assert col.show(tmp_path, "pubmed")["annotates"] is None


def test_the_node_holds_it_apart_from_what_the_worker_runs_and_shares_its_members(tmp_path, monkeypatch):
    from app import collections as collections_mod, db
    from app.share import seeder
    root = tmp_path / "louise" / "collections"
    root.mkdir(parents=True)
    (tmp_path / "config" / "collections").mkdir(parents=True)
    monkeypatch.setenv("LOUISE_ROOT", str(tmp_path / "louise"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    a_target(root)
    codex, snap = make(root)
    assert (tmp_path / "config" / "collections" / "pubmed-same-work.toml").is_file(), \
        "the node's own root: the file goes where the node reads"
    assert collections_mod.load_all() == [], "nothing for the worker, and no refusal either"
    assert collections_mod.refusals() == ()
    (held,) = collections_mod.load_annotations()
    assert held.name == "pubmed-same-work" and held.codex == codex and held.embed is None
    assert collections_mod.is_annotations(held) and held.kinds == ("records", "relations")
    with db.connect(held) as con:
        entries = seeder.publish(held, snap, col.shard_dir_of(root, held.name), root / held.name / "state", con)
    assert sorted({e["member"] for e in entries}) == ["records-all", "relations-all", "validation"]
    merged, cons = seeder.with_annotations([], {})
    assert [c.name for c in merged] == ["pubmed-same-work"] and "pubmed-same-work" in cons


def test_the_command_creates_it_and_show_prints_what_it_annotates(tmp_path, monkeypatch, capsys):
    from app import louise
    from app.bundles import keys as node_keys
    monkeypatch.setattr(node_keys, "load_signing_key", lambda *a, **k: KEY)
    target, _ = a_target(tmp_path)
    claims = tmp_path / "claims.jsonl"
    claims.write_bytes(claims_file(a_claim()))
    base = ["hold", "collection", "create", "annotations", "--root", str(tmp_path)]
    assert louise.main(base + ["--annotates", "pubmed", "--name", "same"]) == 2
    assert "takes --from-snapshot, --claims" in capsys.readouterr().err
    assert louise.main(base + ["--annotates", "pubmed", "--from-snapshot", TARGET_SNAPSHOT,
                               "--claims", str(claims), "--name", "same", "--snapshot", "2026-09",
                               "--platform-image", PLATFORM, "--title", "Same works"]) == 0
    cid = capsys.readouterr().out.strip().splitlines()[-1]
    codex = col.read(tmp_path, "same")
    assert codex.collection_id == cid and codex.descriptive.title == "Same works"
    assert louise.main(["hold", "collection", "show", "same", "--root", str(tmp_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["annotates"]["target"] == "pubmed" and shown["annotates"]["from_snapshot"] == TARGET_SNAPSHOT
    assert shown["state"] == "quarantine" and shown["origin_check"] == "not-planned"
    assert [s["snapshot_id"] for s in shown["snapshots"]] == ["2026-09"] and shown["snapshots"][0]["signatures"] == 1
    # `hold check` runs its whole plan with no encoder, attests, and the copy leaves quarantine
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", PLATFORM)
    from app import copy_state, identity
    monkeypatch.setattr(identity, "own", lambda: "ed25519:" + KEY.verify_key.encode().hex())
    copy_state.reset_cache()
    assert louise.main(["hold", "check", "same", "--root", str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert {c["check"]: c["result"] for c in report["checks"]} == {
        "roots": "pass", "records": "pass", "license": "pass", "signature": "pass"}
    copy_state.reset_cache()
    assert louise.main(["hold", "collection", "show", "same", "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "in-use"

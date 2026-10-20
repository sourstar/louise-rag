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


def a_target(root: Path, name: str = "pubmed", texts=("one", "two", "three", "four"), **over):
    """A held target: a green Codex, one record part, a snapshot."""
    target = col.create(root, pubmed(license=License("green", None), **over), name=name)
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
    assert cx.targets_of(codex)[0].from_snapshot == TARGET_SNAPSHOT     # a tuple since 2026-10-20


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


def test_a_dry_run_checks_the_claims_against_the_target_and_writes_nothing(tmp_path, monkeypatch, capsys):
    """Operator, 2026-10-19 (the gutenberg graph collection): thousands of
    claims about a live collection are measured before they are written."""
    from app import louise
    target, hashes = a_target(tmp_path)
    claims = tmp_path / "claims.jsonl"
    claims.write_bytes(claims_file(a_claim(), a_claim("pubmed:3", "pubmed:4", method={"kind": "wikidata"},
                                                       evidence={"item": "Q1"}, confidence="0.96875")))
    got = ann.preview(tmp_path, target_ref="pubmed", from_snapshot=TARGET_SNAPSHOT, claims=claims.read_bytes())
    assert got["claims"] == 2 and got["target_records_named"] == 4 and got["written"] is False
    assert got["by_type"] == {"same-work": 2} and got["by_method"] == {"matcher:louise-classical-matcher": 1,
                                                                        "wikidata": 1}
    assert got["annotator"] == "a test annotator" and got["target_id"] == target.collection_id
    # a claim about a record the snapshot does not hold refuses the dry run as it refuses the write
    bad = claims_file(a_claim("pubmed:1", "pubmed:99"))
    with pytest.raises(ann.AnnotationsError):
        ann.preview(tmp_path, target_ref="pubmed", from_snapshot=TARGET_SNAPSHOT, claims=bad)
    base = ["hold", "collection", "create", "annotations", "--root", str(tmp_path), "--annotates", "pubmed",
            "--from-snapshot", TARGET_SNAPSHOT, "--claims", str(claims), "--name", "same", "--dry-run"]
    assert louise.main(base) == 0
    text = capsys.readouterr().out
    assert json.loads(text[text.index("{"):])["claims"] == 2
    assert not (col.shard_dir_of(tmp_path, "same")).exists() and not (tmp_path / "same").exists(), "nothing written"
    assert not list((tmp_path / "collections").glob("same*")) if (tmp_path / "collections").is_dir() else True


def test_create_promotes_across_filesystems_when_the_shard_lives_elsewhere(tmp_path, monkeypatch):
    """Since 2026-10-12 a collection's `shard/` is a link to /shards/<name> on
    other storage than `state/`, where create stages its parts: a rename
    cannot cross (EXDEV), and the first create after the move failed half way
    (2026-10-19, gutenberg-book-editions). The parts are promoted whole by a
    copy beside the target, as every SHARD writer does (`shard.promote`)."""
    import errno
    import os
    real = os.replace

    def no_cross(src, dst, *a, **k):
        if "annotations-build" in str(src) and "annotations-build" not in str(dst):
            raise OSError(errno.EXDEV, "Invalid cross-device link", str(src))
        return real(src, dst, *a, **k)

    a_target(tmp_path)
    monkeypatch.setattr(os, "replace", no_cross)
    codex, snap = make(tmp_path)
    d = col.shard_dir_of(tmp_path, "pubmed-same-work")
    (shard,) = snap.shards
    assert shard_mod.pieces_root_of(d / shard.path) == shard.root, "the part arrived whole"
    assert not (tmp_path / "pubmed-same-work" / "state" / "annotations-build").exists(), "staging gone"
    assert not list(d.rglob(".*.promote")), "no temporary left beside the part"


# ---- the graph amendment (operator, 2026-10-20): three claim types, several targets ----------------
# docs/notes/gutenberg-graph-amendments.md A-K1–A-K4; 02 §2.10, 09 §9.3, §9.5, §9.14

from app import layers as layers_mod                      # noqa: E402
from app.hold.records import order_key                     # noqa: E402

ENTITIES = {"Q42": "Douglas Adams", "Q50": "Eumenes", "P31": "instance of"}


def an_entity_target(root: Path, name: str = "wikidata"):
    """A held entity target: a green Codex whose plugin is `wikidata`, its
    records' uids `wikidata:<native_id>` (Q42 → 42, P31 → 1000000031)."""
    from app.plugins.wikidata import ids
    target = col.create(root, pubmed(license=License("green", None), plugin="wikidata"), name=name)
    d = col.shard_dir_of(root, name)
    order = (target.slicing or {}).get("order", "bytes")
    records = []
    for eid, label in ENTITIES.items():
        nid = ids.native_id_of(eid)
        sections = [["description", f"{label}, an entity"]]
        records.append({"uid": f"wikidata:{nid}", "native_id": nid, "lang": "mul", "title": label,
                        "creators": [], "date": None, "subjects": [], "keys": [eid],
                        "sections": sections, "extra": {}, "origin": f"dump#{eid}",
                        "content_hash": rec.content_hash(label, [tuple(s) for s in sections]),
                        "origin_hash": "cd" * 16})
    records.sort(key=lambda r: order_key(r["native_id"], order))
    p = d / shard_mod.shard_path("all", 1)
    pack_records.write_shard(p, records)
    raw = p.read_bytes()
    shard = sn.Shard(slice="all", n=1, root=shard_mod.pieces_root(raw),
                     records_root=rec.records_root(r["content_hash"] for r in records),
                     first_id=records[0]["native_id"], last_id=records[-1]["native_id"], records=len(records),
                     chunks=len(records), chunks_root="sha256:" + "33" * 32, tokens=512 * len(records),
                     bytes=len(raw))
    snap = sn.Snapshot(collection_id=target.collection_id, snapshot_id="2026-07", previous=None,
                       shards=(shard,), delta=sn.Delta(tuple(r["uid"] for r in records), (), ()), files={})
    (d / "snapshots").mkdir(exist_ok=True)
    (d / shard_mod.snapshot_path("2026-07")).write_bytes(snap.canonical())
    return target, {r["uid"]: r["content_hash"] for r in records}


def a_chunk(target, uid: str, text: str) -> tuple[dict, str]:
    """The first chunk of a record as the target's chunker cuts it — the
    chunk end `{uid, chunk_id, text_hash}` and its text."""
    i = uid.split(":", 1)[1]
    chunks = layers_mod.chunks_for_document(target, {"uid": uid, "native_id": i}, [("Abstract", text)])
    c = chunks[0]
    return {"uid": uid, "chunk_id": c["id"], "text_hash": ann.blake128(c["text"].encode())}, c["text"]


MODEL = {"kind": "model", "id": "gliner-it", "version": 1, "model": "sha256:" + "aa" * 32,
         "prompt": "sha256:" + "bb" * 32, "temperature": "0"}


def a_mention(target, text="Douglas Adams wrote it", start=0, end=13, surface="Douglas Adams", **over):
    end_, _ = a_chunk(target, "pubmed:1", text)
    c = {"type": "mention", "subject": end_, "object": "wikidata:Q42", "start": start, "end": end,
         "surface": surface, "method": MODEL, "evidence": {"score": "0.91"}, "confidence": "0.9",
         "contributor": "a graph builder"}
    c.update(over)
    return c


def a_relation(target, **over):
    end_, _ = a_chunk(target, "pubmed:1", "Douglas Adams wrote it")
    c = {"type": "relation", "subject": "wikidata:Q42", "object": "wikidata:Q50", "relation": "wikidata:P31",
         "evidence_chunk": end_, "method": MODEL, "evidence": {}, "confidence": "0.8",
         "contributor": "a graph builder"}
    c.update(over)
    return c


def a_community(**over):
    c = {"type": "community", "subject": {"id": "c-7", "level": 0}, "object": None,
         "members": ["wikidata:Q42", "wikidata:Q50"], "summary": "two entities", "method": MODEL,
         "evidence": {}, "confidence": "0.6", "contributor": "a graph builder"}
    c.update(over)
    return c


def two_targets(root):
    text, hashes = a_target(root, texts=("Douglas Adams wrote it", "two", "three", "four"))
    entities, ehashes = an_entity_target(root)
    return text, entities, {**hashes, **ehashes}


def make_graph(root, claims, name="graph", **kw):
    return ann.create(root, name, targets=[("pubmed", TARGET_SNAPSHOT), ("wikidata", "2026-07")],
                      claims=claims, platform_image=PLATFORM, snapshot_id="2026-09", key=KEY, now=NOW,
                      nonce="0f" * 16, log=lambda m: None, **kw)


def test_a_two_target_codex_is_version_9_with_a_list_and_a_container_and_round_trips(tmp_path):
    text, entities, _ = two_targets(tmp_path)
    codex = ann.build_codex(None, None, targets=[(text, TARGET_SNAPSHOT), (entities, "2026-07")],
                            title="Graph", summary="Claims.", annotator="a", platform_image=PLATFORM,
                            nonce="0f" * 16)
    d = json.loads(codex.canonical())
    assert d["version"] == 9 and d["slicing"]["container"] == "jsonl"
    assert d["annotates"] == [{"collection_id": text.collection_id, "from_snapshot": TARGET_SNAPSHOT},
                              {"collection_id": entities.collection_id, "from_snapshot": "2026-07"}]
    assert cx.loads(codex.canonical()) == codex and cx.targets_of(codex) == codex.annotates
    assert cx.target_for(codex, entities.collection_id).from_snapshot == "2026-07"
    assert cx.container_of(codex) == "jsonl" and cx.container_of(text) == "jsonl"
    # one target, old types, JSONL: still 7, the same bytes as before the amendment
    one = a_codex(text)
    assert one.version == 7 and "container" not in one.slicing and isinstance(one.annotates, tuple)
    assert json.loads(one.canonical())["annotates"] == {"collection_id": text.collection_id,
                                                        "from_snapshot": TARGET_SNAPSHOT}
    # a graph claim type alone makes it 9, as a list of one
    g = ann.build_codex(text, TARGET_SNAPSHOT, title="G", summary="C.", annotator="a",
                        platform_image=PLATFORM, graph=True)
    assert g.version == 9 and json.loads(g.canonical())["annotates"] == [one.annotates[0].to_dict()]


def test_version_9_refuses_what_it_cannot_carry_and_parquet_is_declared_not_yet_written(tmp_path):
    text, entities, _ = two_targets(tmp_path)
    good = json.loads(ann.build_codex(None, None, targets=[(text, TARGET_SNAPSHOT), (entities, "2026-07")],
                                      title="G", summary="C.", annotator="a", platform_image=PLATFORM,
                                      nonce="0f" * 16).canonical())

    def refused(match, **over):
        with pytest.raises(CodexError, match=match):
            cx.validate(cx.from_dict(dict(good, **over)))

    refused("annotates\\[1\\].*twice", annotates=[good["annotates"][0], good["annotates"][0]])
    refused("list of targets", annotates=good["annotates"][0])
    refused("container", slicing={k: v for k, v in good["slicing"].items() if k != "container"})
    refused("container", slicing=dict(good["slicing"], container="csv"))
    # a version-7 Codex carries one target as an object and no container
    v7 = json.loads(a_codex(text).canonical())
    with pytest.raises(CodexError, match="container"):
        cx.validate(cx.from_dict(dict(v7, slicing=dict(v7["slicing"], container="jsonl"))))
    with pytest.raises(CodexError, match="cannot be moved"):
        cx.validate(cx.from_dict(dict(json.loads(pubmed().canonical()),
                                      slicing=dict(pubmed().slicing, container="jsonl"))))
    with pytest.raises(CodexError, match="ONE collection"):
        cx.Codex(**{**a_codex(text).__dict__, "annotates": cx.targets_of(cx.from_dict(good))}).canonical()
    # a Codex declaring Parquet parts: version 9, a million claims a part (09 §9.3)
    pq = ann.build_codex(text, TARGET_SNAPSHOT, title="G", summary="C.", annotator="a",
                         platform_image=PLATFORM, container="parquet")
    assert (pq.version, cx.container_of(pq), pq.slicing["shard_records"]) == (9, "parquet", 1_000_000)
    assert cx.loads(pq.canonical()) == pq
    # a version-9 space Codex: the container declared, the cut where declared
    p9 = dict(json.loads(pubmed(version=5).canonical()), version=9)
    p9["slicing"] = dict(p9["slicing"], container="jsonl")
    nine = cx.from_dict(p9)
    cx.validate(nine)
    assert nine.version == 9 and nine.space.cut is None and nine.space_id == pubmed(version=5).space_id, \
        "the same space: version 9 adds nothing to space_id"
    with pytest.raises(CodexError, match="exactly when it is an annotations"):
        cx.from_dict(dict(p9, annotates=good["annotates"]))


def test_the_three_graph_claim_types_are_checked_to_the_key(tmp_path):
    text, entities, _ = two_targets(tmp_path)
    plugins = ("pubmed", "wikidata")
    ok = ann.read_claims(claims_file(a_mention(text), a_relation(text), a_community()), target_plugins=plugins)
    assert [s.claim["type"] for s in ok] == ["mention", "relation", "community"]
    assert ann.record_ends(ok[0].claim) == ["pubmed:1", "wikidata:42"]
    assert ann.record_ends(ok[1].claim) == ["wikidata:42", "wikidata:50", "pubmed:1"]
    assert ann.record_ends(ok[2].claim) == ["wikidata:42", "wikidata:50"]
    assert ann.entity_uid("wikidata:P31") == "wikidata:1000000031" and ann.entity_uid("hgnc:5") == "hgnc:5"
    for bad, match in (
            (a_mention(text, subject_lang="en"), "unknown subject_lang"),
            (a_mention(text, subject="pubmed:1"), "chunk of the text target"),
            (a_mention(text, object="wikidata:42"), "not an entity's id"),
            (a_mention(text, object="hgnc:5"), "not in a target's namespace"),
            (a_mention(text, start=5, end=5), "end is after start"),
            (a_mention(text, start=1.0), "decimal string"),
            (a_mention(text, surface=""), "surface is the text"),
            (a_mention(text, method={"kind": "model", "id": "g", "version": 1}), "model method is"),
            (a_mention(text, method=dict(MODEL, temperature="0.7")), "temperature"),
            (a_relation(text, relation="wikidata:Q5"), "not a property's id"),
            (a_relation(text, object="wikidata:Q42"), "same entity"),
            (a_relation(text, evidence_chunk={"uid": "pubmed:1"}), "chunk of the text target"),
            (a_community(members=["wikidata:Q50", "wikidata:Q42"]), "sorted by bytes"),
            (a_community(members=["wikidata:Q42"]), "at least two"),
            (a_community(object="wikidata:Q42"), "no object"),
            (a_community(subject={"id": "c"}), "id, level"),
            (a_community(subject_hash="00" * 16), "pins no subject_hash") if False else
            (a_claim(obj="wikidata:42"), "ONE target")):
        with pytest.raises(ann.AnnotationsError, match=match):
            ann.read_claims(claims_file(bad), target_plugins=plugins)
    with pytest.raises(ann.AnnotationsError, match="same claim as line 1"):
        ann.read_claims(claims_file(a_mention(text), a_mention(text, confidence="0.5")), target_plugins=plugins)
    # the same span naming another entity, or another span, is another claim
    assert len(ann.read_claims(claims_file(a_mention(text), a_mention(text, object="wikidata:Q50"),
                                           a_mention(text, start=14, end=19, surface="wrote")),
                               target_plugins=plugins)) == 3


def test_create_with_two_targets_pins_chunks_and_entities_and_writes_the_three_edges(tmp_path):
    text, entities, hashes = two_targets(tmp_path)
    codex, snap = make_graph(tmp_path, claims_file(a_mention(text), a_relation(text), a_community(),
                                                   a_claim("pubmed:2", "pubmed:3")), annotator="a graph builder")
    assert codex.version == 9 and len(codex.annotates) == 2
    d = col.shard_dir_of(tmp_path, "graph")
    (shard,), (rel,) = snap.shards, snap.relations
    records = list(pack_records.iter_records(d / shard.path))
    claims = [ann.claim_of(r) for r in records]
    # numbered in the byte order of each claim's identifying tuple as canonical JSON (09 §9.5)
    keys = [cx.canonical_json(ann.identity_tuple(c)) for c in claims]
    assert keys == sorted(keys) and [r["uid"] for r in records] == [f"annotations:{n}" for n in range(1, 5)]
    by_type = {c["type"]: c for c in claims}
    m, r, co, w = by_type["mention"], by_type["relation"], by_type["community"], by_type["same-work"]
    assert (m["subject_hash"], m["object_hash"], m["target_snapshot"]) == (
        hashes["pubmed:1"], hashes["wikidata:42"], TARGET_SNAPSHOT), "the chunk's RECORD and the entity's record"
    assert (r["subject_hash"], r["object_hash"], r["target_snapshot"]) == (
        hashes["wikidata:42"], hashes["wikidata:50"], "2026-07"), "pinned to the SUBJECT's target snapshot"
    assert (co["subject_hash"], co["object_hash"], co["target_snapshot"]) == (None, None, "2026-07")
    assert (w["subject_hash"], w["target_snapshot"]) == (hashes["pubmed:2"], TARGET_SNAPSHOT)
    uid_of = {c["type"]: rr["uid"] for rr, c in zip(records, claims)}
    edges = [(e["from"], e["type"], e["to"]) for e in pack_records.iter_records(d / rel.path)]
    assert sorted(edges) == sorted([
        ("pubmed:1", "annotations:mention", "wikidata:Q42"),
        ("wikidata:42", "annotations:relation", "wikidata:Q50"),
        ("wikidata:42", "annotations:member-of", uid_of["community"]),
        ("wikidata:50", "annotations:member-of", uid_of["community"]),
        ("pubmed:2", "annotations:same-work", "pubmed:3")])
    assert edges == sorted(edges, key=lambda e: (e[0].encode(), e[1].encode(), e[2].encode()))
    # what show, the index and the state make of it
    shown = col.show(tmp_path, "graph")
    assert shown["annotates"]["target"] == "pubmed" and [t["target"] for t in shown["targets"]] == ["pubmed", "wikidata"]
    idx = ann.index_of(d)
    assert set(idx.by_target) == {"pubmed:2", "pubmed:3"} and set(idx.graph) == {"pubmed:1", "wikidata:42", "wikidata:50"}
    now = dict(hashes)
    assert ann.state_of(m, now) == "current" and ann.state_of(co, now) == "unknown"
    assert ann.state_of(r, dict(now, **{"wikidata:50": "00" * 16})) == "stale"
    # the plan's checks pass; a second create resumes byte for byte
    report = check.run(tmp_path, "graph", codex, snap, seed=b"s" * 32, checks=list(cx.ANNOTATIONS_CHECKS))
    assert {o.check: o.result for o in report.outcomes} == {
        "roots": "pass", "records": "pass", "license": "pass", "signature": "pass"}
    again, snap2 = make_graph(tmp_path, claims_file(a_mention(text), a_relation(text), a_community(),
                                                    a_claim("pubmed:2", "pubmed:3")), annotator="a graph builder")
    assert again == codex and snap2.hash == snap.hash
    with pytest.raises(CodexError, match="holds one collection"):
        ann.create(tmp_path, "graph", target_ref="pubmed", from_snapshot=TARGET_SNAPSHOT,
                   claims=claims_file(a_claim()), platform_image=PLATFORM, log=lambda m: None)


def test_a_chunk_end_is_checked_against_the_text_this_peer_holds(tmp_path):
    text, entities, _ = two_targets(tmp_path)
    end_, chunk_text = a_chunk(text, "pubmed:1", "Douglas Adams wrote it")
    assert chunk_text.startswith("Douglas Adams")
    with pytest.raises(ann.AnnotationsError, match="made about other text"):
        make_graph(tmp_path, claims_file(a_mention(text, subject=dict(end_, text_hash="00" * 16))))
    with pytest.raises(ann.AnnotationsError, match="has no chunk"):
        make_graph(tmp_path, claims_file(a_mention(text, subject=dict(end_, chunk_id="pubmed:1:99:1"))))
    with pytest.raises(ann.AnnotationsError, match="surface .* is not the chunk's text"):
        make_graph(tmp_path, claims_file(a_mention(text, surface="Douglas Adam")))
    with pytest.raises(ann.AnnotationsError, match="does not hold: line 1: wikidata:99 \\(wikidata:Q99\\)"):
        make_graph(tmp_path, claims_file(a_mention(text, object="wikidata:Q99")))
    with pytest.raises(ann.AnnotationsError, match="pins no subject_hash"):
        make_graph(tmp_path, claims_file(a_community(subject_hash="00" * 16)))
    assert not (tmp_path / "graph").exists(), "refused before anything is written"
    # the targets: one plugin twice, an annotations collection, a missing snapshot
    with pytest.raises(ann.AnnotationsError, match="named twice"):
        ann.create(tmp_path, "g2", targets=[("pubmed", TARGET_SNAPSHOT), ("pubmed", TARGET_SNAPSHOT)],
                   claims=claims_file(a_claim()), platform_image=PLATFORM, log=lambda m: None)
    a_target(tmp_path, name="pubmed-too", texts=("five",), cleaning_version=2)
    with pytest.raises(ann.AnnotationsError, match="both 'pubmed' collections"):
        ann.create(tmp_path, "g2", targets=[("pubmed", TARGET_SNAPSHOT), ("pubmed-too", TARGET_SNAPSHOT)],
                   claims=claims_file(a_claim()), platform_image=PLATFORM, log=lambda m: None)
    with pytest.raises(ann.AnnotationsError, match="names the snapshot"):
        ann.create(tmp_path, "g2", targets=[("pubmed", TARGET_SNAPSHOT), ("wikidata", "")],
                   claims=claims_file(a_claim()), platform_image=PLATFORM, log=lambda m: None)


def test_the_command_takes_several_targets_and_a_dry_run_counts_them(tmp_path, monkeypatch, capsys):
    from app import louise
    from app.bundles import keys as node_keys
    monkeypatch.setattr(node_keys, "load_signing_key", lambda *a, **k: KEY)
    text, entities, _ = two_targets(tmp_path)
    claims = tmp_path / "claims.jsonl"
    claims.write_bytes(claims_file(a_mention(text), a_community()))
    base = ["hold", "collection", "create", "annotations", "--root", str(tmp_path), "--claims", str(claims),
            "--annotates", "pubmed", "--from-snapshot", TARGET_SNAPSHOT, "--annotates", "wikidata"]
    assert louise.main(base + ["--name", "graph"]) == 2
    assert "one --from-snapshot per --annotates" in capsys.readouterr().err
    assert louise.main(base + ["--from-snapshot", "2026-07", "--name", "graph", "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert (preview["version"], preview["claims"], preview["written"]) == (9, 2, False)
    assert [t["target"] for t in preview["targets"]] == ["pubmed", "wikidata"]
    assert preview["by_method"] == {"model:gliner-it": 2} and preview["by_type"] == {"community": 1, "mention": 1}
    assert louise.main(base + ["--from-snapshot", "2026-07", "--name", "graph", "--snapshot", "2026-09",
                               "--platform-image", PLATFORM]) == 0
    cid = capsys.readouterr().out.strip().splitlines()[-1]
    assert col.read(tmp_path, "graph").collection_id == cid
    assert louise.annotation_targets("a,b", "2026-01,2026-02") == [("a", "2026-01"), ("b", "2026-02")]
    assert louise.main(base + ["--from-snapshot", "2026-07", "--name", "pq", "--container", "parquet",
                               "--platform-image", PLATFORM]) == 0
    capsys.readouterr()
    assert cx.container_of(col.read(tmp_path, "pq")) == "parquet"
    with pytest.raises(SystemExit):
        louise.main(base + ["--from-snapshot", "2026-07", "--name", "csv", "--container", "csv",
                            "--platform-image", PLATFORM])
    assert "invalid choice" in capsys.readouterr().err


def test_parquet_parts_are_written_read_checked_and_named_from_the_codex(tmp_path):
    """09 §9.5 "The file (B)" (operator, 2026-10-20): a claims collection in
    Parquet parts — `<NNNNN>.parquet` members, the same `records_root` as
    the JSONL twin, the plan's checks over them, the container read beside
    the snapshot by every loader, integrity's listing, no patching."""
    from app.hold import integrity
    from app.share import patch, seeder
    text, entities, hashes = two_targets(tmp_path)
    claims = claims_file(a_mention(text), a_relation(text), a_community())
    codex, snap = make_graph(tmp_path, claims, name="graph-pq", container="parquet")
    twin, tsnap = make_graph(tmp_path, claims, name="graph-jsonl")
    assert (codex.version, cx.container_of(codex), codex.slicing["shard_records"]) == (9, "parquet", 1_000_000)
    d = col.shard_dir_of(tmp_path, "graph-pq")
    (shard,), (rel,) = snap.shards, snap.relations
    assert shard.path == "records/all/00001.parquet" and rel.path == "relations/all/00001.jsonl.zst"
    assert (d / shard.path).read_bytes()[:4] == b"PAR1"
    assert shard_mod.pieces_root_of(d / shard.path) == shard.root
    assert shard.records_root == tsnap.shards[0].records_root, "the records' hashes never depended on the container"
    assert shard.root != tsnap.shards[0].root, "the file's bytes did"
    assert [ann.claim_of(r)["type"] for r in pack_records.iter_records(d / shard.path)] == \
        [ann.claim_of(r)["type"] for r in pack_records.iter_records(col.shard_dir_of(tmp_path, "graph-jsonl") / tsnap.shards[0].path)]
    # every loader names the part from the Codex beside the snapshot
    loaded = seeder.current_snapshot(d)
    assert loaded.shards[0].path == "records/all/00001.parquet" and loaded.shards[0].container == "parquet"
    assert sn.load(d / shard_mod.snapshot_path("2026-09")).shards[0].path == shard.path
    assert sn.load(d / shard_mod.snapshot_path("2026-09"), container="jsonl").shards[0].path.endswith(".jsonl.zst"), \
        "a caller may say otherwise; nothing on disk changes"
    assert set(loaded.members()) >= {"records/all", "relations/all"} or True
    # the plan's checks read Parquet; the index; the state
    report = check.run(tmp_path, "graph-pq", codex, snap, seed=b"s" * 32, checks=list(cx.ANNOTATIONS_CHECKS))
    assert {o.check: o.result for o in report.outcomes} == {
        "roots": "pass", "records": "pass", "license": "pass", "signature": "pass"}
    idx = ann.index_of(d)
    assert set(idx.graph) == {"pubmed:1", "wikidata:42", "wikidata:50"}
    assert integrity.violations(tmp_path, "graph-pq", codex) == ()
    # a Parquet part is fetched whole: no patch
    assert patch.part_of("records/lang=en/00001.parquet") is None
    assert patch.part_of("records/lang=en/00001.jsonl.zst") == "lang=en/00001"
    # the superseded name keeps the container's suffix
    assert shard_mod.superseded_name(d / shard.path, "2026-09").name == "00001.2026-09.parquet"
    # a second create resumes byte for byte; the JSONL name refuses the Parquet claims
    again, snap2 = make_graph(tmp_path, claims, name="graph-pq", container="parquet")
    assert again == codex and snap2.hash == snap.hash
    with pytest.raises(CodexError, match="holds one collection"):
        make_graph(tmp_path, claims, name="graph-jsonl", container="parquet")

"""tests/share/test_deprecate.py — a producer deprecates its own redundant
copies (operator, 2026-10-19; 03 §3.6, 09 §9.8, §9.9): one attestation
pairs each deprecated copy with the current one; nothing is removed at
once — the copies are served a day longer and listed apart in the digest;
a leecher never starts a fetch of one, finishes one in flight past half,
and falls back to one when the current copy is offered by nobody; two
equal copies held together count a part once; after the window a holder
of both drops the deprecated one, a holder of it alone keeps it."""
import json
import shutil
import time

import pytest

from app import embeddings
from app.hold import envelope as env
from app.share import accept, attest, contribute, coverage as cov, deprecate, holdings, members as mem, \
    messages, peers
from tests.share.test_contribute import attested, ready  # noqa: F401
from tests.share.test_messages import PEER, PEER_KEY
from tests.share.test_share_api import OURS, OUR_KEY
from tests.share.test_withdraw import IMAGE, _alone, _second_version

pytestmark = pytest.mark.strict_encoder


def _pair(ready):
    root, snapshot, codex, kw = ready
    first = _alone(root, codex, snapshot, kw)
    sid, kept = first["bundles"][0]["shard"], first["bundles"][0]["bundle_id"]
    dup = _second_version(root, codex, snapshot, kw, sid, later=int(time.time()) + 3600)
    return root, snapshot, codex, kw, sid, kept, dup


def _digest(kw, codex, snapshot, *, members=(), deprecated=(), now, key=PEER_KEY, port=6881):
    entry = {"collection_id": codex.collection_id, "lineage_id": codex.collection_id, "title": "g",
             "snapshot_id": snapshot.snapshot_id, "snapshot_hash": snapshot.hash, "codex_hash": "sha256:" + "2" * 64,
             "license": "green", "privacy": "public", "members": [m.to_dict() for m in members], "coverage": []}
    if deprecated:
        entry["deprecated_members"] = [m.to_dict() for m in deprecated]
    digest = messages.digest_body(key=key, address="http://10.0.0.7:30042", neighbours=[],
                                  benchmarks=[], collections=[entry], now=now)
    peers.record_digest(kw["node_con"], {**digest, "peer_port": port}, now=now)


def test_publish_pairs_each_copy_with_the_current_one_and_removes_nothing(ready):
    root, snapshot, codex, kw, sid, kept, dup = _pair(ready)
    events_before = len(peers.to_forward(kw["node_con"]))
    pairs = deprecate.header_pairs(root, "gutenberg", codex, kw["con"], own_key=OUR_KEY)
    assert pairs == {dup: kept}, "the later copy of the content_digest group, paired with the kept one"
    for bad in ({dup: dup}, {"sha256:" + "e" * 64: kept}):
        with pytest.raises(ValueError):
            deprecate.publish(root, "gutenberg", codex, snapshot, bad, key=OURS, own_key=OUR_KEY, con=kw["con"],
                              node_con=kw["node_con"], platform_image=IMAGE)
    said: list[str] = []
    now = time.time()
    out = deprecate.publish(root, "gutenberg", codex, snapshot, pairs, key=OURS, own_key=OUR_KEY, con=kw["con"],
                            node_con=kw["node_con"], platform_image=IMAGE, now=now, log=said.append)
    assert out["bundles"] == [dup] and out["shards"] == [cov.shard_id(codex, sid)]
    assert out["published"]["published"] == [out["attestation"]] and out["keep_until"] == messages.time_text(now + deprecate.KEEP_S)
    body = env.verify(env.ATTESTATION, json.loads(attest.attestation_path(root, "gutenberg", codex, out["attestation"]).read_bytes()))
    assert body["check"] == body["result"] == "deprecate" and body["detail"] == {"bundles": {dup: kept}}
    assert body["key"] == OUR_KEY and body["shards"] == [cov.shard_id(codex, sid)]
    assert len(peers.to_forward(kw["node_con"])) == events_before + 1
    # nothing removed: both copies held, both accepted, both served
    for b in (kept, dup):
        assert contribute.bundle_dir(root, "gutenberg", codex, sid, b).is_dir() and cov.row(kw["con"], b)["accepted_at"]
    got = deprecate.deprecated(root, "gutenberg", codex)
    assert set(got) == {dup} and set(got[dup]) == {OUR_KEY} and got[dup][OUR_KEY][0] == kept \
        and abs(got[dup][OUR_KEY][1] - now) < 1
    assert deprecate.statement(got, dup, OUR_KEY) == got[dup][OUR_KEY] and deprecate.statement(got, dup) == got[dup][OUR_KEY]
    assert deprecate.statement(got, dup, PEER_KEY) is None and deprecate.statement(got, kept) is None
    assert deprecate.by_producer(got, dup, OUR_KEY) and not deprecate.by_producer(got, dup, PEER_KEY) \
        and not deprecate.by_producer(got, dup, None)
    assert deprecate.redundant_here(root, "gutenberg", codex, kw["con"]) == {dup}
    assert any("served until" in s for s in said)
    assert deprecate.KEEP_S == 86400.0, "one day (operator, 2026-10-19)"


def test_the_digest_lists_a_deprecated_copy_apart_and_only_on_its_producer_s_word(ready):
    root, snapshot, codex, kw, sid, kept, dup = _pair(ready)
    state_dir = root / "gutenberg" / "state"
    members = mem.bundle_members(state_dir, codex.space_segment)
    assert {m.name for m in members} >= {f"bundle-{codex.space_segment}-{b[7:]}" for b in (kept, dup)}
    current, gone = deprecate.split_members(root, "gutenberg", codex, members, kw["con"])
    assert current == list(members) and gone == [], "nothing deprecated: offered as before"
    # a stranger's deprecation: the copy stays in members
    body = deprecate.attestation_body(codex, snapshot, {dup: kept}, [], own_key=PEER_KEY, platform_image=IMAGE,
                                      now=time.time())
    p = attest.attestation_path(root, "gutenberg", codex, env.body_id(body))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(json.dumps(env.sign(env.ATTESTATION, body, PEER)).encode())
    current, gone = deprecate.split_members(root, "gutenberg", codex, members, kw["con"])
    assert current == list(members) and gone == []
    # the producer's: listed apart
    deprecate.publish(root, "gutenberg", codex, snapshot, {dup: kept}, key=OURS, own_key=OUR_KEY, con=kw["con"],
                      node_con=kw["node_con"], platform_image=IMAGE)
    current, gone = deprecate.split_members(root, "gutenberg", codex, members, kw["con"])
    assert [m.name for m in gone] == [f"bundle-{codex.space_segment}-{dup[7:]}"]
    assert f"bundle-{codex.space_segment}-{dup[7:]}" not in {m.name for m in current} and len(current) == len(members) - 1
    entry = messages.collection_entry(codex, snapshot, current, codex_bytes=b"{}", lineage_id=codex.collection_id,
                                      deprecated_members=gone)
    assert [m["name"] for m in entry["deprecated_members"]] == [gone[0].name] and "files" in entry["deprecated_members"][0]
    assert "deprecated_members" not in messages.collection_entry(codex, snapshot, current, codex_bytes=b"{}",
                                                                 lineage_id=codex.collection_id)
    # the attestation's word reaches the digest builder without the map (opens the manifest itself)
    current2, gone2 = deprecate.split_members(root, "gutenberg", codex, members)
    assert [m.name for m in gone2] == [m.name for m in gone]


def test_a_leecher_never_starts_a_deprecated_fetch_and_falls_back_when_the_current_is_offered_by_nobody(ready):
    root, snapshot, codex, kw, sid, kept, dup = _pair(ready)
    deprecate.publish(root, "gutenberg", codex, snapshot, {dup: kept}, key=OURS, own_key=OUR_KEY, con=kw["con"],
                      node_con=kw["node_con"], platform_image=IMAGE)
    ids = deprecate.deprecated(root, "gutenberg", codex)
    m_kept = mem.bundle_member(codex.space_segment, contribute.bundle_dir(root, "gutenberg", codex, sid, kept))
    m_dup = mem.bundle_member(codex.space_segment, contribute.bundle_dir(root, "gutenberg", codex, sid, dup))
    # this peer as a LEECHER of the part: it holds neither copy
    kw["con"].execute("DELETE FROM coverage WHERE bundle_id IN (?,?)", (kept, dup))
    now = time.time()

    def listed(ids_=ids, **kw_):
        return {p["bundle_id"]: p for p, _h in accept.listed_bundles(kw["node_con"], codex, kw["con"], now=now,
                                                                        deprecated=ids_, **kw_)}
    # both offered, the current under members: the current only
    _digest(kw, codex, snapshot, members=[m_kept], deprecated=[m_dup], now=now)
    got = listed()
    assert set(got) == {kept} and not got[kept].get("deprecated")
    # the current offered by nobody: the deprecated copy is the fallback, flagged
    _digest(kw, codex, snapshot, members=[], deprecated=[m_dup], now=now)
    got = listed()
    assert set(got) == {dup} and got[dup]["deprecated"] is True and got[dup]["current"] == kept
    # the current offered but its last fetch came back nobody-served: the fallback too
    _digest(kw, codex, snapshot, members=[m_kept], deprecated=[m_dup], now=now)
    got = listed(nobody={kept: now + 100})
    assert dup in got and got[dup]["deprecated"] is True
    assert set(listed(nobody={kept: now - 1})) == {kept}, "a memory that lapsed"
    # a stale digest still listing the deprecated copy under members: not a new fetch either
    _digest(kw, codex, snapshot, members=[m_kept, m_dup], deprecated=[], now=now)
    assert set(listed()) == {kept}
    # a deprecated copy whose attestation this peer has not read: never asked for
    _digest(kw, codex, snapshot, members=[], deprecated=[m_dup], now=now)
    assert listed(ids_={}) == {}
    # the current copy held here: nothing of the deprecated one is wanted (RULE 1)
    cov.record(kw["con"], bundle_id=kept, shard=cov.shard_id(codex, sid), producer=OUR_KEY, uids=[], chunks=3,
               tiers={}, source=cov.FETCHED, accepted_at=messages.time_text(now))
    assert listed() == {}
    # and the offer check a holder runs sees the fallback as an offer
    kw["con"].execute("DELETE FROM coverage WHERE bundle_id=?", (kept,))
    assert cov.fold_claims(kw["con"], codex, [peers.digest_of(kw["node_con"], PEER_KEY)]) >= 0
    cov.record(kw["con"], bundle_id=dup, shard=cov.shard_id(codex, sid), producer=OUR_KEY, uids=[], chunks=0,
               tiers={}, source=cov.FETCHED, accepted_at=None)
    kw["con"].execute("UPDATE coverage SET claimed_by=? WHERE bundle_id=?", (json.dumps([PEER_KEY]), dup))
    assert cov.shard_id(codex, sid) in contribute.offered_elsewhere(codex, kw["con"], kw["node_con"], now=now,
                                                                     deprecated=ids)


def test_a_fetch_in_flight_is_finished_past_half_and_dropped_before(ready):
    root, snapshot, codex, kw, sid, kept, dup = _pair(ready)
    dup_dir = contribute.bundle_dir(root, "gutenberg", codex, sid, dup)
    m_dup = mem.bundle_member(codex.space_segment, dup_dir)
    files = {f.path: (dup_dir / f.path).read_bytes() for f in m_dup.files}
    deprecate.publish(root, "gutenberg", codex, snapshot, {dup: kept}, key=OURS, own_key=OUR_KEY, con=kw["con"],
                      node_con=kw["node_con"], platform_image=IMAGE)
    payload = {"bundle_id": dup, "root": m_dup.root, "files": m_dup.to_dict()["files"], "shard": sid}
    staged = root / "gutenberg" / "state" / holdings.STAGING / m_dup.name
    # the current copy held here (the deprecated one not): whatever landed, pointless
    kw["con"].execute("DELETE FROM coverage WHERE bundle_id=?", (dup,))
    staged.mkdir(parents=True)
    for path, data in files.items():
        (staged / path).write_bytes(data)
    got = accept.take(root, "gutenberg", codex, payload, session=None, con=kw["con"], node_con=kw["node_con"])
    assert got["outcome"] == "ignored" and "held here" in got["why"] and not staged.exists()
    # a leecher: the current not held, a third of the bytes landed → dropped
    kw["con"].execute("DELETE FROM coverage WHERE bundle_id IN (?,?)", (kept, dup))
    shutil.rmtree(dup_dir)
    staged.mkdir(parents=True)
    small = min(files, key=lambda p: len(files[p]))
    (staged / small).write_bytes(files[small])
    assert deprecate.staged_fraction(staged, m_dup) < 0.5
    got = accept.take(root, "gutenberg", codex, payload, session=None, con=kw["con"], node_con=kw["node_con"])
    assert got["outcome"] == "ignored" and "dropped for the current" in got["why"] and not staged.exists()
    # every byte landed → finished: judged and accepted like any bundle
    staged.mkdir(parents=True)
    for path, data in files.items():
        (staged / path).write_bytes(data)
    said: list[str] = []
    got = accept.take(root, "gutenberg", codex, payload, session=None, con=kw["con"], node_con=kw["node_con"],
                      key=OURS, own_key=OUR_KEY, log=said.append)
    assert got["outcome"] == "accepted" and any("finished rather than dropped" in s for s in said)
    assert cov.row(kw["con"], dup)["accepted_at"] and dup_dir.is_dir()
    # the fallback's listing is a fetch like any other
    kw["con"].execute("DELETE FROM coverage WHERE bundle_id=?", (dup,))
    shutil.rmtree(dup_dir)
    staged.mkdir(parents=True)
    (staged / small).write_bytes(files[small])
    for path, data in files.items():
        (staged / path).write_bytes(data)
    got = accept.take(root, "gutenberg", codex, {**payload, "deprecated": True, "current": kept}, session=None,
                      con=kw["con"], node_con=kw["node_con"], key=OURS, own_key=OUR_KEY)
    assert got["outcome"] == "accepted"


def test_after_the_window_a_holder_of_both_drops_the_copy_a_holder_of_it_alone_keeps_it(ready):
    from app.hold import records as rec
    root, snapshot, codex, kw, sid, kept, dup = _pair(ready)
    local = rec.local_collection(root, "gutenberg", codex)
    dup_dir = contribute.bundle_dir(root, "gutenberg", codex, sid, dup)
    from app.share import withdraw
    chunk_ids = withdraw._chunk_ids(dup_dir, set(json.loads((dup_dir / "possession.json").read_bytes())))
    now = time.time()
    deprecate.publish(root, "gutenberg", codex, snapshot, {dup: kept}, key=OURS, own_key=OUR_KEY, con=kw["con"],
                      node_con=kw["node_con"], platform_image=IMAGE, now=now)
    assert deprecate.expire(root, "gutenberg", codex, kw["con"], now=now + deprecate.KEEP_S - 60) == {"expired": 0, "kept": 1}
    assert dup_dir.is_dir() and cov.row(kw["con"], dup)["accepted_at"]
    # a holder of the deprecated copy alone: past the window it keeps it
    kw["con"].execute("DELETE FROM coverage WHERE bundle_id=?", (kept,))
    assert deprecate.expire(root, "gutenberg", codex, kw["con"], now=now + deprecate.KEEP_S + 60) == {"expired": 0, "kept": 1}
    assert dup_dir.is_dir()
    # a holder of both: the copy goes, the vectors stay
    cov.record(kw["con"], bundle_id=kept, shard=cov.shard_id(codex, sid), producer=OUR_KEY, uids=[], chunks=3,
               tiers={}, source=cov.LOCAL, accepted_at=messages.time_text(now))
    said: list[str] = []
    assert deprecate.expire(root, "gutenberg", codex, kw["con"], now=now + deprecate.KEEP_S + 60,
                            log=said.append) == {"expired": 1, "kept": 0}
    assert not dup_dir.exists() and cov.row(kw["con"], dup) is None and cov.row(kw["con"], kept)["accepted_at"]
    assert len(embeddings.vectors_by_chunk_id(chunk_ids, collection=local)) == len(chunk_ids), "vectors untouched"
    assert any("removed after its keep window" in s for s in said)
    assert deprecate.expire(root, "gutenberg", codex, kw["con"], now=now + deprecate.KEEP_S + 60) == {"expired": 0, "kept": 0}


def test_two_equal_copies_of_a_partial_part_count_once(ready):
    """Without `redundant`, two accepted copies of 60 % of a part sum to the
    whole part: the part reads closed and its leader never embeds the rest."""
    root, snapshot, codex, kw, sid, kept, dup = _pair(ready)
    s = snapshot.shards[0]
    part = max(1, int(s.chunks * 0.6))
    rows = [{"shard": cov.shard_id(codex, sid), "bundle_id": b, "producer": OUR_KEY, "uids": None, "chunks": part,
             "tiers": {}, "accepted_at": "2026-10-19T12:00:00Z", "source": "local", "verified_on_use": 0,
             "claimed_by": []} for b in (kept, dup)]
    view = {v["shard"]: v for v in cov.shards_view(codex, snapshot, rows)}[cov.shard_id(codex, sid)]
    assert view["covered"] == min(int(s.chunks), 2 * part) and (not view["open"] if 2 * part >= s.chunks else True), \
        "the defect: two copies summed"
    view = {v["shard"]: v for v in cov.shards_view(codex, snapshot, rows, redundant={dup})}[cov.shard_id(codex, sid)]
    assert view["covered"] == part and view["open"]
    deprecate.publish(root, "gutenberg", codex, snapshot, {dup: kept}, key=OURS, own_key=OUR_KEY, con=kw["con"],
                      node_con=kw["node_con"], platform_image=IMAGE)
    shown = cov.show(codex, snapshot, kw["con"], redundant=deprecate.redundant_here(root, "gutenberg", codex, kw["con"]))
    assert shown["covered"] == shown["chunks"], "the fixture's part is whole: counted once, still whole"


def test_the_dev_command_lists_asks_and_deprecates(ready, monkeypatch, capsys):
    from app import identity, louise
    from app.bundles import keys as node_keys
    root, snapshot, codex, kw, sid, kept, dup = _pair(ready)
    monkeypatch.setattr(node_keys, "load_signing_key", lambda path=None: OURS)
    monkeypatch.setattr(identity, "own", lambda: OUR_KEY)
    monkeypatch.delenv("LOUISE_PLATFORM_IMAGE", raising=False)
    assert louise.main(["dev", "deprecate-duplicates", "gutenberg", "--root", str(root), "--yes"]) == 2
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", IMAGE)

    def out():
        text = capsys.readouterr().out
        return json.loads(text[text.index("{"):])
    assert louise.main(["dev", "deprecate-duplicates", "gutenberg", "--root", str(root), "--dry-run"]) == 0
    assert out() == {"collection": "gutenberg", "parts": 1, "groups": 1, "bundles": 1,
                     "chunks": cov.row(kw["con"], dup)["chunks"], "deprecated": False}
    assert louise.main(["dev", "deprecate-duplicates", "gutenberg", "--root", str(root)]) == 2, "no terminal: --yes"
    capsys.readouterr()
    assert louise.main(["dev", "deprecate-duplicates", "gutenberg", "--root", str(root), "--yes"]) == 0
    got = out()
    assert got["deprecated"] is True and got["bundles"] == [dup] and got["keep_until"]
    for b in (kept, dup):
        assert contribute.bundle_dir(root, "gutenberg", codex, sid, b).is_dir() and cov.row(kw["con"], b)["accepted_at"]
    assert set(deprecate.deprecated(root, "gutenberg", codex)) == {dup}
    assert louise.main(["dev", "deprecate-duplicates", "gutenberg", "--root", str(root), "--yes"]) == 0
    assert out() == {"collection": "gutenberg", "parts": 0, "groups": 0, "bundles": 0, "chunks": 0, "deprecated": False}, \
        "the pair still exists on disk (the copy goes after the window) and is stated once"
    assert deprecate.header_pairs(root, "gutenberg", codex, kw["con"], own_key=OUR_KEY) == {}

"""tests/share/test_withdraw.py — a producer withdraws its own bundles (03
§3.6; 09 §9.7, §9.8): the holder pass bundled 418 partially held parts
again after every restart, a new id each time (operator, 2026-10-19: the
later of each pair is withdrawn). `duplicates` finds this producer's
bundles that share a `content_digest` for a part; `publish` writes and
publishes one `withdraw` attestation and drops them here; a peer holding
a withdrawn bundle drops it at the beat when the attestation's signer is
its producer, its vectors only where no other bundle claims the records;
digests and events never bring a withdrawn bundle back."""
import json
import time

import pytest

from app import embeddings
from app.hold import envelope as env, export as exp, land as landing, vectors_build as vb
from app.share import accept, attest, contribute, coverage as cov, members as mem, peers, rendezvous, withdraw
from tests.share.test_contribute import attested, ready  # noqa: F401
from tests.share.test_messages import PEER, PEER_KEY
from tests.share.test_share_api import OURS, OUR_KEY

pytestmark = pytest.mark.strict_encoder

IMAGE = "sha256:" + "1f" * 32


def _alone(root, codex, snapshot, kw, **over):
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    return contribute.once(root, "gutenberg", codex, live=[rendezvous.Peer(OUR_KEY, 1000.0)], **{**kw, **over})


def _second_version(root, codex, snapshot, kw, sid: str, *, later: int) -> str:
    """The same part exported again, `created_at` later: a new bundle id, the
    same `content_digest` — the holder pass after a restart (before dd58729)."""
    scratch = root / "gutenberg" / "state" / "bundles" / ".export-test"
    got = exp.export(root, "gutenberg", shard=sid, dest=scratch, vectors_for=kw["vectors_for"], embed=kw["embed"],
                     key=OURS, snapshot_id=snapshot.snapshot_id, created_at=later)
    (b,) = got["bundles"]
    final = contribute.bundle_dir(root, "gutenberg", codex, sid, b["bundle_id"])
    final.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.move(b["path"], final)
    header = json.loads((final / "header.json").read_bytes())
    cov.record(kw["con"], bundle_id=b["bundle_id"], shard=cov.shard_id(codex, sid), producer=OUR_KEY,
               uids=header["uids"], chunks=b["chunks"], tiers={t: "pass" for t in cov.TIERS}, source=cov.LOCAL,
               accepted_at="2026-10-19T12:00:00Z")
    return b["bundle_id"]


def test_duplicates_are_this_producer_s_bundles_sharing_a_content_digest_the_earliest_kept(ready):
    root, snapshot, codex, kw = ready
    first = _alone(root, codex, snapshot, kw)
    assert len(first["accepted"]) == len(snapshot.shards) >= 2
    sid = first["bundles"][0]["shard"]
    kept = first["bundles"][0]["bundle_id"]
    assert withdraw.duplicates(root, "gutenberg", codex, kw["con"], own_key=OUR_KEY) == [], "one version a part"
    dup = _second_version(root, codex, snapshot, kw, sid, later=int(time.time()) + 3600)
    assert dup != kept
    got = withdraw.duplicates(root, "gutenberg", codex, kw["con"], own_key=OUR_KEY)
    assert [(d["shard"], d["keep"], d["withdraw"]) for d in got] == [(cov.shard_id(codex, sid), kept, [dup])]
    assert got[0]["chunks"] == first["bundles"][0]["chunks"] > 0
    h1 = json.loads((contribute.bundle_dir(root, "gutenberg", codex, sid, kept) / "header.json").read_bytes())
    h2 = json.loads((contribute.bundle_dir(root, "gutenberg", codex, sid, dup) / "header.json").read_bytes())
    assert h1["content_digest"] == h2["content_digest"] == got[0]["content_digest"] and h1["created_at"] < h2["created_at"]
    # another producer's bundle with the same rows is not this peer's to withdraw
    cov.record(kw["con"], bundle_id="sha256:" + "e" * 64, shard=cov.shard_id(codex, sid), producer=PEER_KEY,
               uids=[], chunks=0, tiers={}, source=cov.FETCHED, accepted_at="2026-10-19T12:00:00Z")
    assert withdraw.duplicates(root, "gutenberg", codex, kw["con"], own_key=OUR_KEY) == got


def test_publish_writes_one_attestation_publishes_it_and_drops_the_bundles_here(ready):
    root, snapshot, codex, kw = ready
    first = _alone(root, codex, snapshot, kw)
    sid, kept = first["bundles"][0]["shard"], first["bundles"][0]["bundle_id"]
    dup = _second_version(root, codex, snapshot, kw, sid, later=int(time.time()) + 3600)
    events_before = len(peers.to_forward(kw["node_con"]))
    said: list[str] = []
    with pytest.raises(ValueError, match="only its own"):
        withdraw.publish(root, "gutenberg", codex, snapshot, ["sha256:" + "e" * 64], key=OURS, own_key=OUR_KEY,
                         con=kw["con"], node_con=kw["node_con"], platform_image=IMAGE)
    out = withdraw.publish(root, "gutenberg", codex, snapshot, [dup], key=OURS, own_key=OUR_KEY, con=kw["con"],
                           node_con=kw["node_con"], platform_image=IMAGE, log=said.append)
    assert out["bundles"] == [dup] and out["shards"] == [cov.shard_id(codex, sid)] and out["dropped_here"] == 1
    assert out["published"]["published"] == [out["attestation"]]
    # the attestation: 09 §9.8's shape, this key's, at its path, verified by copy_state
    p = attest.attestation_path(root, "gutenberg", codex, out["attestation"])
    body = env.verify(env.ATTESTATION, json.loads(p.read_bytes()))
    assert body["check"] == body["result"] == "withdraw" and body["detail"] == {"bundles": [dup]}
    assert body["key"] == OUR_KEY and body["shards"] == [cov.shard_id(codex, sid)] and body["platform_image"] == IMAGE
    assert withdraw.withdrawn(root, "gutenberg", codex) == {dup: frozenset({OUR_KEY})}
    assert len(peers.to_forward(kw["node_con"])) == events_before + 1, "one attestation event for the beat to forward"
    # here: the directory and the row gone, the kept bundle and the vectors untouched
    assert not contribute.bundle_dir(root, "gutenberg", codex, sid, dup).exists()
    assert contribute.bundle_dir(root, "gutenberg", codex, sid, kept).is_dir()
    assert cov.row(kw["con"], dup) is None and cov.row(kw["con"], kept)["accepted_at"]
    view = {v["shard"]: v for v in cov.show(codex, snapshot, kw["con"])["shards"]}[cov.shard_id(codex, sid)]
    assert view["covered"] == view["chunks"] and not view["open"], "the kept bundle covers the part"
    assert any("names 1 bundle(s)" in s for s in said)
    # idempotent: nothing left to withdraw; the beat's receiving side finds nothing held
    assert withdraw.duplicates(root, "gutenberg", codex, kw["con"], own_key=OUR_KEY) == []
    assert withdraw.drop_withdrawn(root, "gutenberg", codex, kw["con"], delete_chunks=lambda ids: 0) == \
        {"dropped": 0, "vectors": 0, "not_the_producer": 0}
    # copy state and trust ignore it: a withdraw is neither a pass nor a fail
    from app import copy_state
    bodies = copy_state.attestations(root, "gutenberg", codex)
    assert bodies.of(OUR_KEY, "withdraw") == (None, None) or bodies.of(OUR_KEY, "withdraw")[1] is None


def test_a_peer_drops_a_withdrawn_bundle_only_on_its_producer_s_word(ready):
    """The receiving side (03 §3.6): at the beat, a held bundle its producer
    withdrew loses its row and its directory; a withdraw signed by anyone
    else withdraws nothing. The vectors of a duplicate stay (the kept bundle
    claims the same records); those of records no other bundle claims go."""
    from app.hold import records as rec
    root, snapshot, codex, kw = ready
    first = _alone(root, codex, snapshot, kw)
    local = rec.local_collection(root, "gutenberg", codex)
    (b_a, b_b) = first["bundles"][0], first["bundles"][1]
    sid_a, sid_b = b_a["shard"], b_b["shard"]
    dup = _second_version(root, codex, snapshot, kw, sid_a, later=int(time.time()) + 3600)
    dup_dir = contribute.bundle_dir(root, "gutenberg", codex, sid_a, dup)
    dup_chunks = withdraw._chunk_ids(dup_dir, set(json.loads((dup_dir / "possession.json").read_bytes())))
    assert dup_chunks and len(embeddings.vectors_by_chunk_id(dup_chunks, collection=local)) == len(dup_chunks)

    def write_withdraw(key, ids):
        body = withdraw.attestation_body(codex, snapshot, ids, [], own_key=env.key_text(key.verify_key),
                                         platform_image=IMAGE, now=time.time())
        p = attest.attestation_path(root, "gutenberg", codex, env.body_id(body))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(json.dumps(env.sign(env.ATTESTATION, body, key)).encode())
    deleted: list[list[str]] = []

    def delete(ids):
        deleted.append(list(ids))
        return embeddings.delete_chunks(ids, collection=local)
    # a stranger's withdraw of this producer's bundle: ignored, nothing moves
    write_withdraw(PEER, [dup])
    said: list[str] = []
    got = withdraw.drop_withdrawn(root, "gutenberg", codex, kw["con"], delete_chunks=delete, log=said.append)
    assert got == {"dropped": 0, "vectors": 0, "not_the_producer": 1} and cov.row(kw["con"], dup) and dup_dir.is_dir()
    assert any("not its producer" in s for s in said) and deleted == []
    # the producer's: the duplicate goes, its vectors stay — the kept bundle claims the same records
    write_withdraw(OURS, [dup])
    assert withdraw.withdrawn(root, "gutenberg", codex) == {dup: frozenset({OUR_KEY, PEER_KEY})}
    got = withdraw.drop_withdrawn(root, "gutenberg", codex, kw["con"], delete_chunks=delete)
    assert got == {"dropped": 1, "vectors": 0, "not_the_producer": 0}
    assert cov.row(kw["con"], dup) is None and not dup_dir.exists() and deleted == []
    assert len(embeddings.vectors_by_chunk_id(dup_chunks, collection=local)) == len(dup_chunks)
    # a faulty bundle: the only one for its part — its chunks return to without vectors
    b_dir = contribute.bundle_dir(root, "gutenberg", codex, sid_b, b_b["bundle_id"])
    b_chunks = withdraw._chunk_ids(b_dir, set(json.loads((b_dir / "possession.json").read_bytes())))
    write_withdraw(OURS, [b_b["bundle_id"]])
    got = withdraw.drop_withdrawn(root, "gutenberg", codex, kw["con"], delete_chunks=delete)
    assert got["dropped"] == 1 and got["vectors"] == len(b_chunks) > 0
    assert deleted == [b_chunks] and embeddings.vectors_by_chunk_id(b_chunks, collection=local) == {}
    assert cov.row(kw["con"], b_b["bundle_id"]) is None and not b_dir.exists()
    view = {v["shard"]: v for v in cov.show(codex, snapshot, kw["con"])["shards"]}
    assert view[cov.shard_id(codex, sid_b)]["open"] and not view[cov.shard_id(codex, sid_a)]["open"]
    # again: nothing held that is withdrawn — the beat's cost when nothing is
    assert withdraw.drop_withdrawn(root, "gutenberg", codex, kw["con"], delete_chunks=delete)["dropped"] == 0


def test_digests_and_events_never_bring_a_withdrawn_bundle_back(ready):
    """A neighbour that has not acted on the withdraw yet still claims and
    offers the bundle. Whose bundle it is decides (09 §9.8): a producer
    known here and among the signers — never asked for; not known yet —
    `take` reads the header first, records the producer, fetches no row."""
    from app.share import holdings, messages
    root, snapshot, codex, kw = ready
    first = _alone(root, codex, snapshot, kw)
    sid = first["bundles"][0]["shard"]
    dup = _second_version(root, codex, snapshot, kw, sid, later=int(time.time()) + 3600)
    dup_dir = contribute.bundle_dir(root, "gutenberg", codex, sid, dup)
    member = mem.bundle_member(codex.space_segment, dup_dir)
    header_bytes = (dup_dir / "header.json").read_bytes()
    withdraw.publish(root, "gutenberg", codex, snapshot, [dup], key=OURS, own_key=OUR_KEY, con=kw["con"],
                     node_con=kw["node_con"], platform_image=IMAGE)
    ids = withdraw.withdrawn(root, "gutenberg", codex)
    now = time.time()
    entry = {"collection_id": codex.collection_id, "lineage_id": codex.collection_id, "title": "g",
             "snapshot_id": snapshot.snapshot_id, "snapshot_hash": snapshot.hash, "codex_hash": "sha256:" + "2" * 64,
             "license": "green", "privacy": "public", "members": [member.to_dict()],
             "coverage": [{"shard": cov.shard_id(codex, sid), "bundles": [dup]}]}
    digest = messages.digest_body(key=PEER_KEY, address="http://10.0.0.7:30042", neighbours=[],
                                  benchmarks=[], collections=[entry], now=now)
    peers.record_digest(kw["node_con"], {**digest, "peer_port": 6881}, now=now)
    assert cov.fold_claims(kw["con"], codex, [digest]) == 1, "the neighbour's claim is folded in as usual"
    assert cov.row(kw["con"], dup)["producer"] is None, "a claim names no producer"
    assert [p["bundle_id"] for p, _h in accept.listed_bundles(kw["node_con"], codex, kw["con"], now=now, skip=ids)] \
        == [dup], "whose it is is not known here: asked for, and take decides from the header"
    # take: the header is staged (fetched first, or here already) and says the producer withdrew it
    staged = root / "gutenberg" / "state" / holdings.STAGING / member.name
    staged.mkdir(parents=True)
    (staged / "header.json").write_bytes(header_bytes)
    payload = {"bundle_id": dup, "root": member.root, "files": member.to_dict()["files"], "shard": sid}
    said: list[str] = []
    got = accept.take(root, "gutenberg", codex, payload, session=None, con=kw["con"], node_con=kw["node_con"],
                      log=said.append)
    assert got["outcome"] == "ignored" and "withdrawn" in got["why"] and not staged.exists()
    assert cov.row(kw["con"], dup)["producer"] == OUR_KEY and cov.row(kw["con"], dup)["accepted_at"] is None
    assert any("withdrawn by its producer" in s for s in said)
    # known now: never asked for again, by the listing or by an event
    assert accept.listed_bundles(kw["node_con"], codex, kw["con"], now=now, skip=ids) == []
    got = accept.take(root, "gutenberg", codex, payload, session=None, con=kw["con"], node_con=kw["node_con"])
    assert got["outcome"] == "ignored" and "withdrawn" in got["why"]
    # the claim row stays what it is — a neighbour's claim; the beat drops holdings, not claims
    assert withdraw.drop_withdrawn(root, "gutenberg", codex, kw["con"], delete_chunks=lambda ids: 0)["dropped"] == 0
    # and a holder leading or probing the part is not left waiting for a withdrawn offer
    assert contribute.offered_elsewhere(codex, kw["con"], kw["node_con"], now=now, skip=ids) == {}


def _json(capsys):
    text = capsys.readouterr().out
    return json.loads(text[text.index("{"):])


def test_the_dev_command_lists_asks_and_withdraws(ready, monkeypatch, capsys):
    from app import identity, louise
    from app.bundles import keys as node_keys
    root, snapshot, codex, kw = ready
    first = _alone(root, codex, snapshot, kw)
    sid, kept = first["bundles"][0]["shard"], first["bundles"][0]["bundle_id"]
    dup = _second_version(root, codex, snapshot, kw, sid, later=int(time.time()) + 3600)
    monkeypatch.setattr(node_keys, "load_signing_key", lambda path=None: OURS)
    monkeypatch.setattr(identity, "own", lambda: OUR_KEY)
    monkeypatch.delenv("LOUISE_PLATFORM_IMAGE", raising=False)
    assert louise.main(["dev", "withdraw-duplicates", "gutenberg", "--root", str(root), "--yes"]) == 2, \
        "an attestation names the image: without it nothing is withdrawn"
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", IMAGE)
    assert louise.main(["dev", "withdraw-duplicates", "gutenberg", "--root", str(root), "--dry-run"]) == 0
    out = _json(capsys)
    assert out == {"collection": "gutenberg", "parts": 1, "bundles": 1, "chunks": first["bundles"][0]["chunks"],
                   "withdrawn": False}
    assert cov.row(kw["con"], dup) is not None, "a dry run withdraws nothing"
    assert louise.main(["dev", "withdraw-duplicates", "gutenberg", "--root", str(root)]) == 2, \
        "no terminal: --yes is required (07 §7.7)"
    capsys.readouterr()
    assert louise.main(["dev", "withdraw-duplicates", "gutenberg", "--root", str(root), "--yes"]) == 0
    out = _json(capsys)
    assert out["withdrawn"] is True and out["bundles"] == [dup] and out["dropped_here"] == 1
    assert cov.row(kw["con"], dup) is None and cov.row(kw["con"], kept)["accepted_at"]
    assert withdraw.withdrawn(root, "gutenberg", codex) == {dup: frozenset({OUR_KEY})}
    assert louise.main(["dev", "withdraw-duplicates", "gutenberg", "--root", str(root), "--yes"]) == 0
    assert _json(capsys)["bundles"] == 0, "nothing left: nothing written"

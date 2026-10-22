"""END TO END (04 §4.5, amended 2026-10-22, operator): a same-space re-mint
keeps every bundle as coverage. gutenberg's parser-10 re-mint re-cut the
parts, changed some records, and the node's held path then bundled and
signed as its own 117 parts every record of which the predecessor's bundles
already held. Here, on one fixture:

- P (the predecessor) holds this peer's bundles of every part and another
  producer's bundle of one part;
- N is P re-minted in the SAME space (another parser), its parts re-cut
  (one record a part where P had two) and one record changed upstream;
  `carry --rebuilt --copy`, the build, the snapshot, the reconcile.

Then: the changed record's old vectors are gone; P's bundles are N's
coverage by possession for every unchanged record; the held path bundles
the changed record alone and re-signs nothing; N's digest offers P's
bundles under N's parts (a bundle spanning two re-cut parts under both); a
FRESH peer with no carry admits P's other-producer bundle without its
part number mattering, keys it under P's id and counts it by possession;
a bundle of another space is never keyed under a foreign id; and the
predecessor producer's withdraw still applies to its bundle id."""
import json
import shutil
from dataclasses import replace

import pytest

from app import config, db, embeddings
from app.hold import carry, collection as col, export as exp, land as landing, records as rec, \
    snapshot as sn, vectors_build as vb
from app.hold.codex import Parser
from app.share import accept, contribute, coverage as cov, gossip, holdings, members as mem, possession, \
    rendezvous, withdraw as wd
from tests.share.test_contribute import attested, ready  # noqa: F401
from tests.share.test_messages import PEER, PEER_KEY
from tests.share.test_share_api import OURS, OUR_KEY

pytestmark = pytest.mark.strict_encoder


def _possession(root, name, codex, sid, bundle_id):
    d = contribute.bundle_dir(root, name, codex, sid, bundle_id)
    return json.loads((d / "possession.json").read_bytes())


def _take(root, name, codex, bundle_dir, *, con, node_con, kw, collection_id, sid):
    """Stage a bundle's four files as a fetch would leave them, and take it in."""
    member = mem.bundle_member(codex.space_segment, bundle_dir)
    staged = root / name / "state" / holdings.STAGING / member.name
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundle_dir, staged)
    payload = {"collection": collection_id, "space_id": codex.space_id, "snapshot_id": "2026-09",
               "shard": sid, "uids": [], "bundle_id": "sha256:" + bundle_dir.name, "root": member.root,
               "bytes": member.bytes, "files": member.to_dict()["files"]}
    return accept.take(root, name, codex, payload, session=None, con=con, node_con=node_con,
                       vectors_for=kw["vectors_for"], write=kw["write"], key=OURS, own_key=OUR_KEY)


def test_a_same_space_remint_keeps_every_bundle_as_coverage(ready, corpus_dir, tmp_path, monkeypatch):
    root, snapshot, codex, kw = ready
    monkeypatch.setattr(config, "VECTORS_MIN_FREE_GB", 0.0)
    monkeypatch.setattr(config, "VECTORS_MIN_FREE_FRACTION", 0.0)
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", "sha256:" + "1f" * 32)
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)

    # ---- P: every vector here, bundled by this peer; another producer's bundle of part 0
    vb.build(codex, shards=list(snapshot.shards),
             records_for=lambda s: landing.records_for(root, "gutenberg", snapshot, f"{s.slice}/{s.n:05d}", None),
             vectors_for=kw["vectors_for"], embed=kw["embed"], write=kw["write"], batch=2)
    live = [rendezvous.Peer(OUR_KEY, 1000.0)]
    p_out = contribute.once(root, "gutenberg", codex, live=live, **kw)
    assert p_out["accepted"], p_out
    s0 = snapshot.shards[0]
    sid0 = f"{s0.slice}/{s0.n:05d}"
    peer = exp.export(root, "gutenberg", shard=sid0, dest=tmp_path / "peer", vectors_for=kw["vectors_for"],
                      embed=kw["embed"], key=PEER, snapshot_id=snapshot.snapshot_id)["bundles"][0]
    peer_dir = tmp_path / "peer-named" / peer["bundle_id"].split(":", 1)[1]
    peer_dir.parent.mkdir()
    shutil.move(peer["path"], peer_dir)
    took = _take(root, "gutenberg", codex, peer_dir, con=kw["con"], node_con=kw["node_con"], kw=kw,
                 collection_id=codex.collection_id, sid=sid0)
    assert took["outcome"] == "accepted", took
    peer_uids = set(_possession(root, "gutenberg", codex, sid0, peer["bundle_id"]))
    assert len(peer_uids) >= 2, "the fixture: part 0 holds two records"

    # ---- N: the same space re-minted, its parts re-cut, one record changed upstream
    n_codex = col.create(root, replace(codex, parser=Parser("gutenberg", 99)), name="re")
    assert n_codex.space_id == codex.space_id and n_codex.collection_id != codex.collection_id
    shutil.copytree(root / "gutenberg" / "shard" / "calibration", root / "re" / "shard" / "calibration",
                    dirs_exist_ok=True)
    rec.write_validation(root, "re")
    if kw["con"].execute("SELECT 1 FROM space").fetchone() is None:     # what the worker's first write pins
        kw["con"].execute("INSERT INTO space(id, space_id, dim, pinned_at) VALUES(1, ?, ?, 0)",
                          (codex.space_id, codex.space.dim))
        kw["con"].commit()
    carry.carry(root, "gutenberg", "re", rebuilt=True, copy=True, apply=True, log=lambda *_: None)
    victim = next(u for s in snapshot.shards for u, _ in possession.part_records(root, "gutenberg", snapshot, s)
                  if u not in peer_uids)      # a record of another part: part 0's bundles stay whole
    book = corpus_dir / victim.split(":")[1]
    txt = next(book.glob("*.txt*"))
    data = txt.read_bytes()
    i = data.index(b"*** END OF")
    txt.write_bytes(data[:i] + b"A paragraph revised upstream.\n\n" + data[i:])
    n_local = rec.local_collection(root, "re", n_codex)
    n_con = db.connect(n_local)
    n_con.execute("UPDATE documents SET mtime=mtime+1 WHERE uid=?", (victim,))
    n_con.commit()
    rec.build(root, "re", corpus_dir=corpus_dir, log=lambda *_: None, records_per_shard=1)
    rec.write_validation(root, "re")
    n_snap = sn.create(root, "re", snapshot_id="2026-09", log=lambda *_: None)
    assert len(n_snap.shards) > len(snapshot.shards), "the parts were re-cut"
    out = carry.reconcile(root, "gutenberg", "re", log=lambda *_: None)
    assert out["reset"] == 1, out
    held_ids = embeddings.chunk_ids_of_books([int(victim.split(":")[1])], collection=n_local)
    assert held_ids == [], "the changed record's old vectors are dropped (the 133 on gutenberg)"
    assert contribute.predecessors(root, "re") == [codex.collection_id]

    # ---- P's bundles are N's coverage by possession, for every unchanged record
    for s in n_snap.shards:   # the embed stage's count, which vb.build does not write (one record a part)
        (u, _h), = possession.part_records(root, "re", n_snap, s)
        n_con.execute("UPDATE documents SET chunks=? WHERE uid=? AND uid<>?", (s.chunks, u, victim))
    n_con.commit()
    union = possession.covered(root, "re", n_codex, n_con, n_snap)
    victim_part = next(cov.shard_id(n_codex, f"{s.slice}/{s.n:05d}") for s in n_snap.shards
                       if victim in {u for u, _ in possession.part_records(root, "re", n_snap, s)})
    assert union[victim_part] == 0, "the changed record: covered by nobody"
    assert all(v > 0 for k, v in union.items() if k != victim_part), "every other part: covered by P's bundles"

    # ---- the held path bundles the changed record alone; nothing re-signed
    n_kw = {**kw, "con": n_con,
            "vectors_for": lambda ids: embeddings.vectors_by_chunk_id(ids, collection=n_local),
            "write": lambda rows: embeddings.upsert_chunks(rows, collection=n_local, dtype=n_codex.space.dtype)}
    vb.build(n_codex, shards=list(n_snap.shards),
             records_for=lambda s: [r for r in landing.records_for(root, "re", n_snap, f"{s.slice}/{s.n:05d}", None)
                                    if r["uid"] == victim],
             vectors_for=n_kw["vectors_for"], embed=n_kw["embed"], write=n_kw["write"], batch=2)
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, n_codex, n_snap, check=check, key=key, name="re")
    n_out = contribute.once(root, "re", n_codex, live=live, **n_kw)
    made = n_out["bundles"]
    assert made, n_out
    for b in made:
        assert set(_possession(root, "re", n_codex, b["shard"], b["bundle_id"])) == {victim}, \
            "only the changed record is bundled; every other record is P's bundles' already"

    # ---- N's digest offers P's bundles under N's parts; a spanning one under both
    entries = {e["shard"]: set(e["bundles"]) for e in gossip.coverage_of(root, "re")}
    assert all(s.startswith(n_codex.collection_id) for s in entries), "never under P's ids"
    listed = [s for s, ids in entries.items() if peer["bundle_id"] in ids]
    assert len(listed) == len(peer_uids), "the other producer's two-record bundle: under both re-cut parts"
    spanning = [b for b in {x for ids in entries.values() for x in ids}
                if sum(b in ids for ids in entries.values()) > 1]
    assert spanning, "a bundle of a two-record P part covers two re-cut N parts"

    # ---- a FRESH peer (no carry) admits P's other-producer bundle, keys it under P, counts it by possession
    fetcher = tmp_path / "fetcher" / "collections"
    fetcher.mkdir(parents=True)
    col.write(fetcher, n_codex)
    shutil.copytree(root / "re" / "shard", col.shard_dir_of(fetcher, "re"), dirs_exist_ok=True)
    f_local = rec.local_collection(fetcher, "re", n_codex)
    f_con = db.connect(f_local)
    f_kw = {**kw, "vectors_for": lambda ids: embeddings.vectors_by_chunk_id(ids, collection=f_local),
            "write": lambda rows: embeddings.upsert_chunks(rows, collection=f_local, dtype=n_codex.space.dtype)}
    taken = _take(fetcher, "re", n_codex, contribute.bundle_dir(root, "gutenberg", codex, sid0, peer["bundle_id"]),
                  con=f_con, node_con=kw["node_con"], kw=f_kw, collection_id=n_codex.collection_id, sid=sid0)
    assert taken["outcome"] == "accepted", taken
    row = cov.row(f_con, peer["bundle_id"])
    assert row["shard"].startswith(codex.collection_id + "/"), "keyed under its own collection's id"
    assert contribute.predecessors(fetcher, "re") == [codex.collection_id]
    held = possession.index(fetcher, "re", n_codex, f_con)     # its manifest is empty: count records, not chunks
    got = set().union(*(possession.covered_uids(held, possession.part_records(fetcher, "re", n_snap, s))
                        for s in n_snap.shards))
    assert got == peer_uids, "counted by possession on a peer that never carried"

    # (ii) a bundle of another space is never keyed under a foreign id
    other = {"collection": codex.collection_id, "space_id": "sha256:" + "ee" * 32, "shard": sid0}
    assert accept._row_shard(n_codex, other) == (cov.shard_id(n_codex, sid0), None)

    # (iii) the predecessor producer's withdraw still applies to its bundle id
    wd.publish(fetcher, "re", n_codex, n_snap, [peer["bundle_id"]], key=PEER, own_key=PEER_KEY, con=f_con,
               node_con=kw["node_con"], platform_image="sha256:" + "1f" * 32)
    assert cov.row(f_con, peer["bundle_id"]) is None, "withdrawn by its producer: gone here"
    f_con.close()
    n_con.close()

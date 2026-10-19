"""The worker's publisher (app/worker.py `_publisher`; 07 §7.8 "all p2p",
2026-10-19): where the mode embeds, every collection this worker runs
under `embed = "p2p"` gets a publish-only contribute pass — the open parts
whose vectors the node holds, bundled and announced — one collection at a
time, bounded by a budget and resumable. Until then publishing was the
`share contribute` command's alone, which cannot run while the worker
holds the collection's lock, so a node published only paused."""
import time
from dataclasses import replace

from app import collections as collections_mod, db, encoder_use, identity, node_state, worker
from app.bundles import keys as node_keys
from app.hold import land as landing, records as rec, vectors_build as vb
from app.share import peers
from tests.share.test_contribute import OURS, OUR_KEY, PEER, attested, ready  # noqa: F401


def _set_mode(local, mode: str) -> None:
    """The file's mode as the worker applies it to the runtime scope at boot
    (invariant 10, `_apply_tier2_embed_mode`): the beat reads the scope."""
    con = db.connect(local)
    scope = db.get_scope(con)
    scope["embed_mode"] = mode
    db.set_scope(con, scope)
    con.close()


def _running(ready, monkeypatch, *, mode="p2p", share=True, node_mode="running"):
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    local = replace(rec.local_collection(root, "gutenberg", codex), embed_mode_default=mode, share=share)
    _set_mode(local, mode)
    monkeypatch.setenv("LOUISE_ROOT", str(root.parent))
    monkeypatch.setattr(collections_mod, "load_all", lambda *a, **k: [local])
    monkeypatch.setattr(collections_mod, "recipe_for", lambda c: object())
    monkeypatch.setattr(encoder_use, "callables", lambda codex_, recipe, **k: (kw["embed"], None))
    monkeypatch.setattr(node_keys, "load_signing_key", lambda *a, **k: OURS)
    monkeypatch.setattr(identity, "own", lambda: OUR_KEY)
    # this peer with a benchmark for the space and no neighbour: it leads every open part
    from app.share import coverage as cov, rendezvous
    monkeypatch.setattr(cov, "live_peers", lambda codex_, node_con, **k: [rendezvous.Peer(OUR_KEY, 1000.0)])
    node_state.set_worker_state(node_mode)
    worker._locks.clear()
    worker._locks["gutenberg"] = object()
    worker._publish_state.clear()
    worker._publish_said.clear()
    return root, snapshot, codex, kw, local


def test_the_beat_publishes_what_the_embed_stage_finished_and_only_under_p2p(ready, monkeypatch):
    root, snapshot, codex, kw, local = _running(ready, monkeypatch)
    shards = [f"{s.slice}/{s.n:05d}" for s in snapshot.shards]
    vb.build(codex, shards=[snapshot.shards[0]],
             records_for=lambda s: landing.records_for(root, "gutenberg", snapshot, f"{s.slice}/{s.n:05d}", None),
             vectors_for=kw["vectors_for"], embed=kw["embed"], write=kw["write"], batch=2)
    try:
        done = worker._publisher_beat()
        assert list(done) == ["gutenberg"]
        out = done["gutenberg"]
        assert out["built"] == 0 and out["held"] == [shards[0]] and out["awaiting"] == shards[1:]
        assert out["events"] == 1, "share = true and the mode seeds: announced"
        with peers.connect() as node_con:      # the node's store under LOUISE_ROOT, as the worker opens it
            assert len(peers.to_forward(node_con)) == 1
        state = worker._publish_state["gutenberg"]
        assert (state["bundled"], state["awaiting"], state["announced"], state["interrupted"]) == (1, len(shards) - 1, 1, False)
        events = [r["message"] for r in db.connect(local).execute(
            "SELECT message FROM events WHERE message LIKE '%publish:%' ORDER BY id")]
        assert any("1 part(s) bundled from vectors held here" in m for m in events), events
        # all local: the embed stage embeds everything itself and nothing is published by this thread
        _set_mode(local, "all")
        assert worker._publisher_beat() == {}
        # a mode that does not embed publishes nothing
        _set_mode(local, "p2p")
        node_state.set_worker_state("seed-only")
        assert worker._publisher_beat() == {}
        # a collection this worker does not run (another writer holds it) is left alone
        node_state.set_worker_state("running")
        worker._locks.clear()
        assert worker._publisher_beat() == {}
    finally:
        worker._locks.clear()


def test_a_pass_yields_at_its_budget_and_a_refusal_is_said_once(ready, monkeypatch):
    root, snapshot, codex, kw, local = _running(ready, monkeypatch)
    vb.build(codex, shards=list(snapshot.shards),
             records_for=lambda s: landing.records_for(root, "gutenberg", snapshot, f"{s.slice}/{s.n:05d}", None),
             vectors_for=kw["vectors_for"], embed=kw["embed"], write=kw["write"], batch=2)
    try:
        # a budget already spent: the pass stops before its first shard and says so
        done = worker._publisher_beat(budget_s=-1.0)
        assert done["gutenberg"]["interrupted"] is True and done["gutenberg"]["bundles"] == []
        assert worker._publish_state["gutenberg"]["interrupted"] is True
        # the next beat resumes and finishes
        done = worker._publisher_beat()
        assert len(done["gutenberg"]["accepted"]) == len(snapshot.shards)
        # a refusal (no attestation of a check) is one log line, not one a beat
        from app.share import contribute
        monkeypatch.setattr(contribute, "preconditions", lambda *a, **k: ["roots: not attested"])
        worker._publisher_beat()
        worker._publisher_beat()
        rows = [r["message"] for r in db.connect(local).execute(
            "SELECT message FROM events WHERE message LIKE '%not this beat%'")]
        assert len(rows) == 1 and "roots: not attested" in rows[0]
    finally:
        worker._locks.clear()


def test_the_deadline_stop_is_a_stop(monkeypatch):
    import threading
    ev = threading.Event()
    u = worker._Until(time.monotonic() + 60, ev)
    assert not u.is_set()
    ev.set()
    assert u.is_set()
    assert worker._Until(time.monotonic() - 1, threading.Event()).is_set()

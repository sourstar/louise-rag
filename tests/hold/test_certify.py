"""tests/hold/test_certify.py — `hold collection certify` (07 §7.3; 03
§3.7; 09 §9.11): the certifier's signature in the sidecar, the pointer
signed under its key with a seq that only grows, and the refusals — a
key that is not the certifier's, an open collection, a snapshot not
held."""
import json

import pytest
from nacl import signing

from app.hold import anchor, certify, envelope as env, snapshot as sn
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.hold.conftest import KEY, a_root, pubmed
from tests.hold.test_anchor import T0, a_signed_drag, certified

OTHER = signing.SigningKey(b"\x09" * 32)


def test_certify_signs_the_sidecar_and_writes_the_next_pointer(node):  # noqa: F811
    codex = certified()
    a_root(node, codex)
    name, d, snap, _ = a_signed_drag(node, codex)
    (d / "snapshots/2026-08.sig.json").unlink()                 # start unsigned
    out = certify.certify(node, name, codex, "2026-08", KEY, now=T0)
    assert (out["snapshot_id"], out["snapshot_hash"], out["seq"], out["signatures"]) == ("2026-08", snap.hash, 1, 1)
    doc = json.loads((d / "snapshots/2026-08.sig.json").read_bytes())
    assert sn.sidecar_keys(doc, snap.hash)[0] == [env.key_text(KEY.verify_key)]
    assert anchor.certification(node, name, codex, snap, now=T0)["certified_by"] == env.key_text(KEY.verify_key)
    pointer = certify.read_pointer(node, name, codex)
    body = certify.verify_pointer(pointer, codex)
    assert (body["kind"], body["version"], body["collection_id"], body["snapshot_id"], body["snapshot_hash"],
            body["seq"], body["key"]) == ("louise-pointer", 1, codex.collection_id, "2026-08", snap.hash, 1,
                                          env.key_text(KEY.verify_key))
    assert certify.certify(node, name, codex, "2026-08", KEY, now=T0 + 60)["seq"] == 2, "seq only grows"
    assert certify.read_pointer(node, name, codex)["body"]["seq"] == 2
    doc = json.loads((d / "snapshots/2026-08.sig.json").read_bytes())
    assert len(doc["signatures"]) == 2, "a sidecar is a set: the second signature is merged, not replaced"


def test_the_refusals(node):  # noqa: F811
    codex = certified()
    a_root(node, codex)
    name, d, snap, _ = a_signed_drag(node, codex)
    with pytest.raises(certify.CertifyError, match="not the Codex's certifier.key") as e:
        certify.certify(node, name, codex, "2026-08", OTHER, now=T0)
    assert e.value.exit_code == 2
    with pytest.raises(certify.CertifyError, match="no snapshot 2026-09"):
        certify.certify(node, name, codex, "2026-09", KEY, now=T0)
    with pytest.raises(certify.CertifyError, match="open collection"):
        certify.certify(node, name, pubmed(), "2026-08", KEY, now=T0)
    assert certify.read_pointer(node, name, codex) is None
    # a pointer signed by another key, or for another collection, does not read
    certify.certify(node, name, codex, "2026-08", KEY, now=T0)
    p = certify.pointer_path(node, name, codex)
    doc = json.loads(p.read_bytes())
    forged = env.sign(env.POINTER, dict(doc["body"], key=env.key_text(OTHER.verify_key)), OTHER)
    p.write_bytes(json.dumps(forged).encode())
    assert certify.read_pointer(node, name, codex) is None
    with pytest.raises(certify.CertifyError, match="certifier key"):
        certify.verify_pointer(forged, codex)


def test_the_pointer_is_served_at_the_well_known_path(node, monkeypatch):  # noqa: F811
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from app import api, collections as collections_mod
    from app.hold import drag as drag_mod
    codex = certified()
    a_root(node, codex)
    name, d, snap, _ = a_signed_drag(node, codex)
    monkeypatch.setattr(collections_mod, "louise_root", lambda: node.parent)
    monkeypatch.setattr(collections_mod, "load_all", lambda **kw: [SimpleNamespace(name=name, codex=codex)])
    client = TestClient(api.app)
    hexid = drag_mod._hex(codex.collection_id)
    assert client.get(f"/.well-known/louise-pointer/{hexid}.json").status_code == 404, "not certified yet"
    certify.certify(node, name, codex, "2026-08", KEY, now=T0)
    r = client.get(f"/.well-known/louise-pointer/{hexid}.json")
    assert r.status_code == 200 and certify.verify_pointer(r.json(), codex)["seq"] == 1
    assert client.get("/.well-known/louise-pointer/nope.json").status_code == 404


def test_the_worker_refreshes_a_certified_collection_s_anchor_when_due(node, monkeypatch):  # noqa: F811
    import sqlite3
    from types import SimpleNamespace
    from app import collections as collections_mod, worker
    from app.hold import anchor
    codex = certified()
    a_root(node, codex)
    name, d, snap, _ = a_signed_drag(node, codex)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts INTEGER, level TEXT, stage TEXT, message TEXT, "
                "doc_uid TEXT)")
    monkeypatch.setattr(worker.db, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(collections_mod, "louise_root", lambda: node.parent)
    calls = []
    monkeypatch.setattr(anchor, "refresh", lambda root, n, c, **kw: calls.append(n) or
                        {"state": "anchored", "why": "ok"})
    c = SimpleNamespace(name=name, codex=codex)
    monkeypatch.setattr(worker.node_state, "worker_state", lambda: "seed-only")
    assert worker._refresh_anchor(con, c) is False and calls == [], "seeding only fetches nothing"
    monkeypatch.setattr(worker.node_state, "worker_state", lambda: "acquire-only")
    assert worker._refresh_anchor(con, c) is True and calls == [name]
    assert worker._refresh_anchor(con, SimpleNamespace(name="g", codex=pubmed())) is False, "open: no anchor"

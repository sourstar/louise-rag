"""tests/hold/test_forgery.py — a proof of forgery (03 §3.5; 09 §9.8): the
attestation a reproduced T4 miss writes — both vectors by hash over their
big-endian bytes, the text unless the collection is red, the encoder that
recomputed — and the burn it records in the trust file, which the trust
level then counts against the key."""
import json

import numpy as np
from nacl import signing

from app.hold import codex as cx, envelope as env, forgery, snapshot as sn, trust_file
from app.hold.codex import License
from app.share import attest, levels, messages
from tests.hold.conftest import pubmed

KEY = signing.SigningKey(b"\x07" * 32)
NOW = 1_800_000_000.0
PRODUCER = "ed25519:" + "a" * 64


def a_snapshot(codex):
    return sn.Snapshot(collection_id=codex.collection_id, snapshot_id="2026-08", previous=None, shards=(),
                       delta=sn.Delta((), (), ()), files={})


def prove(root, codex, **over):
    fields = dict(chunk_id="pubmed:1:0:1", producer=PRODUCER, bundle="sha256:" + "b" * 64, text="SECRET",
                  claimed=np.array([0.6, 0.8], dtype=np.float32),
                  recomputed=np.array([1.0, 0.0], dtype=np.float32), content_hash="c" * 32)
    fields.update(over)
    return forgery.prove(root, "pubmed", codex, a_snapshot(codex), KEY, platform_image="sha256:" + "1" * 64,
                         now=NOW, **fields)


def test_the_proof_names_both_vectors_by_hash_the_encoder_and_the_text_unless_red(tmp_path):
    codex = pubmed()                                    # red
    root = tmp_path / "louise" / "collections"
    aid, doc = prove(root, codex)
    body = env.verify(env.ATTESTATION, doc)
    assert (body["check"], body["result"], body["shards"], body["seed"]) == ("forgery", "forgery", [], None)
    assert body["snapshot_id"] == "2026-08" and body["key"] == env.key_text(KEY.verify_key)
    kind = ">f2" if codex.space.dtype == "f16" else ">f4"
    d = body["detail"]
    assert d["claimed"] == cx.digest(np.array([0.6, 0.8], dtype=np.float32).astype(kind).tobytes())
    assert d["recomputed"] == cx.digest(np.array([1.0, 0.0], dtype=np.float32).astype(kind).tobytes())
    assert d["claimed"] != d["recomputed"]
    assert (d["chunk_id"], d["producer"], d["bundle"]) == ("pubmed:1:0:1", PRODUCER, "sha256:" + "b" * 64)
    assert d["text"] is None and d["content_hash"] == "c" * 32, "red: the content hash stands in for the text"
    engine_key = "encoder_engine" if "encoder_engine" in body else "encoder_image"
    assert body[engine_key] and body["encoder_weights"] == codex.space.encoder.weights, \
        "the proof names the encoder that recomputed (09 §9.8, amended)"
    assert env.body_id(body) == aid
    p = attest.attestation_path(root, "pubmed", codex, aid)
    assert p.is_file() and json.loads(p.read_bytes()) == doc

    green = pubmed(license=License("green", None))
    _, doc = prove(root, green)
    assert env.verify(env.ATTESTATION, doc)["detail"]["text"] == "SECRET", "green: the text travels"


def test_a_burn_is_recorded_once_in_the_trust_file_and_ends_only_by_restore(tmp_path):
    root = tmp_path / "louise"
    assert trust_file.read(root) == {"kind": "louise-trust", "version": 1, "allow": [], "deny": [], "quarantined": []}
    doc = trust_file.burn(PRODUCER, "sha256:" + "f" * 64, root=root, now=NOW)
    assert doc["quarantined"] == [{"key": PRODUCER, "attestation": "sha256:" + "f" * 64,
                                   "time": messages.time_text(NOW)}]
    trust_file.burn(PRODUCER, "sha256:" + "e" * 64, root=root, now=NOW + 5)
    assert len(trust_file.read(root)["quarantined"]) == 1, "a second proof against a burned key changes nothing"
    assert trust_file.path(root) == root / "state" / "trust.json"
    assert json.loads(trust_file.path(root).read_text(encoding="utf-8"))["kind"] == "louise-trust"
    assert trust_file.as_filter(root) == {"allow": set(), "deny": set(), "burn": {PRODUCER}}
    assert trust_file.restore(PRODUCER, root=root) is True
    assert trust_file.as_filter(root)["burn"] == set()
    assert trust_file.restore(PRODUCER, root=root) is False
    trust_file.path(root).write_text("{not json", encoding="utf-8")
    assert trust_file.read(root)["quarantined"] == [], "a file that does not read trusts nothing by accident"


def test_a_burned_key_s_attestations_stop_counting(tmp_path):
    codex = pubmed()
    snap = a_snapshot(codex)
    body = {"collection_id": codex.collection_id, "snapshot_hash": snap.hash, "check": "roots", "result": "pass",
            "key": PRODUCER, "time": messages.time_text(NOW)}
    assert levels.counting(codex, snap, [body], now=NOW) == [body]
    root = tmp_path / "louise"
    trust_file.burn(PRODUCER, None, root=root, now=NOW)
    assert levels.counting(codex, snap, [body], now=NOW, trust=trust_file.as_filter(root)) == []

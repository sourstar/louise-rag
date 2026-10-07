"""tests/hold/test_anchor.py — the certifier's anchor (03 §3.7; 09 §9.11):
the document parsed and refused, fetched over TLS without redirects and
kept; `anchored` · `stale` · `mismatch` · `unknown`; the periodic fetch
due a day plus jitter later and an hour after a failure; which sidecar
signatures a certifier key made within its validity; the certification a
result carries; and the roots the manifest-only gate admits."""
import json
import sqlite3

import httpx
import pytest

from app.hold import anchor, drag as drag_mod, envelope as env, snapshot as sn, collection as col
from app.hold.codex import Certifier, canonical_json
from app.hold.drag import MemberFile
from app.share import holdings
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.hold.conftest import KEY, NOW, a_root, pubmed

CERT_KEY = env.key_text(KEY.verify_key)
URL = "https://gazzetta.example/.well-known/louise-certifier.json"
T0 = 1_790_000_000.0                                   # 2026-09-20T…Z, after NOW's signature


def certified(**over):
    return pubmed(publication="certified", content="manifest-only",
                  certifier=Certifier(CERT_KEY, URL, False), **over)


def a_document(codex, *, key=CERT_KEY, valid_from="2026-01-01T00:00:00Z", revoked_at=None, collections=None):
    return {"kind": "louise-certifier", "version": 1,
            "keys": [{"key": key, "valid_from": valid_from, "revoked_at": revoked_at}],
            "collections": [codex.collection_id] if collections is None else collections}


def a_signed_drag(node, codex):
    """A held drag with one shard and a snapshot the certifier (KEY) signed at NOW."""
    name = col.name_of(node, codex.collection_id)
    d = col.drag_of(node, name)
    data = b"r" * 20_000
    (d / "records/year=2023").mkdir(parents=True)
    (d / "records/year=2023/00001.jsonl.zst").write_bytes(data)
    shard = sn.Shard(slice="year=2023", n=1, root=drag_mod.pieces_root(data), records_root="sha256:" + "11" * 32,
                     first_id="a", last_id="z", records=2, chunks=4, chunks_root="sha256:" + "22" * 32,
                     tokens=2048, bytes=len(data))
    snap = sn.Snapshot(collection_id=codex.collection_id, snapshot_id="2026-08", previous=None, shards=(shard,),
                       delta=sn.Delta((), (), ()), files={})
    (d / "snapshots").mkdir(exist_ok=True)
    (d / "snapshots/2026-08.json").write_bytes(snap.canonical())
    (d / "snapshots/2026-08.sig.json").write_bytes(canonical_json(sn.sidecar_add(None, snap.hash, KEY, now=NOW)))
    return name, d, snap, data


class _Client:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.urls = answer, error, []

    def get(self, url):
        self.urls.append(url)
        if self.error:
            raise self.error
        status, body = self.answer
        return httpx.Response(status, content=body)


def test_the_document_is_parsed_and_refused(node):  # noqa: F811
    codex = certified()
    doc = anchor.parse(a_document(codex))
    assert doc["keys"][0]["key"] == CERT_KEY and doc["collections"] == [codex.collection_id]
    for bad in ({"kind": "x"}, {**a_document(codex), "version": 2}, {**a_document(codex), "keys": "k"},
                a_document(codex, valid_from="yesterday"), {**a_document(codex), "collections": [1]}):
        with pytest.raises(anchor.AnchorError) as e:
            anchor.parse(bad)
        assert e.value.exit_code == 2
    assert anchor.valid_keys_at(doc, "2026-06-01T00:00:00Z") == {CERT_KEY}
    assert anchor.valid_keys_at(doc, "2025-06-01T00:00:00Z") == set(), "before valid_from"
    revoked = anchor.parse(a_document(codex, revoked_at="2026-03-01T00:00:00Z"))
    assert anchor.valid_keys_at(revoked, "2026-02-01T00:00:00Z") == {CERT_KEY}
    assert anchor.valid_keys_at(revoked, "2026-04-01T00:00:00Z") == set(), "after revoked_at"


def test_fetched_kept_and_judged(node, monkeypatch):  # noqa: F811
    codex = certified()
    a_root(node, codex)
    name = col.name_of(node, codex.collection_id)
    assert anchor.status(node, name, codex, now=T0)["state"] == "unknown"
    assert anchor.due(node, name, codex, now=T0)
    client = _Client(answer=(200, json.dumps(a_document(codex)).encode()))
    out = anchor.refresh(node, name, codex, now=T0, client=client)
    assert client.urls == [URL] and out["state"] == "anchored" and out["age_s"] == 0
    assert anchor.state_path(node, name, codex).is_file()
    assert not anchor.due(node, name, codex, now=T0 + 3600)
    assert anchor.due(node, name, codex, now=T0 + anchor.REFRESH_S + anchor.JITTER_S), "a day plus jitter later"
    # unreachable: the last document stands, the try is noted, an hour later it is due again
    out = anchor.refresh(node, name, codex, now=T0 + 90_000, client=_Client(error=httpx.ConnectError("down")))
    assert out["state"] == "anchored" and out["fetched_at"] == T0
    assert anchor.read_state(node, name, codex)["error"].startswith(URL)
    assert not anchor.due(node, name, codex, now=T0 + 90_000 + 60)
    assert anchor.due(node, name, codex, now=T0 + 90_000 + anchor.RETRY_S)
    assert anchor.status(node, name, codex, now=T0 + anchor.STALE_S + 1)["state"] == "stale"
    # a document that does not list the key, or the collection: mismatch
    anchor.refresh(node, name, codex, now=T0, client=_Client(
        answer=(200, json.dumps(a_document(codex, key="ed25519:" + "ab" * 32)).encode())))
    assert anchor.status(node, name, codex, now=T0)["state"] == "mismatch"
    anchor.refresh(node, name, codex, now=T0, client=_Client(
        answer=(200, json.dumps(a_document(codex, collections=["sha256:" + "0" * 64])).encode())))
    assert "this collection" in anchor.status(node, name, codex, now=T0)["why"]
    # HTTP errors and non-documents
    with pytest.raises(anchor.Unreachable) as e:
        anchor.fetch(codex, client=_Client(answer=(503, b"")))
    assert e.value.exit_code == 3
    with pytest.raises(anchor.AnchorError, match="not JSON"):
        anchor.fetch(codex, client=_Client(answer=(200, b"<html>")))
    assert anchor.status(node, name, pubmed(), now=T0)["state"] is None, "an open collection has no anchor"
    assert not anchor.due(node, name, pubmed(), now=T0)


def test_which_signatures_certify_and_what_a_result_carries(node):  # noqa: F811
    codex = certified()
    a_root(node, codex)
    name, d, snap, _ = a_signed_drag(node, codex)
    # no anchor document yet: the Codex's key alone counts
    assert [s["key"] for s in anchor.certifier_signatures(node, name, codex, snap)] == [CERT_KEY]
    assert anchor.certification(node, name, codex, snap, now=T0) == {"certified_by": CERT_KEY, "anchor_state": "unknown"}
    anchor.record(node, name, codex, document=anchor.parse(a_document(codex)), now=T0)
    assert anchor.certification(node, name, codex, snap, now=T0) == {"certified_by": CERT_KEY, "anchor_state": "anchored"}
    # the key's validity starts after the signature: it certifies nothing
    anchor.record(node, name, codex, document=anchor.parse(a_document(codex, valid_from="2027-01-01T00:00:00Z")),
                  now=T0)
    assert anchor.certifier_signatures(node, name, codex, snap) == []
    assert anchor.certification(node, name, codex, snap, now=T0) == {"certified_by": None, "anchor_state": "anchored"}
    # revoked before the signature: the same
    anchor.record(node, name, codex, document=anchor.parse(a_document(codex, revoked_at="2026-01-02T00:00:00Z")),
                  now=T0)
    assert anchor.certification(node, name, codex, snap, now=T0)["certified_by"] is None
    assert anchor.certification(node, name, pubmed(), snap, now=T0) is None, "open: null"
    assert anchor.certification(node, name, codex, None, now=T0)["certified_by"] is None


def test_the_manifest_only_gate_admits_only_roots_a_certifier_signed(node):  # noqa: F811
    codex = certified()
    a_root(node, codex)
    name, d, snap, data = a_signed_drag(node, codex)
    roots = anchor.signed_roots(node, name, codex)
    assert snap.shards[0].root in roots and len(roots) >= 1
    (d / "snapshots/2026-08.sig.json").unlink()
    assert anchor.signed_roots(node, name, codex) == set(), "unsigned: nothing is admitted"
    # promotion with the gate: a foreign root is set aside, a listed one promoted
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    staging = node / name / "state" / holdings.STAGING
    foreign = b"f" * 20_000
    f = MemberFile("records/year=2023/00002.jsonl.zst", drag_mod.pieces_root(foreign), len(foreign))
    staged = staging / "records-year=2023" / f.path
    staged.parent.mkdir(parents=True)
    staged.write_bytes(foreign)
    with pytest.raises(holdings.Refused, match="03 §3.8"):
        holdings.promote(con, staged, d, snapshot_id="2026-08", member="records-year=2023", f=f,
                         allowed_roots=roots)
    assert not staged.exists() and (staging / holdings.REFUSED / "records-year=2023" / f.path).is_file()
    assert not (d / f.path).exists() and holdings.marks(con) == {}
    good = MemberFile("records/year=2023/00001.jsonl.zst", snap.shards[0].root, len(data))
    (d / good.path).unlink()
    staged = staging / "records-year=2023" / good.path
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(data)
    assert holdings.promote(con, staged, d, snapshot_id="2026-08", member="records-year=2023", f=good,
                            allowed_roots=roots) == d / good.path

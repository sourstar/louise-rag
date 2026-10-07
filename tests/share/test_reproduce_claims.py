"""tests/share/test_reproduce_claims.py — 03 §3.6: another key's
deterministic failure is acted on after local reproduction — the named
check on the named shard, once per claim (app/share/accept.py)."""
import sqlite3
from types import SimpleNamespace

from app.share import accept, levels


class Report:
    def __init__(self, result):
        self.result = result

    def outcome(self, check):
        return SimpleNamespace(result=self.result)


def test_each_claim_is_run_once_on_its_shard_and_its_outcome_recorded(tmp_path, monkeypatch):
    from app import copy_state
    from app.hold import equivalence
    shard_path = "records/lang=en/00001.jsonl.zst"
    (tmp_path / "g" / "drag" / "records" / "lang=en").mkdir(parents=True)
    (tmp_path / "g" / "drag" / shard_path).write_bytes(b"x")
    codex = SimpleNamespace(collection_id="sha256:" + "c" * 64)
    c = SimpleNamespace(name="g", codex=codex, corpus_dir=tmp_path / "origin", source="origin")
    (tmp_path / "origin").mkdir()
    monkeypatch.setattr(copy_state, "of", lambda col: SimpleNamespace(in_use=True))
    monkeypatch.setattr(copy_state, "attestations", lambda root, name, cx: [])
    monkeypatch.setattr(equivalence, "reference_images", lambda bodies, key, cx: set())
    claims = [{"check": "chunks", "shard": "sha256:" + "c" * 64 + "/lang=en/00001", "key": "ed25519:" + "a" * 64},
              {"check": "records-vs-origin", "shard": "lang=en/00009", "key": "ed25519:" + "b" * 64},
              {"check": "roots", "shard": None, "key": "ed25519:" + "a" * 64}]
    monkeypatch.setattr(levels, "claims", lambda cx, bodies, **kw: claims)
    monkeypatch.setattr(accept, "_current_snapshot_shards",
                        lambda root, name: [SimpleNamespace(slice="lang=en", n=1, path=shard_path)])
    ran = []

    def run(args):
        ran.append((args.checks, args.shards, args.origin_dir))
        return None, None, None, None, Report("fail")
    con = sqlite3.connect(":memory:")
    out = accept.reproduce_claims(tmp_path, [c], con, own_key="ed25519:" + "f" * 64, run=run)
    assert ran == [("chunks", "lang=en/00001", None)], "only the claim whose shard is held runs, on it alone"
    assert out == {"reproduced": 1, "not_reproduced": 0, "cannot": 2}
    outcomes = dict(con.execute("SELECT attestation_id, outcome FROM claims_acted").fetchall())
    assert sorted(outcomes.values()) == ["cannot: no shard named", "cannot: shard not held here", "reproduced"]
    assert accept.reproduce_claims(tmp_path, [c], con, own_key="ed25519:" + "f" * 64, run=run) == \
        {"reproduced": 0, "not_reproduced": 0, "cannot": 0} and len(ran) == 1, "each claim acted on once"

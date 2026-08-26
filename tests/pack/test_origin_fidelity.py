"""CORPUS FIDELITY (CLAUDE.md invariant 11) — a pruned corpus must stay
verifiable against the original form.

The failure this guards against is quiet and permanent. A pack whose
records each hash correctly, whose shards each hash correctly and whose
manifest is validly signed can still be impossible to check against the
source it claims to come from — if nothing ties a record to the origin
BYTES it was parsed from. `origin_hash` is that tie, `records-vs-origin`
is the check it exists for, and a pack is published once and adopted by
peers who cannot repair it. So the absence is a build-time refusal, not a
warning, and `pack verify` reports it locally without needing the network.
"""
from pathlib import Path

import pytest

from app import db, plugin_io
from app.pack import build as pack_build, manifest as manifest_mod, records, verify

from test_build import _discovered      # tests/pack is on the path, not a package


def _pack(tmp_path, corpus_dir, name="gutenberg"):
    origin = _discovered(tmp_path, name, "gutenberg", corpus_dir)
    pack_dir = tmp_path / "packs" / name
    result = pack_build.build(origin, pack_dir, snapshot="2026-08-26",
                              records_per_shard=2, sign=False, log=lambda *_: None)
    return origin, pack_dir, result


def _all_records(pack_dir: Path):
    m = manifest_mod.load(pack_dir)
    for s in m["shards"]:
        yield from records.iter_records(pack_dir / s["path"])


def test_every_packed_record_is_tied_to_its_origin_bytes(isolated_config, tmp_path,
                                                          corpus_dir):
    """Pruning is allowed — no epub reaches the pack — but every surviving
    record still names the hash of the bytes it was parsed from."""
    _, pack_dir, result = _pack(tmp_path, corpus_dir)
    packed = list(_all_records(pack_dir))
    assert len(packed) == result.records
    for rec in packed:
        assert rec.get("origin_hash"), f"{rec['uid']} has no origin_hash"
        assert len(rec["origin_hash"]) == 32          # blake2b digest_size=16


def test_the_origin_hash_is_the_origins_hash_not_a_restatement(isolated_config,
                                                                tmp_path,
                                                                corpus_dir):
    """It must be the hash of the ORIGIN's raw bytes — the same value the
    origin collection computes — or it proves nothing about the origin. A
    record that hashed its own packed text would satisfy a naive check and
    be worthless."""
    origin, pack_dir, _ = _pack(tmp_path, corpus_dir)
    con = db.connect(origin)
    plugin = origin.plugin_instance()
    try:
        want = {}
        for row in con.execute("SELECT * FROM documents WHERE error IS NULL"):
            doc = plugin_io.canonical_doc(plugin, row)
            want[doc.uid] = doc.raw_hash
    finally:
        con.close()
    for rec in _all_records(pack_dir):
        assert rec["origin_hash"] == want[rec["uid"]]
        assert rec["origin_hash"] != rec["content_hash"]


def test_a_plugin_that_cannot_name_its_origin_is_refused_not_packed(
        isolated_config, tmp_path, corpus_dir, monkeypatch):
    """The refusal is the point. Writing nulls would produce a pack that
    passes every internal check and can never be verified against the
    source — and it would be discovered only by whoever tried, long after
    it was published."""
    origin = _discovered(tmp_path, "gutenberg", "gutenberg", corpus_dir)
    real = plugin_io.canonical_doc

    def blind(plugin, row):
        doc = real(plugin, row)
        return doc.__class__(**{**doc.__dict__, "raw_hash": None})

    monkeypatch.setattr(pack_build.plugin_io, "canonical_doc", blind)
    with pytest.raises(ValueError, match="raw_hash|origin"):
        pack_build.build(origin, tmp_path / "packs" / "blind",
                         snapshot="2026-08-26", sign=False, log=lambda *_: None)


def test_verify_reports_a_record_with_no_origin_hash(isolated_config, tmp_path,
                                                      corpus_dir):
    """Local, deterministic and cheap — no network, no re-fetch. The
    absence of the link is detectable long before anyone runs
    records-vs-origin against the origin itself."""
    _, pack_dir, _ = _pack(tmp_path, corpus_dir)
    assert all(r["ok"] for r in verify.run(pack_dir, checks=("records",)))

    m = manifest_mod.load(pack_dir)
    shard = pack_dir / m["shards"][0]["path"]
    stripped = []
    for rec in records.iter_records(shard):
        rec = dict(rec)
        rec["origin_hash"] = None
        stripped.append(rec)
    records.write_shard(shard, stripped)   # 'roots' is not among the checks

    # sample=1.0: verify samples 5% of shards by default, which need not
    # include the one just stripped
    report = verify.run(pack_dir, checks=("records",), sample=1.0)
    failed = [r for r in report if not r["ok"]]
    assert failed, report
    assert any("origin_hash" in r["detail"] for r in failed), failed

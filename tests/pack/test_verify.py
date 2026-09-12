"""`pack verify`: the deterministic checks pass on a freshly built pack,
name exactly what was tampered with, and never execute anything."""
import json

from app import db, discovery, collections as collections_mod
from app.pack import build as pack_build, manifest as manifest_mod, records, verify


def _pack(tmp_path, corpus_dir):
    d = tmp_path / "g"
    c = collections_mod.Collection(
        name="g", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus_dir, manifest_path=d / "manifest.db", fts_dir=d / "fts", lance_dir=d / "lancedb")
    con = db.connect(c)
    discovery.sync_collection(con, c)
    con.close()
    pack_dir = tmp_path / "packs" / "g"
    pack_build.build(c, pack_dir, records_per_shard=1, sign=False, log=lambda *_: None)
    return pack_dir


def test_a_fresh_pack_passes_every_check(isolated_config, tmp_path, corpus_dir):
    pack_dir = _pack(tmp_path, corpus_dir)
    report = verify.run(pack_dir, sample=1.0)
    assert {r["check"] for r in report} == set(verify.ALL_CHECKS)
    assert all(r["ok"] for r in report), report
    by = {r["check"]: r for r in report}
    assert by["signature"]["detail"] == "unsigned manifest"
    assert by["roots"]["sampled"] == 3 and by["records"]["sampled"] == 3
    assert "3 record(s)" in by["chunks"]["detail"]


def test_tampering_is_named_by_the_check_that_sees_it(isolated_config, tmp_path, corpus_dir):
    pack_dir = _pack(tmp_path, corpus_dir)
    m = manifest_mod.load(pack_dir)
    shard = pack_dir / m["shards"][0]["path"]
    # rewrite the shard with a changed record: roots fails (bytes), records fails (hash)
    recs = list(records.iter_records(shard))
    # long enough that the chunker yields a DIFFERENT NUMBER of chunks, which
    # is what the chunks check can still see. It used to be caught by setting
    # the record's own `chunks` to 99, but a record asserting its own count
    # was checking the writer against itself; §9.5 points the check at the
    # count the SHARD publishes, so the tamper has to move the real count.
    recs[0]["sections"][0][1] = "tampered text " * 4000
    records.write_shard(shard, recs)
    by = {r["check"]: r for r in verify.run(pack_dir, sample=1.0)}
    assert not by["roots"]["ok"] and "sha256 mismatch" in by["roots"]["detail"]
    assert not by["records"]["ok"] and "content_hash" in by["records"]["detail"]
    assert not by["chunks"]["ok"] and "shard says" in by["chunks"]["detail"]
    assert by["license"]["ok"], "green: no terms to check"
    # a subset of checks, from the plan file
    (pack_dir / "validation.json").write_text(json.dumps({"checks": ["license"], "sample": 1.0}))
    assert [r["check"] for r in verify.run(pack_dir)] == ["license"]


def test_license_terms_must_match_byte_for_byte(isolated_config, tmp_path, corpus_dir):
    pack_dir = _pack(tmp_path, corpus_dir)
    m = manifest_mod.load(pack_dir)
    (pack_dir / "TERMS.txt").write_bytes(b"terms\n")
    m["license"]["terms_sha256"] = "00" * 32
    manifest_mod.write(pack_dir, m)
    by = {r["check"]: r for r in verify.run(pack_dir, checks=["license"])}
    assert not by["license"]["ok"]
    (pack_dir / "TERMS.txt").unlink()
    by = {r["check"]: r for r in verify.run(pack_dir, checks=["license"])}
    assert "missing" in by["license"]["detail"]

"""Genesis import — published vector archives admitted as same-function
compatible data (architecture-v2.md §8, amendment 10). Admission, not trust:
possession-scoped, chunk-shape-matched, sample re-encoded against the
collection's own encoder, refused whole on failure, cursor-resumable.
"""
import json

import numpy as np
import pytest

from app import collections as collections_mod, config, db, embeddings
from app.bundles import genesis, genesis_fetch
from app.bundles.genesis import GenesisArchive, GenesisError, GenesisUnavailable

DIM = 8


def _unit(seed: str) -> np.ndarray:
    rng = np.random.default_rng(abs(hash(seed)) % (2**32))
    v = rng.normal(size=DIM)
    return (v / np.linalg.norm(v)).astype(np.float32)


class FakePlugin:
    """Protocol-only plugin: one section per doc, text derived from the id."""
    source_id = "fake"

    def fetch(self, ref):
        from app.plugins.base import RawDoc
        return RawDoc(ref=ref, raw_hash="h", data=ref.native_id.encode())

    def parse(self, raw):
        from app.plugins.base import CanonicalDoc
        nid = raw.data.decode()
        return CanonicalDoc(
            uid=f"fake:{nid}", title=f"doc {nid}", creators=[], language="en",
            date=None, subjects=[], keys=[], extra={},
            sections=[("Abstract", f"abstract text of document {nid}")],
            raw_hash="h")


@pytest.fixture
def collection(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setitem(collections_mod.PLUGIN_REGISTRY, "fake", FakePlugin())
    c = collections_mod.Collection(
        name="fake", plugin="fake", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb",
        embed=collections_mod.EmbedRecipe(
            url="http://fake-encoder:1", api="openai", model="fake-encoder",
            dtype="f16", ctx=512, dim=DIM))
    con = db.connect(c)
    for i in range(1, 21):
        con.execute(
            "INSERT INTO documents(uid,source,native_id,path,lang,in_scope,"
            "rdf_done,fts_done,embed_done) VALUES(?,?,?,?,?,1,1,1,0)",
            (f"fake:{i}", "fake", str(i), f"/x/{i}", "en"))
    return c


def _archive_vector(i: int) -> np.ndarray:
    """What the archive holds — by construction identical to what the 'local
    encoder' fake produces for the doc's text (same function)."""
    return _unit(f"abstract text of document {i}")


def _write_medcpt_shard(cache: dict, shard: str, ids, scale=1.0):
    vectors = np.stack([_archive_vector(i) * scale for i in ids])
    cache[f"embeds_chunk_{shard}.npy"] = vectors
    cache[f"pmids_chunk_{shard}.json"] = [str(i) for i in ids]


def _fake_fetch(cache: dict):
    def fetch(name, dest):
        if name not in cache:
            raise GenesisUnavailable(f"archive missing {name}")
        value = cache[name]
        if name.endswith(".npy"):
            np.save(dest.with_suffix(""), value)   # np.save appends .npy
        else:
            dest.write_text(json.dumps(value), encoding="utf-8")
    return fetch


def _same_function_embed(texts, **kwargs):
    return [_unit(t).tolist() for t in texts]


def _different_model_embed(texts, **kwargs):
    return [_unit("other-model:" + t).tolist() for t in texts]


def _archive(shards):
    return GenesisArchive(archive_id="test-arc", base_url="http://arc",
                           adapter="medcpt-npy", shards=tuple(shards),
                           uid_prefix="fake:")


def test_import_applies_held_docs_and_is_resumable(collection):
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 11))
    _write_medcpt_shard(cache, "1", list(range(11, 16)) + [999])   # 999 not held

    archive = _archive(["0", "1"])
    result = genesis.import_archive(con, collection, archive,
                                     embed_fn=_same_function_embed,
                                     fetch=_fake_fetch(cache))
    assert result["docs"] == 15 and result["chunks"] == 15
    shard1 = [r for r in result["shards"] if r["shard"] == "1"][0]
    assert shard1["skipped_absent"] == 1

    # vectors landed, f16, unit-norm; embed_done set
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    assert tbl.count_rows() == 15
    import pyarrow as pa
    assert pa.types.is_float16(tbl.schema.field("vector").type.value_type)
    done = con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0]
    assert done == 15

    # resume: both shards short-circuit, nothing re-fetched
    def exploding_fetch(name, dest):
        raise AssertionError("applied shard must not re-fetch")
    again = genesis.import_archive(con, collection, archive,
                                    embed_fn=_same_function_embed,
                                    fetch=exploding_fetch)
    assert all(r["state"] == "already-applied" for r in again["shards"])


def test_incompatible_archive_is_refused_whole(collection):
    """The same-function proof: a well-formed archive from a DIFFERENT model
    must not land a single row."""
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 11))
    with pytest.raises(GenesisError, match="NOT produced by this"):
        genesis.import_shard(con, collection, _archive(["0"]), "0",
                              embed_fn=_different_model_embed,
                              fetch=_fake_fetch(cache))
    assert embeddings.TABLE not in embeddings._conn(collection).table_names()
    assert con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0] == 0
    state = con.execute("SELECT state, detail FROM genesis_imports").fetchone()
    assert state["state"] == "rejected" and "cos" in state["detail"]


def test_endpoint_down_is_retryable_not_a_verdict(collection):
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 11))

    def down(texts, **kwargs):
        raise ConnectionError("no encoder")
    with pytest.raises(GenesisUnavailable):
        genesis.import_shard(con, collection, _archive(["0"]), "0",
                              embed_fn=down, fetch=_fake_fetch(cache))
    assert con.execute("SELECT state FROM genesis_imports").fetchone()["state"] == "skipped"

    # the same shard imports cleanly once the encoder is back
    result = genesis.import_shard(con, collection, _archive(["0"]), "0",
                                   embed_fn=_same_function_embed,
                                   fetch=_fake_fetch(cache))
    assert result["state"] == "applied" and result["docs"] == 10


def test_zero_or_nonfinite_vectors_reject_the_shard(collection):
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 6), scale=0.0)   # all-zero vectors
    with pytest.raises(GenesisError, match="zero"):
        genesis.import_shard(con, collection, _archive(["0"]), "0",
                              embed_fn=_same_function_embed,
                              fetch=_fake_fetch(cache))


def test_dim_mismatch_rejects(collection):
    con = db.connect(collection)
    vectors = np.stack([np.ones(4, dtype=np.float32) for _ in range(3)])
    cache = {"embeds_chunk_0.npy": vectors,
             "pmids_chunk_0.json": ["1", "2", "3"]}
    with pytest.raises(GenesisError, match="dim"):
        genesis.import_shard(con, collection, _archive(["0"]), "0",
                              embed_fn=_same_function_embed,
                              fetch=_fake_fetch(cache))


def test_worker_drains_a_declared_archive_automatically(collection, monkeypatch):
    """The default-on path: genesis_archive in the TOML → the worker imports
    a shard per round with no operator action, backs off on unavailability,
    and never retries a rejected (wrong-encoder) archive."""
    from dataclasses import replace
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 11))
    archive = _archive(["0"])
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", archive)
    declared = replace(collection, genesis_archive="test-arc")

    calls = []

    def fake_embed(texts, timeout=300, recipe=None, query=False):
        calls.append(len(texts))
        return _same_function_embed(texts)
    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)

    # amendment 38: the embed thread NEVER downloads — a shard that is not
    # on disk is not drained (the fetcher's thread lands it first)
    assert genesis.drain_next_shard(con, declared) is None
    landed = genesis_fetch.run_fetcher(con, declared, fetch=_fake_fetch(cache))
    assert landed["fetched"] == ["0"]

    result = genesis.drain_next_shard(con, declared)
    assert result is not None and result["docs"] == 10
    assert calls, "the same-function proof ran through the collection recipe"

    # all shards applied + nothing to embed -> nothing more to do
    assert genesis.drain_next_shard(con, declared) is None

    # undeclared collections never touch archives
    assert genesis.drain_next_shard(con, collection) is None


def test_drain_never_retries_a_rejected_archive(collection, monkeypatch):
    from dataclasses import replace
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 11))
    _write_medcpt_shard(cache, "1", range(11, 21))
    archive = _archive(["0", "1"])
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", archive)
    monkeypatch.setattr(
        embeddings, "embed_texts",
        lambda texts, timeout=300, recipe=None, query=False:
            _different_model_embed(texts))
    declared = replace(collection, genesis_archive="test-arc")

    fetches = []

    def counting_fetch(name, dest):
        fetches.append(name)
        _fake_fetch(cache)(name, dest)
    genesis_fetch.run_fetcher(con, declared, fetch=counting_fetch)
    assert genesis.drain_next_shard(con, declared) is None   # rejected inside
    assert con.execute("SELECT state FROM genesis_imports WHERE shard='0'").fetchone()["state"] \
        == "rejected"

    fetches.clear()
    assert genesis.drain_next_shard(con, declared) is None
    # ...and the FETCHER stops too: one rejected shard proves the archive
    # is not this collection's function — the other 36 must not download
    assert genesis_fetch.next_shard_to_fetch(con, archive) is None
    genesis_fetch.run_fetcher(con, declared, fetch=counting_fetch)
    assert fetches == [], "a rejected archive must never be downloaded further"


def test_multi_chunk_documents_are_skipped_not_half_embedded(collection, monkeypatch):
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 6))

    plugin = collections_mod.PLUGIN_REGISTRY["fake"]
    original_parse = plugin.parse

    def two_chunk_parse(raw):
        doc = original_parse(raw)
        if doc.uid == "fake:3":
            from dataclasses import replace
            return replace(doc, sections=[("A", "x " * 3000), ("B", "y " * 3000)])
        return doc
    monkeypatch.setattr(plugin, "parse", two_chunk_parse)

    result = genesis.import_shard(con, collection, _archive(["0"]), "0",
                                   embed_fn=_same_function_embed,
                                   fetch=_fake_fetch(cache))
    assert result["docs"] == 4 and result["skipped_shape"] == 1
    row = con.execute("SELECT embed_done FROM documents WHERE uid='fake:3'").fetchone()
    assert row["embed_done"] == 0, "half-covered documents must stay unembedded"


# --- amendment 38: fetched like a corpus, applied as indexing goes ----------

def _declared_archive(shards, ids_per_shard=10, shard_bytes=0):
    """A fake archive with DECLARED coverage: shard i holds ids
    [i*10, i*10+9], like MedCPT's exact PMID ranges (1e6 per shard)."""
    return GenesisArchive(archive_id="test-arc", base_url="http://arc",
                           adapter="medcpt-npy", shards=tuple(shards),
                           uid_prefix="fake:", ids_per_shard=ids_per_shard,
                           shard_bytes=shard_bytes)


def test_declared_coverage_names_a_documents_shard_from_its_id():
    arc = _declared_archive(["0", "1", "2"])
    assert arc.id_range_of("1") == (10, 19)
    assert arc.shard_of_id(0) == "0" and arc.shard_of_id(19) == "1"
    assert arc.shard_of_id(30) is None, "past the archive: a post-snapshot document"
    assert genesis.MEDCPT_PUBMED.shard_of_id(36_999_999) == "36"
    assert genesis.MEDCPT_PUBMED.shard_of_id(37_000_000) is None
    assert genesis.MEDCPT_PUBMED.id_range_of("5") == (5_000_000, 5_999_999)
    assert _archive(["0"]).shard_of_id(5) is None, "undeclared coverage: unknown"


def test_fetcher_lands_shards_under_the_archive_class_and_records_them(
        collection, monkeypatch):
    """Every byte draws from the bucket under Priority.ARCHIVE (below the
    corpus), the row records size/digest/id range, and the per-collection
    download split sees the bytes."""
    from dataclasses import replace
    from app.transfer.bucket import Priority
    from app.transfer.manager import current_handle
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 10))
    _write_medcpt_shard(cache, "1", range(10, 20))
    archive = _declared_archive(["0", "1"])
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", archive)
    declared = replace(collection, genesis_archive="test-arc")

    seen = []

    def fetch(name, dest):
        h = current_handle()
        seen.append((name, h.priority if h else None))
        _fake_fetch(cache)(name, dest)
    from app import stats
    stats.recorder().reset() if hasattr(stats.recorder(), "reset") else None
    result = genesis_fetch.run_fetcher(con, declared, fetch=fetch)
    assert result["fetched"] == ["0", "1"]
    assert seen and all(p is Priority.ARCHIVE for _, p in seen), seen
    fetched_bytes = stats.recorder().snapshot().get("fake", {}).get("fetch", {}).get("totals", {}).get("bytes", 0)
    assert fetched_bytes > 0, "the per-collection download split sees archive bytes"
    assert Priority.BACKFILL < Priority.ARCHIVE < Priority.SEEDING
    rows = {r["shard"]: r for r in con.execute(
        "SELECT * FROM genesis_imports ORDER BY shard")}
    assert rows["0"]["state"] == "fetched" and rows["0"]["bytes"] > 0
    assert rows["0"]["content_digest"] and rows["0"]["next_pass_at"] == 0
    assert (rows["1"]["id_min"], rows["1"]["id_max"]) == (10, 19)
    # nothing left to fetch; the files are on disk
    assert genesis_fetch.next_shard_to_fetch(con, archive) is None
    for _, path in genesis_fetch.shard_files(
            archive, genesis_fetch.cache_dir(declared, archive), "0"):
        assert path.is_file()


def test_fetcher_is_bounded_by_the_cache_and_the_license(collection, monkeypatch):
    from dataclasses import replace
    from app import config as config_mod
    from app.acquire.spec import LicenseAckRequiredError, LicenseClass
    from app.acquire import specs as specs_mod
    con = db.connect(collection)
    cache = {}
    for i in range(3):
        _write_medcpt_shard(cache, str(i), range(i * 10, i * 10 + 10))
    archive = _declared_archive(["0", "1", "2"], shard_bytes=1000)
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", archive)
    declared = replace(collection, genesis_archive="test-arc")

    # cache bound: shards on disk that are not final count against it
    monkeypatch.setattr(config_mod, "GENESIS_CACHE_BYTES", 1500)
    first = genesis_fetch.run_fetcher(con, declared, fetch=_fake_fetch(cache))
    assert first["fetched"] == ["0"] and first["reason"] == "cache full", first
    resident = genesis_fetch.resident_bytes(con, archive)
    assert resident > 0
    monkeypatch.setattr(config_mod, "GENESIS_CACHE_BYTES", resident + 1000)
    second = genesis_fetch.run_fetcher(con, declared, fetch=_fake_fetch(cache))
    assert second["fetched"] == ["1"]

    # license: a yellow-class collection without an ack fetches nothing
    # (amendment 36 — the archive is USE of the corpus)
    yellow = specs_mod.ACQUIRE_REGISTRY["pubmed"]
    assert yellow.license_class is LicenseClass.YELLOW
    monkeypatch.setitem(specs_mod.ACQUIRE_REGISTRY, "fake", yellow)
    with pytest.raises(LicenseAckRequiredError):
        genesis_fetch.run_fetcher(con, replace(declared, license_ack=None),
                                  fetch=_fake_fetch(cache))


def test_fetcher_defers_an_unreachable_origin_and_resumes(collection, monkeypatch):
    import time as _time
    from dataclasses import replace
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 10))
    archive = _declared_archive(["0"])
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", archive)
    declared = replace(collection, genesis_archive="test-arc")

    def down(name, dest):
        raise GenesisUnavailable("origin unreachable")
    result = genesis_fetch.run_fetcher(con, declared, fetch=down)
    assert result["fetched"] == [] and result["reason"] == "deferred"
    row = con.execute("SELECT state, detail FROM genesis_imports").fetchone()
    assert row["state"] == "skipped" and "retryable" in row["detail"]
    # inside the backoff: not retried; past it: fetched
    assert genesis_fetch.next_shard_to_fetch(con, archive) is None
    later = int(_time.time()) + genesis_fetch.RETRY_AFTER_S + 1
    assert genesis_fetch.next_shard_to_fetch(con, archive, now=later) == "0"
    con.execute("UPDATE genesis_imports SET updated_at=updated_at-?",
                (genesis_fetch.RETRY_AFTER_S + 1,))
    assert genesis_fetch.run_fetcher(con, declared, fetch=_fake_fetch(cache))["fetched"] == ["0"]
    # a deferral never wrote an error onto a document
    assert con.execute("SELECT COUNT(*) FROM documents WHERE error IS NOT NULL").fetchone()[0] == 0


def test_a_fetched_shard_applies_now_revisits_and_finalises(collection, monkeypatch):
    """The old post-settle gate is gone: with most of the collection still
    indexing, a shard on disk applies for the documents that ARE indexed,
    stays on disk while documents in its range wait for fts (revisited
    with a per-shard backoff that doubles after an empty pass), and goes
    FINAL — files deleted — once none is waiting."""
    from dataclasses import replace
    from app import config as config_mod
    con = db.connect(collection)
    # ids 1..20 exist; shard "0" covers 0..9, shard "1" covers 10..19
    # (20 lies past the archive: a post-snapshot document)
    con.execute("UPDATE documents SET fts_done=0, rdf_done=0 "
                "WHERE CAST(native_id AS INTEGER) >= 5")
    db.ensure_browse_index(con)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 10))
    _write_medcpt_shard(cache, "1", range(10, 20))
    archive = _declared_archive(["0", "1"])
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", archive)
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, timeout=300, recipe=None, query=False:
                            _same_function_embed(texts))
    declared = replace(collection, genesis_archive="test-arc")
    genesis_fetch.run_fetcher(con, declared, fetch=_fake_fetch(cache))

    # pass 1: applies for the 4 indexed documents (1..4) despite the backlog
    r1 = genesis.drain_next_shard(con, declared)
    assert r1["shard"] == "0" and r1["docs"] == 4 and r1["state"] == "applied"
    assert r1["pending"] == 5, "ids 5..9 are held and still waiting for fts"
    row0 = con.execute("SELECT * FROM genesis_imports WHERE shard='0'").fetchone()
    assert row0["state"] == "applied" and row0["pending"] == 5
    base = config_mod.GENESIS_MAX_WAIT_SEC
    assert row0["next_pass_at"] - row0["updated_at"] == base
    files0 = [p for _, p in genesis_fetch.shard_files(
        archive, genesis_fetch.cache_dir(declared, archive), "0")]
    assert all(p.is_file() for p in files0), "a shard with pending documents stays on disk"

    # no embed-eligible document at all -> the archive has nothing to do
    # this round (no shard is read for nothing)
    assert genesis.drain_next_shard(con, declared) is None
    # id 20 (past the archive) clears fts: a candidate exists, so the
    # fetched-never-applied shard "1" gets its first pass (nothing indexed
    # in its range: imports 0, backs off) — then nothing is due until the
    # backoff passes
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1 WHERE native_id='20'")
    r2 = genesis.drain_next_shard(con, declared)
    assert r2["shard"] == "1" and r2["docs"] == 0 and r2["pending"] == 10
    row1 = con.execute("SELECT * FROM genesis_imports WHERE shard='1'").fetchone()
    assert row1["next_pass_at"] - row1["updated_at"] == base
    assert genesis.drain_next_shard(con, declared) is None, "both shards inside their backoff"

    # an EMPTY pass doubles the wait; a productive one resets it (the
    # clock moves past the shard's next_pass_at: shift its row back)
    con.execute("UPDATE genesis_imports SET updated_at=updated_at-?, "
                "next_pass_at=next_pass_at-? WHERE shard='1'", (base + 1, base + 1))
    r3 = genesis.drain_next_shard(con, declared)
    assert r3["shard"] == "1" and r3["docs"] == 0
    row1 = con.execute("SELECT * FROM genesis_imports WHERE shard='1'").fetchone()
    assert row1["next_pass_at"] - row1["updated_at"] == 2 * base

    # indexing reaches ids 5..9: shard 0 revisits, imports them, and is FINAL
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1 "
                "WHERE CAST(native_id AS INTEGER) BETWEEN 5 AND 9")
    con.execute("UPDATE genesis_imports SET updated_at=updated_at-?, next_pass_at=next_pass_at-? WHERE shard='0'", (4 * base, 4 * base))
    r4 = genesis.drain_next_shard(con, declared)
    assert r4["shard"] == "0" and r4["docs"] == 5 and r4["state"] == "final"
    row0 = con.execute("SELECT * FROM genesis_imports WHERE shard='0'").fetchone()
    assert row0["state"] == "final" and row0["docs"] == 9 and row0["bytes"] == 0
    assert not any(p.is_file() for p in files0), "final: files deleted"
    assert con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0] == 9
    # a final shard is never walked again, even when asked
    assert genesis.import_shard(con, declared, archive, "0", fetch=_fake_fetch(cache),
                                revisit=True)["state"] == "already-applied"
    # the fetcher may now land the next shard in the freed cache slot
    assert genesis_fetch.resident_bytes(con, archive) == \
        con.execute("SELECT bytes FROM genesis_imports WHERE shard='1'").fetchone()[0]


def test_a_remote_shard_without_declared_coverage_counts_pending_by_probe(
        collection, monkeypatch):
    from dataclasses import replace
    con = db.connect(collection)
    con.execute("UPDATE documents SET fts_done=0 WHERE CAST(native_id AS INTEGER) > 6")
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 11))
    archive = _archive(["0"])          # no ids_per_shard
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", archive)
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, timeout=300, recipe=None, query=False:
                            _same_function_embed(texts))
    declared = replace(collection, genesis_archive="test-arc")
    genesis_fetch.run_fetcher(con, declared, fetch=_fake_fetch(cache))
    r = genesis.drain_next_shard(con, declared)
    assert r["docs"] == 6 and r["pending"] == 4, "ids 7..10 held, waiting for fts"


def test_worker_starts_the_archive_fetcher_beside_the_corpus_fetch(collection, monkeypatch):
    """Amendment 38: a remote archive is the collection's second
    acquisition stream — started where the corpus fetch is, under the
    same opt-in and worker state; a local archive or an undeclared
    collection starts nothing; restarts are throttled unless a shard just
    went final (a cache slot freed)."""
    import threading
    from dataclasses import replace
    from app import worker
    started = []
    monkeypatch.setattr(worker, "_archive_worker",
                        lambda c, reason: started.append((c.name, reason)))
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", _declared_archive(["0"]))
    worker._archive_threads.clear()
    worker._archive_last_kick.clear()
    worker._paused.clear()
    con = db.connect(collection)

    remote = replace(collection, genesis_archive="test-arc", acquire_enabled=True)
    worker._acquire_archive(con, remote, "startup")
    for t in list(worker._archive_threads.values()):
        t.join(2)
    assert started == [("fake", "startup")]

    # throttled: the round-by-round kick does not restart it for a while
    worker._acquire_archive(con, remote, "round")
    assert len(started) == 1
    # ...unless a shard went final and freed a cache slot
    worker._acquire_archive(con, remote, "shard final", force=True)
    for t in list(worker._archive_threads.values()):
        t.join(2)
    assert len(started) == 2

    # opt-in and state gates
    worker._archive_last_kick.clear()
    worker._acquire_archive(con, replace(remote, acquire_enabled=False), "startup")
    worker._acquire_archive(con, replace(remote, genesis_archive=None), "startup")
    worker._acquire_archive(con, replace(remote, genesis_archive="upstash-wikipedia-2024-06"),
                            "startup")
    worker._paused.set()
    try:
        worker._acquire_archive(con, remote, "startup")
    finally:
        worker._paused.clear()
    assert len(started) == 2, "acquisition off / no archive / local archive / paused start nothing"


def test_a_budgeted_pass_pauses_commits_and_resumes(collection, monkeypatch):
    """A remote shard's first pass can hold the collection's own thread
    (its rdf/fts) for hours. It is cut at GENESIS_PASS_BUDGET_S, commits
    what it imported, is NOT final (coverage unknown), and resumes after
    an equal share — skipping what it already imported. A pass interrupted
    by a restart (row left `applying`) resumes the same way."""
    import time as _time
    from dataclasses import replace
    from app import config as config_mod
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 21))          # all 20 held and indexed
    archive = _declared_archive(["0"], ids_per_shard=100)
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", archive)
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, timeout=300, recipe=None, query=False:
                            _same_function_embed(texts))
    declared = replace(collection, genesis_archive="test-arc")
    genesis_fetch.run_fetcher(con, declared, fetch=_fake_fetch(cache))

    # a budget the clock has already spent: the pass stops at once,
    # having imported nothing — nothing lost, nothing marked final
    monkeypatch.setattr(config_mod, "GENESIS_PASS_BUDGET_S", -1)
    r = genesis.drain_next_shard(con, declared)
    assert r["partial"] is True and r["state"] == "applied" and r["docs"] == 0
    row = con.execute("SELECT * FROM genesis_imports").fetchone()
    assert row["state"] == "applied" and row["pending"] is None
    assert row["next_pass_at"] <= int(_time.time()), "resumes without a backoff wait"
    assert con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0] == 0

    # ...and the collection's archive YIELDS the thread for the budget's
    # span (per collection: five shards on disk must not each take a
    # slice back-to-back while rdf/fts wait)
    key = str(declared.manifest_path)
    assert key in genesis._yield_until
    genesis._yield_until[key] = _time.time() + 60
    assert genesis.drain_next_shard(con, declared) is None, "yielding to rdf/fts"
    genesis._yield_until.pop(key)

    # a stop request cuts the pass the same way (SIGTERM: unit = a batch)
    # but does NOT yield: the worker is going down, the row resumes at once
    monkeypatch.setattr(config_mod, "GENESIS_PASS_BUDGET_S", 3600)
    import threading
    stop = threading.Event()
    stop.set()
    r = genesis.drain_next_shard(con, declared, stop=stop)
    assert r["partial"] is True and r["docs"] == 0
    assert key not in genesis._yield_until

    # the resumed pass completes: everything imports, the shard is FINAL
    r = genesis.drain_next_shard(con, declared)
    assert r["partial"] is False and r["docs"] == 20 and r["state"] == "final"
    assert con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0] == 20


def test_a_pass_interrupted_by_a_restart_resumes(collection, monkeypatch):
    from dataclasses import replace
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 11))
    archive = _declared_archive(["0"], ids_per_shard=100)
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", archive)
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, timeout=300, recipe=None, query=False:
                            _same_function_embed(texts))
    declared = replace(collection, genesis_archive="test-arc")
    genesis_fetch.run_fetcher(con, declared, fetch=_fake_fetch(cache))
    # the crash left the row mid-pass
    con.execute("UPDATE genesis_imports SET state='applying', next_pass_at=0")
    r = genesis.drain_next_shard(con, declared)
    assert r is not None and r["docs"] == 10 and r["state"] == "final"


def test_reimporting_a_shard_never_duplicates_vectors(collection, monkeypatch):
    """Delete-then-insert per document (invariant 2), now one delete per
    batch: a second import of the same documents leaves exactly one
    vector per chunk."""
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 11))
    archive = _archive(["0"])
    genesis.import_shard(con, collection, archive, "0",
                         embed_fn=_same_function_embed, fetch=_fake_fetch(cache))
    con.execute("UPDATE documents SET embed_done=0")      # force a re-import
    con.execute("DELETE FROM genesis_imports")
    genesis.import_shard(con, collection, archive, "0",
                         embed_fn=_same_function_embed, fetch=_fake_fetch(cache))
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    assert tbl.count_rows() == 10, "one vector per chunk after a re-import"


# --- the same-function proof is a quorum; a stale sample is skipped ----------

def _stale_embed(stale_ids):
    """The local encoder reproduces the archive exactly — except for the
    documents whose text changed since the snapshot (their archived
    vector was made from OLD text)."""
    def embed(texts, **kwargs):
        out = []
        for t in texts:
            nid = t.rsplit(" ", 1)[-1]
            out.append(_unit(("changed:" if nid in stale_ids else "") + t).tolist())
        return out
    return embed


def test_a_stale_sample_is_skipped_not_held_against_the_archive(collection):
    """Live: shard 0 proved and imported, shard 1 failed worst-of-8 at
    0.874 while every other sample sat at >= 0.993 — one corrected
    abstract. The proof is a quorum (median and 75% >= tau); the stale
    documents are evicted from the batch, never imported, left to the
    local encoder."""
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 21))
    # two of the first 16 (the sample) changed since the snapshot
    r = genesis.import_shard(con, collection, _archive(["0"]), "0",
                             embed_fn=_stale_embed({"3", "11"}),
                             fetch=_fake_fetch(cache))
    assert r["state"] == "applied" and r["docs"] == 18, r
    done = {row["uid"] for row in con.execute("SELECT uid FROM documents WHERE embed_done=1")}
    assert "fake:3" not in done and "fake:11" not in done and len(done) == 18
    assert set(r["residual_uids"]) == {"fake:3", "fake:11"}
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    assert tbl.count_rows() == 18, "stale vectors were never written"


def test_too_many_stale_samples_still_refuse_the_shard(collection):
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 21))
    with pytest.raises(GenesisError, match="NOT produced by this"):
        genesis.import_shard(con, collection, _archive(["0"]), "0",
                             embed_fn=_stale_embed({str(i) for i in range(1, 8)}),
                             fetch=_fake_fetch(cache))
    assert con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0] == 0


def test_a_proven_archive_survives_one_rejected_shard(collection, monkeypatch):
    """One shard proved and imported; a later one fails its quorum. The
    archive is not this function's enemy — the rejected shard alone is
    out: the fetcher and the drain go on, and its id range is released
    to the local encoder. With NOTHING proven, a rejection still stops
    the archive (36 more downloads must not re-prove the same mismatch)."""
    from dataclasses import replace
    from app import worker
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 10))
    _write_medcpt_shard(cache, "1", range(10, 20))
    _write_medcpt_shard(cache, "2", [20])
    archive = _declared_archive(["0", "1", "2"])
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", archive)
    declared = replace(collection, genesis_archive="test-arc")
    db.ensure_browse_index(con)
    # shard 0 proves; shard 1's documents ALL changed since the snapshot
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, timeout=300, recipe=None, query=False:
                            _stale_embed({str(i) for i in range(10, 20)})(texts))
    genesis_fetch.run_fetcher(con, declared, fetch=_fake_fetch(cache))
    assert genesis.drain_next_shard(con, declared)["shard"] == "0"
    assert genesis.drain_next_shard(con, declared) is None      # shard 1 rejected inside
    states = {r["shard"]: r["state"] for r in con.execute("SELECT shard, state FROM genesis_imports")}
    assert states["0"] == "final" and states["1"] == "rejected"
    assert not genesis.archive_stopped(con.execute("SELECT state FROM genesis_imports"))
    # the drain continues with shard 2; the fetcher would still fetch
    assert genesis.drain_next_shard(con, declared)["shard"] == "2"
    assert genesis_fetch.next_shard_to_fetch(con, archive) is None, "all landed already"
    # coverage: shard 1's range (rejected) is the encoder's now, like the
    # final shards' — pretend shards 0 and 1 were never embedded
    con.execute("UPDATE documents SET embed_done=0 WHERE CAST(native_id AS INTEGER) < 20")
    worker.reset_backfill_cursors()
    picked = {int(r["native_id"]) for r in worker._backfill_rows(con, declared, 100)}
    assert picked == set(range(1, 20)), picked
    # ...whereas a rejected shard beside an UNAPPLIED one keeps the latter covered
    con.execute("UPDATE genesis_imports SET state='fetched', pending=NULL WHERE shard='0'")
    worker.reset_backfill_cursors()
    picked = {int(r["native_id"]) for r in worker._backfill_rows(con, declared, 100)}
    assert picked == set(range(10, 20)), picked

    # nothing proven + a rejection = the archive stops, and covers nothing
    con.execute("UPDATE genesis_imports SET state='rejected'")
    assert genesis.archive_stopped(con.execute("SELECT state FROM genesis_imports"))
    assert genesis.drain_next_shard(con, declared) is None
    assert genesis_fetch.next_shard_to_fetch(con, archive) is None
    worker.reset_backfill_cursors()
    assert {int(r["native_id"]) for r in worker._backfill_rows(con, declared, 100)} == set(range(1, 20))


# --- the proof is per ARCHIVE and persists; a small failing sample is never a verdict ---

def test_a_proven_shard_is_not_rejected_by_a_sample_of_one(collection):
    """Live (pubmed shard 1, 2026-08-26): thirty revisit passes imported
    950k vectors, each skipping the same stale document; the pass whose
    ONLY eligible document was that one re-ran the proof on it alone —
    0/1, REJECTED. A proof is taken once per archive and persisted; later
    passes only screen for staleness, and a failing sample too small for
    a verdict is stale documents, not evidence."""
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 21))
    archive = _archive(["0"])
    stale = _stale_embed({"3"})
    first = genesis.import_shard(con, collection, archive, "0",
                                 embed_fn=stale, fetch=_fake_fetch(cache))
    assert first["docs"] == 19 and set(first["residual_uids"]) == {"fake:3"}
    rows = con.execute("SELECT state, proven_at, docs FROM genesis_imports").fetchall()
    assert rows[0]["proven_at"] and genesis.archive_proven(rows)

    # the revisit pass: document 3 is the only one still eligible
    again = genesis.import_shard(con, collection, archive, "0", embed_fn=stale,
                                 fetch=_fake_fetch(cache), revisit=True)
    assert again["state"] != "rejected" and again["docs"] == 0
    assert set(again["residual_uids"]) == {"fake:3"}
    row = con.execute("SELECT state, detail FROM genesis_imports").fetchone()
    assert row["state"] in ("applied", "final") and not row["detail"]
    assert con.execute("SELECT embed_done FROM documents WHERE uid='fake:3'").fetchone()[0] == 0
    stale_events = con.execute(
        "SELECT COUNT(*) FROM events WHERE message LIKE '%stale under a proven function%'").fetchone()[0]
    assert stale_events == 2, "both passes skipped it as stale; neither judged the archive"


def test_an_unproven_archive_with_a_tiny_failing_sample_is_deferred_not_rejected(collection):
    """Two eligible documents, both disagreeing with the encoder: that is
    not enough to convict an archive nobody has proven — nothing written,
    no verdict, retried when more documents are eligible. With the rest
    indexed the same archive IS judged, on sixteen samples."""
    con = db.connect(collection)
    con.execute("UPDATE documents SET fts_done=0 WHERE CAST(native_id AS INTEGER) > 2")
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 21))
    with pytest.raises(GenesisUnavailable, match="inconclusive"):
        genesis.import_shard(con, collection, _archive(["0"]), "0",
                             embed_fn=_different_model_embed, fetch=_fake_fetch(cache))
    row = con.execute("SELECT state, detail, proven_at FROM genesis_imports").fetchone()
    assert row["state"] == "skipped" and "inconclusive" in row["detail"] and not row["proven_at"]
    assert not genesis.archive_stopped([row])
    assert con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0] == 0
    assert embeddings.TABLE not in embeddings._conn(collection).table_names()

    con.execute("UPDATE documents SET fts_done=1")
    with pytest.raises(GenesisError, match="NOT produced by this"):
        genesis.import_shard(con, collection, _archive(["0"]), "0",
                             embed_fn=_different_model_embed, fetch=_fake_fetch(cache))
    assert con.execute("SELECT state FROM genesis_imports").fetchone()["state"] == "rejected"


def test_a_small_failing_sample_on_a_proven_archive_is_stale_not_a_verdict(collection):
    """Second shard of a proven archive, four held documents, all changed
    since the snapshot: evicted and left to the encoder; the shard stays
    in line (it may cover more later); nothing is rejected."""
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 17))
    _write_medcpt_shard(cache, "1", range(17, 21))
    archive = _archive(["0", "1"])
    embed = _stale_embed({"17", "18", "19", "20"})
    r0 = genesis.import_shard(con, collection, archive, "0", embed_fn=embed, fetch=_fake_fetch(cache))
    assert r0["docs"] == 16
    r1 = genesis.import_shard(con, collection, archive, "1", embed_fn=embed, fetch=_fake_fetch(cache))
    assert r1["state"] == "applied" and r1["docs"] == 0
    assert set(r1["residual_uids"]) == {f"fake:{i}" for i in range(17, 21)}
    states = {r["shard"]: r["state"] for r in con.execute("SELECT shard, state FROM genesis_imports")}
    assert states == {"0": "applied", "1": "applied"}
    assert embeddings._conn(collection).open_table(embeddings.TABLE).count_rows() == 16


def test_ledger_rows_from_before_proven_at_count_by_their_imports():
    """Rows written before `proven_at` existed (the NAS's shards 0-6)
    prove the archive by what only a passed proof allowed: imports. A
    pass cut by a crash (`applying`, nothing imported) never counts."""
    assert genesis.archive_proven([{"state": "applied", "proven_at": None, "docs": 5}])
    assert not genesis.archive_proven([{"state": "applied", "proven_at": None, "docs": 0}])
    assert not genesis.archive_proven([{"state": "applying", "proven_at": None, "docs": 0}])
    assert genesis.archive_proven([{"state": "applying", "proven_at": 1, "docs": 0}])
    assert genesis.archive_stopped([{"state": "rejected", "proven_at": None, "docs": 0}])
    assert not genesis.archive_stopped([{"state": "rejected", "proven_at": None, "docs": 0},
                                        {"state": "final", "proven_at": None, "docs": 9}])


def test_genesis_retry_puts_rejected_shards_back_in_line(collection):
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 11))
    with pytest.raises(GenesisError):
        genesis.import_shard(con, collection, _archive(["0"]), "0",
                             embed_fn=_different_model_embed, fetch=_fake_fetch(cache))
    assert genesis.retry_rejected(con, shard="9") == []
    assert genesis.retry_rejected(con) == [("test-arc", "0")]
    row = con.execute("SELECT state, detail, next_pass_at FROM genesis_imports").fetchone()
    assert row["state"] == "fetched" and row["detail"] is None and row["next_pass_at"] == 0
    # and it imports once the encoder agrees (a wrong [embed] fixed meanwhile)
    result = genesis.import_shard(con, collection, _archive(["0"]), "0",
                                  embed_fn=_same_function_embed, fetch=_fake_fetch(cache))
    assert result["state"] == "applied" and result["docs"] == 10


def test_the_card_says_rejected_only_while_nothing_has_proven_the_archive(collection, monkeypatch):
    """Live: the card read REJECTED for the whole archive while six other
    shards kept importing. The archive is rejected only while nothing has
    proven it; a rejected shard beside a proven one is counted apart."""
    from dataclasses import replace
    from app import web
    con = db.connect(collection)
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", _declared_archive(["0", "1"]))
    declared = replace(collection, genesis_archive="test-arc")
    now = 1000
    con.execute("INSERT INTO genesis_imports(archive,shard,state,docs,updated_at) VALUES('test-arc','1','rejected',0,?)", (now,))
    state = web._genesis_state(con, declared)
    assert state["rejected"] is True and state["rejected_shards"] == 1
    # a proven shard (by its imports, an old row) beside it
    con.execute("INSERT INTO genesis_imports(archive,shard,state,docs,updated_at) VALUES('test-arc','0','applied',500,?)", (now,))
    state = web._genesis_state(con, declared)
    assert state["rejected"] is False and state["rejected_shards"] == 1
    # ...or by its recorded proof, nothing imported yet
    con.execute("UPDATE genesis_imports SET docs=0, proven_at=? WHERE shard='0'", (now,))
    assert web._genesis_state(con, declared)["rejected"] is False


def test_a_revisit_pass_costs_the_new_documents_not_the_shard(collection, monkeypatch):
    """Regression (2026-08-27, measured live): every revisit pass of a
    MedCPT shard walked its million rows with a manifest lookup each,
    re-skipping what earlier passes had imported, and was cut by its
    budget — half of pubmed's pipeline thread. A pass now names the
    eligible documents with ONE indexed range query and reads only their
    rows: its manifest lookups are proportional to the new documents."""
    con = db.connect(collection)
    db.ensure_browse_index(con)
    # a shard of 20 documents, 17 already embedded: 3 are new since the last pass
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 21))
    archive = _declared_archive(["0"], ids_per_shard=100)
    con.execute("UPDATE documents SET embed_done=1 WHERE CAST(native_id AS INTEGER) NOT IN (5, 12, 19)")
    fetched = _fake_fetch(cache)
    cache_dir = collection.manifest_path.parent / "genesis" / "test-arc"
    cache_dir.mkdir(parents=True, exist_ok=True)
    fetched("embeds_chunk_0.npy", cache_dir / "embeds_chunk_0.npy")
    fetched("pmids_chunk_0.json", cache_dir / "pmids_chunk_0.json")
    con.execute("INSERT INTO genesis_imports(archive,shard,state,updated_at) VALUES('test-arc','0','applied',1)")
    lookups = []
    con.set_trace_callback(lambda sql: lookups.append(sql) if "FROM documents" in sql and "WHERE uid=?" in sql else None)
    r = genesis.import_shard(con, collection, archive, "0", embed_fn=_same_function_embed,
                             fetch=genesis._not_on_disk, revisit=True)
    con.set_trace_callback(None)
    assert r["docs"] == 3 and r["skipped_absent"] == 0
    assert len(lookups) <= 3, f"{len(lookups)} per-document lookups for 3 new documents"
    done = {int(x["native_id"]) for x in con.execute("SELECT native_id FROM documents WHERE embed_done=1")}
    assert done == set(range(1, 21))


def test_a_remote_archive_is_drained_on_the_pipeline_thread_not_the_gpu_loop(
        collection, monkeypatch):
    """A remote shard's pass is CPU/IO work for minutes; on the GPU-serial
    main loop it blocked every other collection's embedding (live:
    gutenberg's embeds 'done in 478 s'). With a pipeline thread the main
    loop's _run_one_step(pipeline_active=True) leaves the remote archive
    alone and the pipeline loop drains it; a LOCAL archive keeps being
    drained wherever the collection's pipeline runs."""
    from dataclasses import replace
    from app import worker
    con = db.connect(collection)
    drained = []
    monkeypatch.setattr(genesis, "drain_next_shard",
                        lambda con, c, stop=None, eager=True: (drained.append(c.genesis_archive), None)[1])
    monkeypatch.setattr(worker, "_drain_bundle_inbox", lambda con, c: 0)
    monkeypatch.setattr(worker, "_acquire_archive", lambda *a, **k: None)
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", _declared_archive(["0"]))

    remote = replace(collection, genesis_archive="test-arc")
    local = replace(collection, genesis_archive="upstash-wikipedia-2024-06")
    worker._run_one_step(con, remote, {}, pipeline_active=True)
    assert drained == [], "the GPU loop must not run a remote archive's pass"
    worker._run_one_step(con, local, {}, pipeline_active=True)
    assert drained == ["upstash-wikipedia-2024-06"], "a local archive is still drained there"
    assert worker._drain_remote_archive(con, remote, None) is False
    assert drained[-1] == "test-arc", "the pipeline thread's round drains the remote archive"
    # a collection running its whole pipeline on one thread drains both
    worker._run_one_step(con, remote, {}, pipeline_active=False)
    assert drained[-1] == "test-arc"


def test_the_shard_range_queries_use_the_browse_index(collection):
    """Regression (2026-08-28, measured on the live 40M-row pubmed
    manifest): both range queries of a revisit pass were planned on
    `idx_documents_scope (in_scope=?)` and then filtered the whole
    collection — 19.1 s to list a shard's eligible documents and 9.5 s to
    count its pending ones, ~28 s of every 300 s pass, on a plan that
    reads as indexed. They are hinted onto the browse index; without that
    index they still work."""
    con = db.connect(collection)
    # no browse index yet: no hint, and the query still runs
    assert "INDEXED BY" not in genesis.eligible_in_range_sql(con)
    assert con.execute(genesis.eligible_in_range_sql(con), (1, 10)).fetchall() is not None
    assert con.execute(genesis.pending_in_range_sql(con), (1, 10)).fetchone()[0] >= 0

    db.ensure_browse_index(con)
    for sql in (genesis.eligible_in_range_sql(con), genesis.pending_in_range_sql(con)):
        assert "INDEXED BY idx_documents_native_int" in sql
        plan = " ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql, (1, 10)))
        assert "idx_documents_native_int" in plan, plan
        assert "idx_documents_scope" not in plan, plan
    # and they still answer correctly: only 1-5 have cleared fts
    con.execute("UPDATE documents SET fts_done=0 WHERE CAST(native_id AS INTEGER) > 5")
    eligible = {r["uid"] for r in con.execute(genesis.eligible_in_range_sql(con), (1, 20))}
    assert eligible == {f"fake:{i}" for i in range(1, 6)}
    assert con.execute(genesis.pending_in_range_sql(con), (1, 20)).fetchone()[0] == 15
    assert con.execute(genesis.pending_in_range_sql(con), (1, 5)).fetchone()[0] == 0


def test_the_apply_loop_chunks_a_batch_at_a_time(collection, monkeypatch):
    """Regression (2026-08-28, measured): the apply loop called
    fetch+parse+chunk ONE DOCUMENT AT A TIME — 2.87 ms each, 51.7 s of a
    150 s wikivec pass, on one core while eleven idled. It now chunks the
    held batch through the parse pool; the results, the shape gate and the
    proof are unchanged."""
    from app import parallel
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 21))
    calls = []
    real = parallel.chunks_for_rows
    monkeypatch.setattr(parallel, "chunks_for_rows",
                        lambda coll, rows: (calls.append(len(rows)), real(coll, rows))[1])
    monkeypatch.setattr(genesis, "APPLY_BATCH_DOCS", 8)
    r = genesis.import_shard(con, collection, _archive(["0"]), "0",
                             embed_fn=_same_function_embed, fetch=_fake_fetch(cache))
    assert r["docs"] == 20 and r["chunks"] == 20
    # batches of 8, not 20 calls of 1
    assert calls and max(calls) > 1 and sum(calls) == 20, calls
    assert len(calls) <= 4, f"one pooled call per batch, got {calls}"
    assert con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0] == 20


def test_a_document_whose_text_will_not_load_is_skipped_by_the_batch(collection, monkeypatch):
    """One unreadable document costs itself, not its batch — the contract
    the per-document call had, kept through the pool."""
    from app import parallel, plugin_io
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 11))
    real = plugin_io.sections

    def flaky(plugin, row):
        if row["uid"] == "fake:4":
            raise OSError("gone")
        return real(plugin, row)
    monkeypatch.setattr(plugin_io, "sections", flaky)
    r = genesis.import_shard(con, collection, _archive(["0"]), "0",
                             embed_fn=_same_function_embed, fetch=_fake_fetch(cache))
    assert r["docs"] == 9 and "fake:4" in r["residual_uids"]
    done = {x["uid"] for x in con.execute("SELECT uid FROM documents WHERE embed_done=1")}
    assert "fake:4" not in done and len(done) == 9


# ---- the stall ------------------------------------------------------------

def test_a_full_cache_forces_one_backed_off_shard(monkeypatch):
    """Measured on pubmed 2026-09-11: eleven applied shards each carrying a
    9,600 s backoff and a stale `pending`, every document in the ranges
    checked already indexed — so every one of them was finalisable, 37 GiB
    sat resident against a 24 GiB bound, and the import advanced in bursts
    hours apart instead of continuously.

    The deadlock: forward progress needs a shard to go final; a shard goes
    final only on a pass; the pass waits on a backoff that DOUBLED because
    earlier passes imported little — and they imported little because fts
    had not reached those documents yet. `pending` counts `fts_done=0`, so
    the condition the backoff waits on changes continuously while the
    backoff does not.
    """
    from app.bundles import genesis

    rows = {"3": {"state": "applied", "pending": 400000, "next_pass_at": 9_999_999},
            "4": {"state": "applied", "pending": 300000, "next_pass_at": 8_888_888}}
    shards = ["3", "4"]

    class _C:
        manifest_path = "/tmp/x/manifest.sqlite"
    class _A:
        shard_bytes = 1
        shards = ("3", "4")

    genesis._forced_revisit.clear()
    monkeypatch.setattr(genesis, "_cache_is_full", lambda con, a: True)
    monkeypatch.setattr(genesis, "_on_disk", lambda c, a, s: True)
    # nothing is due — without the forcing this is an empty round, forever
    got = genesis._remote_drain_order(shards, rows, 1000, _C(), _A(), object())
    assert got == ["4"], "the soonest-due backed-off shard must be forced"

    # and only once per GENESIS_MAX_WAIT_SEC, so the pending recount is bounded
    assert genesis._remote_drain_order(shards, rows, 1001, _C(), _A(), object()) == []
    genesis._forced_revisit.clear()


def test_a_cache_with_room_still_respects_the_backoff(monkeypatch):
    """The backoff is right in the normal case — a shard that genuinely has
    nothing to do should cost less and less. Only a STALLED archive, where
    revisiting is the one way forward, overrides it."""
    from app.bundles import genesis

    rows = {"3": {"state": "applied", "pending": 400000, "next_pass_at": 9_999_999}}
    class _C:
        manifest_path = "/tmp/y/manifest.sqlite"
    class _A:
        shard_bytes = 1
        shards = ("3",)

    genesis._forced_revisit.clear()
    monkeypatch.setattr(genesis, "_cache_is_full", lambda con, a: False)
    monkeypatch.setattr(genesis, "_on_disk", lambda c, a, s: True)
    assert genesis._remote_drain_order(["3"], rows, 1000, _C(), _A(), object()) == []


def test_a_shard_whose_files_are_gone_is_never_walked(monkeypatch):
    """`_remote_drain_order`'s docstring always said "a shard not on disk
    ... is never walked here", and nothing checked — the walk read the
    LEDGER only.

    A shard whose files were deleted while its row still said `applied`
    was therefore selected every round: `import_shard` raised
    GenesisUnavailable, `drain_next_shard` caught it and returned None,
    and the next round picked the same shard again. Silent, because
    GenesisUnavailable is "recorded as skipped; retried later", and
    permanent, because nothing about the shard ever changed.

    Measured on pubmed 2026-09-11: shards 8-12 `applied` with their files
    gone, the drain returning ['9','10','11','12','13'], 0 documents/s for
    twenty minutes, one core spinning, not one log line — while 30M
    documents waited for vectors the archive already held.
    """
    from app.bundles import genesis

    rows = {"5": {"state": "applied", "pending": None, "next_pass_at": 0},
            "6": {"state": "applied", "pending": None, "next_pass_at": 0}}
    class _C:
        manifest_path = "/tmp/z/manifest.sqlite"
    class _A:
        shard_bytes = 1
        shards = ("5", "6")

    genesis._forced_revisit.clear()
    # 5's files are gone, 6's are here
    monkeypatch.setattr(genesis, "_on_disk",
                        lambda c, a, s: s == "6")
    got = genesis._remote_drain_order(["5", "6"], rows, 1000, _C(), _A(), object())
    assert got == ["6"], "a shard with no files on disk must not be walked"


def test_an_unlocatable_shard_counts_as_absent(monkeypatch, tmp_path):
    """Unknown means NOT on disk: guessing the other way is how the stall
    started."""
    from app.bundles import genesis

    class _A:
        adapter = "nope"
        archive_id = "x"
    class _C:
        manifest_path = tmp_path / "m.db"
    assert genesis._on_disk(_C(), _A(), "1") is False


def test_an_applied_shard_whose_files_vanished_is_re_fetched(collection, monkeypatch):
    """THE LOOP THAT WAS OPEN, closed.

    `_remote_drain_order` walks only shards that are ON DISK — the fix of
    2026-09-11, after shards 8-12 were selected every round with their
    files gone and the import ran at 0 documents/s for twenty minutes.
    That stopped the spin and left the shards stuck: the drain would not
    walk them without files, and the FETCHER would not re-fetch anything
    in state `applied`. A remote shard whose files disappeared could never
    be revisited and never reach `final`.

    Measured on pubmed 2026-09-12: five shards `applied`, `pending`
    unknown and due, 188,323 documents inside the archive's own id range
    still unembedded, and no path by which the archive could deliver them.
    """
    from dataclasses import replace
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 10))
    archive = _declared_archive(["0"])
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", archive)
    declared = replace(collection, genesis_archive="test-arc")

    genesis_fetch.run_fetcher(con, declared, fetch=_fake_fetch(cache))
    assert genesis_fetch.next_shard_to_fetch(con, archive, collection=declared) is None

    # applied, still owing documents, due — and its files are gone
    con.execute("UPDATE genesis_imports SET state='applied', pending=3, "
                "next_pass_at=0 WHERE shard='0'")
    files = list(genesis_fetch.shard_files(
        archive, genesis_fetch.cache_dir(declared, archive), "0"))
    for _, path in files:
        path.unlink()
    assert genesis._on_disk(declared, archive, "0") is False

    # WITHOUT the collection there is nothing to check the disk against,
    # so the old answer stands — the caller that can check, does
    assert genesis_fetch.next_shard_to_fetch(con, archive) is None
    assert genesis_fetch.next_shard_to_fetch(con, archive, collection=declared) == "0"

    genesis_fetch.run_fetcher(con, declared, fetch=_fake_fetch(cache))
    for _, path in files:
        assert path.is_file(), "the files come back"
    row = con.execute("SELECT * FROM genesis_imports WHERE shard='0'").fetchone()
    assert row["state"] == "applied", \
        "a re-fetch for a REVISIT keeps what this peer already imported"
    assert row["pending"] is None and row["next_pass_at"] == 0, "and it is due"


def test_a_never_fetched_shard_is_still_taken_before_a_re_fetch(collection, monkeypatch):
    """Order follows the work: a shard never fetched is free vectors, one
    coming back to be finalised is bookkeeping."""
    from dataclasses import replace
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 10))
    _write_medcpt_shard(cache, "1", range(10, 20))
    archive = _declared_archive(["0", "1"])
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", archive)
    declared = replace(collection, genesis_archive="test-arc")

    # shard 0 applied with its files gone; shard 1 never fetched at all
    con.execute(
        "INSERT INTO genesis_imports(archive,shard,source_url,state,updated_at,"
        "pending,next_pass_at) VALUES('test-arc','0','http://arc','applied',0,5,0)")
    assert genesis_fetch.next_shard_to_fetch(con, archive, collection=declared) == "1"


def test_a_final_shard_is_never_re_fetched(collection, monkeypatch):
    """Its files were deleted on purpose and it has nothing left to give."""
    from dataclasses import replace
    con = db.connect(collection)
    archive = _declared_archive(["0"])
    monkeypatch.setitem(genesis.ARCHIVES, "test-arc", archive)
    declared = replace(collection, genesis_archive="test-arc")
    con.execute(
        "INSERT INTO genesis_imports(archive,shard,source_url,state,updated_at,"
        "pending,next_pass_at) VALUES('test-arc','0','http://arc','final',0,0,0)")
    assert genesis_fetch.next_shard_to_fetch(con, archive, collection=declared) is None


# --- the ledger counts in the flush's transaction (2026-09-14) ---------------
#
# pubmed shards 8, 9 and 13 had 80,654 more embedded documents than their
# ledger rows said (53,000 in shard 13: exactly 106 flushes of 500). The
# ledger was counted once, at the END of a pass, so every flush of a pass
# that ended any other way was imported and never counted — and the
# resumed pass skips those documents (embed_done=1) as not held.

def _ledger_and_what_landed(con, archive, shard):
    """(ledger docs, ledger chunks, embedded in the shard's range,
    provenance rows for the shard) — equal when the ledger is right."""
    lo, hi = archive.id_range_of(shard)
    row = con.execute("SELECT docs, chunks FROM genesis_imports WHERE archive=? AND shard=?",
                      (archive.archive_id, shard)).fetchone()
    embedded = con.execute(
        "SELECT COUNT(*) FROM documents WHERE embed_done=1 "
        "AND CAST(native_id AS INTEGER) BETWEEN ? AND ?", (lo, hi)).fetchone()[0]
    provenance = con.execute(
        "SELECT COUNT(*) FROM chunk_verification WHERE bundle_id=?",
        (f"genesis:{archive.archive_id}/{shard}",)).fetchone()[0]
    return row["docs"], row["chunks"], embedded, provenance


def _fail_the_kth_vector_write(monkeypatch, k, exc):
    """The k-th Lance write of the run (create or add) raises `exc`; the
    ones before it land, and the ones after it land too."""
    real = embeddings._conn
    calls = {"n": 0}

    def maybe_fail():
        calls["n"] += 1
        if calls["n"] == k:
            raise exc

    class Table:
        def __init__(self, t):
            self._t = t

        def add(self, rows):
            maybe_fail()
            return self._t.add(rows)

        def __getattr__(self, name):
            return getattr(self._t, name)

    class Conn:
        def __init__(self, c):
            self._c = c

        def open_table(self, name):
            return Table(self._c.open_table(name))

        def create_table(self, *a, **kw):
            maybe_fail()
            return self._c.create_table(*a, **kw)

        def __getattr__(self, name):
            return getattr(self._c, name)

    monkeypatch.setattr(embeddings, "_conn", lambda c: Conn(real(c)))


def _twenty_held(monkeypatch):
    monkeypatch.setattr(genesis, "APPLY_BATCH_DOCS", 5)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 21))
    return cache, _declared_archive(["0"], ids_per_shard=100)


def _pass(con, collection, archive, cache):
    return genesis.import_shard(con, collection, archive, "0", embed_fn=_same_function_embed,
                                fetch=_fake_fetch(cache), revisit=True)


def _conflict():
    return RuntimeError("lance error: Commit conflict for version 7")


def _busy():
    from app.bundles.writer_lock import VectorWriteBusy
    return VectorWriteBusy("could not acquire the vector write lock: database is locked")


@pytest.mark.parametrize("make_exc, raised, state", [
    (_conflict, "GenesisUnavailable", "skipped"),   # deferred: "what it wrote stands"
    (_busy, "VectorWriteBusy", "applying"),         # not a contention the worker defers
    (lambda: RuntimeError("boom"), "RuntimeError", "applying"),
])
def test_a_pass_that_ends_early_has_counted_every_flush_it_committed(
        collection, monkeypatch, make_exc, raised, state):
    con = db.connect(collection)
    cache, archive = _twenty_held(monkeypatch)
    _fail_the_kth_vector_write(monkeypatch, 3, make_exc())
    with pytest.raises(Exception) as got:
        _pass(con, collection, archive, cache)
    assert type(got.value).__name__ == raised
    assert con.execute("SELECT state FROM genesis_imports").fetchone()["state"] == state
    assert _ledger_and_what_landed(con, archive, "0") == (10, 10, 10, 10), \
        "two flushes of five landed before the third write failed — and are counted"

    # the resumed pass skips those ten as already embedded and counts the rest
    r = _pass(con, collection, archive, cache)
    assert r["state"] == "final" and r["docs"] == 10
    assert _ledger_and_what_landed(con, archive, "0") == (20, 20, 20, 20)


def test_a_shard_rejected_mid_pass_has_counted_what_it_wrote(collection, monkeypatch):
    con = db.connect(collection)
    cache, archive = _twenty_held(monkeypatch)
    cache["embeds_chunk_0.npy"][12] = 0.0          # id 13: after two flushes
    with pytest.raises(GenesisError):
        _pass(con, collection, archive, cache)
    assert con.execute("SELECT state FROM genesis_imports").fetchone()["state"] == "rejected"
    assert _ledger_and_what_landed(con, archive, "0") == (10, 10, 10, 10)


def test_a_failure_after_the_last_flush_loses_no_count(collection, monkeypatch):
    """`_pending_for_shard` and the end-of-pass UPDATE run after the final
    flush, outside the pass's try: a locked manifest there lost the whole
    pass's count."""
    import sqlite3
    con = db.connect(collection)
    cache, archive = _twenty_held(monkeypatch)

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(genesis, "_pending_for_shard", locked)
    with pytest.raises(sqlite3.OperationalError):
        _pass(con, collection, archive, cache)
    assert _ledger_and_what_landed(con, archive, "0") == (20, 20, 20, 20)


@pytest.mark.parametrize("prior", ["skipped", "applying", "fetched"])
def test_a_non_revisit_import_never_lowers_the_ledger(collection, monkeypatch, prior):
    """`genesis import` (import_archive, revisit=False on a remote archive)
    OVERWROTE docs with its own pass's count, erasing every earlier pass of
    the same shard."""
    con = db.connect(collection)
    cache, archive = _twenty_held(monkeypatch)
    _fail_the_kth_vector_write(monkeypatch, 3, _conflict())
    with pytest.raises(GenesisUnavailable):
        _pass(con, collection, archive, cache)
    con.execute("UPDATE genesis_imports SET state=?", (prior,))
    out = genesis.import_archive(con, collection, archive, embed_fn=_same_function_embed,
                                 fetch=_fake_fetch(cache))
    assert out["docs"] == 10, "this pass imported the ten the first one had not"
    assert _ledger_and_what_landed(con, archive, "0") == (20, 20, 20, 20)


def test_recount_sets_the_ledger_from_provenance_and_changes_nothing_twice(
        collection, monkeypatch, capsys):
    from app import cli
    from app.bundles import ledger
    con = db.connect(collection)
    cache = {}
    _write_medcpt_shard(cache, "0", range(1, 10))
    _write_medcpt_shard(cache, "1", range(10, 20))
    archive = _declared_archive(["0", "1"], ids_per_shard=10)
    for shard in ("0", "1"):
        genesis.import_shard(con, collection, archive, shard, embed_fn=_same_function_embed,
                             fetch=_fake_fetch(cache), revisit=True)
    # rows as the pre-fix ledger left them: shard 0 lost flushes; shard 1
    # credits a document the local encoder has since re-embedded (its
    # provenance is gone: not the archive's any more)
    con.execute("UPDATE genesis_imports SET docs=4, chunks=4 WHERE shard='0'")
    ledger.clear_verification(con, "fake:15")
    # a two-chunk document: docs count documents, chunks count chunks
    ledger.mark_imported(con, ["fake:3#extra"], "fake:3", "genesis:test-arc",
                         "genesis:test-arc/0")

    def ledger_rows():
        return {r["shard"]: (r["docs"], r["chunks"]) for r in con.execute(
            "SELECT shard, docs, chunks FROM genesis_imports")}

    dry = genesis.recount_ledger(con)
    assert {(r.shard, r.held_docs, r.held_chunks) for r in dry} == {("0", 9, 10), ("1", 9, 9)}
    assert ledger_rows() == {"0": (4, 4), "1": (10, 10)}, "a dry run writes nothing"

    monkeypatch.setattr(cli.collections_mod, "load_all", lambda: [collection])
    assert cli.main(["genesis", "recount", collection.name]) == 2
    assert "test-arc/0: docs 4 -> 9, chunks 4 -> 10" in capsys.readouterr().out
    assert ledger_rows() == {"0": (4, 4), "1": (10, 10)}

    assert cli.main(["genesis", "recount", collection.name, "--yes"]) == 0
    assert ledger_rows() == {"0": (9, 10), "1": (9, 9)}
    capsys.readouterr()
    assert cli.main(["genesis", "recount", collection.name, "--yes"]) == 0
    assert "already match" in capsys.readouterr().out
    assert not any(r.changed for r in genesis.recount_ledger(con, write=True))


def test_the_recount_walk_is_one_ordered_index_scan(collection):
    """One read of chunk_verification in uid order, answered by the uid
    index — no TEMP B-TREE for the ORDER BY, no LIST SUBQUERY."""
    con = db.connect(collection)
    plan = " | ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + genesis.RECOUNT_SQL))
    assert "idx_chunk_verification_uid" in plan, plan
    assert "TEMP B-TREE" not in plan and "LIST SUBQUERY" not in plan, plan


def test_the_flush_credit_check_searches_the_chunk_id_index(collection):
    con = db.connect(collection)
    sql = genesis.CREDITED_SQL.format(marks="?,?,?")
    plan = " | ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql,
                                                ("a", "b", "c", "d")))
    assert plan.startswith("SEARCH chunk_verification USING INDEX "
                           "sqlite_autoindex_chunk_verification_1 (chunk_id=?)"), plan
    assert "TEMP B-TREE" not in plan and "LIST SUBQUERY" not in plan, plan


def test_a_document_delivered_again_is_credited_once(collection, monkeypatch):
    """A reindex, a stage reset, a changed document or a requeue sets
    embed_done=0 and keeps the document's provenance, and the reopened
    shard's revisit selects it again. mark_imported upserts on chunk_id,
    so provenance counts it once — and so must the ledger, or it drifts
    above provenance and `recount` has to pull it back down."""
    from app.bundles import ledger
    con = db.connect(collection)
    cache, archive = _twenty_held(monkeypatch)
    assert _pass(con, collection, archive, cache)["state"] == "final"
    assert _ledger_and_what_landed(con, archive, "0") == (20, 20, 20, 20)
    for i in range(1, 6):           # requeued: failed verification, embed_done=0
        con.execute("UPDATE documents SET embed_done=0 WHERE uid=?", (f"fake:{i}",))
        for (cid,) in con.execute("SELECT chunk_id FROM chunk_verification WHERE uid=?",
                                  (f"fake:{i}",)).fetchall():
            ledger.set_verified(con, cid, 0.1, False)
    con.execute("UPDATE genesis_imports SET state='applied', pending=NULL, next_pass_at=0")
    r = _pass(con, collection, archive, cache)
    assert r["docs"] == 5, "the revisit delivered the five again"
    assert _ledger_and_what_landed(con, archive, "0") == (20, 20, 20, 20)
    assert not any(rc.changed for rc in genesis.recount_ledger(con))


def test_a_recount_never_un_proves_an_archive(collection):
    """A row from before `proven_at` carries its proof in docs > 0. With no
    provenance rows it recounts to zero; the proof must survive that, or
    one rejected shard STOPS the archive — the drain, the fetcher, and the
    covered ranges handed whole to the local encoder."""
    con = db.connect(collection)
    con.execute("INSERT INTO genesis_imports(archive,shard,state,docs,chunks,updated_at) "
                "VALUES('test-arc','0','final',5,5,123)")
    con.execute("INSERT INTO genesis_imports(archive,shard,state,updated_at) "
                "VALUES('test-arc','1','rejected',124)")

    def rows():
        return con.execute("SELECT * FROM genesis_imports ORDER BY shard").fetchall()
    assert not genesis.archive_stopped(rows())
    out = genesis.recount_ledger(con, write=True)
    assert [(rc.shard, rc.docs, rc.held_docs) for rc in out if rc.changed] == [("0", 5, 0)]
    assert not genesis.archive_stopped(rows()), "the recount stopped a proven archive"
    assert tuple(con.execute("SELECT docs, proven_at FROM genesis_imports WHERE shard='0'")
                 .fetchone()) == (0, 123), "the proof is persisted at the row's own time"


def test_a_recount_holds_the_write_lock_across_its_walk(collection, monkeypatch):
    """BEGIN IMMEDIATE before the walk: nothing may land between the count
    and the write. A writer that arrives mid-walk is refused until the
    recount commits."""
    import sqlite3
    from app.bundles import ledger
    con = db.connect(collection)
    con.execute("INSERT INTO genesis_imports(archive,shard,state,docs,chunks,updated_at) "
                "VALUES('test-arc','0','applied',1,1,0)")
    for i in (1, 2):
        ledger.mark_imported(con, [f"fake:{i}#0"], f"fake:{i}", "genesis:test-arc",
                             "genesis:test-arc/0")
    outcome = []

    def probe(_uid):
        if not outcome:             # the first row of the walk: try to write
            other = sqlite3.connect(collection.manifest_path, timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute(
                    "INSERT INTO chunk_verification(chunk_id,uid,state,producer,bundle_id) "
                    "VALUES('fake:3#0','fake:3','imported','genesis:test-arc',"
                    "'genesis:test-arc/0')")
                other.execute("COMMIT")
                outcome.append("wrote")
            except sqlite3.OperationalError as e:
                outcome.append(str(e))
            finally:
                other.close()
        return 1
    con.create_function("probe", 1, probe)
    monkeypatch.setattr(genesis, "RECOUNT_SQL",
                        genesis.RECOUNT_SQL.replace("WHERE ", "WHERE probe(uid) AND ", 1))
    out = genesis.recount_ledger(con, write=True)
    assert outcome == ["database is locked"], outcome
    assert [(rc.held_docs, rc.held_chunks) for rc in out] == [(2, 2)]
    assert con.execute("SELECT docs FROM genesis_imports").fetchone()[0] == 2


def test_a_space_repin_zeroes_every_ledger_row_it_keeps(collection, monkeypatch):
    """A rejection mid-pass leaves docs on the row (counted per flush);
    `genesis retry` makes it `fetched`, which the re-pin keeps. Its docs
    stood for provenance the re-pin deletes, and its proof for the space
    the re-pin drops: both go, or the re-import counts on top."""
    from app import cli
    con = db.connect(collection)
    cache, archive = _twenty_held(monkeypatch)
    cache["embeds_chunk_0.npy"][12] = 0.0          # id 13: after two flushes
    with pytest.raises(GenesisError):
        _pass(con, collection, archive, cache)
    genesis.retry_rejected(con)
    assert tuple(con.execute("SELECT state, docs FROM genesis_imports").fetchone()) \
        == ("fetched", 10)
    monkeypatch.setattr(cli.collections_mod, "load_all", lambda: [collection])
    assert cli.main(["space", "repin", collection.name, "--yes"]) == 0
    assert tuple(con.execute("SELECT state, docs, chunks, proven_at FROM genesis_imports")
                 .fetchone()) == ("fetched", 0, 0, None)
    # re-imported against the new space: counted from zero, and proven anew
    cache["embeds_chunk_0.npy"][12] = _archive_vector(13)
    for p in (collection.manifest_path.parent / "genesis" / archive.archive_id).iterdir():
        p.unlink()
    con.execute("UPDATE documents SET fts_done=1")
    assert _pass(con, collection, archive, cache)["state"] == "final"
    assert _ledger_and_what_landed(con, archive, "0") == (20, 20, 20, 20)
    assert con.execute("SELECT proven_at FROM genesis_imports").fetchone()[0] is not None

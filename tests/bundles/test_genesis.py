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

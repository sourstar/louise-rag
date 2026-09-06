"""A local archive must not starve the rest of the pipeline.

Local archives revisit their shards (documents become eligible gradually),
so there is always a shard `drain_next_shard` COULD read. Observed live on
wikivec (959 shards, embedded plugin, a CPU encoder — so the pipeline
thread owns rdf, fts AND genesis): with 200 embed-eligible documents
present, genesis claimed every round by re-reading the next shard in
oldest-first order — ~437 MB, ~10 s, "imported 0 vectors across 0
documents" — and rdf/fts got one turn per full sweep. The collection
advanced 200 documents per 3.2 hours, with 13.5 million left.

The contract pinned here, which every later rework of this path must keep:

1. Genesis reads ONLY shards its candidates name. The candidates' own
   locators say which shard holds them; a shard nobody is waiting for is
   never opened, however many shards the corpus has.
2. A document the archive cannot cover never buys a read on its account.
   A shard-boundary straddler (vectors split across two files, so the
   chunk-shape gate refuses it from either side) is known from its
   two-segment locator and is skipped a priori — no read, no state, and
   nothing to forget on restart. A candidate whose chunk text will not load
   costs one read and is then RESIDUAL for its shard. Either way genesis
   yields the round so the local encoder may gap-fill — and a NEW candidate
   in that shard reopens it.
3. While rdf/fts still have work on the same thread, a shard read waits
   for a batch (GENESIS_MIN_CANDIDATES) or an age cap (GENESIS_MAX_WAIT_SEC),
   so the read is amortised instead of paid per fts commit; with nothing
   else to do it happens at once.
4. The candidate walk is an index range, never a sort (plan-checked: a
   merely slow selection passes every behavioural test at fixture scale).
5. End to end through the worker: on a thread that owns the whole
   pipeline, rdf, fts and genesis all make progress, and the number of
   shard reads is bounded by the shards that actually hold candidates —
   not by the size of the shard list.
"""
import time

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app import (collections as collections_mod, config, db, discovery, embeddings,
                 plugin_io, worker)
from app.bundles import genesis
from app.plugins.embedded.source import embed_input

DIM = 8
ARCHIVE = "upstash-wikipedia-2024-06"
TR = 10 * 10_000_000_000          # LANG_ORDINAL["tr"] * _LANG_BASE


def _unit(seed: str) -> np.ndarray:
    rng = np.random.default_rng(abs(hash(seed)) % (2**32))
    v = rng.normal(size=DIM)
    return (v / np.linalg.norm(v)).astype(np.float32)


def _same_function_embed(texts, timeout=300, recipe=None, query=False):
    return [_unit(t).tolist() for t in texts]


def _write_shard(path, specs):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "id": [f"{a}_{s}" for a, s, _, _ in specs],
        "url": [f"https://x/{t}" for _, _, t, _ in specs],
        "title": [t for _, _, t, _ in specs],
        "text": [x for _, _, _, x in specs],
        "embedding": [_unit(embed_input(t, x)).tolist() for _, _, t, x in specs],
    }), path)


def _collection(tmp_path, name="wikivec"):
    from dataclasses import replace
    from app import licensing
    c = collections_mod.Collection(
        name=name, plugin="embedded", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb",
        genesis_archive=ARCHIVE,
        embed=collections_mod.EmbedRecipe(
            url="http://fake:1", api="openai", model="bge-m3-test",
            dtype="f16", ctx=512, dim=DIM))
    # yellow class: the worker withholds the archive without the ack
    # (amendment 46) — this file is about scheduling, so it is acknowledged
    return replace(c, license_ack=licensing.make_ack(c))


def _uid(article: int) -> str:
    return f"embedded:{TR + article}"


def _eligible(con, uids=None):
    if uids is None:
        con.execute("UPDATE documents SET rdf_done=1, fts_done=1 WHERE error IS NULL")
    else:
        marks = ",".join("?" for _ in uids)
        con.execute(f"UPDATE documents SET rdf_done=1, fts_done=1 "
                    f"WHERE uid IN ({marks})", list(uids))


@pytest.fixture
def reads(monkeypatch):
    """Records every shard genesis actually opens."""
    opened: list[str] = []
    real = genesis.import_shard

    def recording(con, collection, archive, shard, **kw):
        opened.append(shard)
        return real(con, collection, archive, shard, **kw)
    monkeypatch.setattr(genesis, "import_shard", recording)
    return opened


@pytest.fixture
def collection(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", _same_function_embed)
    c = _collection(tmp_path)
    # article 12 STRADDLES the two shards: each shard yields a partial
    # vector list, so the chunk-shape gate refuses it — a residual candidate
    # the archive can never cover, exactly like a real shard boundary.
    _write_shard(c.corpus_dir / "data" / "tr" / "000.parquet", [
        ("10", 0, "A", "alpha one"),
        ("12", 0, "C", "straddle start"),
    ])
    _write_shard(c.corpus_dir / "data" / "tr" / "001.parquet", [
        ("12", 1, "C", "straddle end"),
        ("13", 0, "D", "delta one"),
    ])
    con = db.connect(c)
    discovery.sync_collection(con, c)
    _eligible(con)
    return c


# --- 1. no sweep ------------------------------------------------------------

def test_drain_reads_only_the_shards_its_candidates_name(isolated_config, tmp_path,
                                                         monkeypatch, reads):
    """The live failure: hundreds of shards, candidates in one. Before the
    fix every round opened the next shard in oldest-first order and
    imported nothing; now the shard list's size is irrelevant."""
    monkeypatch.setattr(embeddings, "embed_texts", _same_function_embed)
    c = _collection(tmp_path)
    shards = 24
    for i in range(shards):
        _write_shard(c.corpus_dir / "data" / "tr" / f"{i:03d}.parquet", [
            (str(100 + i), 0, f"T{i}", f"text {i}")])
    con = db.connect(c)
    discovery.sync_collection(con, c)
    _eligible(con, [_uid(100 + 7)])          # only shard 007 holds a candidate

    result = genesis.drain_next_shard(con, c)
    assert result is not None and result["docs"] == 1
    assert reads == ["data/tr/007.parquet"], reads

    # covered → nothing to do; and STILL nothing else was opened
    assert genesis.drain_next_shard(con, c) is None
    assert reads == ["data/tr/007.parquet"], \
        "a shard nobody is waiting for must never be read"
    untouched = con.execute(
        "SELECT COUNT(*) FROM genesis_imports WHERE archive=?", (ARCHIVE,)).fetchone()[0]
    assert untouched == 1, "the ledger shows only the shard that was read"


# --- 2. residuals -----------------------------------------------------------

def test_exhausted_revisits_yield_the_round_to_local_embed(collection, reads):
    con = db.connect(collection)

    # drain until the archive has covered everything it can
    for _ in range(40):
        if genesis.drain_next_shard(con, collection) is None:
            break
    covered = con.execute(
        "SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0]
    residual = con.execute(
        "SELECT COUNT(*) FROM documents WHERE fts_done=1 AND embed_done=0 "
        "AND error IS NULL").fetchone()[0]
    assert covered >= 2, "the archive should have covered the whole articles"
    assert residual >= 1, "the straddling article is the residual candidate"

    # THE POINT: with a residual candidate still present, genesis must stop
    # claiming the round, or the local encoder never gets to gap-fill it.
    before = len(reads)
    assert genesis.drain_next_shard(con, collection) is None, (
        "an exhausted revisit cycle must yield the round — otherwise the "
        "worker re-reads shards forever and local gap-fill never runs")
    assert genesis.drain_next_shard(con, collection) is None
    assert len(reads) == before, "yielding must not cost a shard read either"


def test_a_new_candidate_reopens_a_residual_shard(collection, reads):
    """Yielding must not be permanent: a document that becomes eligible
    later (rdf/fts progress, a grown corpus) in a shard that went residual
    is picked up — and only THAT shard is opened for it."""
    con = db.connect(collection)
    for _ in range(40):
        if genesis.drain_next_shard(con, collection) is None:
            break
    assert genesis.drain_next_shard(con, collection) is None

    _write_shard(collection.corpus_dir / "data" / "tr" / "002.parquet", [
        ("20", 0, "E", "epsilon one")])
    discovery.sync_collection(con, collection)
    _eligible(con, [_uid(20)])

    before = len(reads)
    result = genesis.drain_next_shard(con, collection)
    assert result is not None and result["docs"] == 1, \
        "a newly eligible document must be picked up by the archive"
    assert reads[before:] == ["data/tr/002.parquet"]


def test_a_residual_shard_is_not_reread_until_it_has_a_new_candidate(
        isolated_config, tmp_path, monkeypatch, reads):
    monkeypatch.setattr(embeddings, "embed_texts", _same_function_embed)
    c = _collection(tmp_path)
    _write_shard(c.corpus_dir / "data" / "tr" / "000.parquet", [
        ("10", 0, "A", "alpha one"),
        ("12", 0, "C", "straddle start"),
    ])
    _write_shard(c.corpus_dir / "data" / "tr" / "001.parquet", [
        ("12", 1, "C", "straddle end"),
    ])
    con = db.connect(c)
    discovery.sync_collection(con, c)

    # only the straddler is eligible: its two-segment locator says no single
    # shard read can cover it — no read at all, not even once
    _eligible(con, [_uid(12)])
    for _ in range(5):
        assert genesis.drain_next_shard(con, c) is None
    assert reads == [], "a straddler is known from its locator; it costs no read"

    # a NEW candidate in the same shard: one read, and it imports. The read
    # opens only the candidate's row groups (amendment 45), so the
    # straddler is not even seen by it — it used to be walked and counted
    # as a shape mismatch — and still costs nothing afterwards
    _eligible(con, [_uid(10)])
    r = genesis.drain_next_shard(con, c)
    assert r is not None and r["docs"] == 1 and r["skipped_shape"] == 0
    assert reads == ["data/tr/000.parquet"]
    assert genesis.drain_next_shard(con, c) is None
    assert reads == ["data/tr/000.parquet"]

    # a RESTART forgets the process-local residual record — and must not
    # buy a re-read for the straddler: this is the case that grows by one
    # shard per shard boundary fts passes, an hour of reads per restart
    genesis.reset_revisit_state()
    assert genesis.drain_next_shard(con, c) is None
    assert reads == ["data/tr/000.parquet"], "no re-read after restart"


def test_an_unloadable_candidate_is_residual_after_one_read(
        isolated_config, tmp_path, monkeypatch, reads):
    """The other residual class — a candidate whose chunk text will not
    load — cannot be known from the locator, so it costs exactly one read
    and is then remembered for its shard."""
    monkeypatch.setattr(embeddings, "embed_texts", _same_function_embed)
    c = _collection(tmp_path)
    _write_shard(c.corpus_dir / "data" / "tr" / "000.parquet", [
        ("10", 0, "A", "alpha one")])
    con = db.connect(c)
    discovery.sync_collection(con, c)
    _eligible(con)
    # the seam a missing/unreadable shard actually breaks at, for the
    # pooled apply loop as for the serial one it replaced
    monkeypatch.setattr(plugin_io, "sections",
                        lambda plugin, row: (_ for _ in ()).throw(OSError("gone")))
    r = genesis.drain_next_shard(con, c)
    assert r is not None and r["docs"] == 0 and r["residual_uids"] == [_uid(10)]
    assert reads == ["data/tr/000.parquet"]
    for _ in range(3):
        assert genesis.drain_next_shard(con, c) is None
    assert reads == ["data/tr/000.parquet"]


def test_residual_state_is_keyed_on_the_manifest_not_the_name(isolated_config, tmp_path,
                                                              monkeypatch, reads):
    """Two collections may share a name and point at different files; the
    residual record must not leak across them (the counts cache, fts index
    cache and scan cursor each learned this separately)."""
    monkeypatch.setattr(embeddings, "embed_texts", _same_function_embed)
    a = _collection(tmp_path / "a")
    b = _collection(tmp_path / "b")
    for c in (a, b):
        _write_shard(c.corpus_dir / "data" / "tr" / "000.parquet", [
            ("10", 0, "A", "alpha one")])
    con_a, con_b = db.connect(a), db.connect(b)
    discovery.sync_collection(con_a, a)
    discovery.sync_collection(con_b, b)
    _eligible(con_a)
    _eligible(con_b)
    # a's chunk text will not load (its residual class); b's is fine
    real = plugin_io.sections
    a_corpus = str(a.corpus_dir)

    def flaky(plugin, row):
        if str(row["path"]).startswith(a_corpus):
            raise OSError("gone")
        return real(plugin, row)
    monkeypatch.setattr(plugin_io, "sections", flaky)

    r = genesis.drain_next_shard(con_a, a)
    assert r is not None and r["docs"] == 0
    assert genesis.drain_next_shard(con_a, a) is None        # a: residual
    assert len(reads) == 1
    r = genesis.drain_next_shard(con_b, b)
    assert r is not None and r["docs"] == 1, \
        "b's shard must still be read — a's residual is not b's"


# --- 3. batching while ingest is pending ------------------------------------

def test_with_ingest_pending_a_read_waits_for_a_batch_or_the_age_cap(
        isolated_config, tmp_path, monkeypatch, reads):
    monkeypatch.setattr(embeddings, "embed_texts", _same_function_embed)
    monkeypatch.setattr(config, "GENESIS_MIN_CANDIDATES", 3)
    monkeypatch.setattr(config, "GENESIS_MAX_WAIT_SEC", 600)
    c = _collection(tmp_path)
    _write_shard(c.corpus_dir / "data" / "tr" / "000.parquet", [
        (str(n), 0, f"T{n}", f"text {n}") for n in (10, 11, 12, 13)])
    con = db.connect(c)
    discovery.sync_collection(con, c)
    now = int(time.time())

    _eligible(con, [_uid(10), _uid(11)])
    con.execute("UPDATE documents SET updated_at=? WHERE fts_done=1", (now,))
    assert genesis.drain_next_shard(con, c, eager=False) is None, \
        "two fresh candidates are short of a batch while rdf/fts wait"
    assert reads == []

    _eligible(con, [_uid(12)])
    con.execute("UPDATE documents SET updated_at=? WHERE fts_done=1", (now,))
    r = genesis.drain_next_shard(con, c, eager=False)
    assert r is not None and r["docs"] == 3, "a full batch is read at once"
    assert reads == ["data/tr/000.parquet"]

    # one straggler, short of a batch: waits ... until it is old enough
    _eligible(con, [_uid(13)])
    con.execute("UPDATE documents SET updated_at=? WHERE uid=?", (now, _uid(13)))
    assert genesis.drain_next_shard(con, c, eager=False) is None
    con.execute("UPDATE documents SET updated_at=? WHERE uid=?", (now - 601, _uid(13)))
    r = genesis.drain_next_shard(con, c, eager=False)
    assert r is not None and r["docs"] == 1, "the age cap bounds the wait"


def test_with_nothing_else_to_do_every_candidate_is_taken_at_once(
        isolated_config, tmp_path, monkeypatch, reads):
    monkeypatch.setattr(embeddings, "embed_texts", _same_function_embed)
    monkeypatch.setattr(config, "GENESIS_MIN_CANDIDATES", 1000)
    c = _collection(tmp_path)
    _write_shard(c.corpus_dir / "data" / "tr" / "000.parquet", [
        ("10", 0, "A", "alpha one")])
    con = db.connect(c)
    discovery.sync_collection(con, c)
    _eligible(con)
    con.execute("UPDATE documents SET updated_at=?", (int(time.time()),))
    r = genesis.drain_next_shard(con, c)            # eager by default
    assert r is not None and r["docs"] == 1


# --- 4. plan guard ----------------------------------------------------------

def test_candidate_walk_is_an_index_range_never_a_sort(collection):
    con = db.connect(collection)
    plan = " ".join(str(r[-1]) for r in con.execute(
        "EXPLAIN QUERY PLAN " + genesis._CANDIDATES_PAGE_SQL, (-1, 10)))
    assert "idx_documents_scope" in plan, plan
    assert "TEMP B-TREE" not in plan.upper(), plan


# --- 5. through the worker --------------------------------------------------

def test_a_pipeline_thread_that_owns_genesis_still_ingests(isolated_config, tmp_path,
                                                            monkeypatch, reads):
    """The live shape: embedded plugin, local archive, a thread running the
    FULL _run_one_step (CPU encoder → owns_own_embed). Nothing is eligible
    at first, so rdf and fts must run — genesis has no business opening a
    shard — and once fts commits, genesis imports; the total number of
    shard reads is bounded by the shards that hold candidates."""
    monkeypatch.setattr(embeddings, "embed_texts", _same_function_embed)
    monkeypatch.setattr(config, "FTS_BATCH_DOCS", 2)
    monkeypatch.setattr(config, "RDF_BATCH_DOCS", 2)
    c = _collection(tmp_path)
    shards = 6
    for i in range(shards):
        _write_shard(c.corpus_dir / "data" / "tr" / f"{i:03d}.parquet", [
            (str(100 + i), 0, f"T{i}", f"text {i} one"),
            (str(100 + i), 1, f"T{i}", f"text {i} two"),
        ])
    con = db.connect(c)
    discovery.sync_collection(con, c)
    total = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert total == shards

    # round 1: nothing eligible → rdf runs, no shard is opened
    assert worker._run_one_step(con, c, {}, pipeline_active=False) is True
    assert con.execute("SELECT SUM(rdf_done) FROM documents").fetchone()[0] >= 1
    assert reads == [], "genesis must not open a shard with no candidate"

    # the worker's rounds, as the pipeline thread runs them
    for _ in range(60):
        if not worker._run_one_step(con, c, {}, pipeline_active=False):
            break
    rdf, fts_, emb = con.execute(
        "SELECT SUM(rdf_done), SUM(fts_done), SUM(embed_done) FROM documents").fetchone()
    assert (rdf, fts_, emb) == (total, total, total), (rdf, fts_, emb)
    assert len(reads) <= shards, \
        f"{len(reads)} shard reads for {shards} shards — the sweep is back"
    assert worker._run_one_step(con, c, {}, pipeline_active=False) is False, \
        "caught up: the round is idle, not another shard read"

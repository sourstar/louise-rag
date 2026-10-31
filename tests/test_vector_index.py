"""The local vector index (pack-format.md §5, appendix A): built once a
table is big enough, probed by search when present, refreshed as rows
arrive, and never a wrong answer — the probe returns the flat scan's
neighbours on this data."""
import numpy as np
import pyarrow as pa
import pytest

from app import collections as collections_mod, config, embeddings, vector_index

DIM = 8


@pytest.fixture
def collection(isolated_config, tmp_path):
    return collections_mod.Collection(
        name="vx", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb")


def _fill(collection, n: int, seed: int = 0, start: int = 0):
    rng = np.random.default_rng(seed)
    vecs = rng.normal(size=(n, DIM)).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    rows = [{"id": f"c{start + i}", "book_id": start + i, "lang": "en", "seq": 0, "chapter": "",
             "text": f"t{start + i}", "vector": vecs[i].astype(np.float16)} for i in range(n)]
    conn = embeddings._conn(collection)
    if embeddings.TABLE in conn.table_names():
        conn.open_table(embeddings.TABLE).add(rows)
    else:
        schema = pa.schema([
            pa.field("id", pa.string()), pa.field("book_id", pa.int64()), pa.field("lang", pa.string()),
            pa.field("seq", pa.int64()), pa.field("chapter", pa.string()), pa.field("text", pa.string()),
            pa.field("vector", pa.list_(pa.float16(), DIM))])
        conn.create_table(embeddings.TABLE, data=rows, schema=schema)
    return vecs


def _flat_top(collection, q, k=5):
    """The exact answer: a true flat scan, index or not (a plain search on
    an indexed table would silently use the index, unrefined)."""
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    return [r["id"] for r in tbl.search(q).bypass_vector_index().limit(k).to_list()]


@pytest.mark.parametrize("kind", ["ivf_flat", "ivf_pq"])
def test_build_probe_and_refresh(collection, monkeypatch, kind):
    vecs = _fill(collection, 600)
    st = vector_index.stats(collection)
    assert {k: st[k] for k in ("rows", "indexed", "book_id_indexed")} == {
        "rows": 600, "indexed": False, "book_id_indexed": False}
    # fragments ride the same call since 2026-09-17: `compact_due` reads
    # its trigger from here, so a table that cannot say has none
    assert st["fragments"] and st["small_fragments"] is not None
    assert not vector_index.has_index(collection)
    monkeypatch.setattr(config, "VECTOR_INDEX_TYPE", kind)
    out = vector_index.build(collection, partitions=4)
    assert out["built"] and out["type"] == kind and out["partitions"] == 4
    st = vector_index.stats(collection)
    assert st["indexed"] and st["rows"] == 600 and st["unindexed_rows"] == 0
    assert vector_index.has_index(collection)
    # the probe finds what the flat scan finds (the query IS a stored vector)
    monkeypatch.setattr(config, "VECTOR_INDEX_NPROBES", 4)
    # PQ over 8 random dims is coarse (one 8-bit code per vector), so the
    # re-rank is given the whole table here: 120 x 5 = 600 candidates.
    # What the test pins is the mechanism — probe + re-rank reproduce the
    # exact neighbours — not PQ's accuracy on toy data (appendix A has that)
    monkeypatch.setattr(config, "VECTOR_INDEX_REFINE", 120)
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    for i in (3, 250, 599):
        probed = [r["id"] for r in vector_index.query(tbl.search(vecs[i]), collection).limit(5).to_list()]
        assert probed[0] == f"c{i}"
        assert set(probed) == set(_flat_top(collection, vecs[i]))
    # rows added after the build are unindexed until a refresh folds them in
    _fill(collection, 100, seed=1, start=600)
    st = vector_index.stats(collection)
    assert st["rows"] == 700 and st["unindexed_rows"] == 100
    monkeypatch.setattr(config, "VECTOR_INDEX_REFRESH_ROWS", 50)
    did = vector_index.ensure(collection)
    assert did and did.get("refreshed")
    assert vector_index.stats(collection)["unindexed_rows"] == 0


def test_ensure_builds_only_past_the_row_threshold(collection, monkeypatch):
    _fill(collection, 300)
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_ROWS", 1000)
    assert vector_index.ensure(collection) is None
    assert not vector_index.has_index(collection)
    assert not vector_index.stats(collection)["book_id_indexed"]
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_ROWS", 200)
    monkeypatch.setattr(config, "VECTOR_INDEX_TYPE", "ivf_flat")
    monkeypatch.setattr(vector_index, "partitions_for", lambda rows: 4)
    first = vector_index.ensure(collection)             # book_id first (cheap)
    assert first and first["kind"] == "btree"
    out = vector_index.ensure(collection)               # then the vector index
    assert out and out["built"] and out["partitions"] == 4
    assert vector_index.stats(collection)["indexed"]


def test_ensure_asks_before_each_step_and_stops_when_told(collection, monkeypatch):
    """Each step is a Lance call of minutes that cannot be interrupted, so
    a pause is honoured between them (2026-09-15: pubmed's prune ran 485 s
    on a paused node, and more was queued behind it)."""
    steps: list[str] = []
    monkeypatch.setattr(vector_index, "stats", lambda c: {
        "rows": 10**6, "indexed": True, "book_id_indexed": True, "unindexed_rows": 10**6})
    monkeypatch.setattr(vector_index, "prune", lambda c, con=None, log=None:
                        steps.append("prune") or {"pruned": True})
    monkeypatch.setattr(vector_index, "refresh", lambda c, con=None, log=None:
                        steps.append("refresh") or {"refreshed": True})

    assert vector_index.ensure(collection, may_continue=lambda: False) is None
    assert steps == [], "paused before it began: nothing runs"

    answers = iter([True, False])                  # paused while the prune ran
    out = vector_index.ensure(collection, may_continue=lambda: next(answers))
    assert steps == ["prune"] and out == {"pruned": True}

    vector_index._pruned_at.pop(str(collection.lance_dir), None)
    steps.clear()
    vector_index.ensure(collection)                # the CLI: no gate, every step
    assert steps == ["prune", "refresh"]


def test_search_falls_back_to_the_flat_scan_without_an_index(collection, monkeypatch):
    vecs = _fill(collection, 50)
    monkeypatch.setattr(embeddings, "_embed_texts",
                        lambda texts, timeout=300, recipe=None, query=False: [vecs[7].tolist()])
    hits = embeddings.search("anything", k=3, collection=collection)
    assert hits and hits[0]["chunk_id"] == "c7"


def test_the_book_id_index_is_built_first_and_writes_still_agree(collection, monkeypatch):
    """Regression (2026-08-28, measured): every vector write is
    delete-then-insert keyed on book_id, and without an index the
    predicate scanned the whole table — 0.90 s per 500-document genesis
    flush on wikivec's 35M rows, about a fifth of an archive pass. The
    maintainer builds the book_id index BEFORE the vector index, and the
    filtered reads/writes return exactly what they did."""
    vecs = _fill(collection, 300)
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_ROWS", 200)
    monkeypatch.setattr(config, "VECTOR_INDEX_TYPE", "ivf_flat")
    monkeypatch.setattr(vector_index, "partitions_for", lambda rows: 4)
    st = vector_index.stats(collection)
    assert st["indexed"] is False and not st.get("book_id_indexed")
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    before = [r["id"] for r in tbl.search().where("book_id IN (7,9)").limit(10).to_list()]

    first = vector_index.ensure(collection)
    assert first["built"] and first["kind"] == "btree" and first["column"] == "book_id"
    assert vector_index.stats(collection)["book_id_indexed"] is True
    assert not vector_index.stats(collection)["indexed"], "the vector index is the NEXT round"
    # the same rows, through the index
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    assert [r["id"] for r in tbl.search().where("book_id IN (7,9)").limit(10).to_list()] == before
    # the next round builds the vector index, and a third does neither
    second = vector_index.ensure(collection)
    assert second["built"] and second["type"] == "ivf_flat"
    assert vector_index.ensure(collection) is None
    # writes still delete-then-insert correctly under the index
    monkeypatch.setattr(embeddings, "_embed_texts",
                        lambda texts, timeout=300, recipe=None, query=False: [vecs[0].tolist()])
    embeddings.upsert_book(7, "en", [{"id": "c7", "seq": 0, "chapter": "", "text": "t7"}], collection)
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    rows = [r for r in tbl.search().where("book_id = 7").limit(10).to_list()]
    assert len(rows) == 1 and rows[0]["id"] == "c7"


def test_refresh_is_due_by_rows_and_by_fraction(monkeypatch):
    """Regression (2026-08-27): a refresh compacted the 16.8M-row wikivec
    table for 41 minutes after 115k new rows. It is due at 50k rows AND
    2 % of the table — never every 50k rows on a table that size."""
    monkeypatch.setattr(config, "VECTOR_INDEX_REFRESH_ROWS", 50_000)
    monkeypatch.setattr(config, "VECTOR_INDEX_REFRESH_FRACTION", 0.02)
    assert not vector_index.refresh_due({"indexed": True, "rows": 16_800_000, "unindexed_rows": 115_000})
    assert vector_index.refresh_due({"indexed": True, "rows": 16_800_000, "unindexed_rows": 340_000})
    assert vector_index.refresh_due({"indexed": True, "rows": 300_000, "unindexed_rows": 50_000})
    assert not vector_index.refresh_due({"indexed": True, "rows": 300_000, "unindexed_rows": 49_999})
    assert not vector_index.refresh_due({"indexed": False, "rows": 10, "unindexed_rows": None})
    assert not vector_index.refresh_due(None)


def test_maintenance_marks_itself_busy_for_its_own_sake(collection, monkeypatch):
    """Index maintenance runs on its own thread (2026-08-27) and marks the
    collection busy — for ONE reason only: two maintenance passes must not
    run on the same table at once. It does NOT stop that collection's
    writers; see the test below for why that rule was reversed."""
    from app import worker
    _fill(collection, 300)
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_ROWS", 200)
    monkeypatch.setattr(config, "VECTOR_INDEX_TYPE", "ivf_flat")
    monkeypatch.setattr(vector_index, "partitions_for", lambda rows: 4)
    seen = []
    real_build = vector_index.build
    monkeypatch.setattr(vector_index, "build",
                        lambda c, **k: (seen.append(vector_index.busy(c)), real_build(c, **k))[1])
    assert vector_index.ensure(collection)["kind"] == "btree"   # book_id index first
    assert vector_index.ensure(collection)["built"]
    assert seen == [True], "busy for the duration of the build"
    assert not vector_index.busy(collection), "released afterwards"
    assert worker._vector_writes_allowed(collection)


def test_the_worker_maintains_the_index_off_the_gpu_loop_and_throttled(collection, monkeypatch):
    from app import db, node_state, worker
    _fill(collection, 300)
    con = db.connect(collection)
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_ROWS", 200)
    monkeypatch.setattr(config, "VECTOR_INDEX_TYPE", "ivf_flat")
    monkeypatch.setattr(vector_index, "partitions_for", lambda rows: 4)
    worker._vindex_last.clear()
    # paused: nothing runs, and nothing is stamped — due the moment it lifts
    node_state.set_worker_state("paused")
    assert worker._maintain_vector_index(con, collection) is False
    assert not vector_index.stats(collection)["book_id_indexed"]
    node_state.set_worker_state("running")
    assert worker._maintain_vector_index(con, collection) is True      # book_id index
    worker._vindex_last.clear()
    assert worker._maintain_vector_index(con, collection) is True      # vector index
    assert vector_index.stats(collection)["indexed"]
    events = [r["message"] for r in con.execute("SELECT message FROM events WHERE stage='index'")]
    assert any("vector index built" in m for m in events)
    assert any("book_id index built" in m for m in events)
    # within the refresh interval nothing runs again, however many rows arrived
    _fill(collection, 100, seed=2, start=300)
    assert worker._maintain_vector_index(con, collection) is False
    assert vector_index.stats(collection)["unindexed_rows"] == 100
    # past the interval, the new rows are folded in
    worker._vindex_last.clear()
    monkeypatch.setattr(config, "VECTOR_INDEX_REFRESH_ROWS", 50)
    assert worker._maintain_vector_index(con, collection) is True
    assert vector_index.stats(collection)["unindexed_rows"] == 0


def test_writers_keep_working_while_the_index_is_maintained(collection, monkeypatch):
    """Regression (2026-08-28, measured live): the maintainer marked a
    collection busy and its embed step AND archive pass stood down for the
    whole maintenance window — 41 minutes for wikivec's 70 GB table, hit
    every few minutes of work, so the collection spent most of its life
    idle. Lance is MVCC (measured: 210 concurrent delete+add writes during
    optimize(), zero errors); writers now keep going and a rare commit
    conflict is a DEFERRAL, never an error on the document."""
    from app import worker
    _fill(collection, 300)
    with vector_index.maintenance(collection):
        assert vector_index.busy(collection) is True, "maintenance still marks itself"
        assert worker._vector_writes_allowed(collection) is True, "writers are NOT blocked"


def test_a_vector_store_conflict_defers_the_document_instead_of_erroring_it(collection, monkeypatch):
    from app import db, embeddings, worker
    _fill(collection, 10)
    con = db.connect(collection)
    con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,rdf_done,fts_done,"
                "embed_done) VALUES('g:1','g','1','/x',1,1,1,0)")
    row = con.execute("SELECT * FROM documents WHERE uid='g:1'").fetchone()

    class Conflict(RuntimeError):
        pass
    monkeypatch.setattr(worker, "_load_chunks",
                        lambda r, c: [{"id": "g:1:0:1", "seq": 0, "chapter": "", "text": "t"}])
    monkeypatch.setattr(embeddings, "upsert_book",
                        lambda *a, **k: (_ for _ in ()).throw(
                            Conflict("Commit conflict: concurrent update to the dataset")))
    assert worker.vector_store_contended(Conflict("Commit conflict: x")) is True
    assert worker.vector_store_contended(ValueError("a real bug")) is False
    assert worker._execute_step(con, collection, "embed", row) is False, "deferred"
    got = con.execute("SELECT embed_done, error, retries FROM documents WHERE uid='g:1'").fetchone()
    assert got["embed_done"] == 0 and got["error"] is None and got["retries"] == 0, dict(got)
    events = [r["message"] for r in con.execute("SELECT message FROM events WHERE level='warn'")]
    assert any("vector store busy" in m for m in events)
    con.close()


def test_refresh_is_skipped_when_the_disk_is_nearly_full(isolated_config, tmp_path,
                                                          monkeypatch):
    """optimize() COMPACTS before it prunes, so on a full pool it makes
    things worse before it makes them better. Measured on the reference node
    at 92% full: a compaction took free space from 116 GB to 93 GB without
    releasing anything. A maintenance pass must never fill the disk."""
    from app import collections as collections_mod, vector_index

    c = collections_mod.Collection(
        name="c", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance", embed=None)

    called = {"optimize": False}

    class FakeTable:
        def optimize(self, **kw):
            called["optimize"] = True

    monkeypatch.setattr(vector_index, "_table", lambda coll: FakeTable())
    monkeypatch.setattr(vector_index, "free_bytes", lambda coll: 10 * 2**30)
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_FREE_GB", 150.0)

    out = vector_index.refresh(c)
    assert out["refreshed"] is False and out["reason"] == "low disk"
    assert called["optimize"] is False, "compacted anyway on a full disk"

    # with headroom it proceeds
    monkeypatch.setattr(vector_index, "free_bytes", lambda coll: 500 * 2**30)
    monkeypatch.setattr(vector_index, "stats", lambda coll: {"rows": 1})
    vector_index.refresh(c)
    assert called["optimize"] is True


class _Cleanup:
    def __init__(self, old_versions=0, bytes_removed=0):
        self.old_versions, self.bytes_removed = old_versions, bytes_removed


class _FakeDataset:
    """The Lance dataset `to_lance()` hands back: the two jobs lancedb's
    `optimize()` welds together, reachable apart."""

    def __init__(self, calls, removed=0):
        self.calls, self.removed = calls, removed
        self.optimize = self

    def cleanup_old_versions(self, older_than=None, **kw):
        self.calls.append(("cleanup", older_than))
        return _Cleanup(old_versions=self.removed, bytes_removed=self.removed * 1000)

    def compact_files(self, **kw):
        self.calls.append(("compact", kw.get("target_rows_per_fragment")))

    def optimize_indices(self, **kw):
        self.calls.append(("index", None))


def _table_with_dataset(monkeypatch, vector_index, calls, *, removed=0, versions=10,
                        pylance=True, fragments=(50, 40)):
    class FakeTable:
        version = 7

        def list_versions(self):
            return [None] * versions

        def stats(self):
            return {"fragment_stats": {"num_fragments": fragments[0],
                                       "num_small_fragments": fragments[1]}}

        def count_rows(self):
            return 1000

        def optimize(self, **kw):
            calls.append(("welded", kw.get("cleanup_older_than")))

        def to_lance(self):
            if not pylance:
                raise ImportError("The lance library is required to use this function.")
            return _FakeDataset(calls, removed)
    tbl = FakeTable()
    monkeypatch.setattr(vector_index, "_table", lambda coll: tbl)
    monkeypatch.setattr(vector_index, "free_bytes", lambda coll: 500 * 2**30)
    return tbl


def test_a_prune_drops_versions_and_compacts_nothing(isolated_config, tmp_path, monkeypatch):
    """The two were one call, and the expensive half ran hourly while the
    index refresh behind it starved. Measured on wikipedia's 155 GiB table
    (2026-09-17): cleanup alone 18.5 s, the welded call 40–51 minutes."""
    from app import collections as collections_mod, vector_index
    c = collections_mod.Collection(
        name="c", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance", embed=None)
    calls = []
    _table_with_dataset(monkeypatch, vector_index, calls, removed=2176)
    said = []
    out = vector_index.prune(c, log=said.append)

    assert [k for k, _ in calls] == ["cleanup"], "a prune must not compact"
    assert out["versions_removed"] == 2176 and out["pruned"] is True
    assert said and said[0].startswith("pruned 2176 superseded Lance version(s)")


def test_without_the_lance_package_a_prune_still_works(isolated_config, tmp_path, monkeypatch):
    """No pylance: `to_lance()` raises and the welded call is all there is."""
    from app import collections as collections_mod, vector_index
    c = collections_mod.Collection(
        name="c", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance", embed=None)
    calls = []
    _table_with_dataset(monkeypatch, vector_index, calls, pylance=False)
    vector_index.prune(c, log=lambda m: None)
    assert [k for k, _ in calls] == ["welded"]


def test_a_compaction_waits_for_enough_small_fragments(monkeypatch):
    from app import vector_index
    monkeypatch.setattr(config, "VECTOR_INDEX_COMPACT_FRAGMENTS", 4000)
    assert not vector_index.compact_due({"small_fragments": 3999})
    assert vector_index.compact_due({"small_fragments": 4000})
    assert not vector_index.compact_due({"small_fragments": None})
    assert not vector_index.compact_due(None)


def test_a_compaction_takes_the_table_and_writers_defer(isolated_config, tmp_path, monkeypatch):
    """A writer that commits under a rewrite takes its commit away: three
    of wikipedia's passes died that way in two days — "Retryable commit
    conflict … Rewrite" (2026-09-16, twice) and "Too many concurrent
    writers" (2026-09-17) — 40–51 minutes of work each. Marking the table
    busy was not enough: `_vector_writes_allowed` answered True for every
    kind of maintenance."""
    from app import collections as collections_mod, vector_index, worker
    c = collections_mod.Collection(
        name="c", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance", embed=None)
    calls, seen = [], []
    _table_with_dataset(monkeypatch, vector_index, calls, fragments=(5000, 4800))
    real = vector_index.rewrite

    class watched(real):
        def __enter__(self):
            out = super().__enter__()
            seen.append((vector_index.busy(c), vector_index.rewriting(c),
                         worker._vector_writes_allowed(c)))
            return out
    monkeypatch.setattr(vector_index, "rewrite", watched)

    assert worker._vector_writes_allowed(c) is True, "before: writers run"
    out = vector_index.compact(c, log=lambda m: None)
    assert out["compacted"] is True
    assert seen == [(True, True, False)], "busy, rewriting, and writers deferred"
    assert [k for k, _ in calls] == ["compact"]
    assert vector_index.rewriting(c) is False
    assert worker._vector_writes_allowed(c) is True, "and run again after it"


def test_only_a_rewrite_stands_the_writers_down(isolated_config, tmp_path):
    """A build or a refresh must not: they run often, Lance is MVCC, and
    standing down for each left the collection idle most of its life
    (2026-08-28). Only the compaction is exclusive."""
    from app import collections as collections_mod, vector_index, worker
    c = collections_mod.Collection(
        name="c", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance", embed=None)
    with vector_index.maintenance(c):
        assert vector_index.busy(c) is True
        assert vector_index.rewriting(c) is False
        assert worker._vector_writes_allowed(c) is True
    with vector_index.rewrite(c):
        assert vector_index.busy(c) is True, "a rewrite is maintenance too"
        assert worker._vector_writes_allowed(c) is False
    assert vector_index.busy(c) is False and vector_index.rewriting(c) is False


def test_the_index_refresh_is_not_starved_by_a_compaction(isolated_config, tmp_path, monkeypatch):
    """Both due: the index wins. An unindexed row is scanned by every
    query; a small fragment only makes reads and writes dearer."""
    from app import collections as collections_mod, vector_index
    c = collections_mod.Collection(
        name="c", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance", embed=None)
    steps = []
    monkeypatch.setattr(vector_index, "stats", lambda coll: {
        "rows": 10_000_000, "indexed": True, "book_id_indexed": True,
        "unindexed_rows": 4_700_000, "small_fragments": 9_000})
    monkeypatch.setattr(vector_index, "prune_due", lambda coll: False)
    monkeypatch.setattr(vector_index, "refresh", lambda coll, con=None, log=None:
                        steps.append("refresh") or {"refreshed": True})
    monkeypatch.setattr(vector_index, "compact", lambda coll, con=None, log=None, light=False:
                        steps.append("light" if light else "compact") or {"compacted": True})
    vector_index.ensure(c)
    assert steps == ["light", "refresh"], \
        "the index first, and the fragments in the same pass: a refresh due every pass " \
        "starved the compaction (normattiva, 2026-10-14: 33,701 fragments)"

    steps.clear()
    monkeypatch.setattr(vector_index, "compact", lambda coll, con=None, log=None, light=False:
                        steps.append("light" if light else "compact") or {"compacted": True})
    told = iter([True, False])                  # asked before anything, then after the light pass
    vector_index.ensure(c, may_continue=lambda: next(told))
    assert steps == ["light"], "a pause after the light pass is honoured"

    steps.clear()
    monkeypatch.setattr(vector_index, "stats", lambda coll: {
        "rows": 10_000_000, "indexed": True, "book_id_indexed": True,
        "unindexed_rows": 0, "small_fragments": 9_000})
    vector_index.ensure(c)
    assert steps == ["light"], "with the index current, the fragments get their turn"




def test_the_light_compaction_goes_first_even_where_the_full_one_is_due(isolated_config, tmp_path, monkeypatch):
    """München, 2026-10-30: wikipedia's 8,246 small fragments were past the
    full threshold, so the light pass was not due and the full one ran — and
    lost its commit to a writer after 4.5 minutes of ~10 cores. The light
    one merges them in seconds; the full one is due only if small ones remain."""
    from app import collections as collections_mod, vector_index
    c = collections_mod.Collection(
        name="c", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance", embed=None)
    steps = []
    monkeypatch.setattr(config, "VECTOR_INDEX_LIGHT_COMPACT_FRAGMENTS", 256)
    monkeypatch.setattr(config, "VECTOR_INDEX_COMPACT_FRAGMENTS", 4000)
    monkeypatch.setattr(vector_index, "stats", lambda coll: {
        "rows": 145_640_538, "indexed": True, "book_id_indexed": True,
        "unindexed_rows": 0, "small_fragments": 8_246})
    monkeypatch.setattr(vector_index, "prune_due", lambda coll: False)
    monkeypatch.setattr(vector_index, "compact", lambda coll, con=None, log=None, light=False:
                        steps.append("light" if light else "full") or {"compacted": True})
    vector_index.ensure(c)
    assert steps == ["light"]


def test_a_prune_counts_what_it_removed_while_writers_keep_committing(
        isolated_config, tmp_path, monkeypatch):
    """Pubmed's embed commits a version per document while optimize runs,
    and every one lands inside the retention window. Counted as before -
    after, a 146 s prune logged "pruned -1656 superseded Lance version(s)"
    (2026-09-15). The table's version number counts the writes."""
    from app import collections as collections_mod, vector_index

    c = collections_mod.Collection(
        name="c", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance", embed=None)

    class FakeTable:
        """`drops` old versions go, `writes` new ones arrive meanwhile."""
        def __init__(self, kept, version, drops, writes):
            self.kept, self.version, self.drops, self.writes = kept, version, drops, writes

        def list_versions(self):
            return [None] * self.kept

        def optimize(self, **kw):
            self.kept += self.writes - self.drops
            self.version += self.writes

    monkeypatch.setattr(vector_index, "free_bytes", lambda coll: 500 * 2**30)
    monkeypatch.setattr(vector_index, "_table", lambda coll: tbl)
    said = []

    tbl = FakeTable(kept=15_000, version=230_000, drops=300, writes=2_000)
    out = vector_index.prune(c, log=said.append)
    assert (out["versions_removed"], out["versions_written"]) == (300, 2_000)
    assert said == ["pruned 300 superseded Lance version(s), 16700 kept (0s)"]

    # nothing old enough to drop while writers are busy: nothing is claimed
    said.clear()
    tbl = FakeTable(kept=15_000, version=230_000, drops=0, writes=1_656)
    out = vector_index.prune(c, log=said.append)
    assert out["versions_removed"] == 0 and said == []


def test_a_long_call_says_it_started_before_it_runs(collection, monkeypatch):
    """München, 2026-10-23: normattiva's night logged nothing from 01:00 to
    06:00, and a refresh logs only when it ends (one of 10-22 read 20,838 s)
    — a call that cannot be interrupted read as a hang. The refresh and the
    compaction each write a line BEFORE the Lance call."""
    _fill(collection, 600)
    vector_index.build(collection, index_type="ivf_flat", partitions=4)
    _fill(collection, 200, seed=1, start=600)
    said: list[str] = []
    seen_at_call: list[int] = []
    real = vector_index._dataset

    def watched(tbl):
        ds = real(tbl)
        inner = ds.optimize.optimize_indices

        class Opt:
            def optimize_indices(self, **kw):
                seen_at_call.append(len(said))
                return inner(**kw)
        ds_proxy = type("DS", (), {"optimize": Opt()})()
        return ds_proxy
    monkeypatch.setattr(vector_index, "_dataset", watched)
    out = vector_index.refresh(collection, log=said.append)
    assert out["refreshed"]
    assert seen_at_call == [1] and said[0].startswith("vector index refresh started: 200 unindexed rows of 800")
    assert said[1].startswith("vector index refreshed: 200 unindexed rows folded in")

    said.clear()
    monkeypatch.setattr(vector_index, "_dataset", real)
    out = vector_index.compact(collection, log=said.append)
    assert out["compacted"]
    assert said[0].startswith("compaction started:") and said[1].startswith("compacted ")


def test_a_refresh_writes_a_delta_and_merges_the_deltas_past_the_cap(collection, monkeypatch):
    """München, 2026-10-23, on a clone of normattiva: the default call folded
    591,591 new rows into the 7.5M-row base, rewritten whole, in 404.9 s; a
    delta over the new rows took 93.1 s. Deltas pile up, and a query pays per
    index past five, so they are merged — never the base — past the cap."""
    monkeypatch.setattr(config, "VECTOR_INDEX_MAX_INDICES", 3)
    _fill(collection, 600)
    vector_index.build(collection, index_type="ivf_flat", partitions=4)
    asked: list[int | None] = []
    real = vector_index._dataset

    def spied(tbl):
        ds = real(tbl)
        inner = ds.optimize.optimize_indices

        class Opt:
            def optimize_indices(self, **kw):
                asked.append(kw.get("num_indices_to_merge"))
                return inner(**kw)
        return type("DS", (), {"optimize": Opt()})()
    monkeypatch.setattr(vector_index, "_dataset", spied)
    counts = []
    for i in range(4):
        _fill(collection, 50, seed=10 + i, start=1000 + 100 * i)
        out = vector_index.refresh(collection)
        assert out["refreshed"] and out["after"]["unindexed_rows"] == 0
        counts.append(out["after"]["indices"])
    assert asked[0] == 0, "a delta over the new rows, never the newest index rewritten"
    assert counts == [2, 3, 2, 3], "past the cap the deltas merge into one beside the base"
    assert 3 in asked, "the merge takes the deltas (indices - 1), never the base"


def test_the_light_compaction_merges_the_small_fragments_and_catches_the_book_id_index_up(collection, monkeypatch):
    """A COST test (München, 2026-10-30): wikipedia's table held 2,368 small
    fragments, 1.96 M rows outside the book_id index, and each 128-document
    delete or held-chunks read took 2.4–3.4 s — a third of its pipeline
    thread in genesis's delete. Bench: 2,000 small fragments, a 128-id
    delete 0.36–0.66 s; merged, 0.011–0.022 s. The light compaction merges
    the small ones only (no deletion is materialized, so no large fragment
    is rewritten) and folds the book_id index's new rows in as a delta; it
    is due long before the full one."""
    _fill(collection, 2000)
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    tbl.create_scalar_index("book_id", index_type="BTREE")
    for i in range(40):
        _fill(collection, 5, seed=i + 1, start=2000 + 5 * i)       # the per-flush appends
    tbl.delete("book_id = 3")                                       # a deletion in the big fragment
    monkeypatch.setattr(config, "VECTOR_INDEX_LIGHT_COMPACT_FRAGMENTS", 30)
    monkeypatch.setattr(config, "VECTOR_INDEX_COMPACT_FRAGMENTS", 4000)
    st = vector_index.stats(collection)
    assert vector_index.light_compact_due(st) and not vector_index.compact_due(st), st
    ds = vector_index._dataset(embeddings._conn(collection).open_table(embeddings.TABLE))
    big_before = {f.fragment_id for f in ds.get_fragments() if f.count_rows() > 1000}
    out = vector_index.compact(collection, log=lambda m: None, light=True)
    assert out["compacted"] is True, out
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    ds = vector_index._dataset(tbl)
    frags = ds.get_fragments()
    assert len(frags) <= 2, f"{len(frags)} fragments"
    assert {f.fragment_id for f in frags if f.count_rows() > 1000} == big_before, \
        "the large fragment is not rewritten (its deletion stays a deletion file)"
    assert ds.stats.index_stats("book_id_idx")["num_unindexed_rows"] == 0
    assert tbl.count_rows() == 2000 + 200 - 1
    assert not vector_index.light_compact_due(vector_index.stats(collection))


def test_a_write_waits_out_a_short_compaction_and_defers_past_a_long_one(collection):
    """A write refused at once during a light compaction (seconds) cost the
    embed deferred documents and a genesis pass its 10-minute backoff; it
    waits now, up to REWRITE_WAIT_S, and only then is a retryable conflict."""
    import threading
    import time
    from app import worker
    rw = vector_index.rewrite(collection, drain_s=1)
    rw.__enter__()
    released = threading.Timer(0.3, lambda: rw.__exit__(None, None, None))
    released.start()
    t0 = time.monotonic()
    with vector_index.writing(collection, wait_s=5):
        waited = time.monotonic() - t0
    assert 0.2 < waited < 3, waited
    rw = vector_index.rewrite(collection, drain_s=1)
    rw.__enter__()
    try:
        with pytest.raises(vector_index.RewriteInProgress) as e:
            with vector_index.writing(collection, wait_s=0.2):
                pass
        assert worker.vector_store_contended(e.value), "a deferral, never a document error"
    finally:
        rw.__exit__(None, None, None)


def test_genesis_writes_through_the_writer_registration():
    """genesis's flush committed under the manifest lock alone, so a
    compaction neither waited for it nor held it off: wikipedia's full pass
    lost its commit to a genesis Delete (München, 2026-10-30)."""
    import inspect
    from app.bundles import genesis
    src = inspect.getsource(genesis.import_shard)
    assert "embeddings._write_lock(con, collection)" in src
    assert "vector_write_lock(con)" not in src


def test_the_bundle_importer_writes_through_the_registration_and_defers_on_a_rewrite():
    import inspect
    from app.bundles import importer
    assert "embeddings._write_lock(con, collection)" in inspect.getsource(importer)
    assert "vector_write_lock(con)" not in inspect.getsource(importer)


def test_the_light_compaction_is_not_queued_behind_the_long_passes(isolated_config, tmp_path, monkeypatch):
    """München, 2026-10-30: fragments come back at 70–80 a minute, and the
    light pass waited behind a 253 s prune and a 305 s refresh, the table at
    ~2,000 small fragments meanwhile. It runs before both now, once a pass."""
    from app import collections as collections_mod, vector_index
    c = collections_mod.Collection(
        name="c", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance", embed=None)
    steps = []
    monkeypatch.setattr(vector_index, "stats", lambda coll: {
        "rows": 145_000_000, "indexed": True, "book_id_indexed": True,
        "unindexed_rows": 4_000_000, "small_fragments": 2_029})
    monkeypatch.setattr(vector_index, "prune_due", lambda coll: True)
    monkeypatch.setattr(vector_index, "prune", lambda coll, con=None, log=None:
                        steps.append("prune") or {})
    monkeypatch.setattr(vector_index, "refresh", lambda coll, con=None, log=None:
                        steps.append("refresh") or {"refreshed": True})
    monkeypatch.setattr(vector_index, "compact", lambda coll, con=None, log=None, light=False:
                        steps.append("light" if light else "full") or {"compacted": True})
    vector_index.ensure(c)
    assert steps == ["light", "prune", "refresh"], steps


def _small_fragments_appended(collection, n: int, start: int) -> None:
    for i in range(n):
        _fill(collection, 5, seed=start + i + 1, start=start + 5 * i)   # one commit, one fragment


def test_the_light_compaction_has_its_own_cadence_between_the_index_passes(collection, monkeypatch):
    """#107's AFTER (München, 2026-10-30): run once per index pass (every
    VECTOR_INDEX_REFRESH_S, 30 minutes), the light compaction let
    wikipedia's table drift from 206 small fragments to 2,101 in half an
    hour, and held_chunks of 128 from 0.43 s to 2.45 s. Between the passes
    the maintainer now asks every VECTOR_INDEX_LIGHT_CHECK_S, and the check
    reads the fragment statistics alone (no row count, no index statistics)."""
    import time
    from app import db, node_state, worker
    _fill(collection, 2000)
    embeddings._conn(collection).open_table(embeddings.TABLE).create_scalar_index(
        "book_id", index_type="BTREE")
    _small_fragments_appended(collection, 40, 2000)
    monkeypatch.setattr(config, "VECTOR_INDEX_LIGHT_COMPACT_FRAGMENTS", 30)
    monkeypatch.setattr(config, "VECTOR_INDEX_COMPACT_FRAGMENTS", 4000)
    con = db.connect(collection)
    node_state.set_worker_state("running")
    key = str(collection.lance_dir)
    worker._vindex_last[key] = time.time()             # the index's pass ran just now
    assert vector_index.small_fragments(collection) >= 30
    assert worker._maintain_vector_index(con, collection) is True
    assert vector_index.small_fragments(collection) < 30
    events = [r["message"] for r in con.execute("SELECT message FROM events WHERE stage='index'")]
    assert any(m.startswith("light-compacted") for m in events), events
    # within the light cadence nothing is asked, however many fragments arrived
    _small_fragments_appended(collection, 40, 3000)
    assert worker._maintain_vector_index(con, collection) is False
    assert vector_index.small_fragments(collection) >= 30
    # paused: nothing runs, nothing is stamped
    worker._vlight_last.clear()
    node_state.set_worker_state("paused")
    assert worker._maintain_vector_index(con, collection) is False
    assert key not in worker._vlight_last
    node_state.set_worker_state("running")
    assert worker._maintain_vector_index(con, collection) is True
    # a check with nothing due reads the fragment statistics only
    asked = []
    real_stats = vector_index.stats
    monkeypatch.setattr(vector_index, "stats", lambda c: asked.append(1) or real_stats(c))
    worker._vlight_last.clear()
    assert worker._maintain_vector_index(con, collection) is False
    assert asked == [], "the light check asks no row count and no index statistics"


def test_the_light_check_stands_down_while_the_table_is_held(collection, monkeypatch):
    _fill(collection, 50)
    _small_fragments_appended(collection, 10, 50)
    monkeypatch.setattr(config, "VECTOR_INDEX_LIGHT_COMPACT_FRAGMENTS", 5)
    ran = []
    monkeypatch.setattr(vector_index, "compact", lambda c, con=None, log=None, light=False:
                        ran.append(light) or {"compacted": True})
    with vector_index.maintenance(collection):         # a build or a refresh
        assert vector_index.light(collection) is None
    rw = vector_index.rewrite(collection, drain_s=1)
    rw.__enter__()
    try:
        assert vector_index.light(collection) is None
    finally:
        rw.__exit__(None, None, None)
    assert ran == []
    assert vector_index.light(collection) == {"compacted": True}
    assert ran == [True]
    monkeypatch.setattr(config, "VECTOR_INDEX_LIGHT_COMPACT_FRAGMENTS", 1000)
    assert vector_index.light(collection) is None, "not due below the threshold"


def test_the_lang_bitmap_comes_with_the_book_id_index_and_filters_agree(collection, monkeypatch):
    """München, 2026-10-31: on wikipedia's 146 M rows a `lang IN ('en')`
    prefilter took 2,107 ms against 86 ms unfiltered — Lance read every
    row's lang. A BITMAP (Lance's index for a column of few values) is built
    in the same cheap step as book_id's, and the filtered answer is the same."""
    _fill(collection, 300)
    rows = [{"id": f"d{i}", "book_id": 1000 + i, "lang": "it", "seq": 0, "chapter": "",
             "text": f"it{i}", "vector": np.ones(DIM, np.float16)} for i in range(20)]
    embeddings._conn(collection).open_table(embeddings.TABLE).add(rows)
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_ROWS", 200)
    assert vector_index.stats(collection)["lang_indexed"] is False
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    before = sorted(r["id"] for r in tbl.search().where("lang IN ('it')").limit(100).to_list())
    first = vector_index.ensure(collection)
    assert first["kind"] == "btree" and first["lang"]["kind"] == "bitmap"
    assert vector_index.stats(collection)["lang_indexed"] is True
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    assert sorted(r["id"] for r in tbl.search().where("lang IN ('it')").limit(100).to_list()) \
        == before == sorted(f"d{i}" for i in range(20))


def test_a_table_indexed_on_book_id_before_gets_its_lang_bitmap_in_its_own_step(collection,
                                                                                  monkeypatch):
    _fill(collection, 300)
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_ROWS", 200)
    vector_index.build_scalar(collection)                      # as before 2026-10-31
    st = vector_index.stats(collection)
    assert st["book_id_indexed"] and st["lang_indexed"] is False
    out = vector_index.ensure(collection)
    assert out["kind"] == "bitmap" and out["column"] == "lang"
    assert vector_index.stats(collection)["lang_indexed"] is True

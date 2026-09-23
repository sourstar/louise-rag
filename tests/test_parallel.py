"""The parse pool (app/parallel.py, amendment 47): child processes compute
chunks, metadata and pack records — the same results as the serial path,
one failure per document, and the writers stay in the worker."""
import time
from pathlib import Path

import pytest

from app import collections as collections_mod, config, db, discovery, layers, parallel, plugin_io, worker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def two_processes(monkeypatch):
    monkeypatch.setattr(config, "PARSE_PROCESSES", 2)
    parallel.shutdown()
    yield
    parallel.shutdown()


def _gutenberg(tmp_path, corpus_dir):
    d = tmp_path / "g"
    c = collections_mod.Collection(
        name="g", plugin="gutenberg", profile=None, embed_mode_default=None, corpus_dir=corpus_dir,
        manifest_path=d / "manifest.db", fts_dir=d / "fts", lance_dir=d / "lancedb")
    con = db.connect(c)
    discovery.sync_collection(con, c)
    rows = [dict(r) for r in con.execute("SELECT * FROM documents ORDER BY uid")]
    con.close()
    return c, rows


def test_children_compute_the_same_chunks_and_metadata_as_the_serial_path(isolated_config, tmp_path,
                                                                            corpus_dir, two_processes):
    c, rows = _gutenberg(tmp_path, corpus_dir)
    plugin = c.plugin_instance()
    serial_chunks = [layers.chunks_for_document(c, r, plugin_io.sections(plugin, r)) for r in rows]
    serial_meta = [plugin_io.metadata(plugin, r) for r in rows]
    assert parallel.pool() is not None and parallel.processes() == 2
    pooled = parallel.chunks_for_rows(c, rows)
    assert [ok for ok, _ in pooled] == [True] * 3
    assert [v for _, v in pooled] == serial_chunks
    metas = parallel.metadata_for_rows(c, rows)
    assert [v for _, v in metas] == serial_meta
    # a document that cannot load fails alone, with the corpus-fact class kept
    # (a book directory with no text file: UnindexableDocument, unprefixed)
    (tmp_path / "nowhere" / "10002").mkdir(parents=True)
    broken = dict(rows[1], path=str(tmp_path / "nowhere" / "10002"))
    out = parallel.chunks_for_rows(c, [rows[0], broken, rows[2]])
    assert out[0][0] and out[2][0] and not out[1][0]
    assert out[1][1]["type"] == "UnindexableDocument"
    with pytest.raises(Exception) as e:
        parallel.raise_for(out[1][1])
    assert type(e.value).__name__ == "UnindexableDocument"
    # the pool survives: same children, same answers
    assert [v for _, v in parallel.chunks_for_rows(c, rows)] == serial_chunks


def test_the_fts_and_rdf_stages_index_through_the_pool(isolated_config, tmp_path, corpus_dir, two_processes):
    c, rows = _gutenberg(tmp_path, corpus_dir)
    con = db.connect(c)
    for stage in ("rdf", "fts"):
        first = con.execute("SELECT * FROM documents WHERE error IS NULL ORDER BY uid LIMIT 1").fetchone()
        assert worker._execute_step(con, c, stage, first) is True
    marks = con.execute("SELECT SUM(rdf_done) r, SUM(fts_done) f, COUNT(*) n FROM documents").fetchone()
    assert (marks["r"], marks["f"]) == (marks["n"], marks["n"]), "the whole batch, through the pool"
    titles = {r["uid"]: r["title"] for r in con.execute("SELECT uid, title FROM documents")}
    assert all(titles.values()), "metadata from the children landed in the manifest"
    assert any(Path(c.fts_dir).rglob("*")), "the tantivy index was written by the worker"
    con.close()


def test_prefetch_prepares_the_next_container_once_in_the_background(isolated_config, tmp_path,
                                                                      two_processes, monkeypatch):
    """Regression (2026-08-28, measured): pubmed's pipeline spent 17 s of
    every ~38 s building the pack of the container a batch first touched,
    while eleven pool children idled — 34 builds an hour, each blocking a
    batch. The pool now PREPARES containers ahead, once per container."""
    from dataclasses import replace
    from app import licensing
    from app.plugins.pubmed import pack
    fixtures = Path(__file__).resolve().parent / "fixtures" / "pubmed-mini"
    d = tmp_path / "pm"
    c = collections_mod.Collection(
        name="pm", plugin="pubmed", profile=None, embed_mode_default=None, corpus_dir=fixtures,
        manifest_path=d / "manifest.db", fts_dir=d / "fts", lance_dir=d / "lancedb")
    c = replace(c, license_ack=licensing.make_ack(c))       # yellow class (invariant 9)
    con = db.connect(c)
    discovery.sync_collection(con, c)
    rows = [dict(r) for r in con.execute(
        "SELECT uid, native_id, path, lang FROM documents WHERE error IS NULL ORDER BY rowid")]
    con.close()
    plugin = c.plugin_instance()
    # the key is the CONTAINER, not the document
    keys = {plugin_io.prepare_key(plugin, r) for r in rows}
    assert len(keys) < len(rows) and all(str(k).endswith(".xml") for k in keys)
    # discovery already built the packs; drop them to watch a prepare rebuild
    for container in keys:
        pack.pack_path(Path(container)).unlink(missing_ok=True)
    pack.reset_caches()
    parallel.forget_prefetched()

    first = parallel.prefetch(c, rows)
    assert sorted(first) == sorted(keys), "one submission per container"
    assert parallel.prefetch(c, rows) == [], "already prepared: never submitted twice"
    # the children build them in the background; the packs appear
    deadline = time.time() + 60
    while time.time() < deadline and not all(pack.pack_path(Path(k)).is_file() for k in keys):
        time.sleep(0.2)
    assert all(pack.pack_path(Path(k)).is_file() for k in keys), "prepared by the pool"
    # ...and the stage that needs one no longer builds it
    built = []
    monkeypatch.setattr(pack, "build", lambda *a, **k: built.append(a) or 0)
    for r in rows[:3]:
        plugin_io.prepare(plugin, r)
    assert built == [], "a fresh pack is never rebuilt"


def test_prefetch_is_a_noop_without_a_pool(isolated_config, tmp_path, corpus_dir):
    c, rows = _gutenberg(tmp_path, corpus_dir)
    parallel.forget_prefetched()
    assert parallel.prefetch(c, rows) == [], "serial: nothing to prepare in the background"


def test_serial_path_needs_no_children(isolated_config, tmp_path, corpus_dir):
    assert config.PARSE_PROCESSES == 1 and parallel.pool() is None
    c, rows = _gutenberg(tmp_path, corpus_dir)
    out = parallel.chunks_for_rows(c, rows)
    assert len(out) == 3 and all(ok for ok, _ in out)


def test_pack_records_are_computed_by_the_pool(isolated_config, tmp_path, corpus_dir, two_processes):
    from app.pack import build as pack_build, manifest as manifest_mod, records
    c, rows = _gutenberg(tmp_path, corpus_dir)
    pack_dir = tmp_path / "packs" / "g"
    r = pack_build.build(c, pack_dir, records_per_shard=2, sign=False, log=lambda *_: None)
    assert r.records == 3 and r.skipped == 0
    m = manifest_mod.load(pack_dir)
    got = {rec["uid"]: rec for s in m["shards"] for rec in records.iter_records(pack_dir / s["path"])}
    assert set(got) == {"gutenberg:10001", "gutenberg:10002", "gutenberg:10003"}
    # byte-identical to the serial build
    parallel.shutdown()
    config.PARSE_PROCESSES = 1
    serial_dir = tmp_path / "packs" / "g-serial"
    pack_build.build(c, serial_dir, records_per_shard=2, sign=False, log=lambda *_: None)
    for s in m["shards"]:
        assert (pack_dir / s["path"]).read_bytes() == (serial_dir / s["path"]).read_bytes()


def test_a_pool_that_does_not_answer_never_blocks_the_caller(isolated_config, tmp_path,
                                                              corpus_dir, monkeypatch):
    """Regression (2026-08-28, found with py-spy on the live node): the
    pubmed pipeline thread sat in the executor forever — every child gone,
    the futures never completing — so the collection stopped dead and a
    SIGTERM could not have been honoured either. The pool is an
    OPTIMISATION: a batch it does not answer within PARSE_TIMEOUT_S is
    computed in-process, and the pool is rebuilt."""
    c, rows = _gutenberg(tmp_path, corpus_dir)
    expected = parallel.compute("chunks", c, rows)      # serial truth
    parallel.reset_failures()

    class DeadFuture:
        def result(self, timeout=None):
            raise TimeoutError("the pool never answered")

        def cancel(self):
            return True

    class DeadPool:
        def submit(self, *a, **k):
            return DeadFuture()

    monkeypatch.setattr(config, "PARSE_PROCESSES", 4)
    monkeypatch.setattr(config, "PARSE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(parallel, "pool", lambda: DeadPool())
    got = parallel.compute("chunks", c, rows)
    assert got == expected, "the batch is computed in-process, correctly"
    assert parallel.health()["failures"] == 1

    # it keeps working, and after PARSE_MAX_FAILURES it stops trying children
    monkeypatch.setattr(config, "PARSE_MAX_FAILURES", 2)
    assert parallel.compute("chunks", c, rows) == expected
    assert parallel.health()["disabled"] is True
    monkeypatch.undo()
    monkeypatch.setattr(config, "PARSE_PROCESSES", 4)
    assert parallel.pool() is None, "disabled: no children until a restart"
    parallel.reset_failures()


def test_a_broken_pool_falls_back_and_recovers(isolated_config, tmp_path, corpus_dir, monkeypatch):
    """A child that dies (the executor raises BrokenProcessPool) costs the
    batch nothing but a serial recompute, and the next call rebuilds."""
    from concurrent.futures.process import BrokenProcessPool
    c, rows = _gutenberg(tmp_path, corpus_dir)
    expected = parallel.compute("chunks", c, rows)
    parallel.reset_failures()

    class BrokenPool:
        def submit(self, *a, **k):
            raise BrokenProcessPool("a child died")

    monkeypatch.setattr(config, "PARSE_PROCESSES", 4)
    monkeypatch.setattr(parallel, "pool", lambda: BrokenPool())
    assert parallel.compute("chunks", c, rows) == expected
    assert parallel.health()["failures"] == 1
    parallel.reset_failures()
    got = parallel.health()
    # occupancy is a time average over whatever this test just did, so it is
    # reported but never pinned here — tests/test_parallel.py's own
    # occupancy tests are where its value is asserted
    assert got.pop("busy_children") >= 0
    assert got == {"processes": 4, "pool": False, "failures": 0, "disabled": False}


def test_the_window_stays_full_instead_of_draining_between_waves(isolated_config, tmp_path,
                                                                 corpus_dir, monkeypatch):
    """A COST test. `compute` used to submit `wave` tasks, wait for ALL of
    them, and only then submit the next wave — so at every boundary the
    children drained to idle while the wave's slowest task finished alone.
    With per-document costs spanning two orders of magnitude (wikipedia:
    median ~0 ms, tail past 100 ms) that last task is reliably far behind,
    and the barrier cost most of the pool: MEASURED on the node,
    2026-09-22, a document costs 59 ms of CPU alone and the pipeline thread
    saw 41 ms per document with ELEVEN children — 1.4x where the pool
    exists to give ten.

    The cap on how many children one collection may occupy is unchanged;
    what changed is that a slot is refilled as soon as its result is taken.
    """
    c, rows = _gutenberg(tmp_path, corpus_dir)
    rows = (rows * 40)[:400]                      # enough parts to see waves
    expected = parallel.compute("chunks", c, rows)        # serial truth
    parallel.reset_failures()

    submits: list[int] = []                       # in-flight count at each submit

    class Fake:
        def __init__(self, part):
            self.part = part
            self.taken = False

        def result(self, timeout=None):
            self.taken = True
            Pool.live -= 1                    # the CLASS attribute submit() reads
            return parallel._compute("chunks", c, self.part)

        def cancel(self):
            return True

    class Pool:
        live = 0

        def submit(self, _fn, _kind, _collection, part):
            submits.append(Pool.live)
            Pool.live += 1
            return Fake(part)

    pool = Pool()
    monkeypatch.setattr(config, "PARSE_PROCESSES", 11)
    monkeypatch.setattr(parallel, "pool", lambda: pool)
    parallel.forget_users()
    # A SECOND COLLECTION holding slots, because that is the only way the
    # boundary exists at all: a batch is cut into ~PARTS_PER_CHILD tasks per
    # child and the window is IN_FLIGHT_PER_CHILD per child, so a collection
    # ALONE always submits every part at once and never refills. Halve its
    # admission and the parts no longer fit — which is exactly the live case
    # this was found in, and the case the old code drained the pool in.
    parallel._want("another-collection", 1)
    parallel._occupy("another-collection", 12)
    n = parallel.processes()
    window = parallel.slots("g", n * parallel.IN_FLIGHT_PER_CHILD)
    got = parallel.compute("chunks", c, rows)

    assert got == expected, "order and content are unchanged"
    assert len(submits) > window, "the batch is cut into more parts than one window"
    assert max(submits) == window - 1, "the window fills to the cap and no further"
    # the whole point: after the window first fills, no submission ever finds
    # the pool empty again — that emptiness WAS the wave barrier
    assert min(submits[window:]) == window - 1, \
        f"the children drained between waves: in-flight at submit {submits}"
    # the batch gave back every slot it took, and left the other collection's
    # holding exactly as it found it
    assert parallel.held() == {"another-collection": 12}
    assert parallel.wanting() == {"another-collection": 1}


def test_a_batch_is_not_cut_finer_than_the_children_can_use(
        isolated_config, tmp_path, corpus_dir, monkeypatch):
    """A COST test, and a REGRESSION test for a fix that made things worse.

    The tempting reasoning: a part is the unit of load balancing, so cutting
    finer stops one dear document idling a child, and it "costs nothing per
    row" because a part still carries MIN_PART rows and the bytes pickled
    are the same bytes. That was deployed on 2026-09-23 at eight tasks per
    child and measured on the node:

        wikipedia rdf   6.3 -> 28.8 ms/doc
        wikipedia fts  10.2 -> 27.2 ms/doc
        worker CPU      77% -> 193% of 1200%
        throughput     54.2 -> 24.3 docs/s

    More CPU for less work is redundant computation. A part's cost is NOT
    independent of the other parts: wikipedia's documents come out of shared
    multistream blocks, the cut is by equal COUNT, so every child holding a
    document from a block decompresses that whole block — four times the
    parts, four times the re-decompression.

    So the cut is bounded ABOVE, and this test is what says so. Balance can
    only be bought once the cut follows the blocks; until then, finer is
    slower.
    """
    c, rows = _gutenberg(tmp_path, corpus_dir)
    rows = (rows * 40)[:400]
    parts: list[int] = []

    class Fake:
        def __init__(self, part):
            self.part = part

        def result(self, timeout=None):
            return parallel._compute("chunks", c, self.part)

        def cancel(self):
            return True

    class Pool:
        def submit(self, _fn, _kind, _collection, part):
            parts.append(len(part))
            return Fake(part)

    monkeypatch.setattr(config, "PARSE_PROCESSES", 11)
    monkeypatch.setattr(parallel, "pool", lambda: Pool())
    parallel.forget_users()
    parallel.compute("chunks", c, rows)

    n = parallel.processes()
    assert sum(parts) == len(rows), "every row is computed exactly once"
    # THE CEILING. One spare task per child so no child waits on the parent,
    # and no more: past that, shared work is repeated rather than divided.
    assert len(parts) <= n * parallel.PARTS_PER_CHILD, \
        (f"{len(parts)} tasks for {n} children — cutting finer than "
         f"{parallel.PARTS_PER_CHILD} per child cost 4.6x live, see the "
         f"docstring")
    # ...and still more than one task per child, or there is no pool at all
    assert len(parts) > n, f"{len(parts)} tasks cannot occupy {n} children"
    # the last part is whatever is left over; every other one is full
    assert min(parts[:-1] or parts) >= parallel.MIN_PART, \
        f"a part smaller than MIN_PART pays more in pickling than it saves: {parts}"


# ------------------------------------------------- occupancy (the instrument)

def test_the_pool_reports_how_many_of_its_children_had_work(monkeypatch):
    """A COST test for the INSTRUMENT, not for the pool.

    A starved pool and a slow pool look identical from outside: both show
    long batches. On 2026-09-23 admission handed wikipedia 3 of 11 children
    for a day, and the batch log, `docker stats` and the sizer all reported
    "slow" — the sizer even fitted a cost model to a machine running on one
    core and shrank the batches, which read as the sizer working. The one
    number that tells the two apart is how many children actually had work,
    and nothing published it. This is that number.
    """
    monkeypatch.setattr(config, "PARSE_PROCESSES", 8)
    parallel.forget_users()
    assert parallel.occupancy()["busy_children"] == 0, "an idle pool is idle"

    parallel._occupy("wikipedia", 4)
    time.sleep(0.05)
    parallel._occupy("wikipedia", -4)
    got = parallel.occupancy()
    assert 0 < got["busy_children"] <= 4, got
    assert got["processes"] == 8
    assert got["utilisation"] == pytest.approx(got["busy_children"] / 8, abs=1e-3)

    # and the read RESET it: the next interval starts from nothing, or every
    # line would report the whole life of the process
    time.sleep(0.02)
    assert parallel.occupancy()["busy_children"] == 0


def test_queued_tasks_are_not_counted_as_extra_busy_children(monkeypatch):
    """A slot is a task in flight, and the window deliberately holds more
    tasks than children (IN_FLIGHT_PER_CHILD) so no child waits for the
    parent. A task queued behind a busy child is not a second busy child,
    and counting it would report a saturated pool exactly when it is not —
    the failure this instrument exists to catch."""
    monkeypatch.setattr(config, "PARSE_PROCESSES", 2)
    parallel.forget_users()
    parallel._occupy("wikipedia", 10)
    time.sleep(0.05)
    parallel._occupy("wikipedia", -10)
    got = parallel.occupancy()
    assert got["busy_children"] <= 2, f"ten tasks cannot busy more than two children: {got}"
    assert got["utilisation"] <= 1.0


def test_health_reports_occupancy_without_consuming_it(monkeypatch):
    """`health()` answers a question; `_log_shares` owns the meter. If a
    health check reset the interval, whoever polled it would silently blank
    the next log line."""
    monkeypatch.setattr(config, "PARSE_PROCESSES", 4)
    parallel.forget_users()
    parallel._occupy("wikipedia", 2)
    time.sleep(0.05)
    parallel._occupy("wikipedia", -2)
    first = parallel.health()["busy_children"]
    assert first > 0
    assert parallel.health()["busy_children"] > 0, "health() consumed the interval"
    assert parallel.occupancy()["busy_children"] > 0, "health() consumed the interval"

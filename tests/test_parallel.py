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


def test_held_record_lines_are_taken_and_cut_by_the_pool(isolated_config, tmp_path, corpus_dir,
                                                       two_processes, monkeypatch):
    """A build that takes its records' lines from held parts (a re-slicing
    daughter, a discarded snapshot's rebuild) runs them through the pool:
    in one process, wikipedia-v8's rebuild of 52.9 M records used one core
    of eight (München, 2026-10-30; operator: "multi threading is a must,
    not an option"). The bytes are the serial build's."""
    from app.pack import build as pack_build, manifest as manifest_mod
    c, _rows = _gutenberg(tmp_path, corpus_dir)
    parallel.shutdown()
    config.PARSE_PROCESSES = 1
    mother = tmp_path / "packs" / "mother"
    pack_build.build(c, mother, records_per_shard=2, sign=False, log=lambda *_: None)
    parts = tuple(s["path"] for s in manifest_mod.load(mother)["shards"])
    config.PARSE_PROCESSES = 2
    kinds: list[str] = []
    real = parallel._submit
    monkeypatch.setattr(parallel, "_submit", lambda ex, kind, col, part: kinds.append(kind) or real(ex, kind, col, part))
    held_dir = tmp_path / "packs" / "held"
    r = pack_build.build(c, held_dir, records_per_shard=2, sign=False, log=lambda *_: None,
                         held=pack_build.HeldRecords(mother, parts))
    assert r.records == 3 and r.skipped == 0
    assert kinds and set(kinds) == {"held-record"}, "every held record went to a child"
    for rel in parts:
        assert (held_dir / rel).read_bytes() == (mother / rel).read_bytes()


def test_a_build_under_a_changing_profile_writes_the_serial_bytes(isolated_config, tmp_path, monkeypatch):
    """The node profile (app/cpu_limit.py) changes how many parts run, never
    what they produce: a build whose limit goes 12 -> 0 (paused) -> 8 -> 1
    through real children writes every part byte-identical to a serial build."""
    from app import cpu_limit, node_profile
    from app.node_profile import Active, GpuLevel, Profile
    from app.pack import build as pack_build, manifest as manifest_mod
    from tests.conftest import gutenberg_books
    corpus = gutenberg_books(tmp_path / "many", 160)
    c, rows = _gutenberg(tmp_path, corpus)
    assert len(rows) == 160                          # 50 a page: several pages per slice

    def build(dest):
        said: list[str] = []
        pack_build.build(c, dest, records_per_shard=20, sign=False,
                         log=lambda m: said.append(m))
        return said
    serial_dir = tmp_path / "packs" / "serial"
    build(serial_dir)

    reads = {"n": 0}
    phases = [12, 12, 0, 0, 0, 0, 8, 8, 8, 1]

    def scripted(now=None):
        cores = phases[min(reads["n"], len(phases) - 1)]
        reads["n"] += 1
        return Active(Profile({0: "quiet", 12: "night"}.get(cores, "day"), cores, "full"),
                      GpuLevel("full"), None, None)
    monkeypatch.setattr(node_profile, "active", scripted)
    monkeypatch.setattr(cpu_limit, "TTL_S", 0.0)
    monkeypatch.setattr(cpu_limit, "PAUSE_TICK_S", 0.01)
    monkeypatch.setattr(config, "PARSE_PROCESSES", 3)
    cpu_limit.reset()                                # the serial build's read, cached for TTL_S
    parallel.shutdown()
    try:
        pooled_dir = tmp_path / "packs" / "pooled"
        said = build(pooled_dir)
    finally:
        parallel.shutdown()
    profile_lines = [m for m in said if "profile" in m]
    assert any("paused" in m for m in profile_lines) and any("8 cores" in m for m in profile_lines) \
        and any("1 core" in m for m in profile_lines), profile_lines
    assert reads["n"] >= len(phases), "every phase was in force for part of the build"
    m = manifest_mod.load(pooled_dir)
    assert [s["path"] for s in m["shards"]] == [s["path"] for s in manifest_mod.load(serial_dir)["shards"]]
    assert len(m["shards"]) >= 8
    for s in m["shards"]:
        assert (pooled_dir / s["path"]).read_bytes() == (serial_dir / s["path"]).read_bytes(), s["path"]


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
    assert got.pop("slots_in_flight") >= 0
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
    assert parallel.occupancy()["slots_in_flight"] == 0, "an idle pool is idle"

    parallel._occupy("wikipedia", 4)
    time.sleep(0.05)
    parallel._occupy("wikipedia", -4)
    got = parallel.occupancy()
    assert 0 < got["slots_in_flight"] <= 4, got
    assert got["processes"] == 8
    assert got["utilisation"] == pytest.approx(got["slots_in_flight"] / 8, abs=1e-3)

    # and the read RESET it: the next interval starts from nothing, or every
    # line would report the whole life of the process
    time.sleep(0.02)
    assert parallel.occupancy()["slots_in_flight"] == 0


def test_queued_tasks_are_not_counted_beyond_the_pool_size(monkeypatch):
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
    assert got["slots_in_flight"] <= 2, f"ten tasks cannot busy more than two children: {got}"
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
    first = parallel.health()["slots_in_flight"]
    assert first > 0
    assert parallel.health()["slots_in_flight"] > 0, "health() consumed the interval"
    assert parallel.occupancy()["slots_in_flight"] > 0, "health() consumed the interval"


# ------------------------------------------- cutting a batch along its units

class _Chunked:
    """A plugin whose rows come in shared units of `per` rows, each unit
    costing `sizes[i]` — wikipedia's multistream chunks in miniature."""

    def __init__(self, sizes, per):
        self.sizes, self.per = sizes, per

    def work_span(self, row):
        u = int(row["uid"]) // self.per
        return (f"chunk{u}", self.sizes[u])


def _rows(n):
    return [{"uid": str(i)} for i in range(n)]


def _units(part, per):
    return {int(r["uid"]) // per for r in part}


def test_a_batch_is_cut_between_units_never_through_one(monkeypatch):
    """THE 4.6x. wikipedia's pages arrive in multistream chunks of ~100 and
    `source._stream_pages` caches ONE chunk per process, so a chunk split
    across two children is decompressed twice. Cutting 8 parts per child
    instead of 2 put a third of a chunk in each part and cost 4.6x on the
    node (2026-09-23) — the defect was never the number of parts, it was
    cutting through the unit. No part may share a unit with another."""
    monkeypatch.setattr(config, "PARSE_PROCESSES", 4)
    per, n_units = 10, 24
    plugin = _Chunked([1000] * n_units, per)
    monkeypatch.setattr(parallel, "_spans",
                        lambda c, rows: [plugin.work_span(r) for r in rows])

    parts = parallel._partition(object(), _rows(per * n_units), 8)
    assert sum(len(p) for p in parts) == per * n_units, "every row exactly once"
    assert [r for p in parts for r in p] == _rows(per * n_units), "order kept"
    seen: dict = {}
    for i, p in enumerate(parts):
        for u in _units(p, per):
            assert u not in seen, \
                f"unit {u} is in part {seen[u]} AND part {i} — decompressed twice"
            seen[u] = i


def test_parts_are_balanced_by_cost_not_by_count(monkeypatch):
    """THE STRAGGLER TAIL. Equal COUNT is not equal WORK when the units
    differ: measured live, 3.7 of 11 children running on average, bursting
    to 11 and troughing at 0, because the batch ended with ten children
    waiting on one. Here one unit costs 100x its neighbours; cut by count it
    lands in a part like any other and that part takes 100x as long."""
    monkeypatch.setattr(config, "PARSE_PROCESSES", 4)
    per = 10
    sizes = [10] * 24
    sizes[5] = 10_000                       # one very dear chunk
    plugin = _Chunked(sizes, per)
    monkeypatch.setattr(parallel, "_spans",
                        lambda c, rows: [plugin.work_span(r) for r in rows])
    rows = _rows(per * len(sizes))

    parts = parallel._partition(object(), rows, 8)
    cost = [sum(sizes[u] for u in _units(p, per)) for p in parts]
    # the dear unit is alone, or nearly: the cut spends its budget on it
    dear = [i for i, p in enumerate(parts) if 5 in _units(p, per)][0]
    assert len(_units(parts[dear], per)) <= 2, \
        f"the expensive unit was bundled with {len(_units(parts[dear], per))-1} others"
    # and no OTHER part carries a wildly disproportionate share of the rest
    rest = sorted(c for i, c in enumerate(cost) if i != dear)
    assert rest[-1] <= 4 * max(1, rest[0]), f"parts still lopsided: {cost}"


def test_a_plugin_with_no_units_keeps_the_plain_equal_count_cut(monkeypatch):
    """gutenberg and pubmed: a document IS the unit, so equal count already
    is equal work and nothing here should change what they get."""
    monkeypatch.setattr(config, "PARSE_PROCESSES", 4)
    monkeypatch.setattr(parallel, "_spans", lambda c, rows: None)
    rows = _rows(100)
    parts = parallel._partition(object(), rows, 8)
    assert [r for p in parts for r in p] == rows
    assert len(parts) == 8 and {len(p) for p in parts} == {13, 100 - 13 * 7}


def test_a_plugin_whose_hint_raises_falls_back_instead_of_failing(monkeypatch):
    """The span is an optimisation hint. A plugin that cannot answer must
    cost a worse cut, never a failed batch."""
    monkeypatch.setattr(config, "PARSE_PROCESSES", 4)

    class Boom:
        def work_span(self, row):
            raise RuntimeError("no idea")

    class Coll:
        def plugin_instance(self):
            return Boom()

    rows = _rows(40)
    parts = parallel._partition(Coll(), rows, 8)
    assert [r for p in parts for r in p] == rows


def test_the_part_target_is_a_size_not_a_cap(monkeypatch):
    """A COST test for a 2x mistake caught in simulation, not on the node.

    Refusing to open part number `want` forces every remaining run into the
    last part. Simulated on real wikipedia locators that final part cost
    607k against equal-count's worst of 242k, and the makespan over eleven
    children came out 2.03x WORSE — while total work fell to 0.72x, so the
    idea was right and the greedy was wrong.

    Extra parts are free here in a way they are not for an equal-count cut:
    a part never splits a unit, so nothing is decompressed twice however
    many parts there are. The tail must be spread, not stacked."""
    monkeypatch.setattr(config, "PARSE_PROCESSES", 4)
    per = 10
    sizes = [10] * 60                       # many cheap units, one target
    plugin = _Chunked(sizes, per)
    monkeypatch.setattr(parallel, "_spans",
                        lambda c, rows: [plugin.work_span(r) for r in rows])
    rows = _rows(per * len(sizes))

    parts = parallel._partition(object(), rows, 8)
    cost = [sum(sizes[u] for u in _units(p, per)) for p in parts]
    assert [r for p in parts for r in p] == rows, "order kept"
    assert max(cost) <= 2 * (sum(cost) / len(cost)), \
        f"one part carries the tail: {cost}"


# ---- the page stream (compute_pages), München's profile of wikipedia-v8, 2026-10-24 -------------

def _done(value):
    from concurrent.futures import Future
    f = Future()
    f.set_result(value)
    return f


def test_the_pool_reads_the_next_page_before_the_caller_takes_this_one(isolated_config, tmp_path,
                                                                      corpus_dir, monkeypatch):
    """A COST test. `compute` per page waited for the page's slowest part
    before the next page was even read: the children idled behind every
    page's longest article (47.8 % of their samples; ~1.3 of 4 cpus).
    `compute_pages` submits page k+1's parts while page k is still with the
    caller, and gives the same results in the same order."""
    c, rows = _gutenberg(tmp_path, corpus_dir)
    rows = [dict(r) for r in (rows * 20)[:60]]          # one object per row: a part names its page
    pages = [rows[i:i + 12] for i in range(0, 60, 12)]
    truth = [parallel.compute("chunks", c, p) for p in pages]          # serial
    real = parallel._compute
    events: list[tuple[str, int]] = []

    class Pool:
        def submit(self, _fn, kind, collection, part):
            page = next(i for i, p in enumerate(pages) if any(part[0] is r for r in p))
            events.append(("submit", page))
            return _done(real(kind, collection, part))

    monkeypatch.setattr(config, "PARSE_PROCESSES", 3)
    monkeypatch.setattr(parallel, "pool", lambda: Pool())
    parallel.forget_users()
    got = []
    for k, out in enumerate(parallel.compute_pages("chunks", c, iter(pages))):
        events.append(("taken", k))
        got.append(out)
    assert got == truth
    first_taken = events.index(("taken", 0))
    assert ("submit", 1) in events[:first_taken], f"page 1 waited for page 0: {events}"
    assert parallel.held() == {} and parallel.wanting() == {}, "every slot given back"


def test_a_part_that_stalls_costs_only_itself_and_what_finished_is_kept(isolated_config, tmp_path,
                                                                      corpus_dir, monkeypatch):
    """The timeout bounds the wait for the NEXT answer, not the page: a
    stalled part is computed in-process after PARSE_TIMEOUT_S without
    progress, and every part that did answer is used, not computed again
    (15 whole pages were thrown away and re-parsed serially in 18 h)."""
    from concurrent.futures import Future
    c, rows = _gutenberg(tmp_path, corpus_dir)
    rows = (rows * 20)[:24]
    pages = [rows[:12], rows[12:]]
    truth = [parallel.compute("chunks", c, p) for p in pages]
    real = parallel._compute
    stalled = {"n": 0}

    class Pool:
        def submit(self, _fn, kind, collection, part):
            if stalled["n"] == 0:
                stalled["n"] += 1
                return Future()                       # never answers
            return _done(real(kind, collection, part))

    inprocess = []

    def counting(kind, collection, part):
        inprocess.append(len(part))
        return real(kind, collection, part)
    monkeypatch.setattr(config, "PARSE_PROCESSES", 3)
    monkeypatch.setattr(config, "PARSE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(parallel, "pool", lambda: Pool())
    monkeypatch.setattr(parallel, "_compute", counting)
    parallel.reset_failures()
    parallel.forget_users()
    assert list(parallel.compute_pages("chunks", c, iter(pages))) == truth
    assert len(inprocess) == 1, f"only the stalled part was computed in-process: {inprocess}"
    assert parallel.health()["failures"] == 1
    parallel.reset_failures()


def test_one_slow_part_does_not_hold_back_the_pages_after_it(isolated_config, tmp_path,
                                                           corpus_dir, monkeypatch):
    """A COST test (München, 2026-10-25, wikipedia-v8 at 12 children: the cpu
    sat at one core for stretches between 800 % readings). The stream waited
    on its OLDEST part alone and counted a part that had answered as holding
    a slot until its page's turn, so one long article held every child idle.
    Here the first part answers only once parts of later pages have been
    submitted: the old stream waits for it until the watchdog fires and
    computes it in-process; this one keeps submitting, and the results are
    the serial ones, in order, with no failure."""
    from concurrent.futures import Future
    c, rows = _gutenberg(tmp_path, corpus_dir)
    rows = [dict(r) for r in (rows * 30)[:72]]
    pages = [rows[i:i + 12] for i in range(0, 72, 12)]
    truth = [parallel.compute("chunks", c, p) for p in pages]
    real = parallel._compute
    first: dict = {}
    submitted: list[int] = []

    class Pool:
        def submit(self, _fn, kind, collection, part):
            page = next(i for i, p in enumerate(pages) if any(part[0] is r for r in p))
            submitted.append(page)
            if not first:
                first["f"], first["args"] = Future(), (kind, collection, part)
                return first["f"]
            if page >= 3 and not first["f"].done():
                first["f"].set_result(real(*first["args"]))   # the long article ends
            return _done(real(kind, collection, part))

    inprocess: list[int] = []

    def counting(kind, collection, part):
        inprocess.append(len(part))
        return real(kind, collection, part)
    monkeypatch.setattr(config, "PARSE_PROCESSES", 3)
    monkeypatch.setattr(config, "PARSE_TIMEOUT_S", 0.5)
    monkeypatch.setattr(parallel, "pool", lambda: Pool())
    monkeypatch.setattr(parallel, "_compute", counting)
    parallel.reset_failures()
    parallel.forget_users()
    assert list(parallel.compute_pages("chunks", c, iter(pages))) == truth
    assert inprocess == [], f"the slow part was given up on and computed in-process: {inprocess}"
    assert max(submitted) == len(pages) - 1
    parallel.reset_failures()
    assert parallel.held() == {} and parallel.wanting() == {}, "every slot given back"


def test_the_page_stream_through_real_children_gives_the_serial_results(isolated_config, tmp_path,
                                                                       corpus_dir, two_processes):
    c, rows = _gutenberg(tmp_path, corpus_dir)
    rows = (rows * 10)[:30]
    pages = [rows[i:i + 7] for i in range(0, 30, 7)]
    got = list(parallel.compute_pages("chunks", c, iter(pages)))
    parallel.shutdown()
    config.PARSE_PROCESSES = 1
    assert got == [parallel.compute("chunks", c, p) for p in pages]
    assert list(parallel.compute_pages("chunks", c, iter([]))) == []


def test_the_pool_is_sized_from_the_cpu_quota_not_the_host(tmp_path):
    """München's profile: `os.cpu_count()` read the host's 12 inside a
    4-cpu container, and 11 children shared 4 cpus at nice 19."""
    import os
    host = len(os.sched_getaffinity(0))
    (tmp_path / "v2").mkdir()
    (tmp_path / "v2" / "cpu.max").write_text("400000 100000\n")
    assert config.available_cpus(str(tmp_path / "v2")) == min(host, 4)
    (tmp_path / "v2" / "cpu.max").write_text("250000 100000\n")
    assert config.available_cpus(str(tmp_path / "v2")) == min(host, 3), "2.5 cpus: three"
    (tmp_path / "v2" / "cpu.max").write_text("max 100000\n")
    assert config.available_cpus(str(tmp_path / "v2")) == host, "no quota: the affinity"
    (tmp_path / "v1" / "cpu").mkdir(parents=True)
    (tmp_path / "v1" / "cpu" / "cpu.cfs_quota_us").write_text("200000\n")
    (tmp_path / "v1" / "cpu" / "cpu.cfs_period_us").write_text("100000\n")
    assert config.available_cpus(str(tmp_path / "v1")) == min(host, 2)
    assert config.available_cpus(str(tmp_path / "none")) == host


def test_the_records_build_takes_the_whole_quota_unless_told(monkeypatch):
    """The A/B on wikipedia-v8 (2026-10-25): 4 children in a 4-cpu quota were
    1.16x faster than 3, every part byte-identical. The worker keeps quota - 1."""
    monkeypatch.setattr(config, "available_cpus", lambda: 4)
    monkeypatch.delenv("PARSE_PROCESSES", raising=False)
    assert config.build_parse_processes() == 4
    monkeypatch.setenv("PARSE_PROCESSES", "2")
    assert config.build_parse_processes() == 2, "an operator's setting wins"


# ---- an idle pool gives its memory back (München, 2026-10-28) --------------
#
# Twelve children held 10.8 GiB, idle and resident 26-42 minutes after the
# quiet profile stopped parsing and through a pause; wikipedia-v8's first
# snapshot could not start beside them. The beat thread now releases a pool
# nobody has been inside for PARSE_IDLE_RELEASE_S.

def test_an_idle_pool_is_released_and_its_children_end(isolated_config, tmp_path, corpus_dir,
                                                       two_processes):
    c, rows = _gutenberg(tmp_path, corpus_dir)
    assert parallel.chunks_for_rows(c, rows[:4])          # a real pool, with children
    ex = parallel._pool
    children = list(ex._processes.values())
    assert children and all(p.is_alive() for p in children)
    now = time.monotonic()
    assert parallel.release_idle(600.0, now=now) == 0, "just used: not idle"
    assert parallel.release_idle(0, now=now + 10_000) == 0, "0 keeps the pool for the process's life"
    assert parallel.release_idle(600.0, now=now + 601) == 2
    assert parallel._pool is None and not parallel.health()["pool"]
    for p in children:
        p.join(5)
    assert not any(p.is_alive() for p in children), "the children ended: their memory is back"
    # the next parse starts a fresh pool and computes the same chunks
    again = parallel.chunks_for_rows(c, rows[:4])
    assert parallel._pool is not None and parallel._pool is not ex
    assert again == parallel._compute("chunks", c, rows[:4])


def test_a_pool_with_a_caller_inside_is_never_released(monkeypatch):
    """A batch inside `compute`/`compute_pages`, or a slot held, is use —
    however long it lasts (a paused records build waits inside its stream)."""
    stopped = []

    class Pool:
        def shutdown(self, wait=True, cancel_futures=False):
            stopped.append(True)

    monkeypatch.setattr(parallel, "_pool", Pool())
    monkeypatch.setattr(parallel, "_pool_size", 11)
    parallel.forget_users()
    parallel._want("wikipedia", 1)
    later = time.monotonic() + 10_000
    assert parallel.release_idle(600.0, now=later) == 0, "a caller is inside"
    parallel._want("wikipedia", -1)
    parallel._occupy("wikipedia", 1)
    assert parallel.release_idle(600.0, now=later) == 0, "a slot is held"
    parallel._occupy("wikipedia", -1)
    assert parallel.release_idle(600.0, now=time.monotonic()) == 0, "the caller just left"
    assert parallel.release_idle(600.0, now=later) == 11 and stopped == [True]
    assert parallel._pool is None


def test_the_worker_beat_releases_the_pool_and_says_so(monkeypatch):
    """The call sits on the beat thread beside the shares line (telemetry-
    shaped work, never a pipeline thread), and a release is one log line."""
    import inspect
    src = inspect.getsource(worker)
    beat = src[src.index("_log_shares(con, [c.manifest_path for c in collections])"):]
    beat = beat[:beat.index("except Exception")]
    assert "parallel.release_idle(config.PARSE_IDLE_RELEASE_S)" in beat
    assert "parse pool released:" in beat
    assert config.PARSE_IDLE_RELEASE_S == 600.0

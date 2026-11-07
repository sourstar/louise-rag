"""Local (corpus-resident) genesis archives — amendment 11: the corpus IS
the archive. Vectors are read from the collection's own parquet shards,
shards are revisited as documents become eligible, the settle-wait is
waived, and corpus files are never deleted by the importer.
"""
from dataclasses import replace
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app import collections as collections_mod, db, discovery, embeddings, licensing
from app.bundles import genesis
from app.plugins.embedded.source import embed_input

DIM = 8
ARCHIVE = "upstash-wikipedia-2024-06"


def _unit(seed: str) -> np.ndarray:
    rng = np.random.default_rng(abs(hash(seed)) % (2**32))
    v = rng.normal(size=DIM)
    return (v / np.linalg.norm(v)).astype(np.float32)


def _same_function_embed(texts, timeout=300, recipe=None, query=False):
    return [_unit(t).tolist() for t in texts]


def _different_model_embed(texts, timeout=300, recipe=None, query=False):
    return [_unit("other:" + t).tolist() for t in texts]


def _write_shard(path, specs):
    """specs: (article, seq, title, text) — embedding = the same function
    the fake local encoder computes over the title-prefixed chunk text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "id": [f"{a}_{s}" for a, s, _, _ in specs],
        "url": [f"https://x/{t}" for _, _, t, _ in specs],
        "title": [t for _, _, t, _ in specs],
        "text": [x for _, _, _, x in specs],
        "embedding": [_unit(embed_input(t, x)).tolist() for _, _, t, x in specs],
    }), path)


@pytest.fixture
def collection(isolated_config, tmp_path):
    c = collections_mod.Collection(
        name="wikivec", plugin="embedded", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb",
        genesis_archive=ARCHIVE,
        embed=collections_mod.EmbedRecipe(
            url="http://fake:1", api="openai", model="bge-m3-test",
            dtype="f16", ctx=512, dim=DIM))
    _write_shard(c.corpus_dir / "data" / "tr" / "000.parquet", [
        ("10", 0, "Cengiz Han", "para one"),
        ("10", 1, "Cengiz Han", "para two"),
        ("11", 0, "Ankara", "capital"),
    ])
    _write_shard(c.corpus_dir / "data" / "tr" / "001.parquet", [
        ("13", 0, "Izmir", "coastal"),
        ("14", 0, "Konya", "plains"),
    ])
    # `embedded` is a YELLOW plugin, and importing an archive is USE of
    # the corpus it describes (invariant 9). The vectors here travel WITH
    # the corpus, so nothing is ever fetched and the fetch-path gate never
    # sees this collection — which is exactly the case `import_shard`
    # gates, and exactly why this fixture needs a real acknowledgement
    # rather than none.
    c = replace(c, license_ack=licensing.make_ack(c))
    con = db.connect(c)
    discovery.sync_collection(con, c)
    return c


def _make_eligible(con, uids=None):
    if uids is None:
        con.execute("UPDATE documents SET rdf_done=1, fts_done=1 WHERE error IS NULL")
    else:
        marks = ",".join("?" for _ in uids)
        con.execute(f"UPDATE documents SET rdf_done=1, fts_done=1 "
                    f"WHERE uid IN ({marks})", uids)


def test_drain_imports_revisits_and_keeps_corpus_files(collection, monkeypatch):
    con = db.connect(collection)
    monkeypatch.setattr(embeddings, "_embed_texts", _same_function_embed)

    uids = [r["uid"] for r in con.execute(
        "SELECT uid FROM documents ORDER BY CAST(native_id AS INTEGER)")]
    assert len(uids) == 4

    # only the first two documents are eligible yet — rdf/fts backlog EXISTS
    # for the rest, and a local archive must NOT wait for settle
    _make_eligible(con, uids[:2])
    for _ in range(10):
        if genesis.drain_next_shard(con, collection) is None:
            break
    done = con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0]
    assert done == 2

    # the rest become eligible later — REVISIT imports them through the
    # same (already-applied) shards
    _make_eligible(con)
    for _ in range(10):
        if genesis.drain_next_shard(con, collection) is None:
            break
    done = con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0]
    assert done == 4

    # all vectors landed with the right shape; corpus files untouched
    tbl = embeddings._conn(collection).open_table(embeddings.TABLE)
    assert tbl.count_rows() == 5     # article 10 has two chunks
    assert (collection.corpus_dir / "data" / "tr" / "000.parquet").is_file()
    assert (collection.corpus_dir / "data" / "tr" / "001.parquet").is_file()

    # ledger coverage ACCUMULATED across revisits
    total_docs = con.execute(
        "SELECT SUM(docs) FROM genesis_imports WHERE archive=?", (ARCHIVE,)).fetchone()[0]
    assert total_docs == 4

    # fully covered → nothing left to do
    assert genesis.drain_next_shard(con, collection) is None


def test_wrong_model_archive_rejected_whole_and_never_retried(collection, monkeypatch):
    """A verdict needs evidence (amendment 42): the fixture's two small
    shards (3 + 2 chunks) are INCONCLUSIVE against an unproven archive —
    deferred, nothing written — and a shard with enough eligible
    documents is what convicts the archive; after that it is never
    retried, even once the encoder agrees."""
    con = db.connect(collection)
    _write_shard(collection.corpus_dir / "data" / "tr" / "002.parquet",
                 [(str(a), 0, f"Title {a}", f"text {a}") for a in range(20, 30)])
    discovery.sync_collection(con, collection)
    _make_eligible(con)
    monkeypatch.setattr(embeddings, "_embed_texts", _different_model_embed)

    for _ in range(6):
        genesis.drain_next_shard(con, collection)
        rows = {r["shard"]: r for r in con.execute(
            "SELECT shard, state, detail FROM genesis_imports")}
        if any(r["state"] == "rejected" for r in rows.values()):
            break
    assert rows["data/tr/002.parquet"]["state"] == "rejected"
    for small in ("data/tr/000.parquet", "data/tr/001.parquet"):
        assert rows[small]["state"] == "skipped" and "inconclusive" in rows[small]["detail"], rows[small]
    assert embeddings.TABLE not in embeddings._conn(collection).table_names()

    monkeypatch.setattr(embeddings, "_embed_texts", _same_function_embed)
    assert genesis.drain_next_shard(con, collection) is None, \
        "a rejected archive must never be retried"


def test_no_sample_within_truncation_budget_defers_then_imports_once_proven(collection, monkeypatch):
    """If every chunk exceeds the local truncation budget the same-function
    proof cannot run on THIS shard: against an unproven archive that is
    inconclusive (deferred, nothing written — never a refusal without
    evidence); once another shard has proven the archive, the over-budget
    document imports — its archived vector is strictly better than
    anything the local encoder could produce (amendment 11)."""
    con = db.connect(collection)
    big = "z" * (512 * 4 + 100)     # over recipe.ctx*4 chars
    shard = collection.corpus_dir / "data" / "tr" / "002.parquet"
    _write_shard(shard, [("77", 0, "Big", big)])
    discovery.sync_collection(con, collection)
    big_uid = f"embedded:{10 * 10_000_000_000 + 77}"
    _make_eligible(con, [big_uid])
    archive = genesis.ARCHIVES[ARCHIVE]

    with pytest.raises(genesis.GenesisUnavailable, match="truncation budget"):
        genesis.import_shard(con, collection, archive, "data/tr/002.parquet",
                             embed_fn=lambda t: _same_function_embed(t), revisit=True)
    assert con.execute("SELECT embed_done FROM documents WHERE uid=?",
                       (big_uid,)).fetchone()["embed_done"] == 0
    row = con.execute("SELECT state, detail FROM genesis_imports").fetchone()
    assert row["state"] == "skipped" and "truncation budget" in row["detail"]

    # another shard proves the archive; the big document then imports
    _make_eligible(con)
    first = genesis.import_shard(con, collection, archive, "data/tr/000.parquet",
                                 embed_fn=lambda t: _same_function_embed(t), revisit=True)
    assert first["docs"] == 2
    again = genesis.import_shard(con, collection, archive, "data/tr/002.parquet",
                                 embed_fn=lambda t: _same_function_embed(t), revisit=True)
    assert again["docs"] == 1
    assert con.execute("SELECT embed_done FROM documents WHERE uid=?",
                       (big_uid,)).fetchone()["embed_done"] == 1


def test_a_candidate_read_touches_only_the_candidates_row_groups(collection, monkeypatch):
    """Regression (2026-08-27, measured live): every 1,000-document fts
    batch triggered a read of the WHOLE 437 MB shard to import those
    documents. A candidate read now opens only the row groups the
    candidates' locators name."""
    import pyarrow.parquet as pq
    con = db.connect(collection)
    # a shard of 6 articles in 3 row groups of 2 rows (one article per row)
    shard = collection.corpus_dir / "data" / "tr" / "003.parquet"
    specs = [(str(a), 0, f"T{a}", f"text {a}") for a in range(30, 36)]
    shard.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "id": [f"{a}_{s}" for a, s, _, _ in specs],
        "url": [f"https://x/{t}" for _, _, t, _ in specs],
        "title": [t for _, _, t, _ in specs],
        "text": [x for _, _, _, x in specs],
        "embedding": [_unit(embed_input(t, x)).tolist() for _, _, t, x in specs],
    }), shard, row_group_size=2)
    discovery.sync_collection(con, collection)
    assert pq.ParquetFile(shard).num_row_groups == 3
    monkeypatch.setattr(embeddings, "_embed_texts", _same_function_embed)
    # only article 34 (row 4 -> row group 2) is eligible
    uid = f"embedded:{10 * 10_000_000_000 + 34}"
    _make_eligible(con, [uid])
    read_groups = []
    real = pq.ParquetFile.read_row_group
    monkeypatch.setattr(pq.ParquetFile, "read_row_group",
                        lambda self, g, *a, **k: (read_groups.append(g), real(self, g, *a, **k))[1])
    whole = []
    real_iter = pq.ParquetFile.iter_batches
    monkeypatch.setattr(pq.ParquetFile, "iter_batches",
                        lambda self, *a, **k: (whole.append(1), real_iter(self, *a, **k))[1])
    result = genesis.drain_next_shard(con, collection)
    assert result is not None and result["docs"] == 1
    # the archive read AND the text fetch for the proof touch group 2 only
    # (the plugin's fetch reads row groups too, amendment 47) — never the
    # other groups, never the whole shard
    assert set(read_groups) == {2} and not whole, (read_groups, whole)
    assert con.execute("SELECT embed_done FROM documents WHERE uid=?", (uid,)).fetchone()[0] == 1


def test_local_shards_listing(collection):
    shards = genesis._local_shards(collection)
    assert shards == ("data/tr/000.parquet", "data/tr/001.parquet")




# ---- the candidate demand: announced and seeded (2026-11-03) ----------------

def _windows(con) -> list[str]:
    seen: list[str] = []
    con.set_trace_callback(lambda q: seen.append(q) if "rowid<=" in q else None)
    return seen


def _uid_lookups(con) -> list[str]:
    seen: list[str] = []
    con.set_trace_callback(lambda q: seen.append(q) if "uid IN (" in q else None)
    return seen


def _seed_to_the_end(con, collection) -> None:
    key = str(collection.manifest_path)
    while not genesis._walks.get(key, genesis._Walk()).done:
        genesis._candidate_shards(con, collection)


@pytest.fixture
def announced(monkeypatch):
    """The production demand: announced and seeded, not walked per call."""
    monkeypatch.setattr(genesis, "_WALK_EVERY_CALL", False)
    monkeypatch.setattr(genesis, "_SEED_DUTY", 1.0)       # no pacing, unless a test sets it
    genesis.reset_revisit_state()


def test_the_candidate_walk_is_not_repeated_for_every_round(collection, announced, monkeypatch):
    """A COST test. The walk used to page up to 65,536 candidates FROM THE
    START every 10 s: on wikipedia, whose embedding is off, 14.6 M
    candidates, the same first 65,536 read every time and nothing found —
    23.5 % of the pipeline thread (München's py-spy, 2026-11-03 04:05).

    Now the seed reads ONE rowid window per call from a cursor and STOPS at
    the end of the table; after that a call reads nothing."""
    monkeypatch.setattr(genesis, "_SEED_WINDOW", 2)
    con = db.connect(collection)
    _make_eligible(con)
    con.commit()
    windows = _windows(con)

    first = genesis._candidate_shards(con, collection)
    assert len(windows) == 1, "one window per call"
    while not genesis._walks[str(collection.manifest_path)].done:
        before = len(windows)
        genesis._candidate_shards(con, collection)
        assert len(windows) == before + 1
    top = con.execute("SELECT max(rowid) FROM documents").fetchone()[0]
    assert len(windows) == -(-top // 2), "windows of 2 rowids up to the table's last"
    found = genesis._candidate_shards(con, collection)
    assert {d.shard for d in found} == {"data/tr/000.parquet", "data/tr/001.parquet"}
    assert first, "what a window finds is demand at once"

    windows.clear()
    for _ in range(5):
        assert genesis._candidate_shards(con, collection) == found
    assert windows == [], "a finished seed reads nothing"


def test_the_seed_is_paced_to_its_share_of_the_thread(collection, announced, monkeypatch):
    monkeypatch.setattr(genesis, "_SEED_WINDOW", 1)
    monkeypatch.setattr(genesis, "_SEED_DUTY", 0.05)
    clock = [100.0]
    monkeypatch.setattr(genesis.time, "monotonic", lambda: clock[0])
    con = db.connect(collection)
    _make_eligible(con)
    con.commit()
    windows = _windows(con)
    real = con.execute

    def slow(sql, *a):
        if "rowid<=" in sql:
            clock[0] += 0.1             # each window "takes" 100 ms
        return real(sql, *a)
    genesis._candidate_shards(type("C", (), {"execute": staticmethod(slow)})(), collection)
    w = genesis._walks[str(collection.manifest_path)]
    assert w.next_at == pytest.approx(clock[0] + 0.1 * 19), "19x the window's time"
    windows.clear()
    genesis._candidate_shards(con, collection)
    assert windows == [], "not due yet"
    clock[0] = w.next_at
    genesis._candidate_shards(con, collection)
    assert len(windows) == 1


def test_fts_announces_what_becomes_a_candidate(collection, announced):
    """After the seed, a document becomes a candidate when fts commits it:
    the stage announces it, and the next call places it by its primary key
    — no window, whatever the table holds."""
    con = db.connect(collection)
    uids = [r["uid"] for r in con.execute(
        "SELECT uid FROM documents ORDER BY CAST(native_id AS INTEGER)")]
    _seed_to_the_end(con, collection)
    assert genesis._candidate_shards(con, collection) == []

    _make_eligible(con, uids[:1])
    con.commit()
    genesis.announce(collection, uids[:1])
    windows = _windows(con)
    found = genesis._candidate_shards(con, collection)
    assert windows == []
    assert [d.uids for d in found] == [uids[:1]]


def test_an_announcement_costs_one_lookup_per_500_documents(collection, announced):
    con = db.connect(collection)
    uids = [r["uid"] for r in con.execute("SELECT uid FROM documents")]
    _seed_to_the_end(con, collection)
    genesis.announce(collection, uids)
    lookups = _uid_lookups(con)
    genesis._candidate_shards(con, collection)
    assert len(lookups) == 1


def test_an_announcement_before_its_commit_is_looked_for_again(collection, announced):
    con = db.connect(collection)
    uid = con.execute("SELECT uid FROM documents LIMIT 1").fetchone()[0]
    genesis._walks[str(collection.manifest_path)] = genesis._Walk(done=True, finished_at=1e18)
    genesis.announce(collection, [uid])
    assert genesis._candidate_shards(con, collection) == [], "not a candidate yet"
    _make_eligible(con, [uid])
    con.commit()
    assert [d.uids for d in genesis._candidate_shards(con, collection)] == [[uid]]


def test_only_a_local_archive_keeps_announced_demand(collection, announced):
    other = replace(collection, genesis_archive=None)
    genesis.announce(other, ["x"])
    assert str(other.manifest_path) not in genesis._walks


def test_an_import_drops_its_shard_and_walks_nothing(collection, announced):
    con = db.connect(collection)
    _make_eligible(con)
    con.commit()
    _seed_to_the_end(con, collection)
    first = genesis._candidate_shards(con, collection)
    windows = _windows(con)
    genesis._record_residual(collection, first[0].shard, [first[0].uids[0]])
    left = genesis._candidate_shards(con, collection)
    assert windows == [], "a residual recorded no longer re-walks the table"
    assert [d.shard for d in left] == [d.shard for d in first[1:]]


def test_a_demand_is_checked_before_its_shard_is_read(collection, announced, monkeypatch):
    """What left the candidate set since its demand was found buys no read."""
    con = db.connect(collection)
    _make_eligible(con)
    con.commit()
    _seed_to_the_end(con, collection)
    assert genesis._candidate_shards(con, collection)
    con.execute("UPDATE documents SET embed_done=1")
    con.commit()
    reads = []
    monkeypatch.setattr(genesis, "import_shard", lambda *a, **k: reads.append(a) or {})
    assert genesis.drain_next_shard(con, collection) is None
    assert reads == [] and genesis._candidate_shards(con, collection) == []


def test_a_walk_in_progress_is_not_walked_twice(collection, announced):
    """The pipeline thread and the main loop both drain a local archive."""
    con = db.connect(collection)
    _make_eligible(con)
    con.commit()
    genesis._walks[str(collection.manifest_path)] = genesis._Walk(busy=True)
    windows = _windows(con)
    assert genesis._candidate_shards(con, collection) == [] and windows == []


def test_the_seed_starts_again_after_its_reseed_interval(collection, announced, monkeypatch):
    """Announced demand can miss an edge; a finished seed is one paced probe
    again after _RESEED_S, never a document stranded for good."""
    con = db.connect(collection)
    genesis._walks[str(collection.manifest_path)] = genesis._Walk(done=True, finished_at=0.0)
    monkeypatch.setattr(genesis, "_RESEED_S", 0.0)
    _make_eligible(con)
    con.commit()
    windows = _windows(con)
    assert genesis._candidate_shards(con, collection) and len(windows) == 1


def test_the_drain_imports_through_announced_demand(collection, announced, monkeypatch):
    """End to end with the production demand: seed, announce, import."""
    con = db.connect(collection)
    monkeypatch.setattr(embeddings, "_embed_texts", _same_function_embed)
    uids = [r["uid"] for r in con.execute("SELECT uid FROM documents")]
    _seed_to_the_end(con, collection)
    _make_eligible(con)
    con.commit()
    genesis.announce(collection, uids)
    for _ in range(10):
        if genesis.drain_next_shard(con, collection) is None:
            break
    done = con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0]
    assert done == len(uids)


def test_announcements_nobody_drains_stay_bounded(collection, announced, monkeypatch):
    """An archive that never drains (stopped, writes held) must not grow the
    announced set for ever: past the cap the seed starts again instead."""
    monkeypatch.setattr(genesis, "_ANNOUNCE_CAP", 3)
    key = str(collection.manifest_path)
    genesis._walks[key] = genesis._Walk(done=True, cursor=99, finished_at=1e18)
    genesis.announce(collection, ["a", "b", "c"])
    assert len(genesis._walks[key].announced) == 3 and genesis._walks[key].done
    genesis.announce(collection, ["d"])
    w = genesis._walks[key]
    assert w.announced == {} and not w.done and w.cursor == 0


def test_a_seed_says_when_it_starts_and_when_it_reaches_the_end(collection, announced,
                                                                 monkeypatch, capsys):
    """Two lines a seed, for the AFTER row to time it."""
    monkeypatch.setattr(genesis, "_SEED_WINDOW", 2)
    con = db.connect(collection)
    _seed_to_the_end(con, collection)
    out = capsys.readouterr().out
    assert out.count("archive demand seed started") == 1
    assert out.count("archive demand seed reached the end") == 1


def test_a_restart_resumes_the_seed_from_its_saved_cursor(collection, announced, monkeypatch,
                                                          capsys):
    """Every deploy restarts the worker; a seed that began at rowid 0 each
    time might never end (München, 2026-11-03: 04:39, then 05:26)."""
    monkeypatch.setattr(genesis, "_SEED_WINDOW", 1)
    monkeypatch.setattr(genesis, "_SEED_REPORT", 2)
    con = db.connect(collection)
    _make_eligible(con)
    con.commit()
    for _ in range(2):
        genesis._candidate_shards(con, collection)
    assert "archive demand seed: window 2 of" in capsys.readouterr().out
    genesis.reset_revisit_state()               # the restart: memory gone
    windows = _windows(con)
    genesis._candidate_shards(con, collection)
    assert "rowid>2 " in windows[0], windows
    assert "archive demand seed resumed at rowid 2" in capsys.readouterr().out


def test_a_seed_finished_within_the_hour_stays_finished_after_a_restart(collection, announced):
    con = db.connect(collection)
    _seed_to_the_end(con, collection)
    genesis.reset_revisit_state()
    windows = _windows(con)
    genesis._candidate_shards(con, collection)
    assert windows == []


def test_an_unreadable_saved_cursor_starts_the_seed_over(collection, announced):
    genesis._seed_file(collection).write_text("{not json")
    con = db.connect(collection)
    windows = _windows(con)
    genesis._candidate_shards(con, collection)
    assert "rowid>0 " in windows[0], windows


def test_the_time_left_is_read_off_the_wall_not_the_reading(collection, announced, monkeypatch,
                                                            capsys):
    """The rounds pace the seed (one window a round), so a window that reads
    in ~0 s still takes the round's time: 08:05's line said "~0.0 h" for a
    seed that took an hour."""
    monkeypatch.setattr(genesis, "_SEED_WINDOW", 1)
    monkeypatch.setattr(genesis, "_SEED_REPORT", 2)
    clock = [1000.0]
    monkeypatch.setattr(genesis.time, "monotonic", lambda: clock[0])
    con = db.connect(collection)
    for _ in range(2):
        genesis._candidate_shards(con, collection)
        clock[0] += 3.0                    # a round between windows, no reading
    line = [l for l in capsys.readouterr().out.splitlines() if "window 2 of" in l][0]
    assert "1.5 s a window (0.00 s reading)" in line, line


def test_a_pass_waits_for_its_encoder_to_load_behind_the_thread(collection, monkeypatch):
    """München, 2026-11-07: 21.7 % of wikipedia's pipeline thread waited in
    its encoder's warm, inside the archive pass's same-function proof
    (`embed = "off"`: the encoder is unloaded between passes). The load now
    starts behind the thread: the drain returns at once with nothing
    imported, the rdf/fts rounds have the thread, one warm is sent; once the
    model is loaded, the pass imports as before."""
    import threading
    import time

    from app import model_load
    con = db.connect(collection)
    monkeypatch.setattr(embeddings, "_embed_texts", _same_function_embed)
    loaded: set[str] = set()
    monkeypatch.setattr(model_load, "_fetch_system", lambda server: frozenset(loaded))
    gate = threading.Event()
    warms: list[tuple[str, bool]] = []

    def warm_request(recipe, pair, level):
        def warm(bound_s):
            warms.append((threading.current_thread().name, pair))
            gate.wait(10)
            loaded.add(recipe.model)
        return warm
    monkeypatch.setattr(embeddings, "_warm_request", warm_request)
    _make_eligible(con)
    try:
        t0 = time.monotonic()
        for _ in range(3):
            assert genesis.drain_next_shard(con, collection) is None
        assert time.monotonic() - t0 < 2
        assert con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0] == 0
        deadline = time.monotonic() + 5
        while not warms and time.monotonic() < deadline:
            time.sleep(0.01)
        assert warms == [("model-load-bge-m3-test", False)]   # one, of the text form, behind
    finally:
        gate.set()
    deadline = time.monotonic() + 5
    while not loaded and time.monotonic() < deadline:
        time.sleep(0.01)
    for _ in range(10):
        if genesis.drain_next_shard(con, collection) is None:
            break
    assert con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0] == 4
    assert len(warms) == 1


# ---- the shard's read: its vectors in one conversion, the next read behind --

@pytest.mark.parametrize("arrow_type", [
    pa.list_(pa.float32()), pa.list_(pa.float64()), pa.large_list(pa.float32()),
    pa.list_(pa.float32(), 4), pa.list_(pa.float16())])
def test_the_vectors_are_the_bytes_the_per_value_conversion_gave(arrow_type):
    """`_vectors` replaces `np.asarray(row, float32)` over `to_pylist()` (13.2 %
    of wikipedia's pipeline thread): the same float32 bytes per row, for every
    list type a shard may carry, on the sliced batches `to_batches` yields."""
    rng = np.random.default_rng(7)
    n = 4 if pa.types.is_fixed_size_list(arrow_type) else None
    rows = [rng.normal(size=n or int(rng.integers(0, 9))).tolist() for _ in range(3000)]
    table = pa.table({"embedding": pa.array(rows, type=arrow_type)})
    offsets = []
    for batch in table.to_batches(max_chunksize=1024):
        col = batch.column(0)
        offsets.append(col.offset)
        old = [np.asarray(v, dtype=np.float32) for v in col.to_pylist()]
        new = genesis._vectors(col)
        assert len(new) == len(old)
        assert all(a.dtype == b.dtype == np.float32 and a.tobytes() == b.tobytes()
                   for a, b in zip(old, new))
    assert offsets[0] == 0 and all(offsets[1:])     # the later batches are slices


def test_a_column_with_a_null_takes_the_per_value_path():
    col = pa.array([[1.0, 2.0], None, [3.0]], type=pa.list_(pa.float32()))
    got = genesis._vectors(col)
    want = [np.asarray(v, dtype=np.float32) for v in col.to_pylist()]
    assert [g.tobytes() for g in got] == [w.tobytes() for w in want]


def test_the_read_behind_keeps_its_order_its_bound_and_stops_with_its_reader():
    import threading
    import time
    assert list(genesis._read_behind(iter(range(50)), name="t", depth=2)) == list(range(50))

    def failing():
        yield 1
        raise OSError("disk")
    got = genesis._read_behind(failing(), name="t")
    assert next(got) == 1
    with pytest.raises(OSError):
        next(got)

    never = threading.Event()

    def silent():
        never.wait(10)
        yield 1
    t0 = time.monotonic()
    with pytest.raises(genesis.GenesisUnavailable):
        next(genesis._read_behind(silent(), name="t", timeout=0.2))
    assert time.monotonic() - t0 < 2
    never.set()

    read: list[int] = []

    def counted():
        for i in range(1000):
            read.append(i)
            yield i
    reader = genesis._read_behind(counted(), name="genesis-read-t", depth=2)
    assert [next(reader) for _ in range(3)] == [0, 1, 2]
    reader.close()                                   # the pass stopped
    time.sleep(0.3)
    assert len(read) <= 3 + 2 + 1                    # its depth ahead, no more
    assert not any(t.name == "genesis-read-t" for t in threading.enumerate())

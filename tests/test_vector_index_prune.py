"""tests/test_vector_index_prune.py — Lance versions do not accumulate.

Lance is a versioned store: every write leaves a manifest, and a manifest
pins the data fragments it referenced. So a table grows with its write
COUNT, not its row count, and something has to drop the old versions.

Measured on the reference node 2026-09-10, with the app stopped:

    table     rows         versions   data      _versions   _indices
    pubmed    35,457,919     29,007   76.2 GiB   36.6 GiB    3.7 GiB
    wikivec   91,238,804     13,656  208.7 GiB    2.9 GiB   63.0 GiB

The `data` columns are honest — 91 M chunks at 1024 f16 dimensions IS
about 209 GiB — so this is not fragments piling up. It is 29,007 retained
manifests costing 36.6 GiB on one table. Pruning existed — `refresh()`
called `optimize(cleanup_older_than=…)` — and never ran, for two
independent reasons this file pins:

  1. it was reachable only through `refresh`, which `refresh_due` gates
     on UNINDEXED ROWS. A table with an index and few new rows never
     pruned; a table with NO index never pruned at all, because
     `refresh_due` returns False without one. Version growth is caused
     by writes and has nothing to do with either condition.
  2. it shared a FLAT free-space floor with compaction. Below the floor
     `refresh` returned early having freed nothing — so free space could
     never climb back above it. A maintenance deadlock, now broken by
     making the requirement proportional to the table.
"""
import time

import numpy as np
import pytest

from app import collections as collections_mod
from app import config, embeddings, vector_index


DIM = 8


@pytest.fixture
def collection(isolated_config, tmp_path):
    return collections_mod.Collection(
        name="vp", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb",
        embed=collections_mod.EmbedRecipe(
            url="http://encoder:1", api="openai", model="m",
            dtype="f32", ctx=512, dim=DIM))


@pytest.fixture
def table(collection):
    """A Lance table written MANY TIMES OVER — which is the only thing
    that makes versions, and is what a real embed pass does: one commit
    per batch of documents, thousands of them over a corpus."""
    coll = collection
    conn = embeddings._conn(coll)
    for i in range(12):
        rows = [{"id": f"u{i}:{j}:1", "book_id": i, "text": f"t{i}{j}",
                 "vector": np.random.default_rng(i * 100 + j)
                 .standard_normal(DIM).astype("float32").tolist()}
                for j in range(4)]
        if embeddings.TABLE not in conn.table_names():
            conn.create_table(embeddings.TABLE, data=rows)
        else:
            conn.open_table(embeddings.TABLE).add(rows)
    return coll


def versions(coll) -> int:
    tbl = vector_index._table(coll)
    return len(tbl.list_versions())


# ---- the mechanism -------------------------------------------------------

def test_writes_accumulate_versions_which_is_the_thing_being_fixed(table):
    """Not a test of our code — a test of the premise. If Lance ever stops
    versioning every write, the rest of this file is measuring nothing."""
    assert versions(table) >= 12, \
        "twelve commits did not leave twelve versions; the premise changed"


def test_prune_drops_them(table, monkeypatch):
    monkeypatch.setattr(config, "VECTOR_INDEX_KEEP_VERSIONS_H", 0.0)
    before = versions(table)
    out = vector_index.prune(table)
    assert out["pruned"] is True
    after = versions(table)
    assert after < before, f"{before} versions before, {after} after"


def test_prune_keeps_the_current_version_and_every_row(table, monkeypatch):
    """The point of a prune is that it is invisible to a reader."""
    monkeypatch.setattr(config, "VECTOR_INDEX_KEEP_VERSIONS_H", 0.0)
    tbl = vector_index._table(table)
    rows_before = tbl.count_rows()
    vector_index.prune(table)
    tbl = vector_index._table(table)
    assert tbl.count_rows() == rows_before == 48
    assert versions(table) >= 1, "the current version is never a candidate"
    got = tbl.search().limit(100).to_list()
    assert len({r["id"] for r in got}) == 48


def test_a_retention_window_keeps_recent_versions(table, monkeypatch):
    """`VECTOR_INDEX_KEEP_VERSIONS_H` is a window, not a switch: a version
    written a moment ago may still be the one a reader has open."""
    monkeypatch.setattr(config, "VECTOR_INDEX_KEEP_VERSIONS_H", 24.0)
    tbl = vector_index._table(table)
    oldest_before = min(v["version"] for v in tbl.list_versions())
    vector_index.prune(table)
    tbl = vector_index._table(table)
    assert min(v["version"] for v in tbl.list_versions()) == oldest_before, \
        "nothing here is older than 24 hours, so nothing may be dropped"
    # The COUNT is not the assertion: `optimize` compacts and commits, so
    # it leaves a version of its own even when it prunes none.
    assert len(tbl.list_versions()) >= 12


# ---- the two reasons it never ran ----------------------------------------

def test_prune_runs_for_a_table_with_NO_index(table, monkeypatch):
    """Reason 1, and the sharper half of it. `refresh_due` returns False
    when the table has no index, so an unindexed table used to prune
    NEVER — no matter how many times it was written."""
    monkeypatch.setattr(config, "VECTOR_INDEX_KEEP_VERSIONS_H", 0.0)
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_ROWS", 10**9)  # never build
    st = vector_index.stats(table)
    assert not st["indexed"], "the fixture must be unindexed for this to mean anything"
    assert vector_index.refresh_due(st) is False, "the old path would do nothing"

    before = versions(table)
    vector_index.ensure(table)
    assert versions(table) < before, \
        "ensure() must prune an unindexed table; that is the whole defect"


def test_the_headroom_test_is_proportional_and_not_a_flat_floor(table, monkeypatch):
    """Reason 2: the deadlock, and why a flat floor caused it.

    A flat 150 GiB floor says the same thing about a 300 GiB table and a
    100 MiB one, and it is wrong about the second: compacting 100 MiB
    needs about 25 MiB, so refusing it because the POOL is tight forbids
    exactly the cheap reclamation that would relieve the pool. Under a
    flat floor nothing runs, nothing is freed, and free space never rises
    back over the floor — for every table at once.
    """
    monkeypatch.setattr(vector_index, "free_bytes", lambda c: 8 * 2**30)  # 8 GiB
    assert vector_index.headroom_ok(table) is True, \
        "a tiny table with 8 GiB free must be maintainable"

    # The old rule, restored, refuses it — which is the regression.
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_FREE_GB", 150.0)
    assert vector_index.headroom_ok(table) is False


def test_a_big_table_still_defers_when_there_is_no_room_for_it(table, monkeypatch):
    """The guard it does NOT lose. `optimize` writes new fragments before
    deleting the old — observed taking free space DOWN 23 GiB on a 291 GiB
    table before releasing any — so a maintenance pass must never be the
    thing that fills the pool."""
    monkeypatch.setattr(vector_index, "free_bytes", lambda c: 8 * 2**30)
    monkeypatch.setattr(vector_index, "table_bytes", lambda c: 300 * 2**30)
    assert vector_index.headroom_ok(table) is False
    out = vector_index.prune(table)
    assert out["pruned"] is False and out["reason"] == "low disk"
    out = vector_index.refresh(table)
    assert out["refreshed"] is False and out["reason"] == "low disk"


def test_headroom_scales_with_the_table(monkeypatch, table):
    """25 % of the table, which is the measured 8 % with margin."""
    monkeypatch.setattr(config, "VECTOR_INDEX_MIN_FREE_GB", 0.0)
    monkeypatch.setattr(vector_index, "table_bytes", lambda c: 100 * 2**30)
    monkeypatch.setattr(vector_index, "free_bytes", lambda c: 26 * 2**30)
    assert vector_index.headroom_ok(table) is True
    monkeypatch.setattr(vector_index, "free_bytes", lambda c: 24 * 2**30)
    assert vector_index.headroom_ok(table) is False


def test_an_unknown_free_space_does_not_block_maintenance(table, monkeypatch):
    """`free_bytes` returns None when it cannot tell. Refusing on that
    would make an unreadable statvfs stop maintenance forever."""
    monkeypatch.setattr(vector_index, "free_bytes", lambda c: None)
    assert vector_index.headroom_ok(table) is True


# ---- the interval --------------------------------------------------------

def test_pruning_is_rate_limited_per_collection(table, monkeypatch):
    """A prune per embed batch would be a per-round cost that scales with
    the work, which the engineering rules forbid. Once an hour by
    default, per collection, tracked in-process."""
    monkeypatch.setattr(config, "VECTOR_INDEX_PRUNE_S", 3600)
    vector_index._pruned_at.clear()
    assert vector_index.prune_due(table) is True
    assert vector_index.prune_due(table) is False, "twice in a row is once"
    vector_index._pruned_at[str(table.lance_dir)] = time.monotonic() - 7200
    assert vector_index.prune_due(table) is True


def test_prune_on_a_collection_with_no_table_is_not_an_error(collection):
    out = vector_index.prune(collection)
    assert out["pruned"] is False and out["reason"] == "no table"


def test_a_failing_prune_never_breaks_the_caller(table, monkeypatch):
    """It is a cost, not a fault: the table is intact and larger than it
    needs to be. An embed pass must not die of it."""
    class Boom:
        def list_versions(self):
            return []

        def optimize(self, *a, **k):
            raise RuntimeError("object store unavailable")
    monkeypatch.setattr(vector_index, "_table", lambda c: Boom())
    out = vector_index.prune(table)
    assert out["pruned"] is False and "object store" in out["reason"]

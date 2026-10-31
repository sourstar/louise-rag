"""`louise dev rewrite-vectors` (app/vector_rewrite.py; operator, 2026-10-31:
"Rewrite wikipedia now"): a table created before stable row ids is copied
row for row into one that has them, indexed, verified and swapped in —
resumable, refused when the source moved, the old table kept until asked."""
from __future__ import annotations

import shutil
import threading
from collections import namedtuple
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pytest

lancedb = pytest.importorskip("lancedb")
lance = pytest.importorskip("lance")

from app import embeddings, vector_index, vector_rewrite  # noqa: E402

DIM = 16


def _batch(lo: int, hi: int) -> pa.Table:
    rng = np.random.default_rng(lo)
    vec = rng.standard_normal((hi - lo) * DIM).astype(np.float16)
    return pa.table({
        "id": [f"{i // 4}:{i % 4}:1" for i in range(lo, hi)],
        "book_id": pa.array([i // 4 for i in range(lo, hi)], pa.int64()),
        "lang": ["en"] * (hi - lo), "seq": pa.array([i % 4 for i in range(lo, hi)], pa.int64()),
        "chapter": [""] * (hi - lo), "text": [f"chunk {i}" for i in range(lo, hi)],
        "vector": pa.FixedSizeListArray.from_arrays(pa.array(vec), DIM)})


@pytest.fixture
def legacy(tmp_path):
    """A collection whose table predates stable row ids: five fragments,
    an IVF_PQ index and the book_id one, and a delete."""
    col = SimpleNamespace(name="wiki", lance_dir=tmp_path)
    db = lancedb.connect(str(tmp_path))
    tbl = db.create_table(embeddings.TABLE, data=_batch(0, 600))       # no storage option
    for k in range(1, 5):
        tbl.add(_batch(600 * k, 600 * (k + 1)))
    vector_index.build_table(tbl, partitions=4)
    vector_index.build_scalar_table(tbl)
    tbl.delete("book_id = 3")
    embeddings.forget(col)
    return col


def _src(col):
    return lance.dataset(str(col.lance_dir / f"{embeddings.TABLE}.lance"))


def _rows(ds) -> dict:
    return {r["id"]: (r["text"], r["vector"]) for r in
            ds.to_table(columns=["id", "text", "vector"]).to_pylist()}


def test_the_table_is_copied_indexed_verified_and_swapped(legacy):
    before = _rows(_src(legacy))
    assert not _src(legacy).has_stable_row_ids
    out = vector_rewrite.run(legacy, group=2)
    new = _src(legacy)
    assert out["stage"] == "swapped" and new.has_stable_row_ids
    assert _rows(new) == before, "every row, its text and its vector bytes kept"
    names = {i["name"] for i in new.list_indices()}
    assert {"vector_idx", "book_id_idx", "lang_idx"} <= names, "indexed BEFORE the swap"
    assert (legacy.lance_dir / f"{vector_rewrite.OLD}.lance").exists(), "the old table kept"
    tbl = lancedb.connect(str(legacy.lance_dir)).open_table(embeddings.TABLE)
    hit = tbl.search(np.asarray(before["0:0:1"][1], np.float32)).limit(3).nprobes(4).to_list()
    assert hit and hit[0]["id"] == "0:0:1"
    # and the reason for all of it: a compaction after deletes keeps the one index
    idx = sorted(p.name for p in (legacy.lance_dir / f"{embeddings.TABLE}.lance" / "_indices").iterdir())
    tbl.delete("book_id < 50")
    tbl.to_lance().optimize.compact_files(materialize_deletions=True,
                                          materialize_deletions_threshold=0.0)
    assert sorted(p.name for p in (legacy.lance_dir / f"{embeddings.TABLE}.lance"
                                   / "_indices").iterdir()) == idx


def test_a_stopped_copy_resumes_where_it_stopped(legacy):
    before = _rows(_src(legacy))
    stop = threading.Event()
    out = vector_rewrite.run(legacy, group=2, stop=stop,
                             log=lambda m: stop.set() if m.startswith("copied") else None)
    assert out["interrupted"] and out["fragments"] == 2
    assert not _src(legacy).has_stable_row_ids, "nothing swapped"
    assert vector_rewrite.run(legacy, group=2)["stage"] == "swapped"
    assert _rows(_src(legacy)) == before


def test_a_group_committed_but_not_recorded_is_counted_once(legacy):
    before = _rows(_src(legacy))
    stop = threading.Event()
    vector_rewrite.run(legacy, group=2, stop=stop,
                       log=lambda m: stop.set() if m.startswith("copied") else None)
    # the next group lands in the copy, and the run dies before recording it
    src = _src(legacy)
    nxt = src.get_fragments()[2:4]
    lance.write_dataset(src.scanner(fragments=nxt).to_reader(),
                        str(legacy.lance_dir / f"{vector_rewrite.TMP}.lance"), mode="append")
    vector_rewrite.run(legacy, group=2)
    assert _rows(_src(legacy)) == before, "no row twice"


def test_a_write_during_the_rewrite_refuses_the_swap(legacy, monkeypatch):
    real = vector_index.build_table

    def build_and_a_write_lands(tbl, **kw):
        lancedb.connect(str(legacy.lance_dir)).open_table(embeddings.TABLE).add(_batch(9000, 9004))
        return real(tbl, **kw)
    monkeypatch.setattr(vector_index, "build_table", build_and_a_write_lands)
    with pytest.raises(vector_rewrite.RewriteError, match="written during the rewrite"):
        vector_rewrite.run(legacy, group=2)
    assert not _src(legacy).has_stable_row_ids, "the source stays in place"
    monkeypatch.setattr(vector_index, "build_table", real)
    assert vector_rewrite.run(legacy, group=2, restart=True)["stage"] == "swapped"
    assert "2250:0:1" in _rows(_src(legacy)), "the restart copies the write (row 9000)"


def test_no_room_means_nothing_written(legacy, monkeypatch):
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(vector_rewrite.shutil, "disk_usage", lambda p: Usage(1, 1, 0))
    with pytest.raises(vector_rewrite.RewriteError, match="GiB more"):
        vector_rewrite.run(legacy)
    assert not (legacy.lance_dir / f"{vector_rewrite.TMP}.lance").exists()


def test_a_table_with_stable_row_ids_is_left_alone_and_the_old_one_dropped_on_request(legacy):
    vector_rewrite.run(legacy, group=2)
    assert vector_rewrite.run(legacy)["stage"] == "swapped"     # again: nothing to do
    out = vector_rewrite.drop_old(legacy)
    assert out["dropped"] and not (legacy.lance_dir / f"{vector_rewrite.OLD}.lance").exists()
    assert vector_rewrite.drop_old(legacy) == {"dropped": False, "reason": "no old table"}


def test_the_old_table_is_not_dropped_while_the_live_one_lacks_stable_ids(legacy):
    old = legacy.lance_dir / f"{vector_rewrite.OLD}.lance"
    shutil.copytree(legacy.lance_dir / f"{embeddings.TABLE}.lance", old)
    with pytest.raises(vector_rewrite.RewriteError, match="no stable row ids"):
        vector_rewrite.drop_old(legacy)
    assert old.exists()


def test_the_command_plans_without_yes_and_refuses_an_unpaused_collection(isolated_config, capsys):
    from app import louise
    assert louise.main(["dev", "rewrite-vectors", "gutenberg"]) == 0
    assert '"table": null' in capsys.readouterr().out
    assert louise.main(["dev", "rewrite-vectors", "gutenberg", "--yes"]) == 2
    assert "is not paused" in capsys.readouterr().err

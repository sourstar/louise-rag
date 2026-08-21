"""The scan cursor must distinguish shards by PATH, not by file name.

`app/plugins/cursor.py` keyed every entry on `path.name`. That is unique for
pubmed (`pubmed26nNNNN.xml.gz`, one flat directory), for gutenberg (book
directories) and for wikipedia (one dump plus its index). It is NOT unique
for the `embedded` plugin, whose format-v1 layout is `data/{lang}/{NNN}.parquet`
— every language directory restarts the numbering, so `data/de/000.parquet`
and `data/tr/000.parquet` both key on `000.parquet`.

The consequence is not a wrong skip, it is no skipping at all: each language
overwrites the other's entry for the same number, the signatures never match,
and every shard is re-read on every scan. Live, wikivec re-read ~437 MB
parquet shards indefinitely and its discovery scan never finished — which
also meant fts never ran, so its genesis archive (vectors carried inside the
corpus, no encoder needed) could import nothing.

The existing cursor test in test_plugins_embedded_source.py passes because
its fixture has a single language.
"""
import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.plugins.base import DiscoverState
from app.plugins.embedded.source import EmbeddedSource

DIM = 8


def _rows(*specs):
    return {
        "id": [f"{a}_{s}" for a, s, _, _ in specs],
        "url": [f"https://x.example/{t}" for _, _, t, _ in specs],
        "title": [t for _, _, t, _ in specs],
        "text": [x for _, _, _, x in specs],
        "embedding": [np.ones(DIM, dtype=np.float32).tolist() for _ in specs],
    }


def _write_shard(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(rows), path)


@pytest.fixture
def multilang(tmp_path):
    """Two languages, deliberately sharing shard file names — the real
    Upstash layout, and what the single-language fixture cannot catch."""
    root = tmp_path / "corpus"
    _write_shard(root / "data" / "tr" / "000.parquet",
                 _rows(("10", 0, "Ankara", "capital")))
    _write_shard(root / "data" / "tr" / "001.parquet",
                 _rows(("11", 0, "Izmir", "coastal")))
    _write_shard(root / "data" / "de" / "000.parquet",
                 _rows(("20", 0, "Berlin", "capital")))
    _write_shard(root / "data" / "de" / "001.parquet",
                 _rows(("21", 0, "Hamburg", "port")))
    return root


def test_cursor_keeps_one_entry_per_shard_not_per_file_name(multilang, tmp_path):
    src = EmbeddedSource()
    state = DiscoverState(corpus_root=multilang, state_dir=tmp_path)
    assert len(list(src.discover(state))) == 4

    saved = json.loads((tmp_path / "embedded_scan_cursor.json").read_text())
    assert len(saved) == 4, (
        "same-named shards in different language directories collapsed into "
        f"one cursor entry each: {saved}")


def test_second_scan_skips_every_shard_across_languages(multilang, tmp_path):
    """The point of the cursor: an unchanged corpus costs a stat pass. With
    colliding keys the second scan re-reads every shard instead."""
    src = EmbeddedSource()
    state = DiscoverState(corpus_root=multilang, state_dir=tmp_path)
    assert len(list(src.discover(state))) == 4
    assert list(src.discover(state)) == [], \
        "an unchanged multi-language corpus must be fully skipped"


def test_a_cursor_written_under_the_old_key_is_not_thrown_away(tmp_path):
    """Changing the key must not force a gratuitous full re-parse of a corpus
    already scanned under the bare-name scheme — on pubmed that is ~1,330
    containers. A legacy entry counts, but only on an exact signature match.
    """
    from pathlib import Path
    from app.plugins import cursor as cur

    root = tmp_path / "corpus"
    shard = root / "data" / "tr" / "000.parquet"
    _write_shard(shard, _rows(("10", 0, "Ankara", "capital")))

    legacy = {shard.name: cur.signature(shard)}          # old-style entry
    assert cur.unchanged(legacy, shard, root) is True

    # ...but a DIFFERENT file that merely shares the name does not match
    other = root / "data" / "de" / "000.parquet"
    _write_shard(other, _rows(("20", 0, "Berlin", "capital"), ("21", 0, "Kiel", "port")))
    assert cur.unchanged(legacy, other, root) is False


def test_re_marking_retires_the_legacy_entry(tmp_path):
    """Otherwise the cursor file carries both keys for every file forever."""
    from app.plugins import cursor as cur

    root = tmp_path / "corpus"
    shard = root / "data" / "tr" / "000.parquet"
    _write_shard(shard, _rows(("10", 0, "Ankara", "capital")))

    scan = {shard.name: [1, 1]}                          # stale legacy entry
    cur.mark(scan, shard, root)
    assert list(scan) == ["data/tr/000.parquet"], scan


def test_a_changed_shard_is_still_re_read(multilang, tmp_path):
    """Distinguishing by path must not make the cursor blind to real edits."""
    src = EmbeddedSource()
    state = DiscoverState(corpus_root=multilang, state_dir=tmp_path)
    assert len(list(src.discover(state))) == 4

    _write_shard(multilang / "data" / "de" / "001.parquet",
                 _rows(("21", 0, "Hamburg", "port"), ("22", 0, "Koeln", "cathedral")))
    again = list(src.discover(state))
    assert {r.extra["article"] for r in again} == {"21", "22"}, \
        f"only the edited shard should be re-read, got {[r.extra for r in again]}"

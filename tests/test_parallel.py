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

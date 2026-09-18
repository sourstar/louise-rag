"""No collection hands one origin reader to two threads at once.

pubmed's pack cached one ParquetFile per file and shared it between its fts
stage and four embed workers; pyarrow's reader is not safe for that, and
the worker died three times in three hours on 2026-09-18
(tests/test_plugins_pubmed_pack.py pins the fix). The other collections
were audited the same day. wikipedia's archive opens a reader per read and
caches only immutable tables; normattiva caches its zips but reads them
under a lock; gutenberg keeps nothing open. These tests hold them to it:
every read on a reader enters one no other thread is using, and six
threads get the same answers one would.
"""
import threading
import time
import zipfile

import pyarrow as pa
import pyarrow.parquet as pq

from app import db, discovery, plugin_io
from app import collections as collections_mod
from app.plugins.normattiva import source as normattiva_source
from app.plugins.wikipedia import archive


def _watch(monkeypatch, owner, attr: str, methods: tuple[str, ...]) -> list[str]:
    """Replace owner.attr (a reader class) with a subclass that records any
    call entering an instance another thread is inside."""
    real = getattr(owner, attr)
    guard = threading.Lock()
    active: dict[int, int] = {}
    shared: list[str] = []

    def wrap(name):
        def method(self, *a, **k):
            with guard:
                active[id(self)] = active.get(id(self), 0) + 1
                if active[id(self)] > 1:
                    shared.append(name)
            try:
                time.sleep(0.005)                  # widen the window
                return getattr(real, name)(self, *a, **k)
            finally:
                with guard:
                    active[id(self)] -= 1
        return method

    monkeypatch.setattr(owner, attr, type(f"Watched{real.__name__}", (real,),
                                          {m: wrap(m) for m in methods}))
    return shared


def _threads(n: int, target) -> list[str]:
    start = threading.Barrier(n)
    failed: list[str] = []

    def run(k):
        start.wait()
        try:
            target(k)
        except Exception as e:                     # noqa: BLE001
            failed.append(repr(e))
    threads = [threading.Thread(target=run, args=(k,)) for k in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return failed


def test_wikipedia_archive_reads(tmp_path, monkeypatch):
    rows = 36
    table = pa.table({"id": [f"{i}_0" for i in range(rows)],
                      "url": [f"https://x/{i}" for i in range(rows)],
                      "title": [f"t{i}" for i in range(rows)],
                      "text": [f"text {i}" * 5 for i in range(rows)]})
    shard = tmp_path / "000.parquet"
    pq.write_table(table, shard, row_group_size=3)          # 12 groups > the LRU's 8
    expected = table.to_pylist()
    monkeypatch.setattr(archive, "_layouts", type(archive._layouts)())
    monkeypatch.setattr(archive, "_groups", type(archive._groups)())
    shared = _watch(monkeypatch, pq, "ParquetFile", ("read_row_group", "read"))
    wrong: list[int] = []

    def reader(k):
        for i in range(rows):
            start = (i * 5 + k) % rows
            got = archive.read_rows(str(shard), start, start + 4)
            if got != expected[start:start + 4]:
                wrong.append(start)
    assert _threads(6, reader) == [] and wrong == []
    assert shared == [], f"{len(shared)} read(s) entered a reader in use"


def test_normattiva_zip_reads(tmp_path, monkeypatch):
    path = tmp_path / "acts.zip"
    members = {f"act{i}.xml": f"<act n='{i}'>{'x' * (i * 97)}</act>".encode() for i in range(24)}
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    monkeypatch.setattr(normattiva_source, "_zip_cache", {})
    shared = _watch(monkeypatch, zipfile, "ZipFile", ("read",))
    names = list(members)
    wrong: list[str] = []

    def reader(k):
        for i in range(len(names)):
            name = names[(i + k) % len(names)]
            if normattiva_source._read_member(path, name) != members[name]:
                wrong.append(name)
    assert _threads(6, reader) == [] and wrong == []
    assert shared == [], f"{len(shared)} read(s) entered a zip in use"


def test_gutenberg_sections(isolated_config, tmp_path, corpus_dir):
    d = tmp_path / "g"
    c = collections_mod.Collection(
        name="g", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus_dir, manifest_path=d / "manifest.db", fts_dir=d / "fts",
        lance_dir=d / "lancedb")
    con = db.connect(c)
    discovery.sync_collection(con, c)
    docs = [dict(r) for r in con.execute("SELECT * FROM documents ORDER BY uid")]
    con.close()
    plugin = c.plugin_instance()
    expected = [plugin_io.sections(plugin, r) for r in docs]
    wrong: list[str] = []

    def reader(k):
        for i in range(len(docs) * 3):
            j = (i + k) % len(docs)
            if plugin_io.sections(plugin, docs[j]) != expected[j]:
                wrong.append(docs[j]["uid"])
    assert _threads(6, reader) == [] and wrong == []

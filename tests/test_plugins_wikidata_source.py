"""tests/test_plugins_wikidata_source.py — the Wikidata plugin's pack, label
table and source (docs/notes/wikidata-build-plan.md §2): one streaming pass
writes the pack in chunk files and the label table, resumable at a chunk
without re-parsing; discover yields every entity that is not a scholarly
article, fetch returns the entity's own line bytes, parse the record with
one text section per language and the whole entity in `extra`."""
import bz2
import hashlib
import json
from pathlib import Path

import pytest

from app.plugins.base import DiscoverState
from app.plugins.wikidata import dump as dump_mod, ids, pack, textify
from app.plugins.wikidata.source import WikidataSource, find_dumps, split_locator

FIXTURES = Path(__file__).parent / "fixtures" / "wikidata-mini"


def entity_lines() -> list[bytes]:
    lines = []
    for name in ("entities.jsonl", "dump-sample.jsonl"):
        for l in (FIXTURES / name).read_bytes().split(b"\n"):
            l = l.strip()
            if l and l not in (b"[", b"]"):
                lines.append(l[:-1] if l.endswith(b",") else l)
    return lines                      # Q42, Q5, Q33010486 (a scholarly article), P31, Q2711, Q3331


def a_dump(root: Path, date: str = "20261012", lines=None) -> Path:
    lines = entity_lines() if lines is None else lines
    d = root / "wikidatawiki" / "entities" / date
    d.mkdir(parents=True, exist_ok=True)
    body = b"[\n" + b",\n".join(lines) + b"\n]\n"
    p = d / f"wikidata-{date}-all.json.bz2"
    p.write_bytes(bz2.compress(body))
    return p


@pytest.fixture(autouse=True)
def _fresh():
    pack.reset_caches()
    yield
    pack.reset_caches()


def test_the_pass_writes_the_pack_in_chunks_and_the_label_table(tmp_path):
    dump = a_dump(tmp_path)
    said = []
    meta = pack.build(dump, languages=("en", "fr"), log=said.append, chunk=4)
    assert meta["complete"] and meta["entities"] == 6 and meta["chunks"] == 2 and meta["pruned"] == 1
    files = pack.chunk_files(dump)
    assert [p.name for p in files] == ["00000.parquet", "00001.parquet"]
    t = pack.index_columns(files[0])
    assert t.column("id").to_pylist() == ["Q42", "Q5", "Q33010486", "P31"]
    assert t.column("kind").to_pylist() == ["item", "item", "item", "property"]
    assert t.column("native_id").to_pylist() == ["42", "5", "33010486", str(ids.PROPERTY_BASE + 31)]
    assert t.column("pruned").to_pylist() == [False, False, True, False], "the scholarly article is pruned"
    assert pack.line(dump, 5) == entity_lines()[5], "the line's own bytes"
    assert pack.line(dump, 0) == entity_lines()[0]
    labels = pack.labels(dump)
    assert labels("Q42", "en") == "Douglas Adams" and labels("P31", "en") == "instance of"
    assert labels("Q3331", "fr") == "tunnel de la Croix-Rousse" and labels("Q3331", "de") is None, \
        "only the languages asked (and mul) are in the table"
    assert labels("Q33010486", "en"), "a pruned entity's label is still there: others name it"
    assert pack.is_complete(dump, ("en", "fr")) and not pack.is_complete(dump, ("en",))
    assert pack.build(dump, languages=("en", "fr"), chunk=4) == meta, "complete: nothing to do"


def test_a_stopped_pass_resumes_at_the_chunk_without_parsing_what_it_has(tmp_path, monkeypatch):
    dump = a_dump(tmp_path)
    calls = {"n": 0}
    meta = pack.build(dump, languages=("en",), chunk=2, stop=lambda: True)
    assert not meta["complete"] and meta["chunks"] == 1 and meta["entities"] == 2
    assert [p.name for p in pack.chunk_files(dump)] == ["00000.parquet"]
    with pytest.raises(pack.PackError, match="same"):
        pack.build(dump, languages=("en",), chunk=3)          # a resume keeps the chunk it started with
    # the resume skips two lines unparsed: count the json loads
    real = json.loads

    def counting(s, *a, **k):
        if isinstance(s, (bytes, bytearray)):          # an entity line; meta.json is read as str
            calls["n"] += 1
        return real(s, *a, **k)
    monkeypatch.setattr(dump_mod.json, "loads", counting)
    meta = pack.build(dump, languages=("en",), chunk=2)
    assert meta["complete"] and meta["chunks"] == 3 and meta["entities"] == 6
    assert calls["n"] == 4, "the two entities of the complete chunk were skipped, not parsed"
    assert [pack.line(dump, i)[:20] for i in range(6)] == [l[:20] for l in entity_lines()]


def test_a_truncated_dump_builds_no_complete_pack(tmp_path):
    lines = entity_lines()
    d = tmp_path / "wikidatawiki" / "entities" / "20261012"
    d.mkdir(parents=True)
    p = d / "wikidata-20261012-all.json.bz2"
    p.write_bytes(bz2.compress(b"[\n" + b",\n".join(lines[:3]) + b",\n"))      # cut before the end
    with pytest.raises(pack.PackError, match="truncated"):
        pack.build(p, languages=("en",), chunk=2)
    assert not pack.is_complete(p, ("en",))
    assert [q.name for q in pack.chunk_files(p)] == ["00000.parquet"], "the complete chunk is kept for the resume"


# ---- the pass on several processes (the operator, 2026-10-20: "squeeze everything from the cpu") ----

def _pack_bytes(dump: Path) -> dict:
    """Everything a pack is: its meta, each chunk file's bytes, the label rows."""
    import sqlite3
    d = pack.pack_dir(dump)
    meta = json.loads((d / "meta.json").read_text())
    meta.pop("mtime_ns"), meta.pop("size")            # two dumps' own signatures
    con = sqlite3.connect(d / "labels.sqlite")
    labels = con.execute("SELECT id, lang, label FROM label ORDER BY id, lang").fetchall()
    names = con.execute("SELECT id, lang, label FROM name ORDER BY id").fetchall()
    con.close()
    return {"meta": meta, "labels": labels, "names": names,
            "chunks": {p.name: p.read_bytes() for p in sorted(d.glob("*.parquet"))},
            "stray": sorted(p.name for p in d.glob("*.tmp"))}


@pytest.mark.parametrize("chunk", [2, 4, 6, 7])
def test_the_parallel_pass_writes_the_serial_pass_byte_for_byte(tmp_path, chunk):
    serial = a_dump(tmp_path / "serial")
    parallel = a_dump(tmp_path / "parallel")
    m1 = pack.build(serial, languages=("en", "fr"), chunk=chunk, workers=1)
    said = []
    m2 = pack.build(parallel, languages=("en", "fr"), chunk=chunk, workers=3, log=said.append)
    assert m2["complete"] and m2["entities"] == m1["entities"] == 6 and m2["pruned"] == m1["pruned"] == 1
    a, b = _pack_bytes(serial), _pack_bytes(parallel)
    assert a["meta"] == b["meta"] and a["labels"] == b["labels"] and a["names"] == b["names"]
    assert a["chunks"] == b["chunks"], "every chunk file the same bytes"
    assert b["stray"] == [], "no tmp file left"
    assert any("3 processes" in m for m in said), "the chunk pass's last line (the names follow it)"


def test_a_stopped_parallel_pass_resumes_at_its_last_committed_chunk(tmp_path):
    serial = a_dump(tmp_path / "serial")
    parallel = a_dump(tmp_path / "parallel")
    pack.build(serial, languages=("en",), chunk=2, workers=1)
    meta = pack.build(parallel, languages=("en",), chunk=2, workers=2, stop=lambda: True)
    assert not meta["complete"] and meta["chunks"] == 1 and meta["entities"] == 2, \
        "stopped after the first commit, as the serial pass stops"
    assert [p.name for p in pack.chunk_files(parallel)] == ["00000.parquet"]
    assert not list(pack.pack_dir(parallel).glob("*.tmp")), "the uncommitted chunk's tmp is gone"
    meta = pack.build(parallel, languages=("en",), chunk=2, workers=2)
    assert meta["complete"] and meta["chunks"] == 3
    assert _pack_bytes(parallel) == _pack_bytes(serial)


def test_a_truncated_dump_stops_the_parallel_pass_with_its_complete_chunks_kept(tmp_path):
    lines = entity_lines()
    d = tmp_path / "wikidatawiki" / "entities" / "20261012"
    d.mkdir(parents=True)
    p = d / "wikidata-20261012-all.json.bz2"
    p.write_bytes(bz2.compress(b"[\n" + b",\n".join(lines[:5]) + b",\n"))
    with pytest.raises(pack.PackError, match="truncated"):
        pack.build(p, languages=("en",), chunk=2, workers=2)
    assert not pack.is_complete(p, ("en",))
    assert [q.name for q in pack.chunk_files(p)] == ["00000.parquet", "00001.parquet"]


def test_a_line_that_is_not_json_is_the_dumps_error_on_the_parallel_pass_too(tmp_path):
    lines = entity_lines()
    dump = a_dump(tmp_path, lines=lines[:3] + [b'{"id": "Q9", broken'] + lines[3:])
    with pytest.raises(dump_mod.DumpError, match="entity 3: not JSON"):
        pack.build(dump, languages=("en",), chunk=2, workers=2)
    assert [q.name for q in pack.chunk_files(dump)] == ["00000.parquet"], "the chunks before it committed"


def test_iter_lines_frames_the_dump_as_iter_entities_does(tmp_path):
    dump = a_dump(tmp_path)
    for skip in (0, 2, 6):
        with dump_mod.open_dump(dump) as f:
            want = [(e.index, e.raw) for e in dump_mod.iter_entities(f, skip=skip)]
        r = dump_mod.ReadResult()
        with dump_mod.open_dump(dump) as f:
            got = list(dump_mod.iter_lines(f, skip=skip, result=r))
        assert got == want and r.complete


def test_lbzip2_on_the_path_decompresses_the_same_bytes(tmp_path, monkeypatch):
    """A stand-in `lbzip2` (python's bz2 behind its flags) on the PATH: the
    pass reads its stdout and writes the same pack as the serial pass."""
    import sys
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    called = tmp_path / "called"
    (bin_dir / "lbzip2").write_text(
        f"#!{sys.executable}\nimport bz2, sys, pathlib\n"
        f"pathlib.Path({str(called)!r}).write_text(' '.join(sys.argv[1:]))\n"
        "sys.stdout.buffer.write(bz2.decompress(open(sys.argv[-1], 'rb').read()))\n")
    (bin_dir / "lbzip2").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    serial = a_dump(tmp_path / "serial")
    parallel = a_dump(tmp_path / "parallel")
    pack.build(serial, languages=("en",), chunk=4, workers=1)
    assert pack.build(parallel, languages=("en",), chunk=4, workers=2)["complete"]
    assert called.read_text().startswith("-d -c -n 2 "), "lbzip2 was asked, with the pass's processes"
    assert _pack_bytes(parallel) == _pack_bytes(serial)


def test_slices_are_reassembled_as_one_from_pylist_would_cut_them(tmp_path):
    """A chunk's `line` column past Arrow's 2 GiB binary limit is cut by
    `from_pylist` at the value that would overflow, and the Parquet pages
    follow the cut (node, 2026-10-20: chunk 00000 at row 61,707). The slices'
    table is cut by the same rule; here with a small limit."""
    assert pack._split_points([5, 5, 5], limit=10) == [2], "a value that would pass the limit starts a chunk"
    assert pack._split_points([10, 1], limit=10) == [1], "a value that reaches it exactly stays"
    assert pack._split_points([3] * 4, limit=100) == []
    rows = [{"index": i, "id": f"Q{i}", "kind": "item", "native_id": str(i), "modified": "",
             "lastrevid": i, "pruned": False, "line": b"x" * (3 + i % 4)} for i in range(40)]
    slices = [pack.pa.Table.from_pylist(rows[i:i + 7], schema=pack.SCHEMA) for i in range(0, 40, 7)]
    whole = pack._as_serial_table(slices)
    assert [c.num_chunks for c in whole.columns] == [1] * 8, "under the limit: one chunk a column"
    assert whole.equals(pack.pa.Table.from_pylist(rows, schema=pack.SCHEMA))
    cut = pack._as_serial_table(slices, limit=50)
    lengths = [len(r["line"]) for r in rows]
    want = pack._split_points(lengths, limit=50)
    got = list(__import__("itertools").accumulate(len(c) for c in cut.column("line").chunks))[:-1]
    assert got == want and cut.equals(whole), "cut where the rule cuts, the same content"
    pack._write_chunk(whole, tmp_path / "a.parquet")
    pack._write_slices(slices, tmp_path / "b.parquet")
    assert (tmp_path / "a.parquet").read_bytes() == (tmp_path / "b.parquet").read_bytes()


def test_with_a_cache_the_pack_lives_there_keyed_on_the_dumps_resolved_path(tmp_path, monkeypatch):
    """The operator, 2026-10-20: a cache folder on a fast drive. The pack and
    its label table go to `<CACHE_DIR>/wikidata-pack/<path key>/<dump name>/`;
    unset, beside the dump as before."""
    dump = a_dump(tmp_path / "origin")
    assert pack.pack_dir(dump) == dump.parent / ".pack" / dump.name, "no cache: beside the dump"
    monkeypatch.setattr(pack.config, "CACHE_DIR", tmp_path / "nvme")
    d = pack.pack_dir(dump)
    assert d.is_relative_to(tmp_path / "nvme" / "wikidata-pack") and d.name == dump.name
    other = a_dump(tmp_path / "elsewhere")
    assert other.name == dump.name and pack.pack_dir(other) != d, "the same name in another place: another pack"
    link = tmp_path / "linked"
    link.symlink_to(dump.parent.parent.parent.parent, target_is_directory=True)
    via = link / dump.relative_to(dump.parent.parent.parent.parent)
    assert pack.pack_dir(via) == d, "the same file through a link: the same pack"
    meta = pack.build(dump, languages=("en",), chunk=4, workers=1)
    assert meta["complete"] and (d / "meta.json").is_file() and (d / "labels.sqlite").is_file()
    assert not (dump.parent / ".pack").exists(), "nothing written beside the dump"
    assert pack.labels(dump)("Q42", "en") == "Douglas Adams"


def test_a_pack_built_beside_the_dump_is_adopted_into_the_cache_and_resumes_there(tmp_path, monkeypatch):
    """The node built a third of the pack beside the dump on tank before the
    cache existed: set the cache, and the next pass copies it over and goes on
    from its last chunk — nothing rebuilt (RULE 1)."""
    dump = a_dump(tmp_path / "origin")
    first = pack.build(dump, languages=("en",), chunk=2, workers=1, stop=lambda: True)
    assert not first["complete"] and first["chunks"] == 1
    beside = dump.parent / ".pack" / dump.name
    monkeypatch.setattr(pack.config, "CACHE_DIR", tmp_path / "nvme")
    said = []
    calls = {"n": 0}
    real = json.loads

    def counting(s, *a, **k):
        if isinstance(s, (bytes, bytearray)):
            calls["n"] += 1
        return real(s, *a, **k)
    monkeypatch.setattr(dump_mod.json, "loads", counting)
    meta = pack.build(dump, languages=("en",), chunk=2, workers=1, log=said.append)
    assert meta["complete"] and meta["chunks"] == 3
    assert calls["n"] == 4, "resumed after the adopted chunk: its two entities were not parsed again"
    assert any("adopted into the fast drive" in s for s in said)
    assert (pack.pack_dir(dump) / "00000.parquet").read_bytes() == (beside / "00000.parquet").read_bytes()
    assert (beside / "meta.json").is_file(), "the copy beside the dump is left for the operator"
    assert pack.is_complete(dump, ("en",))


def test_reads_stay_beside_the_dump_until_the_adopted_pack_has_landed(tmp_path, monkeypatch):
    """2026-10-20: the fts stage read the fast drive while the ~100 GB copy
    ran, and two documents were marked "not in its pack". The pack is read
    where it IS: beside the dump until the copy's rename lands, then there;
    a pack file missing where it is read is the node's condition — deferred
    (PlatformMissing), across the parse pool too — never the entity's."""
    from app import parallel, worker
    from app.chunking import PlatformMissing
    dump = a_dump(tmp_path / "origin")
    pack.build(dump, languages=("en",), chunk=2, workers=1)
    beside = dump.parent / ".pack" / dump.name
    monkeypatch.setattr(pack.config, "CACHE_DIR", tmp_path / "nvme")
    target = pack._target(dump)
    assert target != beside and pack.pack_dir(dump) == beside, "not adopted yet: read beside"
    target.mkdir(parents=True)                         # a place being filled holds no meta.json
    assert pack.pack_dir(dump) == beside
    target.rmdir()
    assert pack.adopt_beside(dump) and pack.pack_dir(dump) == target, "landed: read there"
    src = WikidataSource(languages=("en",))
    ref = next(iter(src.discover(DiscoverState(corpus_root=tmp_path, state_dir=_state(tmp_path)))))
    (target / "00000.parquet").unlink()               # before any read: a chunk read once is cached
    with pytest.raises(PlatformMissing):
        src.fetch(ref)
    with pytest.raises(PlatformMissing) as crossed:
        parallel.raise_for({"type": "PlatformMissing", "msg": "the pack cannot be read"})
    assert worker.platform_missing(crossed.value), "the worker defers what crossed the pool"


def _state(tmp_path):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    return d


def test_the_pass_follows_the_parse_pools_size(monkeypatch):
    monkeypatch.setattr(pack.config, "PARSE_PROCESSES", 11)
    assert pack.processes() == 11
    monkeypatch.setattr(pack.config, "PARSE_PROCESSES", 1)
    assert pack.processes() == 1, "the test suite's serial path"
    monkeypatch.setattr(pack, "_parallel_failed", True)
    monkeypatch.setattr(pack.config, "PARSE_PROCESSES", 11)
    assert pack.processes() == 1, "after a failed pool, this process goes serial"


def test_discover_fetch_and_parse_over_the_pack(tmp_path):
    dump = a_dump(tmp_path)
    src = WikidataSource(languages=("en", "fr"))
    state = DiscoverState(corpus_root=tmp_path, state_dir=tmp_path / "state")
    (tmp_path / "state").mkdir()
    refs = list(src.discover(state))
    assert [r.native_id for r in refs] == ["42", "5", str(ids.PROPERTY_BASE + 31), "2711", "3331"], \
        "every entity but the scholarly article, in dump order"
    assert all(r.scope == "document" and r.error is None for r in refs)
    assert refs[0].locator == f"{dump}#0" and split_locator(refs[-1].locator) == (dump, 5)
    assert refs[-1].extra == {"modified": "2025-08-21T18:18:41Z"}
    raw = src.fetch(refs[-1])
    assert raw.data == entity_lines()[5]
    assert raw.raw_hash == hashlib.blake2b(raw.data, digest_size=16).hexdigest()
    doc = src.parse(raw)
    assert doc.uid == "wikidata:3331" and doc.title == "tunnel de la Croix-Rousse" and doc.language == "mul"
    assert doc.keys == ["wikidata:Q3331", "wikidata:Q2354973"], "itself, then what it is an instance of"
    assert [lang for lang, _ in doc.sections] == ["en", "fr"]
    labels = pack.labels(dump)
    assert doc.sections[0][1] == textify.entity_text(json.loads(raw.data), "en", labels)
    assert "19 April 1952" not in doc.sections[0][1], "P1619 has no label in this small table: its claim is left out"
    assert doc.sections[0][1].startswith("tunnel de la Croix-Rousse, road tunnel in Lyon. Attributes include:\n- ")
    assert "instance of: Q2354973" in doc.sections[0][1], "a value the table has no label for is its id"
    assert doc.extra["id"] == "Q3331" and doc.extra["claims"]["P625"][0]["mainsnak"]["datavalue"]["value"]["latitude"] == 45.772677777778
    assert doc.extra["sitelinks"]["commonswiki"]["title"] == "Category:Tunnel de la Croix-Rousse", "kept whole (invariant 11)"
    assert doc.date is None and doc.creators == [] and doc.subjects == []
    # a property is a record too
    p31 = src.parse(src.fetch(refs[2]))
    assert p31.uid == f"wikidata:{ids.PROPERTY_BASE + 31}" and p31.keys[0] == "wikidata:P31"
    assert p31.title == "instance of"
    # the second scan skips the unchanged dump by its cursor
    assert list(src.discover(DiscoverState(corpus_root=tmp_path, state_dir=tmp_path / "state"))) == []


def test_the_pack_beside_a_dump_is_never_taken_for_a_dump(tmp_path):
    """The pack is a directory named after its dump, under `.pack/` beside it
    (the node, 2026-10-21: discovery opened it as the newest dump and said the
    dump was truncated)."""
    from app.plugins.wikidata import pack
    dump = a_dump(tmp_path, "20261012")
    (dump.parent / pack.PACK_DIRNAME / dump.name).mkdir(parents=True)
    (dump.parent / pack.PACK_DIRNAME / dump.name / "meta.json").write_text("{}")
    assert find_dumps(tmp_path) == [dump]
    assert find_dumps(tmp_path, "20261012") == [dump]


def test_the_pinned_snapshot_alone_is_read_and_a_bad_dump_is_one_container_error(tmp_path):
    older = a_dump(tmp_path, "20261001", lines=entity_lines()[:2])
    newer = a_dump(tmp_path, "20261012")
    assert find_dumps(tmp_path) == [newer, older] and find_dumps(tmp_path, "20261001") == [older]
    src = WikidataSource()
    refs = list(src.discover(DiscoverState(corpus_root=tmp_path, snapshot="20261001")))
    assert [r.native_id for r in refs] == ["42", "5"]
    refs = list(src.discover(DiscoverState(corpus_root=tmp_path)))
    assert len(refs) == 5, "unpinned: the newest dump alone"
    broken = tmp_path / "wikidatawiki" / "entities" / "20261020"
    broken.mkdir(parents=True)
    (broken / "wikidata-20261020-all.json.bz2").write_bytes(b"not bz2 at all")
    refs = list(src.discover(DiscoverState(corpus_root=tmp_path, snapshot="20261020")))
    assert len(refs) == 1 and refs[0].scope == "container" and "not usable" in refs[0].error

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

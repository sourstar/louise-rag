"""tests/test_plugins_wikidata_dump.py — the Wikidata entity dump's reader
and ids (docs/notes/wikidata-build-plan.md §2): the JSON array one entity a
line, bz2, gz or plain; each entity's own line bytes, no trailing comma; a
truncated stream yields what it holds and says where it stopped; the kind
encoded in the native id and recovered from it."""
import bz2
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from app.plugins.wikidata import dump, ids

FIXTURE = Path(__file__).parent / "fixtures" / "wikidata-mini" / "entities.jsonl"


def entity_lines() -> list[bytes]:
    return [l for l in FIXTURE.read_bytes().split(b"\n") if l.strip()]


def framed(lines: list[bytes], *, close: bool = True) -> bytes:
    """The dump's framing: `[`, one entity a line ending in `,` but the
    last, `]`."""
    body = b",\n".join(lines)
    return b"[\n" + body + (b"\n]\n" if close else b"\n")


def write_dump(path: Path, data: bytes) -> Path:
    if path.name.endswith(".bz2"):
        path.write_bytes(bz2.compress(data))
    elif path.name.endswith(".gz"):
        path.write_bytes(gzip.compress(data))
    else:
        path.write_bytes(data)
    return path


@pytest.mark.parametrize("suffix", [".json.bz2", ".json.gz", ".json"])
def test_the_reader_yields_each_entity_s_own_line_bytes_in_order(tmp_path, suffix):
    lines = entity_lines()
    path = write_dump(tmp_path / f"wikidata-20261013-all{suffix}", framed(lines))
    got, result = dump.read_all(path)
    assert result.complete and result.truncated_at is None and result.entities == len(lines) == 4
    assert [e.index for e in got] == [0, 1, 2, 3]
    assert [e.obj["id"] for e in got] == ["Q42", "Q5", "Q33010486", "P31"]
    for e, line in zip(got, lines):
        assert e.raw == line, "the line's bytes, no trailing comma, no newline: what origin_hash covers"
        assert json.loads(e.raw) == e.obj
    # the hash of the bytes is the record's origin_hash: a comma or a newline would move it
    h = hashlib.blake2b(got[0].raw, digest_size=16).hexdigest()
    assert h == hashlib.blake2b(lines[0], digest_size=16).hexdigest()


def test_a_truncated_stream_yields_what_it_holds_and_says_so(tmp_path):
    """bz2 decodes a block whole, so a cut inside the only block of a tiny
    file yields nothing; the real dump is written in many blocks (pbzip2's
    independent streams), and a Range of its first megabytes decodes every
    block but the cut one. The fixture has one stream per entity."""
    lines = entity_lines()
    streams = [bz2.compress(b) for b in (b"[\n", lines[0] + b",\n", lines[1] + b",\n",
                                          lines[2] + b",\n", lines[3] + b"\n]\n")]
    whole = b"".join(streams)
    multi = tmp_path / "multi.json.bz2"
    multi.write_bytes(whole)                                   # already compressed: written as is
    got_all, result_all = dump.read_all(multi)
    assert [e.obj["id"] for e in got_all] == ["Q42", "Q5", "Q33010486", "P31"] and result_all.complete, \
        "a multi-stream bz2 reads as one"
    head = whole[: sum(len(s) for s in streams[:3]) + len(streams[3]) // 2]   # cut inside the 4th stream
    path = tmp_path / "head.json.bz2"
    path.write_bytes(head)
    got, result = dump.read_all(path)
    assert [e.obj["id"] for e in got] == ["Q42", "Q5"] and not result.complete and result.truncated_at
    assert all(json.loads(e.raw) == e.obj for e in got), "no half entity is yielded"
    with pytest.raises(dump.DumpError, match="incomplete"):
        dump.read_all(path, tolerate_truncation=False)
    # an array left open (no `]`) is a truncation too; a last line without its newline parses if whole
    path2 = write_dump(tmp_path / "open.json", framed(lines, close=False))
    got2, result2 = dump.read_all(path2)
    assert len(got2) == len(lines) and not result2.complete and result2.truncated_at
    cut = framed(lines, close=False)[:-1] if framed(lines, close=False).endswith(b"\n") else framed(lines, close=False)
    path3 = write_dump(tmp_path / "cut.json", cut)
    got3, _ = dump.read_all(path3)
    assert len(got3) == len(lines), "the last entity, complete but for its newline, is read"


def test_what_is_not_an_entity_dump_is_refused(tmp_path):
    with pytest.raises(dump.DumpError, match="does not open"):
        dump.read_all(write_dump(tmp_path / "x.json", b'{"id":"Q1"}\n'))
    with pytest.raises(dump.DumpError, match="not JSON"):
        dump.read_all(write_dump(tmp_path / "y.json", b'[\n{"id":"Q1"},\nnot json,\n{"id":"Q2"}\n]\n'))
    with pytest.raises(dump.DumpError, match="object with an"):
        dump.read_all(write_dump(tmp_path / "z.json", b'[\n[1,2],\n{"id":"Q2"}\n]\n'))
    got, result = dump.read_all(write_dump(tmp_path / "empty.json", b"[\n]\n"))
    assert got == [] and result.complete


def test_the_kind_is_encoded_in_the_native_id_and_recovered():
    assert ids.native_id_of("Q42") == "42" and ids.entity_id_of("42") == "Q42"
    assert ids.native_id_of("P31") == str(ids.PROPERTY_BASE + 31) and ids.entity_id_of(ids.PROPERTY_BASE + 31) == "P31"
    assert ids.native_id_of("Q149999999") == "149999999"
    assert ids.kind_of("Q42") == "item" and ids.kind_of("P31") == "property"
    assert ids.canonical("Q42") == "wikidata:Q42" and ids.canonical("P31") == "wikidata:P31"
    for bad in ("L123", "Q", "Q0", "42", "q42", "Q42a", "", None):
        assert not ids.is_entity_id(bad)
    with pytest.raises(ids.IdError):
        ids.native_id_of("L123")
    with pytest.raises(ids.IdError):
        ids.native_id_of(f"Q{ids.PROPERTY_BASE}")
    with pytest.raises(ids.IdError):
        ids.entity_id_of(0)
    # the numeric order the parts rule needs: every item before every property, each in number order
    nids = sorted((int(ids.native_id_of(e)) for e in ("Q42", "Q5", "P31", "Q149999999", "P1")))
    assert [ids.entity_id_of(n) for n in nids] == ["Q5", "Q42", "Q149999999", "P1", "P31"]

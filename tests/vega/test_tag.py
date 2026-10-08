"""app/vega/tag.py — the tag's bytes, its strict parse, the two digests."""
from __future__ import annotations

import hashlib
import json
import struct

import pytest

from app.hold.codex import canonical_json, digest
from app.vega import tag as vtag

ID = "0123456789abcdef0123456789abcdef"


def test_the_tag_is_the_marker_then_canonical_json():
    tid, s = vtag.tag("text", "Aspirin, é\n", id=ID)
    assert tid == ID
    assert s == '\x1eVEGA/1\x1e{"form":"text","id":"%s","text":"Aspirin, é\\n"}' % ID
    tid, s = vtag.tag("pair", ("Title", "Abstract"), id=ID)
    assert s == '\x1eVEGA/1\x1e{"form":"pair","id":"%s","pair":["Title","Abstract"]}' % ID


def test_parse_gives_back_what_was_tagged():
    for form, content in (("text", ""), ("text", "a\x1eb   \U0001F600"),
                          ("pair", ("", "second")), ("pair", ("t\n", "a\tb"))):
        tid, s = vtag.tag(form, content)
        assert vtag.parse(s) == (tid, form, content)


def test_ids_are_fresh_and_hex():
    ids = {vtag.tag("text", "x")[0] for _ in range(100)}
    assert len(ids) == 100 and all(len(i) == 32 and int(i, 16) >= 0 for i in ids)


@pytest.mark.parametrize("s, why", [
    ("plain text", "without a VeGa tag"),
    ("x" + vtag.MARK + "{}", "inside the input"),
    (vtag.MARK + "{not json", "does not parse"),
    (vtag.MARK + "[1]", "is an object"),
    (vtag.MARK + '{"form":"image","id":"%s","text":"a"}' % ID, "form is one of"),
    (vtag.MARK + '{"form":"text","id":"%s"}' % ID, "exactly the keys"),
    (vtag.MARK + '{"form":"text","id":"%s","text":"a","x":1}' % ID, "exactly the keys"),
    (vtag.MARK + '{"form":"text","id":"ABC","text":"a"}', "32 lowercase"),
    (vtag.MARK + '{"form":"text","id":"%s","text":1}' % ID, "is a string"),
    (vtag.MARK + '{"form":"pair","id":"%s","pair":["a"]}' % ID, "two strings"),
    (vtag.MARK + '{"form":"text","id":"%s","text":"a","text":"b"}' % ID, "repeats the key"),
    (vtag.MARK + '{"form":"text","id":"%s","text":1.5}' % ID, "not an integer"),
    (vtag.MARK + '{"id":"%s","form":"text","text":"a"}' % ID, "canonical form"),
    (vtag.MARK + '{"form":"text", "id":"%s","text":"a"}' % ID, "canonical form"),
    (vtag.MARK + '{"form":"text","id":"%s","text":"\\u00e9"}' % ID, "canonical form"),
])
def test_anything_but_the_exact_tag_is_refused_with_its_rule(s, why):
    with pytest.raises(vtag.TagError, match=why):
        vtag.parse(s)


def test_content_that_cannot_be_canonical_is_refused():
    with pytest.raises(vtag.TagError, match="canonical JSON"):
        vtag.tag("text", "lone \ud800 surrogate")
    with pytest.raises(vtag.TagError):
        vtag.tag("pair", ("only one",))


def test_input_digest_is_the_tag_object_without_its_id():
    assert vtag.input_digest("text", "abc") == digest(canonical_json({"form": "text",
                                                                      "text": "abc"}))
    assert vtag.input_digest("pair", ("a", "b")) == digest(b'{"form":"pair","pair":["a","b"]}')
    assert vtag.input_digest("text", "a b") != vtag.input_digest("pair", ("a", "b"))


def test_vector_digest_is_sha256_of_f32_little_endian():
    v = [0.5, -1.0, 0.1]
    raw = struct.pack("<3f", *v)
    assert vtag.vector_digest(v) == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert vtag.vector_digest(raw) == vtag.vector_digest(v), "bytes pass as they are"
    assert vtag.vector_digest([-0.0]) != vtag.vector_digest([0.0])
    with pytest.raises(ValueError):
        vtag.vector_digest([1e39])
    with pytest.raises(ValueError):
        vtag.vector_digest(b"\x00\x00\x00")


def test_the_two_halfway_values_are_repaired_and_nothing_else_moves():
    # tools/vega_f32_json.go: Go writes float32 bits 15ae43fd as 7.038531e-26;
    # Python reads the double exactly halfway to 15ae43fe, and narrowing picks
    # that even neighbour
    read = json.loads("[7.038531e-26, -7.038531e-26]")
    assert struct.pack("<f", read[0]) == bytes.fromhex("fe43ae15"), "the loss being repaired"
    assert vtag.f32_bytes(read) == bytes.fromhex("fd43ae15") + bytes.fromhex("fd43ae95")
    exact = struct.unpack("<2f", bytes.fromhex("fc43ae15fe43ae15"))
    assert vtag.f32_bytes(list(exact)) == bytes.fromhex("fc43ae15fe43ae15"), "neighbours unmoved"


def test_the_pair_form_the_backend_reads_is_the_one_rage_and_the_backend_hold():
    from app.rage import pairs
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "deploy/localai/backends/louise_backend.py"
    spec = importlib.util.spec_from_file_location("louise_backend_for_vega", path)
    lb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lb)
    assert vtag.PAIR_MARK == pairs.MARK == lb.PAIR_MARK
    assert vtag.backend_pair("Té", "x\ny") == pairs.encode("Té", "x\ny")
    assert lb.decode_pair(vtag.backend_pair("a", "b")) == ("a", "b")

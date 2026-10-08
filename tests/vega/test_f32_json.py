"""A float32 through LocalAI's JSON keeps its bits — or the digest says so.

LocalAI v4.11.0 writes an embedding with Go's encoding/json; Louise reads it
with Python's json. tests/fixtures/vega/go_f32_json.txt is a fixed sample
written by Go itself (tools/vega_f32_json.go -golden): edges, 2000 arbitrary
bit patterns, 2000 values an embedding holds. The exhaustive run of the same
tool over all 4,278,190,080 finite float32 found two values lost by the
reading, which app/vega/tag.py repairs; negative zero is lost by Python's json
alone, which app/vega/client.py keeps. Still to prove on the node with real
vectors through the real LocalAI (docs/notes/vega.md §6.1).
"""
from __future__ import annotations

import json
import random
import struct
from pathlib import Path

import pytest

from app.vega import client
from app.vega import tag as vtag

from .gofloat import go_json

GOLDEN = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "vega" / "go_f32_json.txt"


def _sample() -> list[tuple[bytes, str]]:
    out = []
    for line in GOLDEN.read_text(encoding="ascii").splitlines():
        bits, text = line.split(" ")
        out.append((struct.pack("<I", int(bits, 16)), text))
    return out


def test_the_fake_localai_writes_a_float32_exactly_as_go_does():
    sample = _sample()
    assert len(sample) == 4000
    wrong = [(raw.hex(), text, go_json(struct.unpack("<f", raw)[0]))
             for raw, text in sample if go_json(struct.unpack("<f", raw)[0]) != text]
    assert wrong == [], wrong[:5]


def test_every_value_go_wrote_reads_back_to_its_bits_through_the_client():
    sample = _sample()
    body = json.dumps({"data": [{"index": 0, "object": "embedding", "embedding": None}]})
    body = body.replace("null", "[" + ",".join(text for _raw, text in sample) + "]")
    (vector,) = client.parse_response(body.encode())
    assert vtag.f32_bytes(vector) == b"".join(raw for raw, _text in sample)
    assert vtag.vector_digest(vector) == vtag.vector_digest(b"".join(r for r, _t in sample))


def test_what_naive_reading_loses_and_the_client_keeps():
    naive = json.loads("[-0, 7.038531e-26]")
    assert struct.pack("<2f", *naive) != bytes.fromhex("00000080fd43ae15")
    body = b'{"data":[{"index":0,"embedding":[-0,7.038531e-26]}]}'
    assert vtag.f32_bytes(client.parse_response(body)[0]) == bytes.fromhex("00000080fd43ae15")


def test_random_float32_round_trip_through_go_style_json():
    rng = random.Random(20261008)
    raws = []
    while len(raws) < 3000:
        raw = struct.pack("<I", rng.getrandbits(32))
        if (struct.unpack("<I", raw)[0] & 0x7F800000) != 0x7F800000:     # finite
            raws.append(raw)
    texts = [go_json(struct.unpack("<f", r)[0]) for r in raws]
    body = ('{"data":[{"index":0,"embedding":[' + ",".join(texts) + "]}]}").encode()
    assert vtag.f32_bytes(client.parse_response(body)[0]) == b"".join(raws)


@pytest.mark.parametrize("x", [float("nan"), float("inf")])
def test_a_non_finite_value_cannot_be_written_as_localai_cannot(x):
    with pytest.raises(ValueError):
        go_json(x)

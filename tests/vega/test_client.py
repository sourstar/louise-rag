"""app/vega/client.py — Louise accepts vectors only with their receipts, and
tells a refusal from a deferral."""
from __future__ import annotations

import base64
import json
import struct
from types import SimpleNamespace

import httpx
import pytest

from app.vega import client
from app.vega import tag as vtag

RUN_A = "sha256:" + "aa" * 32
RUN_B = "sha256:" + "bb" * 32
VECTORS = [[0.5, -0.25, 0.125], [1.0, -0.0, 0.1]]


def _body(vectors, b64=False):
    def enc(v):
        return base64.b64encode(struct.pack(f"<{len(v)}f", *v)).decode() if b64 else v
    data = [{"object": "embedding", "index": i, "embedding": enc(v)}
            for i, v in reversed(list(enumerate(vectors)))]          # any order: by index
    return json.dumps({"object": "list", "data": data}).replace("-0.0", "-0").encode()


def _receipts(batch, vectors, run=RUN_A):
    return {tid: {"id": tid, "run_id": run, "input_digest": d,
                  "vector_digest": vtag.vector_digest(v)}
            for tid, d, v in zip(batch.ids, batch.input_digests, vectors)}


def test_a_batch_is_tagged_one_fresh_id_an_input():
    batch = client.tag_batch(["a", ("title", "abstract")])
    assert len(set(batch.ids)) == 2
    assert vtag.parse(batch.inputs[0]) == (batch.ids[0], "text", "a")
    assert vtag.parse(batch.inputs[1]) == (batch.ids[1], "pair", ("title", "abstract"))
    assert batch.input_digests[1] == vtag.input_digest("pair", ("title", "abstract"))


def test_matching_receipts_give_the_vectors_and_their_run():
    batch = client.tag_batch(["one", "two"])
    vectors = client.parse_response(_body(VECTORS))
    assert client.verify(batch, vectors, _receipts(batch, VECTORS)) == {RUN_A}


def test_base64_vectors_are_digested_as_the_bytes_they_are():
    batch = client.tag_batch(["one", "two"])
    vectors = client.parse_response(_body(VECTORS, b64=True))
    assert all(isinstance(v, bytes) for v in vectors)
    assert client.verify(batch, vectors, _receipts(batch, VECTORS)) == {RUN_A}
    assert client.as_floats(vectors[0]) == VECTORS[0]


def test_a_changed_vector_is_a_refusal_naming_the_input():
    batch = client.tag_batch(["one", "two"])
    receipts = _receipts(batch, VECTORS)
    changed = [VECTORS[0], [1.0, -0.0, 0.10000001]]
    with pytest.raises(client.Refusal, match="input 1: the vector LocalAI returned"):
        client.verify(batch, client.parse_response(_body(changed)), receipts)


def test_negative_zero_read_as_python_reads_it_would_refuse_a_correct_vector():
    batch = client.tag_batch(["one", "two"])
    receipts = _receipts(batch, VECTORS)
    naive = [d["embedding"] for d in sorted(json.loads(_body(VECTORS))["data"],
                                            key=lambda d: d["index"])]
    with pytest.raises(client.Refusal):
        client.verify(batch, naive, receipts)
    assert client.verify(batch, client.parse_response(_body(VECTORS)), receipts) == {RUN_A}


def test_a_changed_input_is_a_refusal():
    batch = client.tag_batch(["one", "two"])
    receipts = _receipts(batch, VECTORS)
    receipts[batch.ids[0]]["input_digest"] = vtag.input_digest("text", "one ")
    with pytest.raises(client.Refusal, match="input 0: the backend received another input"):
        client.verify(batch, VECTORS, receipts)


def test_a_missing_receipt_is_a_deferral_never_a_refusal():
    batch = client.tag_batch(["one", "two"])
    receipts = _receipts(batch, VECTORS)
    del receipts[batch.ids[1]]
    with pytest.raises(client.Deferral, match="no receipt for 1 of 2"):
        client.verify(batch, VECTORS, receipts)


def test_a_count_or_a_receipt_that_does_not_fit_is_a_refusal():
    batch = client.tag_batch(["one", "two"])
    with pytest.raises(client.Refusal, match="2 inputs were sent and 1"):
        client.verify(batch, VECTORS[:1], _receipts(batch, VECTORS))
    receipts = _receipts(batch, VECTORS)
    receipts[batch.ids[0]]["run_id"] = "not a digest"
    with pytest.raises(client.Refusal, match="not well formed"):
        client.verify(batch, VECTORS, receipts)


def test_a_batch_answered_across_a_reload_names_both_runs():
    batch = client.tag_batch(["one", "two"])
    receipts = _receipts(batch, VECTORS)
    receipts[batch.ids[1]]["run_id"] = RUN_B
    assert client.verify(batch, VECTORS, receipts) == {RUN_A, RUN_B}


def _get(status=200, payload=None, exc=None):
    def get(url, params=None, timeout=None):
        if exc is not None:
            raise exc
        return SimpleNamespace(status_code=status, json=lambda: payload)
    return get


def test_vega_unreachable_or_failing_is_a_deferral():
    batch = client.tag_batch(["one"])
    for get in (_get(exc=httpx.ConnectError("refused")), _get(exc=httpx.ReadTimeout("slow")),
                _get(status=503), _get(payload={"nope": 1})):
        with pytest.raises(client.Deferral):
            client.verify_response(batch, _body(VECTORS[:1]), base_url="http://vega", get=get)
    with pytest.raises(client.Refusal, match="HTTP 400"):
        client.verify_response(batch, _body(VECTORS[:1]), base_url="http://vega",
                               get=_get(status=400))


def test_verify_response_asks_for_the_batch_and_returns_floats():
    batch = client.tag_batch(["one", "two"])
    asked = {}

    def get(url, params=None, timeout=None):
        asked.update(url=url, ids=params["ids"].split(","))
        return SimpleNamespace(status_code=200,
                               json=lambda: {"receipts": list(_receipts(batch, VECTORS).values())})
    vectors, runs = client.verify_response(batch, _body(VECTORS), base_url="http://vega/",
                                           get=get)
    assert asked == {"url": "http://vega/vega/receipts", "ids": list(batch.ids)}
    assert runs == {RUN_A} and vectors[1][1] == 0.0 and str(vectors[1][1]) == "-0.0"


@pytest.mark.parametrize("body", [b"not json", b'{"data": 1}', b'{"data":[{"index":1,'
                                  b'"embedding":[1]}]}', b'{"data":[{"index":0,"embedding":"!!"}]}'])
def test_a_body_that_is_not_an_embeddings_list_is_a_refusal(body):
    with pytest.raises(client.Refusal):
        client.parse_response(body)


def test_the_wrappers_unavailable_error_reads_as_a_deferral():
    text = ('{"error":{"code":500,"message":"rpc error: code = Unavailable desc = VeGa '
            'unavailable: the receipt was not acknowledged: …"}}')
    assert client.is_deferral(500, text)
    assert not client.is_deferral(500, "rpc error: code = InvalidArgument desc = VeGa: …")

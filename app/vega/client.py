"""Louise's side of VeGa (docs/notes/vega.md §2): two requests, the embedding
and the receipts, and the vectors are accepted only if she gets both and they
agree.

    batch = tag_batch(items)                 # send batch.inputs as `input`
    body = <LocalAI's /v1/embeddings answer, its raw bytes>
    vectors, run_ids = verify_response(batch, body, base_url=<VeGa>)
    # then: every run in run_ids validated for the space (03 §3.3), or write nothing

THREE OUTCOMES, and the caller tells them apart by type:
- the vectors and the run(s) that produced them;
- `Deferral`: VeGa unreachable, or a receipt missing (expired, never written,
  or LocalAI answered from a backend without the wrapper). Try again later;
  NEVER an error on a document (CLAUDE.md: deferral is not failure);
- `Refusal`: an answer that is not what the run produced for what was sent —
  an input or a vector changed on the way, a receipt that does not fit. Write
  nothing; it names the input and the rule.

READ THE BODY HERE, NOT WITH `response.json()`. LocalAI writes a float32 as
Go's shortest decimal and negative zero as `-0`, which Python's json reads as
the INTEGER 0: the sign is gone and the vector digest no longer matches the
backend's bytes, a refusal of a correct vector. `parse_response` keeps it.
With `encoding_format: "base64"` in the request LocalAI sends the float32
bytes themselves, and no decimal is read at all.

CHECK FINITENESS FIRST. A NaN vector is the intermittent SYCL corruption the
embedding client retries (`embeddings._numeric_failure`); it is never
verified, since a NaN's payload is not something a digest should be asked to
carry across two languages.

Not wired into app/embeddings.py yet (docs/notes/vega.md §8).
"""
from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from . import tag as vtag

#: what VeGa's wrapper puts in the error LocalAI passes on when the receipt
#: could not be delivered (deploy/vega/vega_wrapper.py): a deferral
UNAVAILABLE_MARK = "VeGa unavailable"


class Refusal(RuntimeError):
    """The answer is not what a run produced for what Louise sent: write
    nothing (03 §3.3)."""


class Deferral(RuntimeError):
    """VeGa cannot vouch for the answer yet — unreachable, or a receipt is
    missing. Not a failure of any document: try again later."""


@dataclass(frozen=True)
class TaggedBatch:
    ids: tuple[str, ...]
    inputs: tuple[str, ...]          # what goes in the request's `input`, in order
    input_digests: tuple[str, ...]


def tag_batch(items: Sequence[vtag.Item]) -> TaggedBatch:
    """Each item tagged with a fresh id: a text as `text`, a (first, second)
    tuple as `pair` (09 §9.3's input forms)."""
    ids, inputs, digests = [], [], []
    for item in items:
        form = vtag.TEXT if isinstance(item, str) else vtag.PAIR
        tid, tagged = vtag.tag(form, item)
        ids.append(tid)
        inputs.append(tagged)
        digests.append(vtag.input_digest(form, item))
    return TaggedBatch(tuple(ids), tuple(inputs), tuple(digests))


def _number(text: str) -> int | float:
    # "-0" is negative zero to Go, which wrote it (tools/vega_f32_json.go)
    return -0.0 if text == "-0" else int(text)


def parse_response(body: bytes | str) -> list[list[float] | bytes]:
    """LocalAI's `/v1/embeddings` answer as its vectors in input order: a list
    of numbers each, with `-0` kept negative, or the float32 bytes of a
    base64 `embedding`. A body that is not that shape is a Refusal."""
    try:
        doc = json.loads(body, parse_int=_number)
        data = sorted(doc["data"], key=lambda d: d["index"])
    except (ValueError, KeyError, TypeError) as e:
        raise Refusal(f"LocalAI's answer is not an embeddings list: {type(e).__name__}") \
            from None
    if [d.get("index") for d in data] != list(range(len(data))):
        raise Refusal("LocalAI's answer does not index its vectors 0…n-1")
    out: list[list[float] | bytes] = []
    for d in data:
        v = d.get("embedding")
        if isinstance(v, str):
            try:
                out.append(base64.b64decode(v, validate=True))
            except ValueError:
                raise Refusal(f"vector {d['index']} is not base64") from None
        elif isinstance(v, list) and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                         for x in v):
            out.append(v)
        else:
            raise Refusal(f"vector {d['index']} is neither numbers nor base64")
    return out


def as_floats(vector: list[float] | bytes) -> list[float]:
    """The vector as the float32 values its digest was taken over — the ones
    the backend produced, not the doubles a decimal read gives."""
    raw = vtag.f32_bytes(vector)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def fetch_receipts(base_url: str, ids: Sequence[str], *, timeout: float = 10.0,
                   get: Callable[..., Any] | None = None) -> dict[str, dict[str, str]]:
    """The receipts VeGa holds for these ids, by id. Unreachable, or an
    answer that is not one, is a Deferral."""
    if get is None:
        from .. import httpc
        get = httpc.get
    try:
        r = get(f"{base_url.rstrip('/')}/vega/receipts", params={"ids": ",".join(ids)},
                timeout=timeout)
    except Exception as e:                             # noqa: BLE001 — any transport failure
        raise Deferral(f"VeGa is unreachable: {type(e).__name__}") from None
    if r.status_code >= 500:
        raise Deferral(f"VeGa answered HTTP {r.status_code}")
    if r.status_code != 200:
        raise Refusal(f"VeGa refused the receipts request: HTTP {r.status_code}")
    try:
        found = r.json()["receipts"]
        return {x["id"]: x for x in found}
    except (ValueError, KeyError, TypeError):
        raise Deferral("VeGa's answer is not a receipts list") from None


def verify(batch: TaggedBatch, vectors: Sequence[list[float] | bytes],
           receipts: dict[str, dict[str, str]]) -> set[str]:
    """The run(s) that produced these vectors, or a Refusal / Deferral. Pure:
    `vectors` as `parse_response` gives them, `receipts` by id."""
    if len(vectors) != len(batch.ids):
        raise Refusal(f"{len(batch.ids)} inputs were sent and {len(vectors)} vectors came "
                      f"back")
    missing = [i for i, tid in enumerate(batch.ids) if tid not in receipts]
    if missing:
        raise Deferral(f"no receipt for {len(missing)} of {len(batch.ids)} inputs (first: "
                       f"input {missing[0]}): expired, never written, or answered by a "
                       f"backend without VeGa's wrapper")
    run_ids: set[str] = set()
    for i, (tid, want, vector) in enumerate(zip(batch.ids, batch.input_digests, vectors)):
        r = receipts[tid]
        if (r.get("id") != tid or not all(isinstance(r.get(k), str) and vtag.DIGEST.fullmatch(r[k])
                                          for k in ("run_id", "input_digest", "vector_digest"))):
            raise Refusal(f"input {i}: VeGa's receipt is not well formed")
        if r["input_digest"] != want:
            raise Refusal(f"input {i}: the backend received another input than Louise sent "
                          f"(input digest {r['input_digest']} ≠ {want})")
        try:
            got = vtag.vector_digest(vector)
        except ValueError as e:
            raise Refusal(f"input {i}: {e}") from None
        if r["vector_digest"] != got:
            raise Refusal(f"input {i}: the vector LocalAI returned is not the one the backend "
                          f"produced (vector digest {got} ≠ {r['vector_digest']})")
        run_ids.add(r["run_id"])
    return run_ids


def verify_response(batch: TaggedBatch, body: bytes | str, *, base_url: str,
                    timeout: float = 10.0, get: Callable[..., Any] | None = None
                    ) -> tuple[list[list[float]], set[str]]:
    """LocalAI's answer to `batch.inputs`, checked against VeGa's receipts:
    `(vectors as floats, in input order, the run ids)`."""
    vectors = parse_response(body)
    if len(vectors) != len(batch.ids):
        raise Refusal(f"{len(batch.ids)} inputs were sent and {len(vectors)} vectors came "
                      f"back")
    runs = verify(batch, vectors, fetch_receipts(base_url, batch.ids, timeout=timeout,
                                                 get=get))
    return [as_floats(v) for v in vectors], runs


def is_deferral(status: int, text: str) -> bool:
    """Whether LocalAI's error answer is VeGa saying it could not deliver a
    receipt (the wrapper failed the call UNAVAILABLE): a deferral."""
    return status >= 500 and UNAVAILABLE_MARK in text

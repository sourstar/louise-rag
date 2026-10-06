"""A proof of forgery (03 §3.5; 09 §9.8's `forgery` check): what a T4 miss
that reproduced on a second recompute becomes — an attestation carrying
the chunk id, the producer and its bundle, the hash of the claimed vector
and of the recomputed one (`H` over the vector as big-endian `space.dtype`
bytes), the chunk's text — or, on a red-class collection, the record's
`content_hash` in its place — signed by this peer and written at 09
§9.8's path, for `share attest` / the worker to publish. The body names
the encoder that recomputed (09 §9.8, amended 2026-10-07: the recomputed
hash means nothing without the encoder that made it).

The burn that goes with it is the trust file's (app/hold/trust_file.py).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from app.hold import check as chk, codex as cx, envelope as env
from app.hold.codex import CodexError

CHECK = RESULT = "forgery"
_BIG_ENDIAN = {"f16": ">f2", "f32": ">f4"}


def vector_id(vector, dtype: str) -> str:
    """`H` over the vector as big-endian `space.dtype` bytes (09 §9.8)."""
    kind = _BIG_ENDIAN.get(dtype)
    if kind is None:
        raise CodexError(f"space.dtype {dtype!r} is not f16 or f32; no byte form to hash")
    return cx.digest(np.asarray(vector).astype(kind).tobytes())


def detail(codex, *, chunk_id: str, producer: str, bundle: str | None, text: str | None,
           claimed, recomputed, content_hash: str | None) -> dict[str, Any]:
    red = codex.license.cls == "red"
    return {"chunk_id": chunk_id, "producer": producer, "bundle": bundle,
            "claimed": vector_id(claimed, codex.space.dtype),
            "recomputed": vector_id(recomputed, codex.space.dtype),
            "text": None if red else text, "content_hash": content_hash}


def prove(root: Path, name: str, codex, snapshot, key, *, platform_image: str, now: float | None,
          **fields) -> tuple[str, dict[str, Any]]:
    """The attestation written at 09 §9.8's path; `(id, envelope)`.
    `fields` are `detail`'s."""
    from app.share import attest, messages
    outcome = chk.Outcome(CHECK, RESULT, detail(codex, **fields), None, ())
    doc = chk.attestation(outcome, codex, snapshot, key, platform_image=platform_image,
                          now=messages.time_text(now))
    aid = env.body_id(doc["body"])
    p = attest.attestation_path(root, name, codex, aid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(cx.canonical_json(doc))
    return aid, doc

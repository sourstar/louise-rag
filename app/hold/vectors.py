"""A bundle on disk — the four files of 09 §9.7, written and read back.

`app/hold/bundle.py` owns the BYTES: what a header may say, what
`content_digest` covers, what the signature is over. This owns the FILES:
writing the four of them into a directory and reading them back with every
check §9.7 asks for, in the order it asks for them.

It knows nothing about Lance, about the manifest, or about where vectors
come from. `hold vectors build` and `hold vectors export` supply rows;
this turns rows into a bundle and back.

WHAT ORDER THE CHECKS RUN IN, and why it is not an implementation detail.
§9.7: `possession.json` is "read only after the signature verifies; its
digest is inside the signed payload". So reading is: header bytes ->
signature over the framed payload -> only then trust the parquet and the
possession map. A reader that parsed possession first would be acting on
attacker-controlled bytes nobody has vouched for.

WHAT THE PARQUET IS AND IS NOT. Its SHA-256 is inside the signature, so
the file AS RECEIVED is what was signed — but parquet bytes are not stable
across writers, so a re-export does not reproduce them and is not meant
to. `content_digest` is the container-independent hash of the logical rows
and is what a second implementation checks. Both are in the header; they
answer different questions.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from . import bundle as bd
from .bundle import BundleError

#: The old exporter's options, kept because they are right: no dictionary
#: encoding, no statistics, no embedded schema — every one of those varies
#: between writers and none of them carries information a reader needs.
_PARQUET_OPTS = dict(
    version="2.6",
    data_page_version="1.0",
    compression="none",
    use_dictionary=False,
    write_statistics=False,
    store_schema=False,
)

#: The header fields the CALLER supplies. The other four are derived from
#: the files and filled here, because a caller that computed them by hand
#: could get them wrong and the header is signed.
DERIVED = ("content_digest", "parquet_bytes", "possession_bytes", "chunk_count")


def write(dest: Path, *, header: dict[str, Any],
          challenge_rows: Sequence[tuple[str, Sequence[float]]],
          chunk_rows: Sequence[tuple[str, Sequence[float]]],
          possession: dict[str, str], key) -> str:
    """Write the four files into `dest` and return the `bundle_id`.

    `header` carries §9.7's fields EXCEPT the four in `DERIVED`, which are
    computed from what is written — a caller cannot get them wrong, and
    they are inside the signature.

    Row order is §9.7's: "the challenge rows first in index order, then the
    chunk rows sorted by (uid UTF-8 bytes, `seq` as an integer)". The sort
    happens here rather than being trusted from the caller, because a
    bundle whose rows are in another order is a bundle no other peer
    reproduces.
    """
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    supplied = set(header) & set(DERIVED)
    if supplied:
        raise BundleError(
            f"{', '.join(sorted(supplied))} is derived from the files and "
            f"filled by `write`; a caller that computes it by hand can get "
            f"it wrong, and it is inside the signature")

    for i, (cid, _) in enumerate(challenge_rows):
        if cid != bd.challenge_id(i):
            raise BundleError(
                f"challenge row {i} is {cid!r}; §9.7 fixes "
                f"{bd.challenge_id(i)!r} — the first `n` rows are the "
                f"calibration set's first `n` texts, in index order")

    rows = list(challenge_rows) + sorted(chunk_rows,
                                         key=lambda r: bd.row_order_key(r[0]))

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    dim, dtype = header["dim"], header["dtype"]
    value_type = pa.float16() if dtype == "f16" else pa.float32()
    np_dtype = np.float16 if dtype == "f16" else np.float32
    table = pa.Table.from_pydict(
        {"chunk_id": [r[0] for r in rows],
         "vector": [np.asarray(r[1], dtype=np_dtype) for r in rows]},
        schema=pa.schema([pa.field("chunk_id", pa.string()),
                          pa.field("vector", pa.list_(value_type, dim))]),
    ).combine_chunks()
    pq.write_table(table, dest / bd.VECTORS, **_PARQUET_OPTS)
    parquet_raw = (dest / bd.VECTORS).read_bytes()

    possession_raw = bd.serialise_possession(possession)
    (dest / bd.POSSESSION).write_bytes(possession_raw)

    full = dict(header,
                chunk_count=len(chunk_rows),
                content_digest=bd.content_digest(dim, rows, dtype),
                parquet_bytes=len(parquet_raw),
                possession_bytes=len(possession_raw))
    bd.check_possession_covers(full["uids"], possession)

    header_raw = bd.serialise(full)
    (dest / bd.HEADER).write_bytes(header_raw)

    payload = bd.payload(header_raw,
                         hashlib.sha256(parquet_raw).digest(),
                         hashlib.sha256(possession_raw).digest())
    (dest / bd.SIG).write_bytes(bd.sign(payload, key))
    return bd.bundle_id(payload)


def read(src: Path) -> tuple[dict[str, Any], list[tuple[str, list]], dict[str, str], str]:
    """`(header, rows, possession, bundle_id)`, or raise.

    Every check §9.7 asks for, in its order:

      1. the four files are present;
      2. the header parses and is §9.7's closed schema;
      3. THE SIGNATURE VERIFIES over the framed payload — and nothing
         below this line is trusted before it does;
      4. the parquet's rows match `content_digest`, which is what a second
         implementation can reproduce where the file's own bytes are not;
      5. the possession map covers exactly the header's `uids`.

    The signature is checked against the bytes AS RECEIVED, never against a
    re-serialisation: a header that round-trips to the same object through
    different bytes is a different file, and the file is what was signed.
    """
    src = Path(src)
    missing = [n for n in bd.FILES if not (src / n).is_file()]
    if missing:
        raise BundleError(
            f"{src}: a bundle is a directory of four files (09 §9.7); "
            f"missing {', '.join(missing)}")

    header_raw = (src / bd.HEADER).read_bytes()
    header = bd.parse(header_raw)

    parquet_raw = (src / bd.VECTORS).read_bytes()
    possession_raw = (src / bd.POSSESSION).read_bytes()
    sig = (src / bd.SIG).read_bytes()

    payload = bd.payload(header_raw,
                         hashlib.sha256(parquet_raw).digest(),
                         hashlib.sha256(possession_raw).digest())
    bd.verify(payload, sig, header["producer_pubkey"])
    # --- below here the bytes are vouched for by `producer_pubkey` ---

    for name, got, want in (("parquet_bytes", len(parquet_raw), header["parquet_bytes"]),
                            ("possession_bytes", len(possession_raw),
                             header["possession_bytes"])):
        if got != want:
            raise BundleError(f"{name} says {want} and the file is {got} bytes")

    rows = _read_rows(src / bd.VECTORS)
    got = bd.content_digest(header["dim"], rows, header["dtype"])
    if got != header["content_digest"]:
        raise BundleError(
            f"the rows hash to {got} and the header says "
            f"{header['content_digest']}. The signature covers the parquet's "
            f"own bytes; this covers what those bytes MEAN, and it is what a "
            f"reader in another language checks")

    expected = len(rows) - header["challenge"]["n"]
    if expected != header["chunk_count"]:
        raise BundleError(
            f"chunk_count is {header['chunk_count']} and the parquet holds "
            f"{len(rows)} rows after {header['challenge']['n']} challenge "
            f"rows, i.e. {expected}")

    possession = json.loads(possession_raw)
    bd.check_possession_covers(header["uids"], possession)
    return header, rows, possession, bd.bundle_id(payload)


def _read_rows(path: Path) -> list[tuple[str, list]]:
    """The parquet's rows in FILE ORDER — never sorted on read.

    `content_digest` covers the rows "in file order", so re-ordering here
    would compute a digest of something other than the file.
    """
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    names = table.schema.names
    if names != ["chunk_id", "vector"]:
        raise BundleError(
            f"vectors.parquet has columns {names}; §9.7 fixes exactly "
            f"`chunk_id` and `vector` — `seq` is parsed out of the chunk id "
            f"(§9.5) and is never a third column")
    ids = table.column("chunk_id").to_pylist()
    vecs = table.column("vector").to_pylist()
    return list(zip(ids, vecs))


def challenge_of(rows: Sequence[tuple[str, Sequence[float]]],
                 n: int) -> list[Sequence[float]]:
    """The first `n` rows' vectors — T2's answers (§9.7, 03 §3.5).

    Split off before any tier runs, and returned separately from the chunk
    rows, so that no tier can be shown a matrix that mixes the two. A
    producer that could get challenge rows counted as chunks would dilute
    every statistic computed over the chunks.
    """
    if len(rows) < n:
        raise BundleError(f"the parquet holds {len(rows)} rows and the "
                          f"header's challenge wants {n}")
    for i in range(n):
        if rows[i][0] != bd.challenge_id(i):
            raise BundleError(
                f"row {i} is {rows[i][0]!r} and the challenge rows come "
                f"first in index order (§9.7): expected {bd.challenge_id(i)!r}")
    return [r[1] for r in rows[:n]]


def chunks_of(rows: Sequence[tuple[str, Sequence[float]]],
              n: int) -> list[tuple[str, Sequence[float]]]:
    """The rows after the challenge — what `chunk_count` counts."""
    return list(rows[n:])

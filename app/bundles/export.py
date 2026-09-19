"""Export: pack a signed bundle from this node's Lance table.

Writes the four-file layout described in format.py. The invariant this module
must never break: **no text, ever**. Lance stores a `text` column; the parquet
written here carries only `chunk_id` and `vector`, and a test asserts the
column set explicitly rather than trusting this docstring.

`seq` is deliberately NOT a separate column — it is already encoded in
`chunk_id` ("{uid}:{seq}:{CHUNK_VERSION}"). Storing it twice would create two
values that can disagree, and an importer would then have to decide which one
to believe.

Parquet writer options are pinned for reproducibility, but see
format.content_digest: pyarrow's output is not byte-stable across VERSIONS, so
the signature covers a container-independent content digest as well as the
file digest.
"""
import hashlib
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .. import config, embeddings, spaces
from . import possession
from .format import (DTYPES, FORMAT_VERSION, BundleError, BundleHeader,
                     bundle_id_of, content_digest, dump_possession, sign,
                     signed_payload, sort_rows, uid_of)
from .keys import load_signing_key, public_key_hex

HEADER_NAME = "header.json"
VECTORS_NAME = "vectors.parquet"
POSSESSION_NAME = "possession.json"
SIGNATURE_NAME = "MANIFEST.sig"

# Pinned so two exports on one pyarrow version are byte-identical. Compression
# and dictionary encoding are off because both have changed framing across
# releases; statistics are off because float min/max handling has too.
_PARQUET_OPTS = dict(
    version="2.6",
    data_page_version="1.0",
    compression="none",
    use_dictionary=False,
    write_statistics=False,
    store_schema=False,
    row_group_size=100_000,
    write_page_index=False,
)

_SCAN_LIMIT = 10_000_000


class ExportError(BundleError):
    """The local node cannot produce the requested bundle."""


def _native_ids(con, uids: list[str]) -> dict[str, int]:
    """uid -> native_id, from the manifest. Lance keys rows on book_id
    (native_id), not uid."""
    out: dict[str, int] = {}
    for uid in uids:
        row = con.execute(
            "SELECT native_id FROM documents WHERE uid=?", (uid,)).fetchone()
        if row is None:
            raise ExportError(f"{uid} is not in this node's manifest")
        try:
            out[uid] = int(row["native_id"])
        except (TypeError, ValueError):
            raise ExportError(f"{uid} has a non-integer native_id") from None
    return out


def _read_vectors(collection, native_ids: list[int]) -> tuple[list[tuple], int, str]:
    """Pull (chunk_id, seq, vector) for the given documents out of Lance.

    Returns (rows, dim, dtype). Text, lang and chapter are never read.
    """
    conn = embeddings._conn(collection)
    if embeddings.TABLE not in conn.table_names():
        raise ExportError("this collection has no vector table yet")
    table = conn.open_table(embeddings.TABLE)
    field = table.schema.field("vector")
    dim = int(getattr(field.type, "list_size", 0) or 0)
    if dim <= 0:
        raise ExportError("vector column has no fixed size")
    dtype = "f16" if pa.types.is_float16(field.type.value_type) else "f32"

    ids = ",".join(str(int(n)) for n in native_ids)
    arrow = (table.search().where(f"book_id IN ({ids})")
             .limit(_SCAN_LIMIT).to_arrow())
    chunk_ids = arrow.column("id").to_pylist()
    vectors = arrow.column("vector").to_pylist()
    rows = []
    for chunk_id, vector in zip(chunk_ids, vectors):
        vec = np.asarray(vector, dtype=np.float32 if dtype == "f32" else np.float16)
        if vec.shape != (dim,):
            raise ExportError(f"vector for {chunk_id!r} has the wrong width")
        rows.append((chunk_id, 0, vec))
    if not rows:
        raise ExportError("no vectors found for the requested uids")
    return rows, dim, dtype


def _write_parquet(rows: list[tuple], dim: int, dtype: str, path: Path) -> bytes:
    value_type = pa.float16() if dtype == "f16" else pa.float32()
    schema = pa.schema([
        pa.field("chunk_id", pa.string()),
        pa.field("vector", pa.list_(value_type, dim)),
    ])
    np_dtype = np.float16 if dtype == "f16" else np.float32
    table = pa.Table.from_pydict(
        {
            "chunk_id": [r[0] for r in rows],
            "vector": [np.asarray(r[2], dtype=np_dtype) for r in rows],
        },
        schema=schema,
    ).combine_chunks()
    pq.write_table(table, path, **_PARQUET_OPTS)
    return path.read_bytes()


def export_bundle(con, collection, uids: list[str], dest: Path, *,
                  signing_key=None, backend_fingerprint: str | None = None) -> dict:
    """Pack, sign and write a bundle covering `uids`. Returns a summary dict.

    The signing key is loaded lazily and only here — a consumer-role node never
    needs it in the process at all.
    """
    # invariant 9, "not exchanged": the backstop for every caller — CLI,
    # the Library, `snapshot vectors`, contribute (audit, 2026-09-13)
    from .. import licensing
    licensing.require_ack(collection, use="its vectors are exported as a bundle")
    # 03 §3.6, the same backstop: a copy in quarantine is "not announced and
    # not served" — nothing of it leaves before this peer has tested it
    from .. import copy_state
    withheld = copy_state.withholds(collection)
    if withheld:
        raise ExportError(f"{collection.name}: nothing is exported — {withheld}")
    uids = sorted(set(uids))
    if not uids:
        raise ExportError("no uids requested")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    key = signing_key if signing_key is not None else load_signing_key()
    native_by_uid = _native_ids(con, uids)
    rows, dim, dtype = _read_vectors(collection, list(native_by_uid.values()))

    covered = {uid_of(chunk_id) for chunk_id, _, _ in rows}
    unexpected = covered - set(uids)
    if unexpected:
        raise ExportError(f"vector table returned foreign uids: {sorted(unexpected)[:5]}")

    rows = sort_rows(rows)
    parquet_path = dest / VECTORS_NAME
    parquet_bytes = _write_parquet(rows, dim, dtype, parquet_path)

    possession_map = possession.build_map(con, collection, covered)
    possession_bytes = dump_possession(possession_map)
    (dest / POSSESSION_NAME).write_bytes(possession_bytes)

    space_row = con.execute("SELECT space_id FROM space WHERE id=1").fetchone()
    if space_row is None or not space_row["space_id"]:
        raise ExportError("this collection has no pinned space_id yet")

    header = BundleHeader(
        version=FORMAT_VERSION,
        space_id=space_row["space_id"],
        collection=collection.name,
        uids=tuple(sorted(covered)),
        chunk_count=len(rows),
        dim=dim,
        dtype=dtype,
        producer_pubkey=public_key_hex(key),
        backend_fingerprint=backend_fingerprint or _backend_fingerprint(collection),
        content_digest=content_digest(rows, dim, dtype),
        parquet_bytes=len(parquet_bytes),
        possession_bytes=len(possession_bytes),
        created_at=int(time.time()),
    )
    header_bytes = header.to_wire()
    (dest / HEADER_NAME).write_bytes(header_bytes)

    payload = signed_payload(header_bytes,
                             hashlib.sha256(parquet_bytes).digest(),
                             hashlib.sha256(possession_bytes).digest())
    (dest / SIGNATURE_NAME).write_bytes(sign(payload, key))

    return {
        "bundle_id": bundle_id_of(payload),
        "path": str(dest),
        "uids": len(covered),
        "chunks": len(rows),
        "dim": dim,
        "dtype": dtype,
        "producer": header.producer_pubkey,
        "bytes": len(parquet_bytes),
    }


def _backend_fingerprint(collection=None) -> str:
    """Coarse identification of the embedding backend that produced these
    vectors (architecture-v2.md §6: "the same weights on different GPUs/drivers/
    batch sizes yield different floats, hence the backend fingerprint"). It is
    advisory metadata for a later tolerance decision, never a trust input, and
    it deliberately carries no host identity."""
    from ..collections import recipe_for
    recipe = recipe_for(collection)
    return f"{recipe.api}/{recipe.model}/{recipe.dtype}"

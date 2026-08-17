"""Genesis import — admit a PUBLISHED vector archive into a collection's
space. architecture-v2.md §8 + amendment 10: admission, not trust.

The point (and the pitch): if someone already paid the GPU-hours for a
corpus — NCBI's MedCPT embeddings of all PubMed, a community's Wikipedia
vectors — a node reuses that work instead of re-embedding, and any
"compatible embedded data" can join the platform the same way. Compatible
means: the collection's own [embed] recipe is the SAME function that
produced the archive, which is exactly what the sample re-encode proves.

Rules, all doc-mandated:
- possession-scoped: only vectors whose uids this node's manifest holds
  (RAG needs the text; §8's pointer-only rule covers the rest). The chunk
  count per document must match the archive's vectors per document exactly
  (medcpt: one per article) — partial coverage would fake embed_done.
- convert on import: f32 → the recipe dtype, L2-normalized client-side.
- admission: structural T0 (dim, NaN, dupes) + degenerate-vector check +
  sample re-encode through the collection's [embed] endpoint at
  cos >= RE_ENCODE_TAU. A shard that fails is refused WHOLE.
- provenance: one genesis_imports row per shard — cursor and audit record
  in one; re-running skips applied shards (invariant 2).

Archives are declared per plugin in ARCHIVES; a generic parquet adapter
(columns: id, vector) admits any third-party dataset that maps ids onto a
collection's uids.
"""
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from .. import chunking, config, db, embeddings, plugin_io
from ..collections import recipe_for
from .checks import check_degenerate, check_structural
from .writer_lock import vector_write_lock

RE_ENCODE_TAU = 0.95      # same-function gate; calibrated tau supersedes later
RE_ENCODE_SAMPLE = 8
APPLY_BATCH_DOCS = 500


class GenesisError(RuntimeError):
    """Terminal for this shard: recorded, shard refused whole."""


class GenesisUnavailable(RuntimeError):
    """The embed endpoint (needed for the same-function proof) or the
    archive source is unreachable — retry later, no verdict recorded."""


@dataclass(frozen=True)
class GenesisArchive:
    """Where a published embedding set lives and how to read it."""
    archive_id: str
    base_url: str
    adapter: str                      # medcpt-npy | parquet
    shards: tuple[str, ...]           # shard names, adapter-interpreted
    uid_prefix: str                   # archive id -> manifest uid


# NCBI publishes MedCPT article embeddings for all of PubMed (public
# domain): per shard, embeds_chunk_N.npy (float32 [n, 768]) with
# pmids_chunk_N.json (n PMIDs, same order).
MEDCPT_PUBMED = GenesisArchive(
    archive_id="medcpt-pubmed",
    base_url="https://ftp.ncbi.nlm.nih.gov/pub/lu/MedCPT/pubmed_embeddings",
    adapter="medcpt-npy",
    shards=tuple(str(i) for i in range(37)),
    uid_prefix="pubmed:",
)

ARCHIVES: dict[str, GenesisArchive] = {
    "medcpt-pubmed": MEDCPT_PUBMED,
}


# ---------------- adapters: shard -> iterator of (uid, vector) -------------

def _read_medcpt_shard(cache_dir: Path, shard: str,
                        fetch: Callable[[str, Path], None],
                        uid_prefix: str) -> tuple[Iterator[tuple[str, np.ndarray]], list[Path]]:
    npy = cache_dir / f"embeds_chunk_{shard}.npy"
    ids = cache_dir / f"pmids_chunk_{shard}.json"
    for name, path in ((f"embeds_chunk_{shard}.npy", npy),
                        (f"pmids_chunk_{shard}.json", ids)):
        if not path.is_file():
            fetch(name, path)
    pmids = json.loads(ids.read_text(encoding="utf-8"))
    vectors = np.load(npy, mmap_mode="r")   # 2.7 GB shards never enter RAM whole
    if len(pmids) != len(vectors):
        raise GenesisError(
            f"shard {shard}: {len(pmids)} ids vs {len(vectors)} vectors")

    def rows():
        for pmid, vec in zip(pmids, vectors):
            yield f"{uid_prefix}{pmid}", np.asarray(vec, dtype=np.float32)
    return rows(), [npy, ids]


def _read_parquet_shard(cache_dir: Path, shard: str,
                         fetch: Callable[[str, Path], None],
                         uid_prefix: str) -> tuple[Iterator[tuple[str, np.ndarray]], list[Path]]:
    """Generic adapter: any parquet with (id, vector) columns — the
    "bring your own compatible embeddings" path."""
    import pyarrow.parquet as pq
    path = cache_dir / shard
    if not path.is_file():
        fetch(shard, path)
    table = pq.read_table(path, columns=["id", "vector"])

    def rows():
        for rid, vec in zip(table.column("id").to_pylist(),
                             table.column("vector").to_pylist()):
            yield f"{uid_prefix}{rid}", np.asarray(vec, dtype=np.float32)
    return rows(), [path]


_ADAPTERS = {"medcpt-npy": _read_medcpt_shard, "parquet": _read_parquet_shard}


# ---------------- import ---------------------------------------------------

def _https_fetcher(archive: GenesisArchive):
    from ..acquire.spec import Endpoint, RemoteFile, Transport, TransportError
    from ..acquire.transport_https import HttpsTransport
    transport = HttpsTransport()
    endpoint = Endpoint(transport=Transport.HTTPS, url=archive.base_url)

    def fetch(name: str, dest: Path) -> None:
        part = dest.with_name(dest.name + ".part")
        resume = part.stat().st_size if part.is_file() else 0
        try:
            transport.fetch_one(endpoint, RemoteFile(rel_path=name), dest, resume)
        except TransportError as e:
            raise GenesisUnavailable(str(e)) from e
    return fetch


def _digest_files(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        with open(p, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
    return h.hexdigest()


def _local_chunk_texts(collection, row) -> list[tuple[str, str]]:
    """[(chunk_id, text)] in seq order for one document."""
    plugin = collection.plugin_instance()
    native_id = int(row["native_id"])
    sections = plugin_io.sections(plugin, row)
    return [(c["id"], c["text"])
            for c in chunking.chunk_book(row["uid"], native_id, sections)]


def _sample_re_encode(collection, sampled: list[tuple[str, np.ndarray]], embed_fn) -> None:
    """The same-function proof: the collection's own encoder must reproduce
    the archive's vectors on held texts. This is what makes later gap-filling
    coherent — and what keeps an INCOMPATIBLE dataset out, however well-formed."""
    texts, archived = [], []
    for text, vec in sampled:
        texts.append(text)
        archived.append(vec / (np.linalg.norm(vec) or 1.0))
    try:
        fresh = embed_fn(texts)
    except Exception as e:
        raise GenesisUnavailable(
            f"embed endpoint needed for the same-function proof is unavailable "
            f"({type(e).__name__})") from e
    worst = 1.0
    for a, f in zip(archived, fresh):
        f = np.asarray(f, dtype=np.float64)
        f /= (np.linalg.norm(f) or 1.0)
        worst = min(worst, float(a @ f))
    if worst < RE_ENCODE_TAU:
        raise GenesisError(
            f"sample re-encode failed: archive vs local encoder cos {worst:.4f} "
            f"< {RE_ENCODE_TAU} — the archive was NOT produced by this "
            f"collection's [embed] recipe; importing it would mix spaces")


def import_shard(con, collection, archive: GenesisArchive, shard: str, *,
                 embed_fn=None, fetch=None, keep_cache: bool = False) -> dict:
    """Admit one shard. Idempotent: an applied shard short-circuits."""
    existing = con.execute(
        "SELECT state FROM genesis_imports WHERE archive=? AND shard=?",
        (archive.archive_id, shard)).fetchone()
    if existing and existing["state"] == "applied":
        return {"shard": shard, "state": "already-applied"}

    recipe = recipe_for(collection)
    if embed_fn is None:
        embed_fn = lambda texts: embeddings.embed_texts(texts, recipe=recipe)  # noqa: E731
    fetch = fetch or _https_fetcher(archive)
    cache_dir = collection.manifest_path.parent / "genesis" / archive.archive_id
    cache_dir.mkdir(parents=True, exist_ok=True)

    rows_iter, files = _ADAPTERS[archive.adapter](cache_dir, shard, fetch,
                                                    archive.uid_prefix)
    digest = _digest_files(files)
    con.execute(
        "INSERT INTO genesis_imports(archive,shard,source_url,content_digest,"
        "state,updated_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(archive,shard) DO UPDATE SET state=excluded.state,"
        "content_digest=excluded.content_digest,updated_at=excluded.updated_at",
        (archive.archive_id, shard, archive.base_url, digest, "applying",
         int(time.time())))   # autocommit connection (isolation_level=None)

    np_dtype = np.float16 if recipe.dtype == "f16" else np.float32
    docs = chunks_written = skipped_absent = skipped_shape = 0
    sample: list[tuple[str, np.ndarray]] = []
    batch_rows: list[dict] = []
    batch_native: list[int] = []
    batch_uids: list[str] = []
    header_dim: int | None = None
    checked_structural = False

    def flush():
        nonlocal batch_rows, batch_native, batch_uids
        if not batch_rows:
            return
        with vector_write_lock(con):
            conn = embeddings._conn(collection)
            import pyarrow as pa
            if embeddings.TABLE in conn.table_names():
                table = conn.open_table(embeddings.TABLE)
                for native in batch_native:
                    table.delete(embeddings._book_filter(native))
                table.add(batch_rows)
            else:
                value_type = pa.float16() if recipe.dtype == "f16" else pa.float32()
                schema = pa.schema([
                    pa.field("id", pa.string()), pa.field("book_id", pa.int64()),
                    pa.field("lang", pa.string()), pa.field("seq", pa.int64()),
                    pa.field("chapter", pa.string()), pa.field("text", pa.string()),
                    pa.field("vector", pa.list_(value_type, header_dim)),
                ])
                conn.create_table(embeddings.TABLE, data=batch_rows, schema=schema)
        now = int(time.time())
        for uid in batch_uids:
            con.execute(
                "UPDATE documents SET embed_done=1, embed_version=?, updated_at=? "
                "WHERE uid=?", (config.EMBED_VERSION, now, uid))
        batch_rows, batch_native, batch_uids = [], [], []

    try:
        for uid, vec in rows_iter:
            row = con.execute(
                "SELECT uid, native_id, path, lang FROM documents "
                "WHERE uid=? AND error IS NULL AND fts_done=1 AND embed_done=0",
                (uid,)).fetchone()
            if row is None:
                skipped_absent += 1
                continue

            if header_dim is None:
                header_dim = int(vec.shape[0])
                if recipe.dim is not None and header_dim != recipe.dim:
                    raise GenesisError(
                        f"archive dim {header_dim} != recipe dim {recipe.dim}")
            norm = float(np.linalg.norm(vec))
            if not np.isfinite(vec).all() or norm == 0.0:
                raise GenesisError(f"{uid}: non-finite or zero archive vector")
            unit = (vec / norm).astype(np_dtype)

            try:
                chunk_texts = _local_chunk_texts(collection, row)
            except Exception:
                skipped_absent += 1
                continue
            if len(chunk_texts) != 1:
                # the archive has ONE vector per document; a document that
                # chunks locally into more would be left half-embedded
                skipped_shape += 1
                continue

            chunk_id, text = chunk_texts[0]
            if len(sample) < RE_ENCODE_SAMPLE:
                sample.append((text, np.asarray(vec, dtype=np.float64)))
                if len(sample) == RE_ENCODE_SAMPLE:
                    _sample_re_encode(collection, sample, embed_fn)

            batch_rows.append({
                "id": chunk_id, "book_id": int(row["native_id"]),
                "lang": row["lang"] or "en", "seq": 0, "chapter": "",
                "text": text, "vector": unit,
            })
            batch_native.append(int(row["native_id"]))
            batch_uids.append(uid)
            docs += 1
            chunks_written += 1
            if len(batch_rows) >= APPLY_BATCH_DOCS:
                # structural admission on the first full batch — cheap, and
                # a shard failing here has written nothing yet
                if not checked_structural:
                    ids = [r["id"] for r in batch_rows]
                    vecs = [np.asarray(r["vector"], dtype=np.float32)
                            for r in batch_rows]
                    for result in (check_structural(ids, vecs, header_dim,
                                                     recipe.dtype),
                                    check_degenerate(vecs)):
                        if not result.passed:
                            raise GenesisError(f"admission: {result.detail}")
                    checked_structural = True
                flush()
        if sample and len(sample) < RE_ENCODE_SAMPLE:
            _sample_re_encode(collection, sample, embed_fn)
        flush()
    except GenesisError as e:
        con.execute(
            "UPDATE genesis_imports SET state='rejected', detail=?, updated_at=? "
            "WHERE archive=? AND shard=?",
            (str(e)[:500], int(time.time()), archive.archive_id, shard))
        db.log_event(con, "error", "genesis",
                     f"{archive.archive_id}/{shard} REJECTED: {e}"[:400])
        raise
    except GenesisUnavailable:
        con.execute(
            "UPDATE genesis_imports SET state='skipped', detail='retryable', "
            "updated_at=? WHERE archive=? AND shard=?",
            (int(time.time()), archive.archive_id, shard))
        raise

    con.execute(
        "UPDATE genesis_imports SET state='applied', docs=?, chunks=?, detail=NULL, "
        "updated_at=? WHERE archive=? AND shard=?",
        (docs, chunks_written, int(time.time()), archive.archive_id, shard))
    db.log_event(con, "info", "genesis",
                 f"{archive.archive_id}/{shard}: imported {chunks_written} vectors "
                 f"across {docs} documents ({skipped_absent} not held locally, "
                 f"{skipped_shape} chunk-shape mismatches)")
    if not keep_cache:
        for p in files:
            p.unlink(missing_ok=True)
    return {"shard": shard, "state": "applied", "docs": docs,
            "chunks": chunks_written, "skipped_absent": skipped_absent,
            "skipped_shape": skipped_shape}


def import_archive(con, collection, archive: GenesisArchive, *,
                   shards: list[str] | None = None, embed_fn=None,
                   fetch=None, stop=None, keep_cache: bool = False) -> dict:
    """Walk the archive's shards through import_shard. Resumable by
    construction (applied shards short-circuit); a rejected shard stops the
    walk — a systematically incompatible archive must not burn 36 more
    downloads to prove itself 36 more times."""
    results = []
    for shard in (shards or archive.shards):
        if stop is not None and stop.is_set():
            break
        results.append(import_shard(con, collection, archive, shard,
                                     embed_fn=embed_fn, fetch=fetch,
                                     keep_cache=keep_cache))
    return {"shards": results,
            "docs": sum(r.get("docs", 0) for r in results),
            "chunks": sum(r.get("chunks", 0) for r in results)}

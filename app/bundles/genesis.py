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
    """Where a published embedding set lives and how to read it.

    `local=True` (amendment 11) marks a CORPUS-RESIDENT archive: the vectors
    live inside the collection's own corpus files (pre-embedded parquet),
    so shards are discovered on disk rather than declared, nothing is
    downloaded, corpus files are never deleted after import, and shards are
    REVISITED (oldest first) because documents become import-eligible
    gradually as rdf/fts progresses — instead of the once-only,
    wait-for-settle policy remote archives need."""
    archive_id: str
    base_url: str
    adapter: str                      # medcpt-npy | parquet | corpus-embedded
    shards: tuple[str, ...]           # shard names, adapter-interpreted; () + local → on-disk
    uid_prefix: str                   # archive id -> manifest uid
    local: bool = False


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

# The vectors travel WITH the corpus (format v1: data/{lang}/NNN.parquet
# rows of id/url/title/text/embedding): each corpus shard is a genesis
# shard, read in place.
UPSTASH_WIKIPEDIA = GenesisArchive(
    archive_id="upstash-wikipedia-2024-06",
    base_url="https://huggingface.co/datasets/Upstash/wikipedia-2024-06-bge-m3",
    adapter="corpus-embedded",
    shards=(),
    uid_prefix="embedded:",
    local=True,
)

ARCHIVES: dict[str, GenesisArchive] = {
    "medcpt-pubmed": MEDCPT_PUBMED,
    "upstash-wikipedia-2024-06": UPSTASH_WIKIPEDIA,
}

# The default-on wiring: creating a collection for one of these plugins
# declares its archive in the TOML unless the operator removes the line.
DEFAULT_ARCHIVE_BY_PLUGIN: dict[str, str] = {
    "embedded": "upstash-wikipedia-2024-06",
    "pubmed": "medcpt-pubmed",
}


def drain_next_shard(con, collection, *, stop=None) -> dict | None:
    """The worker's automatic path: import the next pending shard of the
    collection's declared archive, if there is embed work it could satisfy.
    Returns the shard result, or None when there is nothing to do (no
    declaration, no candidates, all shards applied, a shard was rejected —
    a systematically incompatible archive must not be retried forever — or
    a retryable failure is inside its backoff window)."""
    archive = ARCHIVES.get(collection.genesis_archive or "")
    if archive is None:
        return None
    candidates = con.execute(
        "SELECT 1 FROM documents WHERE fts_done=1 AND embed_done=0 "
        "AND error IS NULL LIMIT 1").fetchone()
    if candidates is None:
        return None
    if not archive.local:
        # Wait for the collection's rdf/fts backlog to settle: a REMOTE
        # shard applies ONCE, and running it while most documents are not
        # yet eligible would permanently strand them on the local-GPU path.
        # Post-settle growth is fine — appended corpora land in later,
        # still-pending shards, and any stragglers gap-fill locally into
        # the same proven space. A LOCAL archive skips this: its shards are
        # revisited, so early passes strand nothing (amendment 11).
        backlog = con.execute(
            "SELECT 1 FROM documents WHERE in_scope=1 AND error IS NULL "
            "AND (rdf_done=0 OR fts_done=0) LIMIT 1").fetchone()
        if backlog is not None:
            return None
    shards = _local_shards(collection) if archive.local else archive.shards
    rows = {r["shard"]: r for r in con.execute(
        "SELECT shard, state, updated_at FROM genesis_imports WHERE archive=?",
        (archive.archive_id,))}
    if any(r["state"] == "rejected" for r in rows.values()):
        return None
    now = int(time.time())
    for shard in _drain_order(shards, rows, revisit=archive.local):
        row = rows.get(shard)
        if row is not None and row["state"] == "skipped" \
                and now - (row["updated_at"] or 0) < 600:
            return None                # retryable failure: back off 10 min
        try:
            return import_shard(con, collection, archive, shard,
                                 revisit=archive.local)
        except GenesisUnavailable:
            return None                # recorded as skipped; retried later
        except GenesisError:
            return None                # recorded as rejected; never retried
    return None


def _drain_order(shards, rows, *, revisit: bool):
    """Remote archives: pending shards in declared order, applied ones
    excluded (they applied ONCE, post-settle, and are final). Local
    archives: pending shards first, then applied ones oldest-first — each
    revisit picks up documents that became eligible since the last pass,
    and the cycle starves itself the moment no candidates remain (the
    caller's candidates gate)."""
    pending = [s for s in shards
               if rows.get(s) is None or rows[s]["state"] != "applied"]
    if not revisit:
        return pending
    applied = sorted((s for s in shards
                      if rows.get(s) is not None and rows[s]["state"] == "applied"),
                     key=lambda s: rows[s]["updated_at"] or 0)
    return pending + applied


def _local_shards(collection) -> tuple[str, ...]:
    """Corpus-resident shards, as corpus-relative POSIX paths (stable
    genesis_imports keys across remounts)."""
    root = collection.corpus_dir
    if not root.is_dir():
        return ()
    return tuple(sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*.parquet") if p.is_file()))


# -------- adapters: shard -> iterator of (uid, [vector, ...] by seq) --------
# Every adapter yields ONE entry per document with that document's vectors
# in chunk-seq order; single-chunk archives (medcpt) yield one-element lists.

def _read_medcpt_shard(cache_dir: Path, shard: str,
                        fetch: Callable[[str, Path], None],
                        uid_prefix: str, collection) -> tuple[Iterator, list[Path]]:
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
            yield f"{uid_prefix}{pmid}", [np.asarray(vec, dtype=np.float32)]
    return rows(), [npy, ids]


def _read_parquet_shard(cache_dir: Path, shard: str,
                         fetch: Callable[[str, Path], None],
                         uid_prefix: str, collection) -> tuple[Iterator, list[Path]]:
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
            yield f"{uid_prefix}{rid}", [np.asarray(vec, dtype=np.float32)]
    return rows(), [path]


def _read_corpus_embedded_shard(cache_dir: Path, shard: str,
                                 fetch: Callable[[str, Path], None],
                                 uid_prefix: str, collection) -> tuple[Iterator, list[Path]]:
    """Amendment 11: the corpus IS the archive. `shard` is a corpus-relative
    parquet path (format v1: data/{lang}/NNN.parquet); rows of one article
    are consecutive with id "{article}_{seq}". Nothing is fetched and the
    file is part of the corpus — import_shard never deletes it."""
    import pyarrow.parquet as pq
    from ..plugins.embedded.source import LANG_ORDINAL, encode_native_id, split_row_id
    path = collection.corpus_dir / shard
    if not path.is_file():
        raise GenesisUnavailable(f"corpus shard missing: {path}")
    lang = Path(shard).parts[1] if len(Path(shard).parts) >= 3 else ""
    if lang not in LANG_ORDINAL:
        raise GenesisError(f"shard {shard}: unrecognized language directory")
    pf = pq.ParquetFile(path)

    def rows():
        # An article straddling two shard files surfaces here with a PARTIAL
        # vector list (each shard is read independently), fails the
        # chunk-shape gate, and gap-fills locally instead — ~one article per
        # shard boundary, and never a wrong import.
        current: str | None = None
        vecs: list[np.ndarray] = []
        for batch in pf.iter_batches(columns=["id", "embedding"], batch_size=1024):
            for rid, vec in zip(batch.column(0).to_pylist(), batch.column(1).to_pylist()):
                article, _seq = split_row_id(rid)
                if article != current:
                    if current is not None:
                        yield (f"{uid_prefix}{encode_native_id(lang, int(current))}",
                               vecs)
                    current, vecs = article, []
                vecs.append(np.asarray(vec, dtype=np.float32))
        if current is not None:
            yield f"{uid_prefix}{encode_native_id(lang, int(current))}", vecs
    return rows(), [path]


_ADAPTERS = {"medcpt-npy": _read_medcpt_shard, "parquet": _read_parquet_shard,
             "corpus-embedded": _read_corpus_embedded_shard}


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
    chunk = chunking.chunker_for(getattr(plugin, "chunking_profile", None))
    return [(c["id"], c["text"])
            for c in chunk(row["uid"], native_id, sections)]


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
                 embed_fn=None, fetch=None, keep_cache: bool = False,
                 revisit: bool = False) -> dict:
    """Admit one shard. Idempotent: an applied shard short-circuits, unless
    `revisit` (local archives) — re-running only touches documents that
    became eligible since, via the same delete-then-insert."""
    existing = con.execute(
        "SELECT state FROM genesis_imports WHERE archive=? AND shard=?",
        (archive.archive_id, shard)).fetchone()
    if existing and existing["state"] == "applied" and not revisit:
        return {"shard": shard, "state": "already-applied"}

    recipe = recipe_for(collection)
    if embed_fn is None:
        embed_fn = lambda texts: embeddings.embed_texts(texts, recipe=recipe)  # noqa: E731
    fetch = fetch or _https_fetcher(archive)
    cache_dir = collection.manifest_path.parent / "genesis" / archive.archive_id
    cache_dir.mkdir(parents=True, exist_ok=True)

    rows_iter, files = _ADAPTERS[archive.adapter](cache_dir, shard, fetch,
                                                    archive.uid_prefix, collection)
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
    proven = False

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

    # Truncation-aware sampling (amendment 11): the local serving stack may
    # truncate shorter than the producer did, so the same-function proof
    # samples chunks inside the LOCAL budget — where the two functions must
    # agree. Over-budget chunks still import: their archived vector is
    # strictly better than anything the local encoder could produce.
    sample_char_budget = recipe.ctx * 4

    try:
        for uid, vecs in rows_iter:
            row = con.execute(
                "SELECT uid, native_id, path, lang FROM documents "
                "WHERE uid=? AND error IS NULL AND fts_done=1 AND embed_done=0",
                (uid,)).fetchone()
            if row is None:
                skipped_absent += 1
                continue

            units = []
            for vec in vecs:
                if header_dim is None:
                    header_dim = int(vec.shape[0])
                    if recipe.dim is not None and header_dim != recipe.dim:
                        raise GenesisError(
                            f"archive dim {header_dim} != recipe dim {recipe.dim}")
                norm = float(np.linalg.norm(vec))
                if not np.isfinite(vec).all() or norm == 0.0:
                    raise GenesisError(f"{uid}: non-finite or zero archive vector")
                units.append((vec / norm).astype(np_dtype))

            try:
                chunk_texts = _local_chunk_texts(collection, row)
            except Exception:
                skipped_absent += 1
                continue
            if len(chunk_texts) != len(vecs):
                # vectors bind to chunks 1:1; a count mismatch would leave
                # the document half-embedded (or mis-attached) — skip it,
                # local gap-fill covers it in the same proven space
                skipped_shape += 1
                continue

            for seq, ((chunk_id, text), vec, unit) in enumerate(
                    zip(chunk_texts, vecs, units)):
                if not proven and len(sample) < RE_ENCODE_SAMPLE \
                        and len(text) <= sample_char_budget:
                    sample.append((text, np.asarray(vec, dtype=np.float64)))
                    if len(sample) == RE_ENCODE_SAMPLE:
                        _sample_re_encode(collection, sample, embed_fn)
                        proven = True
                batch_rows.append({
                    "id": chunk_id, "book_id": int(row["native_id"]),
                    "lang": row["lang"] or "en", "seq": seq, "chapter": "",
                    "text": text, "vector": unit,
                })
                chunks_written += 1
            batch_native.append(int(row["native_id"]))
            batch_uids.append(uid)
            docs += 1
            if len(batch_rows) >= APPLY_BATCH_DOCS:
                # ALL admission runs before the first flush — "refused
                # whole" must mean nothing was written. Structural is
                # cheap; the same-function proof runs here on a partial
                # sample if the full quota never accumulated (truncation-
                # aware filter), and an EMPTY sample refuses the shard:
                # vectors admitted without proof would be trust, not
                # admission.
                if not checked_structural:
                    ids = [r["id"] for r in batch_rows]
                    batch_vecs = [np.asarray(r["vector"], dtype=np.float32)
                                  for r in batch_rows]
                    for result in (check_structural(ids, batch_vecs, header_dim,
                                                     recipe.dtype),
                                    check_degenerate(batch_vecs)):
                        if not result.passed:
                            raise GenesisError(f"admission: {result.detail}")
                    checked_structural = True
                if not proven:
                    if not sample:
                        raise GenesisError(
                            "no re-encode sample within the local truncation "
                            "budget — cannot prove the archive matches this "
                            "collection's encoder")
                    _sample_re_encode(collection, sample, embed_fn)
                    proven = True
                flush()
        if chunks_written and not proven:
            if not sample:
                raise GenesisError(
                    "no re-encode sample within the local truncation budget — "
                    "cannot prove the archive matches this collection's encoder")
            _sample_re_encode(collection, sample, embed_fn)
            proven = True
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

    if revisit:
        # each revisit pass imports only newly-eligible documents; the
        # ledger's counters accumulate coverage across passes
        con.execute(
            "UPDATE genesis_imports SET state='applied', "
            "docs=COALESCE(docs,0)+?, chunks=COALESCE(chunks,0)+?, detail=NULL, "
            "updated_at=? WHERE archive=? AND shard=?",
            (docs, chunks_written, int(time.time()), archive.archive_id, shard))
    else:
        con.execute(
            "UPDATE genesis_imports SET state='applied', docs=?, chunks=?, detail=NULL, "
            "updated_at=? WHERE archive=? AND shard=?",
            (docs, chunks_written, int(time.time()), archive.archive_id, shard))
    db.log_event(con, "info", "genesis",
                 f"{archive.archive_id}/{shard}: imported {chunks_written} vectors "
                 f"across {docs} documents ({skipped_absent} not held locally, "
                 f"{skipped_shape} chunk-shape mismatches)")
    if not keep_cache and not archive.local:
        # local archives' files ARE the corpus — never delete them
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
    walk = shards or (list(_local_shards(collection)) if archive.local
                      else archive.shards)
    for shard in walk:
        if stop is not None and stop.is_set():
            break
        results.append(import_shard(con, collection, archive, shard,
                                     embed_fn=embed_fn, fetch=fetch,
                                     keep_cache=keep_cache,
                                     revisit=archive.local))
    return {"shards": results,
            "docs": sum(r.get("docs", 0) for r in results),
            "chunks": sum(r.get("chunks", 0) for r in results)}

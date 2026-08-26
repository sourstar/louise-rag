"""Dense layer — embeddings via Ollama, vectors in LanceDB.

The container never touches the GPU itself: all model work goes through the
Ollama HTTP API at EMBED_URL (optionally a dedicated instance with parallel
slots). EMBED_CONCURRENCY sub-batches are kept in flight to saturate those
slots; order is preserved. Vectors are stored float16 when EMBED_DTYPE=f16
(half the disk, no practical retrieval-quality cost).
"""
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import lancedb
import numpy as np
import pyarrow as pa

from . import collections as collections_mod
from . import config

TABLE = "chunks"
_SAFE_LANG = re.compile(r"[A-Za-z0-9_-]{1,16}")
_dbs: dict[str, "lancedb.DBConnection"] = {}

def _conn(collection=None):
    if collection is None:
        collection = collections_mod.default_collection()
    # Keyed on the physical path, not collection.name — see fts.py's
    # get_index() for why a name-keyed cache is unsafe.
    key = str(collection.lance_dir)
    if key not in _dbs:
        _dbs[key] = lancedb.connect(str(collection.lance_dir))
    return _dbs[key]

def _request_vectors(batch: list[str], timeout: float,
                     recipe: "collections_mod.EmbedRecipe") -> list[list[float]]:
    if recipe.api == "openai":
        r = httpx.post(f"{recipe.url}/v1/embeddings",
                       json={"model": recipe.model, "input": batch},
                       timeout=timeout)
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]
    r = httpx.post(f"{recipe.url}/api/embed",
                   json={"model": recipe.model, "input": batch},
                   timeout=timeout)
    r.raise_for_status()
    return r.json()["embeddings"]

def _invalid(v) -> bool:
    return not v or any(x is None or not math.isfinite(x) for x in v)

def _embed_batch(batch: list[str], timeout: float,
                 recipe: "collections_mod.EmbedRecipe") -> list[list[float]]:
    """Request embeddings with two self-healing behaviours:
    - null/NaN vectors (intermittent SYCL corruption) -> plain retry
    - HTTP 400 (sequence exceeds the server's per-slot budget; char-based
      truncation underestimates tokens for CJK/dense scripts) -> halve the
      char budget and retry until it fits.
    Other HTTP errors surface with the server's actual message."""
    budget = recipe.ctx * 4
    n_bad = 0
    for attempt in range(10):
        cut = [t[:budget] for t in batch]
        try:
            vecs = _request_vectors(cut, timeout, recipe)
        except httpx.HTTPStatusError as e:
            # 400: llama-server's over-budget answer. 413: TEI's (BERT-class
            # servers enforce their 512-token window and do not auto-truncate
            # unless started with --auto-truncate). Same remedy either way:
            # a SMALLER cut, in 20 % steps — halving threw away half of a
            # PubMed abstract for a 5 % overshoot (measured on the MedCPT
            # tokenizer: 3.8–6.2 chars/token, so ctx*4 chars is 410–530
            # tokens; the dense tail case needs ~10 % less, not 50 %).
            if e.response.status_code in (400, 413) and budget > 256:
                budget = max(256, int(budget * 0.8))
                continue
            detail = e.response.text[:200].replace("\n", " ")
            raise RuntimeError(
                f"embed server HTTP {e.response.status_code}: {detail}") from e
        n_bad = sum(1 for v in vecs if _invalid(v))
        if n_bad == 0 and len(vecs) == len(batch):
            out = []
            for v in vecs:
                n = math.sqrt(sum(x * x for x in v)) or 1.0
                out.append([x / n for x in v])
            return out
        time.sleep(0.3 * (attempt + 1))
    raise RuntimeError(
        f"embedding server returned {n_bad} null/NaN vectors even after "
        f"retries (upstream numeric failure — check backend flags)")

def embed_texts(texts: list[str], timeout: float = 300, *,
                recipe: "collections_mod.EmbedRecipe | None" = None,
                query: bool = False) -> list[list[float]]:
    """EMBED_CONCURRENCY sub-batches in flight; result order matches input.

    `recipe` selects the embedding backend (per-collection [embed] block via
    collections.recipe_for(); None = the Tier-1 env recipe, which keeps every
    pre-multi-space caller working unchanged). `query=True` routes through
    the recipe's query encoder when one is declared — bi-encoder pairs like
    MedCPT embed documents and queries with DIFFERENT models, and a query
    embedded with the article encoder would search a coherent-looking wrong
    neighborhood. EMBED_BATCH/EMBED_CONCURRENCY stay node-level throughput
    knobs, deliberately not part of the recipe (they change no vector)."""
    recipe = recipe or collections_mod.env_recipe()
    if query and recipe.query_url:
        recipe = collections_mod.EmbedRecipe(
            url=recipe.query_url, api=recipe.api, model=recipe.model,
            dtype=recipe.dtype, ctx=recipe.ctx, dim=recipe.dim)
    batches = [texts[i:i + config.EMBED_BATCH]
               for i in range(0, len(texts), config.EMBED_BATCH)]
    workers = min(config.EMBED_CONCURRENCY, len(batches))
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda b: _embed_batch(b, timeout, recipe), batches))
    else:
        results = [_embed_batch(b, timeout, recipe) for b in batches]
    return [v for part in results for v in part]

# A document with more chunks than this is not resumed by reading its text
# back — the read would cost more than the re-embedding it saves.
_RESUME_SCAN_MAX = 200_000


class EmbedInterrupted(RuntimeError):
    """Shutdown arrived between slices of one document. Not an error on the
    document: what was written is durable, nothing is marked done, and the
    next run continues from there."""


def _np_dtype_of_table(tbl):
    try:
        vt = tbl.schema.field("vector").type.value_type
        if pa.types.is_float16(vt):
            return np.float16
    except Exception:
        pass
    return np.float32

def _np_dtype_of_recipe(recipe):
    return np.float16 if recipe.dtype == "f16" else np.float32

def _book_filter(book_id) -> str:
    """Build a Lance filter for one document.

    Lance filters are strings, not parameterized statements, so the ONLY safe
    construction is to force the value through int() first. Locally this is
    belt-and-braces (book_id comes from int(row["native_id"])), but the bundle
    import path derives it from a uid inside an attacker-supplied header, and a
    filter string is the wrong place to discover that.
    """
    return f"book_id = {int(book_id)}"

def _written_chunks(db, book_id: int) -> dict[str, str]:
    """id -> text for the vectors this document already has. Reading the
    TEXT back is what makes a resume exact: chunk ids are positional
    (`{uid}:{seq}:{version}`, chunking.py), so an id alone cannot say
    whether the vector behind it still belongs to the current text."""
    if TABLE not in db.table_names():
        return {}
    try:
        tbl = db.open_table(TABLE)
        return {r["id"]: r["text"] for r in
                tbl.search().where(_book_filter(book_id))
                   .select(["id", "text"]).limit(_RESUME_SCAN_MAX).to_list()}
    except Exception:   # noqa: BLE001 — a resume is an optimisation
        return {}


def upsert_book(book_id: int, lang: str, chunks: list[dict], collection=None,
                lock_con=None, stop=None) -> int:
    """Write one document's vectors, delete-then-insert (invariant 2),
    INCREMENTALLY and resumably.

    Why not in one shot, as this used to do. A document is one unit of work,
    but a document is not a bounded amount of work: measured live, gutenberg
    4656 is a 21 MB book that chunks into 5,335 pieces, and at this
    endpoint's ~3.6 s per chunk that is 5.3 HOURS inside a single call. The
    old shape embedded every chunk into memory and wrote once at the end, so
    every restart in those 5.3 hours threw the whole thing away — four
    restarts in one afternoon meant the GPU produced nothing that survived,
    while `embed_done` never moved and every rate on the Stats page read
    zero. It also broke invariant 2's promise that SIGTERM finishes the
    current unit in bounded time, and CLAUDE.md's own rule that the unit is
    "a document where that is possible, smaller where it is not".

    So the vectors land in slices of EMBED_SLICE_CHUNKS, and a re-run
    reuses what is already there. The resume is exact rather than
    optimistic: a stored chunk counts as done only when its text is
    byte-identical to what the chunker produces now. If anything already
    stored does NOT match the current chunking — the document changed, or
    it shrank — the whole document's rows are deleted and it is rebuilt,
    which is the old behaviour and the safe one.

    `embed_done` is still marked by the caller, only after this returns, so
    a partially embedded document simply runs again (invariant 2).

    `lock_con`, when given, is a manifest connection used to serialize the
    delete+add pair against other processes — see app/bundles/writer_lock.py.
    It is optional so existing single-writer callers are unaffected, but the
    worker passes it, because a lock only the importer respects protects
    nothing. The lock is now taken per SLICE, so it is also held for far
    shorter stretches than before.

    `stop`, when given, is checked between slices: the work already written
    stays written and the document resumes on the next run.
    """
    if not chunks:
        return 0
    recipe = collections_mod.recipe_for(collection)
    db = _conn(collection)
    exists = TABLE in db.table_names()
    dtype = _np_dtype_of_table(db.open_table(TABLE)) if exists else _np_dtype_of_recipe(recipe)

    have = _written_chunks(db, book_id)
    wanted = {c["id"]: c["text"] for c in chunks}
    done = {i for i, text in have.items() if wanted.get(i) == text}
    # Anything stored that is not a byte-exact chunk of the CURRENT document
    # means the document changed under us: rebuild it whole.
    rebuild = bool(set(have) - done)
    if rebuild:
        done = set()
    todo = [c for c in chunks if c["id"] not in done]
    if not todo:
        return len(chunks)          # already complete; nothing to embed

    written = 0
    purge = rebuild
    for start in range(0, len(todo), config.EMBED_SLICE_CHUNKS):
        part = todo[start:start + config.EMBED_SLICE_CHUNKS]
        # Embedding happens OUTSIDE the lock: it is the slow part, it touches
        # the network, and holding a cross-process write lock across it would
        # serialize every writer on the slowest endpoint call.
        vectors = embed_texts([c["text"] for c in part], recipe=recipe)
        rows = [{
            "id": c["id"], "book_id": book_id, "lang": lang or "und",
            "seq": c["seq"], "chapter": c["chapter"] or "",
            "text": c["text"], "vector": np.asarray(vectors[j], dtype=dtype),
        } for j, c in enumerate(part)]

        with _write_lock(lock_con):
            # existence re-checked INSIDE the lock: the pre-lock check picked
            # the dtype, but two concurrent FIRST writers (the parallel embed
            # stage on a fresh collection) would otherwise both see "missing"
            # and race create_table — caught by test_worker_parallel_embed
            # before it could happen live
            if TABLE in db.table_names():
                tbl = db.open_table(TABLE)
                if purge:
                    # once, and only when the document must be rebuilt: the
                    # slices that follow are additions to a document whose
                    # remaining rows were verified byte-identical above
                    tbl.delete(_book_filter(book_id))
                    purge = False
                tbl.add(rows)
            else:
                dim = len(rows[0]["vector"])
                vt = pa.float16() if dtype is np.float16 else pa.float32()
                schema = pa.schema([
                    pa.field("id", pa.string()), pa.field("book_id", pa.int64()),
                    pa.field("lang", pa.string()), pa.field("seq", pa.int64()),
                    pa.field("chapter", pa.string()), pa.field("text", pa.string()),
                    pa.field("vector", pa.list_(vt, dim)),
                ])
                try:
                    db.create_table(TABLE, data=rows, schema=schema)
                    purge = False
                except ValueError:
                    # lost a create race despite the lock (a writer without
                    # lock_con, or a cross-process writer between our check
                    # and create) — converge on the append path
                    tbl = db.open_table(TABLE)
                    if purge:
                        tbl.delete(_book_filter(book_id))
                        purge = False
                    tbl.add(rows)
        written += len(rows)
        if stop is not None and stop.is_set() and start + len(part) < len(todo):
            # SIGTERM between slices. The slices already written STAY
            # written, but this document is not finished — and returning
            # normally here would let the caller mark it embed_done, which
            # is the one outcome that loses data. Raising is what keeps
            # invariant 2 honest: the caller treats it as a deferral, the
            # document keeps embed_done=0, and the next run resumes from the
            # vectors on disk.
            raise EmbedInterrupted(
                f"stopped after {written} of {len(todo)} chunks; "
                f"the document resumes from what is already written")
    return written + len(done)

@contextmanager
def _write_lock(lock_con):
    """No-op when no manifest connection is supplied, so callers that predate
    the bundle importer keep working unchanged."""
    if lock_con is None:
        yield
        return
    from .bundles.writer_lock import vector_write_lock
    with vector_write_lock(lock_con):
        yield

def delete_book(book_id: int, collection=None, lock_con=None):
    db = _conn(collection)
    if TABLE in db.table_names():
        with _write_lock(lock_con):
            db.open_table(TABLE).delete(_book_filter(book_id))

def search(query: str, k: int = 20, langs: list[str] | None = None, collection=None,
           book_ids: list[int] | None = None) -> list[dict]:
    """book_ids, when given, is a hard prefilter — see fts.search()'s
    docstring for the same note on why retrieval.search() doesn't use it by
    default."""
    db = _conn(collection)
    if TABLE not in db.table_names():
        return []
    tbl = db.open_table(TABLE)
    qvec = np.asarray(
        embed_texts([query], recipe=collections_mod.recipe_for(collection),
                    query=True)[0],
        dtype=_np_dtype_of_table(tbl))
    from . import vector_index
    # probe the local IVF index when the collection has one (nprobes /
    # refine), else the flat scan it always was — vector_index.query decides
    q = vector_index.query(tbl.search(qvec), collection).limit(k)
    if langs:
        # Lance filters are strings, so a lang containing a quote would break
        # out of the literal. Language codes come from the local manifest today,
        # but restricting to an alphanumeric allowlist costs nothing and means
        # no future caller can turn this into an injection.
        safe = [l for l in langs if l and _SAFE_LANG.fullmatch(l)]
        if safe:
            quoted = ",".join(f"'{l}'" for l in safe)
            q = q.where(f"lang IN ({quoted})", prefilter=True)
    if book_ids:
        ids = ",".join(str(int(i)) for i in book_ids)
        q = q.where(f"book_id IN ({ids})", prefilter=True)
    out = []
    for row in q.to_list():
        out.append({
            "score": 1.0 - float(row.get("_distance", 0.0)) / 2.0,
            "book_id": int(row["book_id"]),
            "chunk_id": row["id"],
            "chapter": row["chapter"],
            "lang": row["lang"],
            "text": row["text"],
            "source": "dense",
        })
    return out

def stats(collection=None) -> dict:
    db = _conn(collection)
    if TABLE not in db.table_names():
        return {"vectors": 0}
    return {"vectors": db.open_table(TABLE).count_rows()}

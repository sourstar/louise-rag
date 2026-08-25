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

def upsert_book(book_id: int, lang: str, chunks: list[dict], collection=None,
                lock_con=None) -> int:
    """Write one document's vectors, delete-then-insert (invariant 2).

    `lock_con`, when given, is a manifest connection used to serialize the
    delete+add pair against other processes — see app/bundles/writer_lock.py.
    It is optional so existing single-writer callers are unaffected, but the
    worker passes it, because a lock only the importer respects protects
    nothing.
    """
    if not chunks:
        return 0
    # Embedding happens OUTSIDE the lock: it is the slow part, it touches the
    # network, and holding a cross-process write lock across it would serialize
    # every writer on the slowest endpoint call.
    recipe = collections_mod.recipe_for(collection)
    vectors = embed_texts([c["text"] for c in chunks], recipe=recipe)
    db = _conn(collection)
    exists = TABLE in db.table_names()
    dtype = _np_dtype_of_table(db.open_table(TABLE)) if exists else _np_dtype_of_recipe(recipe)
    rows = [{
        "id": c["id"], "book_id": book_id, "lang": lang or "und",
        "seq": c["seq"], "chapter": c["chapter"] or "",
        "text": c["text"], "vector": np.asarray(vectors[j], dtype=dtype),
    } for j, c in enumerate(chunks)]

    with _write_lock(lock_con):
        # existence re-checked INSIDE the lock: the pre-lock check picked the
        # dtype, but two concurrent FIRST writers (the parallel embed stage on
        # a fresh collection) would otherwise both see "missing" and race
        # create_table — caught by test_worker_parallel_embed before it could
        # happen live
        if TABLE in db.table_names():
            tbl = db.open_table(TABLE)
            tbl.delete(_book_filter(book_id))
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
            except ValueError:
                # lost a create race despite the lock (a writer without
                # lock_con, or a cross-process writer between our check and
                # create) — converge on the append path
                tbl = db.open_table(TABLE)
                tbl.delete(_book_filter(book_id))
                tbl.add(rows)
    return len(rows)

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

"""Dense layer — embeddings via Ollama, vectors in LanceDB.

The container never touches the GPU itself: all model work goes through the
Ollama HTTP API at EMBED_URL (optionally a dedicated instance with parallel
slots). EMBED_CONCURRENCY sub-batches are kept in flight to saturate those
slots; order is preserved. Vectors are stored float16 when EMBED_DTYPE=f16
(half the disk, no practical retrieval-quality cost).
"""
import contextvars
import hashlib
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace

import httpx
import lancedb
import numpy as np
import pyarrow as pa

from . import collections as collections_mod
from . import config, dispatch, httpc

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

#: docs/09-formats.md §9.12: what RAGE states with every answer
RUN_HEADER = "Louise-Run"


def _note_run(response, runs: set | None) -> None:
    """Remember which run gave this answer (03 §3.3): the header's value, or
    None for an answer that stated none. The caller checks the set BEFORE it
    writes what the answers carried (app/hold/runs.py)."""
    if runs is not None:
        headers = getattr(response, "headers", None) or {}
        runs.add(headers.get(RUN_HEADER) or None)


def _request_vectors(batch: list[str], timeout: float,
                     recipe: "collections_mod.EmbedRecipe",
                     runs: set | None = None) -> list[list[float]]:
    # Every request passes its server's gate (app/dispatch.py): at most the
    # server's concurrency at a time, the highest level first. The wait is
    # bounded by this request's own timeout.
    g = dispatch.gate(recipe.url, recipe.concurrency)
    # the caller's level, for the model container's clock (09 §9.12): CORPUS
    # from the worker, QUERY from a search — whatever this thread works as
    level = dispatch.level_headers()
    with g.admit(timeout=timeout):
        if recipe.api == "openai":
            r = httpc.post(f"{recipe.url}/v1/embeddings", json=_body(recipe, batch),
                           timeout=timeout, headers=level)
            r.raise_for_status()
            _note_run(r, runs)
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            return [d["embedding"] for d in data]
        r = httpc.post(f"{recipe.url}/api/embed", json=_body(recipe, batch),
                       timeout=timeout, headers=level)
        r.raise_for_status()
        _note_run(r, runs)
        return r.json()["embeddings"]

def _invalid(v) -> bool:
    return not v or any(x is None or not math.isfinite(x) for x in v)


def _dense_script(ch: str) -> bool:
    """Characters that cost roughly one token each rather than amortising
    over four: CJK ideographs, kana and hangul. Measured at 1.02 chars per
    token against Qwen3-Embedding-8B, versus 5.70 for Latin prose."""
    o = ord(ch)
    return (0x3040 <= o <= 0x30FF or          # hiragana, katakana
            0x3400 <= o <= 0x4DBF or          # CJK extension A
            0x4E00 <= o <= 0x9FFF or          # CJK unified
            0xAC00 <= o <= 0xD7AF or          # hangul syllables
            0xF900 <= o <= 0xFAFF or          # CJK compatibility
            0x20000 <= o <= 0x2FA1F)          # CJK extension B+

def _over_budget(response) -> bool:
    """Did the server refuse an input for being longer than it will take?

    Three spellings of one answer: 400 (older llama-server), 413 (TEI), and
    500 with "too large to process" (llama-server now: "input (774 tokens)
    is too large to process. increase the physical batch size"). Seen live
    2026-09-15 serving MedCPT as GGUF — a 500 the client did not recognise
    ended the request instead of shrinking the cut, where TEI's 413 for the
    same text had always shrunk it. Any OTHER 500 is a real failure and
    still surfaces with the server's message."""
    if response.status_code in (400, 413):
        return True
    return response.status_code == 500 and "too large to process" in response.text


def _numeric_failure(response) -> bool:
    """Did the server fail to SEND a vector because it held a NaN or an Inf?

    llama-server writes such a vector as nulls, which `_invalid` catches and
    the loop below retries. LocalAI cannot encode it at all and fails the
    whole request: 500 "json: error calling MarshalJSON for type schema.Item:
    json: unsupported value: NaN". Seen on the node 2026-10-04 (migration step
    15c), in two of three passes of wikipedia's gate over bge-m3 on the B70,
    and never in the same texts when they were asked for again. The same fault
    as the nulls, in another spelling, so the same remedy: retry."""
    return response.status_code == 500 and any(
        f"unsupported value: {v}" in response.text for v in ("NaN", "+Inf", "-Inf"))


def _embed_batch(batch: list[str], timeout: float,
                 recipe: "collections_mod.EmbedRecipe", *,
                 raw: bool = False, runs: set | None = None) -> list[list[float]]:
    """Request embeddings with two self-healing behaviours:
    - null/NaN vectors (intermittent SYCL corruption) -> plain retry; also
      when the server could not send them at all (`_numeric_failure`, LocalAI)
    - HTTP 400 (sequence exceeds the server's per-slot budget; char-based
      truncation underestimates tokens for CJK/dense scripts) -> halve the
      char budget and retry until it fits.
    Other HTTP errors surface with the server's actual message.

    `raw`: the vectors as the endpoint returned them, not unit-normalised —
    what 09 §9.12's fingerprint is taken over (`embed_one_at_a_time`).
    `runs`: a set that collects the run each answer named (`_note_run`)."""
    budget = recipe.ctx * 4
    n_bad = 0
    # The FIRST guess is script-aware; the loop below is what makes it
    # correct. Measured 2026-09-09 over the 512 calibration texts against
    # Qwen3-Embedding-8B, chars per real token ran 1.02 to 5.70 — so a flat
    # `ctx * 4` is four times too generous for CJK, which then costs three
    # or four refused round-trips before the 20 % steps find the length.
    # Counting dense scripts at about a token each spends the first attempt
    # near the right size instead.
    #
    # It is an ESTIMATE and cannot be made a bound: fitted over the same
    # 512 texts, `ctx * 4` under-counts 284 of them (worst 4.04x) and the
    # best character rule tried still under-counts 65 (worst 2.38x) while
    # throwing away 37 % of every Latin window. Tokenization follows a
    # vocabulary, not a script, and the platform holds no tokenizer
    # (invariant 7). Shrink-on-refusal is therefore the mechanism that fits
    # every case, and this only decides how often it has to run.
    dense = sum(1 for t in batch for ch in t if _dense_script(ch))
    chars = sum(len(t) for t in batch)
    if dense and chars:
        per_char = (dense / 1.0 + (chars - dense) / 4.0) / chars
        budget = max(256, int(recipe.ctx / per_char))
    for attempt in range(10):
        cut = [t[:budget] for t in batch]
        try:
            # `runs` only where asked for: a test's stand-in for this function
            # takes the three arguments it always took
            vecs = (_request_vectors(cut, timeout, recipe) if runs is None
                    else _request_vectors(cut, timeout, recipe, runs))
        except httpx.HTTPStatusError as e:
            # 400: llama-server's over-budget answer. 413: TEI's (BERT-class
            # servers enforce their 512-token window and do not auto-truncate
            # unless started with --auto-truncate). Same remedy either way:
            # a SMALLER cut, in 20 % steps — halving threw away half of a
            # PubMed abstract for a 5 % overshoot (measured on the MedCPT
            # tokenizer: 3.8–6.2 chars/token, so ctx*4 chars is 410–530
            # tokens; the dense tail case needs ~10 % less, not 50 %).
            if _over_budget(e.response) and budget > 256:
                budget = max(256, int(budget * 0.8))
                continue
            if _numeric_failure(e.response):
                n_bad = len(batch)
                time.sleep(0.3 * (attempt + 1))
                continue
            detail = e.response.text[:200].replace("\n", " ")
            raise RuntimeError(
                f"embed server HTTP {e.response.status_code}: {detail}") from e
        n_bad = sum(1 for v in vecs if _invalid(v))
        if n_bad == 0 and len(vecs) == len(batch):
            if raw:
                return vecs
            out = []
            for v in vecs:
                n = math.sqrt(sum(x * x for x in v)) or 1.0
                out.append([x / n for x in v])
            return out
        time.sleep(0.3 * (attempt + 1))
    raise RuntimeError(
        f"embedding server returned {n_bad} null/NaN vectors even after "
        f"retries (upstream numeric failure — check backend flags)")

def _body(recipe: "collections_mod.EmbedRecipe", batch: list[str]) -> dict:
    """The request: the texts, and the model's name where the side has one."""
    return {"input": batch, **({"model": recipe.model} if recipe.model else {})}


def _side(recipe: "collections_mod.EmbedRecipe", query: bool) -> "collections_mod.EmbedRecipe":
    """The recipe of the side that is asked: the query encoder's when the
    collection declares one and a query is being embedded, else the recipe.

    THE QUERY SIDE NAMES THE MODEL ITS URL NAMES, never the document
    encoder's. It used to send `recipe.model` — the DOCUMENT encoder's name —
    to the query encoder's URL. llama-swap ignored the field; a runner that
    picks the model from the body (LocalAI, Ollama) would embed a MedCPT query
    with the article encoder, a coherent-looking wrong neighbourhood
    (operator, 2026-10-03). On the model container the URL says which model
    (`…/upstream/<model>`, 07 §7.8); elsewhere the side names none, so an
    endpoint that needs a name refuses rather than guessing the wrong one.

    Every other field is carried as it is (`replace`): a field rebuilt by
    hand and forgotten is a field silently dropped."""
    if not (query and recipe.query_url):
        return recipe
    return replace(recipe, url=recipe.query_url, query_url=None,
                   model=collections_mod.model_named_by(recipe.query_url) or "")


def _asked(texts: list[str], recipe: "collections_mod.EmbedRecipe",
           query: bool) -> list[str]:
    """The texts as the QUERY SIDE must receive them: the Codex's
    `space.query.instruction` prepended (02 §2.4), exactly as calibration
    and the gate prepend it (`f"{instruction}{t}"`, hold/calibration.py and
    hold/embedder.py) — no separator added or trimmed, so the function a
    search runs is byte for byte the one the gate passed.

    The one place it is applied on the serving side, so that "a query
    embedding" means the same thing on every path — search, a run's
    fingerprint, a calibration probe. An instruction applied on one path and
    not another puts two kinds of query vector in one collection and nothing
    reports it: both are honest vectors of the right width.

    Documents are never touched: "a document is embedded by space.encoder
    and never prefixed". A prefix that leaked onto the document side would
    move every vector in the collection together, so retrieval would still
    look reasonable while no longer matching the space the Codex pins."""
    if not (query and recipe.query_instruction):
        return texts
    return [f"{recipe.query_instruction}{t}" for t in texts]


def embed_one_at_a_time(texts: list[str], *, recipe: "collections_mod.EmbedRecipe",
                        query: bool = False, timeout: float = 300,
                        runs: set | None = None) -> list[list[float]]:
    """Each text ALONE in its request, in order, truncated as a chunk is, and
    the vectors AS THE ENDPOINT RETURNED THEM — not unit-normalised.

    This is how docs/09-formats.md §9.12 takes a run's fingerprint: "embedded
    one at a time … each as the endpoint returned it". It is also what
    tools/endpoint_vectors.py records before a model changes hands, so that
    what answers afterwards can be held to it bit for bit. No batching, no
    concurrency: neither EMBED_BATCH nor EMBED_CONCURRENCY applies here, on
    purpose — the gate sets this beside the way the peer really contributes
    to measure batch invariance (03 §3.3)."""
    side = _side(recipe, query)
    return [_embed_batch([text], timeout, side, raw=True, runs=runs)[0]
            for text in _asked(texts, side, query)]


def embed_texts(texts: list[str], timeout: float = 300, *,
                recipe: "collections_mod.EmbedRecipe | None" = None,
                query: bool = False, runs: set | None = None,
                raw: bool = False) -> list[list[float]]:
    """EMBED_CONCURRENCY sub-batches in flight; result order matches input.

    `recipe` is the collection's encoder (collections.recipe_for()), and it
    is required: there is no default encoder to fall back on (operator,
    2026-09-18). `query=True` routes through
    the recipe's query encoder when one is declared — bi-encoder pairs like
    MedCPT embed documents and queries with DIFFERENT models, and a query
    embedded with the article encoder would search a coherent-looking wrong
    neighborhood. EMBED_BATCH/EMBED_CONCURRENCY stay node-level throughput
    knobs, deliberately not part of the recipe (they change no vector)."""
    if recipe is None:
        raise collections_mod.NoEncoder(
            "embed_texts: no encoder recipe — a collection names its own, and "
            "there is no default")
    recipe = _side(recipe, query)
    texts = _asked(texts, recipe, query)

    def one(b: list[str]) -> list[list[float]]:
        # the two keywords only where asked for: a test's stand-in for
        # `_embed_batch` takes the three arguments it always took
        if raw or runs is not None:
            return _embed_batch(b, timeout, recipe, raw=raw, runs=runs)
        return _embed_batch(b, timeout, recipe)

    batches = [texts[i:i + config.EMBED_BATCH]
               for i in range(0, len(texts), config.EMBED_BATCH)]
    workers = min(config.EMBED_CONCURRENCY, len(batches))
    if workers > 1:
        # a pool thread starts with an EMPTY context: without handing over
        # the caller's, its requests would reach the gate at the process
        # default level instead of the caller's (one copy per task — a
        # Context cannot be entered by two threads at once)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(contextvars.copy_context().run, one, b) for b in batches]
            results = [f.result() for f in futures]
    else:
        results = [one(b) for b in batches]
    return [v for part in results for v in part]

# A document with more chunks than this is not resumed by reading its text
# back — the read would cost more than the re-embedding it saves.
_RESUME_SCAN_MAX = 200_000


class EmbedInterrupted(RuntimeError):
    """Shutdown arrived between slices of one document. Not an error on the
    document: what was written is durable, nothing is marked done, and the
    next run continues from there."""


def _committed_table(db):
    """The vector table, or None while it does not exist yet — including
    while another writer is creating it. `table_names()` lists a table as
    soon as create_table has made its directory, and `open_table()` refuses
    it ("Table 'chunks' was not found") until its first version commits.
    Seen on the node 2026-09-15: on a fresh collection the parallel embed
    stage's first writers raced, and the refusal was recorded as an error on
    a healthy document. Only a reader OUTSIDE the write lock can meet that
    state — inside it the creator has committed. Any other failure to open
    still raises."""
    if TABLE not in db.table_names():
        return None
    try:
        return db.open_table(TABLE)
    except ValueError as e:
        if "was not found" not in str(e):
            raise
        return None

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

def _create_table(db, rows):
    """The vector table, created with its first rows; the vector type and
    width are the rows' own."""
    dim = len(rows[0]["vector"])
    vt = pa.float16() if rows[0]["vector"].dtype == np.float16 else pa.float32()
    schema = pa.schema([
        pa.field("id", pa.string()), pa.field("book_id", pa.int64()),
        pa.field("lang", pa.string()), pa.field("seq", pa.int64()),
        pa.field("chapter", pa.string()), pa.field("text", pa.string()),
        pa.field("vector", pa.list_(vt, dim)),
    ])
    return db.create_table(TABLE, data=rows, schema=schema)

def _book_filter(book_id) -> str:
    """Build a Lance filter for one document.

    Lance filters are strings, not parameterized statements, so the ONLY safe
    construction is to force the value through int() first. Locally this is
    belt-and-braces (book_id comes from int(row["native_id"])), but the bundle
    import path derives it from a uid inside an attacker-supplied header, and a
    filter string is the wrong place to discover that.
    """
    return f"book_id = {int(book_id)}"

def chunk_text_hash(text: str) -> bytes:
    """The chunk hash of 06 §6.3 — BLAKE2b-128 of the chunk's text.

    The SAME digest `hold/records.chunks_root` folds into the snapshot, so
    "the hashes of the chunks it already holds" means one thing on this
    node whichever side asks.
    """
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()


def _written_chunks(db, book_id: int) -> dict[str, dict]:
    """id -> {"text", "vector"} for the vectors this document already has.

    Reading the TEXT back is what makes a resume exact: chunk ids are
    positional (`{uid}:{seq}:{version}`, chunking.py), so an id alone
    cannot say whether the vector behind it still belongs to the current
    text. Reading the VECTOR back is what makes 06 §6.3's carry-over
    possible: a chunk whose text survived an edit keeps its vector under
    whatever id it now has, and never reaches the encoder again.
    """
    if TABLE not in db.table_names():
        return {}
    try:
        tbl = db.open_table(TABLE)
        return {r["id"]: {"text": r["text"], "vector": r["vector"]} for r in
                tbl.search().where(_book_filter(book_id))
                   .select(["id", "text", "vector"]).limit(_RESUME_SCAN_MAX).to_list()}
    except Exception:   # noqa: BLE001 — a resume is an optimisation
        return {}


def vectors_by_chunk_id(chunk_ids, collection=None) -> dict[str, list]:
    """The stored vectors for these chunk ids, keyed on the id.

    FILTERED ON `book_id`, NOT ON `id`, and the difference is the whole
    reason this is a function rather than a one-liner at the call site.
    `book_id` carries the BTREE index every write already depends on
    (`vector_index.build_scalar`); `id` carries none, so an `id IN (…)`
    over a 91-million-row table is a full scan that reads like a filter.
    The ids asked for are narrowed in Python afterwards, over the rows of
    a handful of documents rather than of a corpus.

    A chunk id is `{uid}:{seq}:{version}` and `uid` is
    `{source}:{native_id}` (app/chunking.py), so the document a chunk
    belongs to is two fields off the right — no manifest lookup needed,
    which also means this works for a chunk whose document row has since
    been deleted.
    """
    ids = list(chunk_ids)
    if not ids:
        return {}
    books: set[int] = set()
    for cid in ids:
        parts = cid.rsplit(":", 2)
        if len(parts) != 3:
            continue
        try:
            books.add(int(parts[0].rsplit(":", 1)[-1]))
        except ValueError:
            continue
    if not books:
        return {}
    db = _conn(collection)
    if TABLE not in db.table_names():
        return {}
    want = set(ids)
    out: dict[str, list] = {}
    tbl = db.open_table(TABLE)
    # In batches: the predicate is a literal string, so a very large IN
    # list is a very large query, and Lance parses it per call.
    books_list = sorted(books)
    for i in range(0, len(books_list), 500):
        chunk = ",".join(str(b) for b in books_list[i:i + 500])
        try:
            rows = (tbl.search().where(f"{BOOK_COLUMN_NAME} IN ({chunk})",
                                       prefilter=True)
                    .select(["id", "vector"]).limit(_RESUME_SCAN_MAX).to_list())
        except Exception:                       # noqa: BLE001
            return out
        for r in rows:
            if r["id"] in want:
                out[r["id"]] = list(r["vector"])
    return out


#: The column `vectors_by_chunk_id` filters on. Named here rather than
#: imported from `vector_index` so this module keeps no dependency on it.
BOOK_COLUMN_NAME = "book_id"


def upsert_book(book_id: int, lang: str, chunks: list[dict], collection=None,
                lock_con=None, stop=None, *, held: "HeldRead | None" = None,
                sink: "PendingWrites | None" = None) -> int:
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

    `held` and `sink` are the parallel embed stage's, and exist for what a
    SMALL document costs. Measured on pubmed (2026-09-15): reading what one
    abstract already held took 503 ms, its commit 37 ms, the encoder 32 ms;
    and each one-document commit left a fragment every later read scans.
    `held` is that read made once for a group of documents (`held_chunks`);
    with `sink`, a document that fits one slice leaves its rows there and
    the caller commits a batch of them at once (`write_pending`), marking
    them done only after. A longer document still writes its own slices.
    """
    if not chunks:
        return 0
    recipe = collections_mod.recipe_for(collection)
    db = _conn(collection)
    if held is None:
        existing = _committed_table(db)
        dtype = (_np_dtype_of_table(existing) if existing is not None
                 else _np_dtype_of_recipe(recipe))
        # The version the read below is judged against, taken BEFORE it: a
        # commit between the two is only checked when it need not have been.
        seen = existing.version if existing is not None else 0
        have = _written_chunks(db, book_id)
    else:
        dtype = held.dtype if held.dtype is not None else _np_dtype_of_recipe(recipe)
        seen = held.seen
        have = held.of(book_id)
    wanted = {c["id"]: c["text"] for c in chunks}
    done = {i for i, r in have.items() if wanted.get(i) == r["text"]}
    # Anything stored that is not a byte-exact chunk of the CURRENT document
    # means the document changed under us: the stored rows are positional and
    # now stale, so they go. What does NOT have to go is the WORK behind
    # them.
    rebuild = bool(set(have) - done)
    carry: dict[bytes, object] = {}
    if rebuild:
        # 06 §6.3: "its vectors are carried over BY CHUNK HASH, never by
        # chunk id: the peer maps the hashes of the chunks it already holds
        # for that record to their vectors, and every recut chunk whose
        # hash is in that map keeps its vector under whatever id it now
        # has." The map is local and rebuilt from what this peer holds, so
        # nothing new travels.
        #
        # Matching by id could not work and the spec says why: `seq` is a
        # position, so inserting one paragraph at the top of an article
        # shifts the id of every chunk after it while changing the text of
        # none of them. Measured on a 71-chunk article: an inserted
        # paragraph left 70 texts byte-identical and 0 (id, text) pairs
        # intact, so every chunk was re-embedded to learn what it already
        # knew — 73 encoder calls where 3 were needed.
        carry = {chunk_text_hash(r["text"]): r["vector"] for r in have.values()}
        done = set()
    todo = [c for c in chunks if c["id"] not in done]
    if not todo:
        return len(chunks)          # already complete; nothing to embed
    if sink is not None and len(todo) <= config.EMBED_SLICE_CHUNKS:
        # one slice: embedded here, committed with the rest of its batch
        sink.put(PendingDoc(int(book_id),
                            _rows_for(book_id, lang, todo, recipe, carry, dtype, collection),
                            purge=rebuild, seen=seen))
        return len(todo) + len(done)

    written = 0
    purge = rebuild
    for start in range(0, len(todo), config.EMBED_SLICE_CHUNKS):
        part = todo[start:start + config.EMBED_SLICE_CHUNKS]
        rows = _rows_for(book_id, lang, part, recipe, carry, dtype, collection)

        with _write_lock(lock_con):
            # existence re-checked INSIDE the lock: the pre-lock check picked
            # the dtype, but two concurrent FIRST writers (the parallel embed
            # stage on a fresh collection) would otherwise both see "missing"
            # and race create_table — caught by test_worker_parallel_embed
            # before it could happen live
            if TABLE in db.table_names():
                tbl = db.open_table(TABLE)
                before = tbl.version
                if purge:
                    # once, and only when the document must be rebuilt: the
                    # slices that follow are additions to a document whose
                    # remaining rows were verified byte-identical above
                    tbl.delete(_book_filter(book_id))
                    purge = False
                elif _unexplained_since(db.uri, seen, before, book_id):
                    # the read above is stale: something this process cannot
                    # account for may have written this document since
                    _drop_clashing(tbl, book_id, [r["id"] for r in rows])
                tbl.add(rows)
                seen = tbl.version
                _note_own_versions(db.uri, before + 1, seen, book_id)
            else:
                try:
                    tbl = _create_table(db, rows)
                    purge = False
                    before = 0
                except ValueError:
                    # lost a create race despite the lock (a writer without
                    # lock_con, or a cross-process writer between our check
                    # and create) — converge on the append path; that writer
                    # is by definition one this process cannot account for
                    tbl = db.open_table(TABLE)
                    before = tbl.version
                    if purge:
                        tbl.delete(_book_filter(book_id))
                        purge = False
                    else:
                        _drop_clashing(tbl, book_id, [r["id"] for r in rows])
                    tbl.add(rows)
                seen = tbl.version
                _note_own_versions(db.uri, before + 1, seen, book_id)
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


def _by_run(collection) -> bool:
    """Is this a collection whose writes are held to a validated run (03
    §3.3)? From Codex version 3; every older collection is untouched."""
    codex = getattr(collection, "codex", None)
    if codex is None:
        return False                   # the common case: no import, no lookup
    from .hold.codex import ENGINE_VERSION
    return getattr(codex, "version", 0) >= ENGINE_VERSION


def _rows_for(book_id: int, lang: str, part: list[dict], recipe, carry: dict,
              dtype, collection=None) -> list[dict]:
    """One slice's rows. Embedding happens OUTSIDE the lock: it is the slow
    part, it touches the network, and holding a cross-process write lock
    across it would serialize every writer on the slowest endpoint call.
    Only the chunks whose TEXT this peer does not already hold reach the
    encoder. The rest are carried at their new ids for free.

    A VERSION-3 COLLECTION'S ROWS LEAVE HERE ONLY IF EVERY ANSWER NAMED A
    VALIDATED RUN (03 §3.3: "checked on every answer, before the write").
    Here, because every write of the worker's — a slice of a long document,
    a small one left in the sink — is made of rows built here, so there is
    no second path to forget. The check is a lookup, inside no lock and no
    transaction; it raises, and the caller defers the document untouched."""
    fresh = [c for c in part if chunk_text_hash(c["text"]) not in carry]
    if fresh and _by_run(collection):
        from . import run_check                  # it imports this module
        seen: set = set()
        vectors = embed_texts([c["text"] for c in fresh], recipe=recipe, runs=seen)
        run_check.check_write(collection, recipe, seen)
    else:
        vectors = embed_texts([c["text"] for c in fresh], recipe=recipe) if fresh else []
    embedded = {c["id"]: vectors[j] for j, c in enumerate(fresh)}
    return [{
        "id": c["id"], "book_id": book_id, "lang": lang or "und",
        "seq": c["seq"], "chapter": c["chapter"] or "",
        "text": c["text"],
        "vector": np.asarray(
            embedded[c["id"]] if c["id"] in embedded
            else carry[chunk_text_hash(c["text"])], dtype=dtype),
    } for c in part]


@dataclass(frozen=True)
class HeldRead:
    """What a group of documents already holds in the vector table, read
    once for the group (`held_chunks`). `seen` is the table version the
    read is judged against, and `dtype` the table's vector type, None while
    there is no table."""
    by_book: dict
    seen: int
    dtype: object = None

    def of(self, book_id) -> dict:
        return self.by_book.get(int(book_id), {})


def held_chunks(collection, book_ids) -> HeldRead:
    """`_written_chunks` for a whole group of documents, in one read.

    It costs about the same for a hundred documents as for one: the time
    goes on the fragments the book_id index does not cover yet, which a
    read scans whatever it asks for (pubmed, 2026-09-15: 697 ms at 3,559
    fragments, 918 ms at 3,972). Raises when the table cannot be read, or
    when the group holds more rows than one read returns: an incomplete
    answer would read as "not written yet" and store chunks twice."""
    db = _conn(collection)
    tbl = _committed_table(db)
    if tbl is None:
        return HeldRead({}, 0, None)
    seen = tbl.version
    by_book: dict[int, dict[str, dict]] = {}
    ids = sorted({int(b) for b in book_ids})
    for i in range(0, len(ids), 500):
        part = ",".join(str(b) for b in ids[i:i + 500])
        rows = (tbl.search().where(f"{BOOK_COLUMN_NAME} IN ({part})", prefilter=True)
                .select(["id", "book_id", "text", "vector"])
                .limit(_RESUME_SCAN_MAX).to_list())
        if len(rows) >= _RESUME_SCAN_MAX:
            raise RuntimeError(f"{len(rows)} stored rows for {len(ids[i:i + 500])} "
                               f"documents: more than one read returns")
        for r in rows:
            by_book.setdefault(int(r["book_id"]), {})[r["id"]] = {
                "text": r["text"], "vector": r["vector"]}
    return HeldRead(by_book, seen, _np_dtype_of_table(tbl))


@dataclass
class PendingDoc:
    """One document's rows, embedded and not yet written."""
    book_id: int
    rows: list
    purge: bool       # rebuilt: its stored rows go first
    seen: int         # the version its read of held rows was judged against


class PendingWrites:
    """Where `upsert_book(..., sink=)` leaves a one-slice document's rows;
    `write_pending` commits a batch of them. The parallel stage's workers
    put, its main thread takes."""

    def __init__(self):
        self._docs: dict[int, PendingDoc] = {}
        self._lock = threading.Lock()

    def put(self, doc: PendingDoc) -> None:
        with self._lock:
            self._docs[doc.book_id] = doc

    def holds(self, book_id) -> bool:
        with self._lock:
            return int(book_id) in self._docs

    def take(self, book_ids) -> list[PendingDoc]:
        with self._lock:
            return [self._docs.pop(int(b)) for b in book_ids if int(b) in self._docs]


def write_pending(docs: list[PendingDoc], collection=None, lock_con=None,
                  on_written=None) -> None:
    """Commit a batch of documents, delete-then-insert per document
    (invariant 2), in at most three Lance commits whatever its size: the
    rebuilt documents' deletes, the chunks another writer stored since a
    document's read, and ONE add. `upsert_book` makes one or more commits
    per document.

    `on_written` runs inside the write lock after the add. With `lock_con`
    the caller's manifest connection, the documents are marked done in the
    transaction that serialised the write, and never before it is durable.
    A failure anywhere rolls that transaction back and marks nothing. A
    batch whose add landed and whose mark did not is read back as complete
    next time, and marked without reaching the encoder again."""
    docs = [d for d in docs if d.rows]
    if not docs:
        return
    db = _conn(collection)
    rows = [r for d in docs for r in d.rows]
    purge = [d.book_id for d in docs if d.purge]
    with _write_lock(lock_con):
        if TABLE in db.table_names():
            tbl = db.open_table(TABLE)
            before = tbl.version
            _purge_books(tbl, purge)
            stale = {d.book_id: [r["id"] for r in d.rows] for d in docs
                     if not d.purge and _unexplained_since(db.uri, d.seen, before, d.book_id)}
            if stale:
                _drop_clashing_many(tbl, stale)
            tbl.add(rows)
        else:
            try:
                tbl = _create_table(db, rows)
                before = 0
            except ValueError:
                # lost a create race despite the lock: converge on the
                # append path; that writer is one this process cannot
                # account for, so every document is checked
                tbl = db.open_table(TABLE)
                before = tbl.version
                _purge_books(tbl, purge)
                _drop_clashing_many(tbl, {d.book_id: [r["id"] for r in d.rows]
                                          for d in docs if not d.purge})
                tbl.add(rows)
        _note_own_versions(db.uri, before + 1, tbl.version, [d.book_id for d in docs])
        if on_written is not None:
            on_written()


def _purge_books(tbl, book_ids) -> None:
    books = sorted({int(b) for b in book_ids})
    for i in range(0, len(books), 500):
        tbl.delete(f"{BOOK_COLUMN_NAME} IN ({','.join(str(b) for b in books[i:i + 500])})")

#: A chunk id is `{uid}:{seq}:{version}` and a uid is `{source}:{native_id}`
#: (app/chunking.py, 09 §9.5). Lance filters are strings and not
#: parameterised statements, so an id that reaches one is checked against
#: the shape FIRST: `upsert_chunks` derives these from a bundle written by
#: a stranger, and a filter string is the wrong place to discover a quote.
_CHUNK_ID = re.compile(r"\A[A-Za-z0-9_.+-]+:[0-9]+:[0-9]+:[0-9]+\Z")


#: The versions of each vector table that THIS process's upsert_book
#: committed, keyed on the table's location (a path, never a name) and
#: mapped to the document each one wrote. Under the write lock a writer
#: compares the table's version with the one it saw before its read; a gap
#: this log explains entirely — its siblings in the parallel embed stage
#: writing OTHER documents — cannot have touched its document and costs no
#: read. Anything else (an import, another process, index maintenance, its
#: own document) is checked by `_drop_clashing`, which measured 0.55–0.65 s
#: on pubmed (35.4M rows, 7,967 fragments, 2026-09-15) whatever its shape:
#: paid per document inside the lock, it would serialise every writer.
_own_versions: dict[str, dict[int, frozenset]] = {}
_own_versions_lock = threading.Lock()
_OWN_VERSIONS_KEPT = 10_000
#: A batched commit names up to EMBED_BATCH_DOCS documents, so the log is
#: bounded by the documents it names as well as by its versions.
_OWN_IDS_KEPT = 200_000
_own_ids: dict[str, int] = {}


def _note_own_versions(where: str, first: int, last: int, book_ids) -> None:
    """Versions first..last of the table at `where` are this process's
    write of `book_ids` (one document, or a batch) — called under the write
    lock, so nothing else committed in between."""
    ids = frozenset([int(book_ids)] if isinstance(book_ids, int)
                    else (int(b) for b in book_ids))
    with _own_versions_lock:
        log = _own_versions.setdefault(where, {})
        total = _own_ids.get(where, 0)
        for v in range(first, last + 1):
            total += len(ids) - len(log.get(v, ()))
            log[v] = ids
        if len(log) > _OWN_VERSIONS_KEPT or total > _OWN_IDS_KEPT:
            for v in sorted(log):
                if len(log) <= _OWN_VERSIONS_KEPT * 0.9 and total <= _OWN_IDS_KEPT * 0.9:
                    break
                total -= len(log.pop(v))
        _own_ids[where] = total


def _unexplained_since(where: str, seen: int, current: int, book_id: int) -> bool:
    """Could a commit after version `seen` have written `book_id`'s rows?
    Only a version this process logged for OTHER documents says no."""
    if current == seen:
        return False
    if current < seen or current - seen > _OWN_VERSIONS_KEPT:
        return True                  # the table was replaced, or the log cannot say
    with _own_versions_lock:
        log = _own_versions.get(where, {})
        for v in range(seen + 1, current + 1):
            ids = log.get(v)
            if ids is None or book_id in ids:
                return True
        return False


def _drop_clashing(tbl, book_id: int, ids: list[str]) -> None:
    """Delete-then-insert keyed on the chunk id (invariant 2), for
    upsert_book's append, whose caller holds the write lock — run only when
    `_unexplained_since` cannot rule a foreign write out.

    upsert_book reads what a document holds BEFORE the lock, to embed only
    what is missing, and that read is stale by the time the lock is held:
    an import in another process (the CLI, the web) may have written the
    same document since, and appending on top of it stored every chunk
    twice — tests/bundles/test_import.py's concurrent test, 24 rows for 12
    ids, in a node suite run on 2026-09-15. Narrowed on `book_id`, the
    BTREE every write depends on, and deleting only on a clash, as
    upsert_chunks does."""
    _drop_clashing_many(tbl, {int(book_id): ids})


def _drop_clashing_many(tbl, ids_by_book: dict) -> None:
    """`_drop_clashing` for several documents, one read per 500 of them —
    `write_pending`'s, for the documents of a batch whose read another
    writer may have overtaken."""
    books = sorted(int(b) for b in ids_by_book)
    want = {c for ids in ids_by_book.values() for c in ids}
    for i in range(0, len(books), 500):
        part = ",".join(str(b) for b in books[i:i + 500])
        have = [x["id"] for x in
                tbl.search().where(f"{BOOK_COLUMN_NAME} IN ({part})", prefilter=True)
                .select(["id"]).limit(_RESUME_SCAN_MAX).to_list()
                if x["id"] in want]
        for cid in have:
            if not _CHUNK_ID.match(cid):
                raise ValueError(
                    f"{cid!r} is not `<source>:<native_id>:<seq>:<version>` "
                    f"(09 §9.5); no filter is built from it")
        for j in range(0, len(have), 500):
            quoted = ",".join(f"'{c}'" for c in have[j:j + 500])
            tbl.delete(f"{BOOK_COLUMN_NAME} IN ({part}) AND id IN ({quoted})")


def upsert_chunks(rows: list[dict], collection=None, lock_con=None, *,
                  dtype: str | None = None) -> int:
    """Write vectors that came from ELSEWHERE, keyed on the chunk id.

    `upsert_book` is the local build: it embeds text and owns a whole
    document, so it deletes the document. This is the import — the
    vectors already exist, they arrived for a subset of chunk ids, and
    the same document may hold rows this peer embedded itself. Deleting
    the document would throw away exactly the rows a partial bundle does
    not carry, so the key is the chunk id (invariant 2 names both).

    The delete is narrowed on `book_id` before `id`, for the reason
    `vectors_by_chunk_id` gives at length: `book_id` carries the BTREE
    index every write already depends on and `id` carries none, so an
    `id IN (…)` alone is a full scan over the corpus that reads like a
    filter — a per-bundle cost that would scale with the table.

    `dtype` is the SPACE's (`"f16"`/`"f32"`), which the importer reads
    from the Codex where it is one of the fields inside `space_id`
    (invariant 1). It beats the node's own recipe, which is a deployment
    detail — but an existing table still wins over both, because a column
    is a fixed width and a bundle does not get to change it.
    """
    if not rows:
        return 0
    for r in rows:
        if not _CHUNK_ID.match(r["id"]):
            raise ValueError(
                f"{r['id']!r} is not `<source>:<native_id>:<seq>:<version>` "
                f"(09 §9.5); it is not written and no filter is built from it")

    db = _conn(collection)
    existing = _committed_table(db)
    if existing is not None:
        np_dtype = _np_dtype_of_table(existing)
    elif dtype is not None:
        np_dtype = np.float16 if dtype == "f16" else np.float32
    else:
        np_dtype = _np_dtype_of_recipe(collections_mod.recipe_for(collection))
    out = [dict(r, vector=np.asarray(r["vector"], dtype=np_dtype)) for r in rows]

    with _write_lock(lock_con):
        if TABLE in db.table_names():
            tbl = db.open_table(TABLE)
            books = sorted({int(r["book_id"]) for r in out})
            ids = {r["id"] for r in out}
            for i in range(0, len(books), 500):
                part = ",".join(str(b) for b in books[i:i + 500])
                have = [x["id"] for x in
                        tbl.search().where(f"{BOOK_COLUMN_NAME} IN ({part})",
                                           prefilter=True)
                        .select(["id"]).limit(_RESUME_SCAN_MAX).to_list()
                        if x["id"] in ids]
                for j in range(0, len(have), 500):
                    quoted = ",".join(f"'{c}'" for c in have[j:j + 500])
                    tbl.delete(f"{BOOK_COLUMN_NAME} IN ({part}) AND "
                               f"id IN ({quoted})")
            tbl.add(out)
        else:
            dim = len(out[0]["vector"])
            vt = pa.float16() if np_dtype is np.float16 else pa.float32()
            db.create_table(TABLE, data=out, schema=pa.schema([
                pa.field("id", pa.string()), pa.field("book_id", pa.int64()),
                pa.field("lang", pa.string()), pa.field("seq", pa.int64()),
                pa.field("chapter", pa.string()), pa.field("text", pa.string()),
                pa.field("vector", pa.list_(vt, dim)),
            ]))
    return len(out)


def delete_chunks(chunk_ids, collection=None, lock_con=None) -> int:
    """Empty specific rows — 02 §2.4's remedy for a vector that no longer
    reproduces (`hold vectors check --drop`).

    Same narrowing and the same id check as `upsert_chunks`, for the same
    reasons. Returns how many of the ids were actually present.
    """
    ids = [str(c) for c in chunk_ids]
    if not ids:
        return 0
    for cid in ids:
        if not _CHUNK_ID.match(cid):
            raise ValueError(
                f"{cid!r} is not `<source>:<native_id>:<seq>:<version>` "
                f"(09 §9.5); nothing is deleted and no filter is built from it")
    db = _conn(collection)
    if TABLE not in db.table_names():
        return 0
    books = sorted({int(c.rsplit(":", 3)[1]) for c in ids})
    want = set(ids)
    gone = 0
    with _write_lock(lock_con):
        tbl = db.open_table(TABLE)
        for i in range(0, len(books), 500):
            part = ",".join(str(b) for b in books[i:i + 500])
            have = [x["id"] for x in
                    tbl.search().where(f"{BOOK_COLUMN_NAME} IN ({part})",
                                       prefilter=True)
                    .select(["id"]).limit(_RESUME_SCAN_MAX).to_list()
                    if x["id"] in want]
            for j in range(0, len(have), 500):
                quoted = ",".join(f"'{c}'" for c in have[j:j + 500])
                tbl.delete(f"{BOOK_COLUMN_NAME} IN ({part}) AND id IN ({quoted})")
            gone += len(have)
    return gone


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
    recipe = collections_mod.recipe_for(collection)
    if _by_run(collection):
        # 03 §3.3, searching: "the same runs, validated the same way". The
        # answer's run is checked BEFORE its vector is used; one not yet
        # tested answers no query, one that failed withholds this leg. It
        # raises, and retrieval.search degrades to the text leg and says so.
        from . import run_check
        seen: set = set()
        vector = embed_texts([query], recipe=recipe, query=True, runs=seen)[0]
        run_check.check_query(collection, recipe, seen)
    else:
        vector = embed_texts([query], recipe=recipe, query=True)[0]
    qvec = np.asarray(vector, dtype=_np_dtype_of_table(tbl))
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

"""Lexical layer — one Tantivy index per (collection, language group).

Languages with a snowball stemmer get their own analyzer; CJK is indexed as
character bigrams; everything else shares a generic lowercase analyzer.
Writes are idempotent: delete_documents(book_id) then re-add.
"""
import re
import threading
import tantivy
from . import collections as collections_mod
from . import config
from . import stemming

_lock = threading.Lock()
_indexes: dict[tuple[str, str], tantivy.Index] = {}
#: the same key -> (the one writer for that index, its lock). See `_writer`.
_writers: dict = {}

def group_for(lang: str) -> str:
    lang = (lang or "und").split("-")[0]
    if lang in config.STEMMER_LANGS:
        return lang
    if lang in config.CJK_LANGS:
        return "cjk"
    return "other"

def pystem_algorithm(group: str) -> str | None:
    """The PyStemmer algorithm for a group, or None when tantivy stems it
    (see config.PYSTEM_LANGS for why the split exists)."""
    if group in config.PYSTEM_LANGS and group in config.STEMMER_LANGS:
        return config.STEMMER_LANGS[group]
    return None

def _schema(group: str) -> tantivy.Schema:
    """Per GROUP, deliberately: adding a field changes the schema, and
    tantivy refuses to open an index whose on-disk schema differs. Scoping
    body_stem to the groups that use it means adopting PyStemmer for one
    language rebuilds ONE index instead of every index on the node."""
    b = tantivy.SchemaBuilder()
    b.add_integer_field("book_id", stored=True, indexed=True, fast=True)
    b.add_text_field("chunk_id", stored=True, tokenizer_name="raw")
    b.add_text_field("chapter", stored=True, tokenizer_name="default")
    b.add_text_field("lang", stored=True, tokenizer_name="raw")
    b.add_text_field("body", stored=True, tokenizer_name="lang_tok")
    if pystem_algorithm(group):
        # stems get their own field so `body` keeps the ORIGINAL text for
        # snippets; not stored, because nobody reads stems
        b.add_text_field("body_stem", stored=False, tokenizer_name="stem_tok")
    return b.build()

def _analyzer_for(group: str) -> tantivy.TextAnalyzer:
    if group == "cjk":
        base = tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.ngram(2, 2, False))
    else:
        base = tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
    base = base.filter(tantivy.Filter.remove_long(64)).filter(tantivy.Filter.lowercase())
    # PYSTEM_LANGS are stemmed by app/stemming.py before they get here, so
    # tantivy must not stem them again — and for Greek it cannot: its
    # stemmer panics (docs/notes/upstream-rust-stemmers-greek.md).
    if group in config.STEMMER_LANGS and not pystem_algorithm(group):
        base = base.filter(tantivy.Filter.stemmer(config.STEMMER_LANGS[group]))
    return base.build()

def _stem_analyzer() -> tantivy.TextAnalyzer:
    """For body_stem, whose content is ALREADY lowercased and stemmed —
    split it and nothing more."""
    base = tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
    return base.filter(tantivy.Filter.remove_long(64)).build()

def get_index(group: str, collection=None) -> tantivy.Index:
    if collection is None:
        collection = collections_mod.default_collection()
    # Keyed on the physical path, not collection.name: two different
    # Collection instances can share a name (most concretely, in tests that
    # build ad-hoc Collections) while pointing at different fts_dirs — a
    # name-keyed cache would silently return the wrong index or crash with a
    # stale-lock error when that happens.
    key = (str(collection.fts_dir), group)
    with _lock:
        if key in _indexes:
            return _indexes[key]
        path = collection.fts_dir / group
        path.mkdir(parents=True, exist_ok=True)
        try:
            idx = tantivy.Index(_schema(group), path=str(path))
        except Exception as e:
            # An index built under a different schema cannot be opened.
            # Say what to do about it: search() swallows exceptions, so
            # without this the only symptom is a language that silently
            # returns no hits.
            raise RuntimeError(
                f"tantivy index at {path} was built with a different schema "
                f"({type(e).__name__}: {e}). This happens when a language "
                f"joins or leaves config.PYSTEM_LANGS. Delete {path} and "
                f"re-index that language's documents "
                f"(UPDATE documents SET fts_done=0 WHERE lang=...)") from None
        idx.register_tokenizer("lang_tok", _analyzer_for(group))
        if pystem_algorithm(group):
            idx.register_tokenizer("stem_tok", _stem_analyzer())
        _indexes[key] = idx
        _writers.pop(key, None)     # a new Index object: a writer on the old one is stale
        return idx

def existing_groups(collection=None) -> list[str]:
    if collection is None:
        collection = collections_mod.default_collection()
    if not collection.fts_dir.is_dir():
        return []
    return sorted(p.name for p in collection.fts_dir.iterdir()
                  if p.is_dir() and (p / "meta.json").exists()
                  and not p.name.startswith("_"))   # `_catalog` is app/catalog.py's, not a group

#: (meta.json path, its mtime_ns) -> the languages that index holds
_group_langs: dict[tuple[str, int], list[str]] = {}
#: stored `lang` values that name no language to translate into
_NOT_LANGUAGES = {"", "und", "zxx", "mul", "mis"}


def group_languages(group: str, collection=None) -> list[str]:
    """The languages a group's index holds. A one-language group is its own
    language; a MIXED group (cjk, other) holds whatever its documents say,
    read off the `lang` field's term dictionary — gutenberg's `other` holds
    some forty (Latin, Esperanto, Tagalog, Welsh, …). Cached on the index's
    own meta.json, path and mtime: a commit changes it, a name never does."""
    if group not in ("cjk", "other"):
        return [group]
    if collection is None:
        collection = collections_mod.default_collection()
    meta = collection.fts_dir / group / "meta.json"
    try:
        key = (str(meta), meta.stat().st_mtime_ns)
    except OSError:
        return []
    hit = _group_langs.get(key)
    if hit is not None:
        return hit
    try:
        idx = get_index(group, collection)
        idx.reload()
        terms = idx.searcher().terms_with_prefix("lang", "")
    except Exception:   # noqa: BLE001 — an unreadable index holds nothing we can name
        return []
    langs = sorted({str(t).split("-")[0].lower() for t, n in terms if n > 0}
                   - _NOT_LANGUAGES)
    if len(_group_langs) > 256:
        _group_langs.clear()
    _group_langs[key] = langs
    return langs


def group_sizes(collection=None) -> dict[str, int]:
    """Chunks indexed per language group, from each index's own searcher —
    what query translation orders its languages by, largest first. An
    unreadable group counts as empty rather than failing the search."""
    sizes: dict[str, int] = {}
    for group in existing_groups(collection):
        try:
            idx = get_index(group, collection)
            idx.reload()
            sizes[group] = int(idx.searcher().num_docs)
        except Exception:   # noqa: BLE001
            sizes[group] = 0
    return sizes


def _add_one(writer, algorithm, lang: str, book_id: int, chunks: list[dict]):
    writer.delete_documents("book_id", book_id)
    for c in chunks:
        fields = dict(book_id=book_id, chunk_id=c["id"],
                      chapter=c["chapter"] or "", lang=lang or "und",
                      body=c["text"])
        if algorithm:
            fields["body_stem"] = stemming.stem_text(algorithm, c["text"])
        writer.add_document(tantivy.Document(**fields))


def _writer(key, idx):
    """The worker's ONE writer for this index, created on first use and kept
    — the same thing app/catalog.py does for the catalog, and for the same
    reason it says there: a writer per batch had to `wait_merging_threads()`
    before it could let go of the directory, and that wait happens inside the
    fts stage's manifest transaction, which every other writer of that
    manifest is queued behind.

    MEASURED, 2026-09-22: a fixed 4.5 s per fts batch, against 35 ms per
    document — a 64 MB heap allocated, a commit, and those merges, none of
    which shrink with the batch. The sizer now amortises a fixed cost over a
    bigger batch (app/sizing.py); this removes most of it instead.

    Returns (writer, lock). The lock is the writer's: tantivy allows ONE
    writer per directory, so the search path's deletes and the stage's
    commits take turns rather than racing for it."""
    with _lock:
        got = _writers.get(key)
        if got is None:
            got = _writers[key] = (idx.writer(heap_size=64_000_000), threading.Lock())
        return got


def close_all() -> None:
    """Let go of every writer this process holds — for tests, whose
    collections come and go. The worker keeps its writers until it exits.
    Each under ITS OWN lock: taking a writer from a thread still inside a
    commit raises "Already borrowed" (app/catalog.py learned that live)."""
    with _lock:
        held = list(_writers.items())
        _writers.clear()
    for _key, (writer, lock) in held:
        with lock:
            writer.wait_merging_threads()      # consumes it: the directory is free


def index_books(entries, collection=None) -> None:
    """Index several documents through ONE writer per language group.

    `entries` is an iterable of (lang, book_id, chunks).

    The per-document writer this replaced cost a fresh 64 MB allocation, a
    commit and a merge wait for EVERY document — a fixed cost that does not
    shrink with the document. Measured live: ~30s for a 2 kb genome
    scaffold, the same as for a 23 MB chromosome arm, because the work is in
    the commit and not in the content. The per-BATCH writer that replaced it
    still cost 4.5 s a batch (the node, 2026-09-22, regressing duration on
    batch size over 31 batches, against 35 ms per document), so the writer is
    now KEPT — see `_writer`.

    Still idempotent per document: `delete_documents(book_id)` stays keyed on
    the individual book, so re-running a batch — in whole or in part — cannot
    duplicate anything. The caller must mark the documents done only AFTER
    this returns (see worker.stage_fts): a crash between the commit here and
    that mark simply re-indexes them, which is a no-op by construction.
    """
    by_group: dict[str, list] = {}
    for lang, book_id, chunks in entries:
        by_group.setdefault(group_for(lang), []).append((lang, book_id, chunks))
    for group, items in by_group.items():
        idx = get_index(group, collection)
        algorithm = pystem_algorithm(group)
        writer, lock = _writer((str(collection.fts_dir if collection is not None
                                     else collections_mod.default_collection().fts_dir),
                                group), idx)
        with lock:
            for lang, book_id, chunks in items:
                _add_one(writer, algorithm, lang, book_id, chunks)
            writer.commit()
        # NOT wait_merging_threads(): the merges run on the writer's own
        # threads and the writer is kept, so nothing here waits for them —
        # which is the whole point, because this runs inside the manifest's
        # write transaction.


def index_book(lang: str, book_id: int, chunks: list[dict], collection=None):
    """One document, one commit — for callers that genuinely have one."""
    index_books([(lang, book_id, chunks)], collection)

def delete_book(lang: str, book_id: int, collection=None):
    group = group_for(lang)
    idx = get_index(group, collection)
    base = (collection.fts_dir if collection is not None
            else collections_mod.default_collection().fts_dir)
    writer, lock = _writer((str(base), group), idx)
    with lock:
        writer.delete_documents("book_id", book_id)
        writer.commit()

def rewrite_without(group: str, collection=None) -> int:
    """Rewrite this group's index from its own live documents, so that what
    was deleted from it is gone from the DISK and not only from search.

    tantivy deletes by marking: a segment keeps a deleted document's bytes
    until a merge rewrites it, and it drops a segment only once every
    document in it is deleted (measured, tantivy 0.26: a document sharing a
    segment with a live one stayed readable in the segment's store after the
    delete, the commit and `garbage_collect_files`). An erasable collection
    must leave no trace (02 §2.9), so after an erase this reads every live
    document's stored fields, deletes everything, adds them back and
    commits: the old segments lose their last live document and go.

    O(the group's documents), which is why only an erasable collection's
    erase calls it — a deposit collection is small (an agent's memory, the
    files one person uploaded). Returns the documents kept."""
    idx = get_index(group, collection)
    base = (collection.fts_dir if collection is not None
            else collections_mod.default_collection().fts_dir)
    writer, lock = _writer((str(base), group), idx)
    algorithm = pystem_algorithm(group)
    with lock:
        idx.reload()
        searcher = idx.searcher()
        n = searcher.num_docs
        hits = (searcher.search(tantivy.Query.all_query(), limit=n).hits
                if n else [])
        docs = [searcher.doc(addr).to_dict() for _score, addr in hits]
        writer.delete_all_documents()
        for d in docs:
            body = d["body"][0]
            fields = dict(book_id=int(d["book_id"][0]), chunk_id=d["chunk_id"][0],
                          chapter=(d.get("chapter") or [""])[0],
                          lang=(d.get("lang") or ["und"])[0], body=body)
            if algorithm:
                fields["body_stem"] = stemming.stem_text(algorithm, body)
            writer.add_document(tantivy.Document(**fields))
        writer.commit()
        writer.garbage_collect_files()
    return len(docs)


def book_chunks(group: str, book_id: int, collection=None) -> list[dict]:
    """Every stored chunk of one document — the reader's text source
    (STATUS 5j): a term query on the `book_id` fast field, no text query,
    no corpus file. Order is the caller's (chunk ids carry the seq)."""
    idx = get_index(group, collection)
    idx.reload()
    q = tantivy.Query.term_query(idx.schema, "book_id", int(book_id))
    searcher = idx.searcher()
    out = []
    for _score, addr in searcher.search(q, 100_000).hits:
        doc = searcher.doc(addr)
        out.append({
            "book_id": doc["book_id"][0],
            "chunk_id": doc["chunk_id"][0],
            "chapter": doc["chapter"][0] if doc["chapter"] else "",
            "lang": doc["lang"][0],
            "text": doc["body"][0],
        })
    return out


_QUERY_STRIP = re.compile(r'[\^\[\]{}~/\\:!()"*]')

def search(group: str, query: str, limit: int = 20, collection=None,
           book_ids: list[int] | None = None, langs: list[str] | None = None) -> list[dict]:
    """book_ids, when given, is a hard prefilter (Tantivy term-set query
    ANDed with the text query) — not used by retrieval.search()'s default
    entity-boost wiring (a soft score bonus instead), but a real, directly
    testable primitive for a future "search only within this entity" mode.
    `langs` is the same kind of prefilter on the stored `lang`, for the
    mixed groups (cjk, other) when a query is restricted to languages."""
    try:
        idx = get_index(group, collection)
        idx.reload()
        clean = _QUERY_STRIP.sub(" ", query).strip()
        if not clean:
            return []
        algorithm = pystem_algorithm(group)
        if algorithm:
            # the query must pass through the SAME stemmer the documents
            # did, or a stemmed index answers nothing at all
            stemmed = stemming.stem_text(algorithm, clean)
            if not stemmed:
                return []
            q = idx.parse_query_lenient(stemmed, ["body_stem"])[0]
            # chapter titles are stored unstemmed, so they keep the raw
            # query — dropping them would quietly lose a matching signal
            chapter_q = idx.parse_query_lenient(clean, ["chapter"])[0]
            q = tantivy.Query.boolean_query(
                [(tantivy.Occur.Should, q), (tantivy.Occur.Should, chapter_q)])
        else:
            q = idx.parse_query_lenient(clean, ["body", "chapter"])[0]
        if book_ids:
            id_filter = tantivy.Query.term_set_query(idx.schema, "book_id", book_ids)
            q = tantivy.Query.boolean_query([(tantivy.Occur.Must, q), (tantivy.Occur.Must, id_filter)])
        if langs:
            lang_filter = tantivy.Query.term_set_query(idx.schema, "lang", list(langs))
            q = tantivy.Query.boolean_query([(tantivy.Occur.Must, q), (tantivy.Occur.Must, lang_filter)])
        searcher = idx.searcher()
        hits = searcher.search(q, limit).hits
        out = []
        for score, addr in hits:
            doc = searcher.doc(addr)
            out.append({
                "score": float(score),
                "book_id": doc["book_id"][0],
                "chunk_id": doc["chunk_id"][0],
                "chapter": doc["chapter"][0] if doc["chapter"] else "",
                "lang": doc["lang"][0],
                "text": doc["body"][0],
                "source": f"fts:{group}",
            })
        # Tantivy doesn't guarantee stable tie-breaking for exactly-equal
        # BM25 scores across separately-built indexes (observed directly:
        # identical scores, swapped order between runs) — re-sort with a
        # deterministic secondary key so results are reproducible.
        out.sort(key=lambda h: (-h["score"], h["chunk_id"]))
        return out
    except Exception:
        return []

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
    # stemmer panics (docs/upstream-rust-stemmers-greek.md).
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
        return idx

def existing_groups(collection=None) -> list[str]:
    if collection is None:
        collection = collections_mod.default_collection()
    if not collection.fts_dir.is_dir():
        return []
    return sorted(p.name for p in collection.fts_dir.iterdir()
                  if p.is_dir() and (p / "meta.json").exists())

def _add_one(writer, algorithm, lang: str, book_id: int, chunks: list[dict]):
    writer.delete_documents("book_id", book_id)
    for c in chunks:
        fields = dict(book_id=book_id, chunk_id=c["id"],
                      chapter=c["chapter"] or "", lang=lang or "und",
                      body=c["text"])
        if algorithm:
            fields["body_stem"] = stemming.stem_text(algorithm, c["text"])
        writer.add_document(tantivy.Document(**fields))


def index_books(entries, collection=None) -> None:
    """Index several documents through ONE writer per language group.

    `entries` is an iterable of (lang, book_id, chunks).

    The per-document writer this replaces cost a fresh 64 MB allocation, a
    `commit()` and a `wait_merging_threads()` for EVERY document — a fixed
    cost that does not shrink with the document. Measured live: ~30s for a
    2 kb genome scaffold, the same as for a 23 MB chromosome arm, because
    the work is in the commit and not in the content.

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
        writer = idx.writer(heap_size=64_000_000)
        for lang, book_id, chunks in items:
            _add_one(writer, algorithm, lang, book_id, chunks)
        writer.commit()
        writer.wait_merging_threads()


def index_book(lang: str, book_id: int, chunks: list[dict], collection=None):
    """One document, one commit — for callers that genuinely have one."""
    index_books([(lang, book_id, chunks)], collection)

def delete_book(lang: str, book_id: int, collection=None):
    idx = get_index(group_for(lang), collection)
    writer = idx.writer()
    writer.delete_documents("book_id", book_id)
    writer.commit()
    writer.wait_merging_threads()

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
           book_ids: list[int] | None = None) -> list[dict]:
    """book_ids, when given, is a hard prefilter (Tantivy term-set query
    ANDed with the text query) — not used by retrieval.search()'s default
    entity-boost wiring (a soft score bonus instead), but a real, directly
    testable primitive for a future "search only within this entity" mode."""
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

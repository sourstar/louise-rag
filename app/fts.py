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

_lock = threading.Lock()
_indexes: dict[tuple[str, str], tantivy.Index] = {}

def group_for(lang: str) -> str:
    lang = (lang or "und").split("-")[0]
    if lang in config.STEMMER_LANGS:
        return lang
    if lang in config.CJK_LANGS:
        return "cjk"
    return "other"

def _schema() -> tantivy.Schema:
    b = tantivy.SchemaBuilder()
    b.add_integer_field("book_id", stored=True, indexed=True, fast=True)
    b.add_text_field("chunk_id", stored=True, tokenizer_name="raw")
    b.add_text_field("chapter", stored=True, tokenizer_name="default")
    b.add_text_field("lang", stored=True, tokenizer_name="raw")
    b.add_text_field("body", stored=True, tokenizer_name="lang_tok")
    return b.build()

def _analyzer_for(group: str) -> tantivy.TextAnalyzer:
    if group == "cjk":
        base = tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.ngram(2, 2, False))
    else:
        base = tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
    base = base.filter(tantivy.Filter.remove_long(64)).filter(tantivy.Filter.lowercase())
    if group in config.STEMMER_LANGS:
        base = base.filter(tantivy.Filter.stemmer(config.STEMMER_LANGS[group]))
    return base.build()

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
        idx = tantivy.Index(_schema(), path=str(path))
        idx.register_tokenizer("lang_tok", _analyzer_for(group))
        _indexes[key] = idx
        return idx

def existing_groups(collection=None) -> list[str]:
    if collection is None:
        collection = collections_mod.default_collection()
    if not collection.fts_dir.is_dir():
        return []
    return sorted(p.name for p in collection.fts_dir.iterdir()
                  if p.is_dir() and (p / "meta.json").exists())

def index_book(lang: str, book_id: int, chunks: list[dict], collection=None):
    idx = get_index(group_for(lang), collection)
    writer = idx.writer(heap_size=64_000_000)
    writer.delete_documents("book_id", book_id)
    for c in chunks:
        writer.add_document(tantivy.Document(
            book_id=book_id, chunk_id=c["id"], chapter=c["chapter"] or "",
            lang=lang or "und", body=c["text"]))
    writer.commit()
    writer.wait_merging_threads()

def delete_book(lang: str, book_id: int, collection=None):
    idx = get_index(group_for(lang), collection)
    writer = idx.writer()
    writer.delete_documents("book_id", book_id)
    writer.commit()
    writer.wait_merging_threads()

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

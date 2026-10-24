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

def _schema(group: str, validity: bool = False, names: bool = False) -> tantivy.Schema:
    """Per GROUP, deliberately: adding a field changes the schema, and
    tantivy refuses to open an index whose on-disk schema differs. Scoping
    body_stem to the groups that use it means adopting PyStemmer for one
    language rebuilds ONE index instead of every index on the node.
    `validity`: a versioned collection's three columns (app/validity.py),
    fast fields a search ranges over; only such a collection's indexes
    carry them, so every other index keeps its schema. `names`: the exact
    names a record is found by and its popularity (`index_names_of`), only
    in a collection whose plugin gives them (wikidata), for the same reason."""
    b = tantivy.SchemaBuilder()
    b.add_integer_field("book_id", stored=True, indexed=True, fast=True)
    if validity:
        for col in ("valid_from", "valid_to", "shown"):
            b.add_integer_field(col, stored=True, indexed=True, fast=True)
    b.add_text_field("chunk_id", stored=True, tokenizer_name="raw")
    b.add_text_field("chapter", stored=True, tokenizer_name="default")
    b.add_text_field("lang", stored=True, tokenizer_name="raw")
    b.add_text_field("body", stored=True, tokenizer_name="lang_tok")
    if HEADING_IN_BODY and not pystem_algorithm(group):
        # the heading folded into the body, indexed only: `body` stays the
        # stored text the reader and the reranker see
        b.add_text_field("body_idx", stored=False, tokenizer_name="lang_tok")
    if pystem_algorithm(group):
        # stems get their own field so `body` keeps the ORIGINAL text for
        # snippets; not stored, because nobody reads stems
        b.add_text_field("body_stem", stored=False, tokenizer_name="stem_tok")
    if names:
        # whole names as keys (`name_key`), matched exactly; doc ids only
        b.add_text_field(NAME_KEYS, stored=False, tokenizer_name="raw", index_option="basic")
        b.add_integer_field(POPULARITY, stored=False, indexed=False, fast=True)
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
        from app import validity as validity_mod
        with_validity = validity_mod.declared(collection) is not None
        try:
            idx = tantivy.Index(_schema(group, with_validity, names_capable(collection)), path=str(path))
        except Exception as e:
            raise RuntimeError(_open_failure(path, e)) from None
        idx.register_tokenizer("lang_tok", _analyzer_for(group))
        if pystem_algorithm(group):
            idx.register_tokenizer("stem_tok", _stem_analyzer())
        _indexes[key] = idx
        _writers.pop(key, None)     # a new Index object: a writer on the old one is stale
        return idx

def _open_failure(path, e: Exception) -> str:
    """Why a text index could not be opened, as what to do about it: search()
    swallows exceptions, so without it the only symptom is a language that
    silently returns no hits. Every failure used to be called a schema
    mismatch — "delete the index and re-index" — and a lock that could not
    be taken read as one too (München, 2026-10-21: a read-only mount; that
    advice would have deleted a healthy index)."""
    said = f"{type(e).__name__}: {e}"
    if "Lockfile" in str(e) or "Read-only file system" in str(e):
        return (f"tantivy index at {path} could not be opened: its lock file cannot be "
                f"taken ({said}). tantivy writes it even to read, so the index "
                f"directory must be writable; the index itself is not at fault")
    if "schema does not match" in str(e):
        return (f"tantivy index at {path} was built with a different schema ({said}). "
                f"This happens when a language joins or leaves config.PYSTEM_LANGS. "
                f"Delete {path} and re-index that language's documents "
                f"(UPDATE documents SET fts_done=0 WHERE lang=...)")
    return f"tantivy index at {path} could not be opened ({said})"


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


#: (meta.json path, its mtime_ns) -> the documents that index holds
_group_docs: dict[tuple[str, int], int] = {}


def group_documents(collection=None) -> dict[str, int]:
    """DOCUMENTS indexed per language group — what a query's main languages
    are counted in (05 §5.2, amended 2026-10-10; `language_plan.main_of`).
    The distinct `book_id`s of each index, by tantivy's cardinality over the
    fast field: an estimate (HyperLogLog, exact for small indexes, 1.6 % off
    on 2M chunks of 50k books) in 46 ms for 2M chunks, measured. Cached on
    the index's own meta.json, path and mtime, as `group_languages` is. A
    group with no index on disk, or one that cannot be read, holds 0 —
    never created by asking."""
    if collection is None:
        collection = collections_mod.default_collection()
    out: dict[str, int] = {}
    for group in existing_groups(collection):
        meta = collection.fts_dir / group / "meta.json"
        try:
            key = (str(meta), meta.stat().st_mtime_ns)
        except OSError:
            out[group] = 0
            continue
        hit = _group_docs.get(key)
        if hit is None:
            try:
                idx = get_index(group, collection)
                idx.reload()
                hit = int(idx.searcher().cardinality(tantivy.Query.all_query(), "book_id"))
            except Exception:   # noqa: BLE001 — an unreadable group counts as empty
                hit = 0
            if len(_group_docs) > 1024:
                _group_docs.clear()
            _group_docs[key] = hit
        out[group] = hit
    return out


def _add_one(writer, algorithm, lang: str, book_id: int, chunks: list[dict],
             extra: dict | None = None, names: tuple | None = None):
    writer.delete_documents("book_id", book_id)
    for i, c in enumerate(chunks):
        fields = dict(book_id=book_id, chunk_id=c["id"],
                      chapter=c["chapter"] or "", lang=lang or "und",
                      body=c["text"])
        if extra:
            fields.update(extra)          # a versioned collection's columns
        if names is not None and i == 0:
            # the record's names on its FIRST chunk alone: one hit per record
            fields[NAME_KEYS], fields[POPULARITY] = name_keys(names[0]), int(names[1])
        fields.update(_indexed(algorithm, c["chapter"] or "", c["text"]))
        writer.add_document(tantivy.Document(**fields))


def _indexed(algorithm, heading: str, text: str) -> dict:
    """The indexed-only fields of a chunk: its stems (a PyStemmer group),
    and with HEADING_IN_BODY its heading folded into the one field the query
    reads, k times ahead of the body."""
    folded = (f"{heading} " * HEADING_IN_BODY + text) if HEADING_IN_BODY and heading else text
    if algorithm:
        return {"body_stem": stemming.stem_text(algorithm, folded)}
    return {"body_idx": folded} if HEADING_IN_BODY else {}


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


def forget(fts_dir) -> int:
    """Drop this process's handles on the indexes under `fts_dir` — for a
    collection that LEFT the node. The caches are keyed on the path, and a
    collection re-created at the same path would otherwise be written through
    the old Index and writer, whose files are gone: normattiva, 2026-10-13,
    "An index writer was killed … io::Error" on 244 documents. A thread still
    holding one keeps its own reference; nothing is closed here. How many
    handles went."""
    base = str(fts_dir)
    with _lock:
        keys = [k for k in _indexes if k[0] == base] + [k for k in _writers if k[0] == base]
        for k in keys:
            _indexes.pop(k, None)
            _writers.pop(k, None)
    return len(set(keys))


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

    `entries` is an iterable of (lang, book_id, chunks[, columns[, names]]):
    `names` is `index_names_of`'s (pairs, popularity) for a collection whose
    plugin gives them, else None.

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
    for entry in entries:
        lang, book_id, chunks = entry[0], entry[1], entry[2]
        extra = entry[3] if len(entry) > 3 else None     # app/validity.py's columns
        names = entry[4] if len(entry) > 4 else None
        parts = by_section_language(lang, chunks, section_languages(collection))
        for k, (part_lang, part) in enumerate(parts):
            # the names ride on the record's first part (its first chunk)
            by_group.setdefault(group_for(part_lang), []).append(
                (part_lang, book_id, part, extra, names if k == 0 else None))
    for group, items in by_group.items():
        idx = get_index(group, collection)
        algorithm = pystem_algorithm(group)
        writer, lock = _writer((str(collection.fts_dir if collection is not None
                                     else collections_mod.default_collection().fts_dir),
                                group), idx)
        with lock:
            for lang, book_id, chunks, extra, names in items:
                _add_one(writer, algorithm, lang, book_id, chunks, extra, names)
            writer.commit()
        # NOT wait_merging_threads(): the merges run on the writer's own
        # threads and the writer is kept, so nothing here waits for them —
        # which is the whole point, because this runs inside the manifest's
        # write transaction.


#: a record IN SEVERAL LANGUAGES (ISO 639-2 `mul`): wikidata's entity, one
#: section per language, its heading the language's code (09 §9.5;
#: wikidata-build-plan.md "What is a record")
MULTILINGUAL = "mul"


# ---- names (operator, 2026-10-24: wikidata, "names only") -----------------
#
# A collection whose plugin gives `index_names(extra)` (wikidata) is also
# searched BY NAME: every label and alias of a record, in every language,
# indexed as a whole-name key on its first chunk, with its popularity.
# A question finds a record when one of the question's word sequences IS one
# of its names — Wikidata's own "near match" (WikibaseCirrusSearch), ranked
# the way it ranks: the longer name first ("Monaco di Baviera" before
# "Monaco"), then the more linked entity (sitelinks) — so a famous entity is
# not outranked by an obscure one for having more names, as BM25 over a bag
# of every name would rank it (a field's length divides its score). The
# names enter no record, no section and no vector: index only, rebuilt from
# the records like the rest of the text index.
#
# THE STANDARD ALTERNATIVE in full: Elasticsearch with WikibaseCirrusSearch's
# mapping (per-language label fields, `labels_all`, near-match and a
# popularity rescore) — a search service beside tantivy; it pays if names
# need fuzzy or prefix matching, which exact keys do not give.

NAME_KEYS, POPULARITY = "name_keys", "popularity"
#: the longest word sequence of a question tried as a name
NAME_MAX_WORDS = 8
#: a one-word sequence shorter than this (in ASCII) is no name worth a hit
NAME_MIN_CHARS = 3


def names_capable(collection) -> bool:
    plugin = collections_mod.PLUGIN_REGISTRY.get(getattr(collection, "plugin", None))
    return callable(getattr(plugin, "index_names", None))


def index_names_of(collection, extra: dict | None) -> tuple | None:
    """`(pairs, popularity)` from a record's `extra`, or None."""
    plugin = collections_mod.PLUGIN_REGISTRY.get(getattr(collection, "plugin", None))
    fn = getattr(plugin, "index_names", None)
    return fn(extra or {}) if callable(fn) else None


def name_key(text: str) -> str:
    """A name as it is matched: NFKC, case-folded, without diacritics, every
    run of what is not a letter or digit one space. The question's word
    sequences are made the same way, so "Pechino", "PECHINO" and "pechino"
    are one key, and "Beyoncé" is "beyonce"."""
    import unicodedata
    t = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text or "").casefold())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return " ".join("".join(ch if ch.isalnum() else " " for ch in t).split())


def name_keys(pairs) -> list[str]:
    """Every name twice: `|<key>` (any language) and `<lang>|<key>` (the
    language's base code: `en-gb` → `en`), for a search limited to
    languages. Sorted, so a record is indexed the same way every time."""
    out: set[str] = set()
    for lang, name in pairs:
        k = name_key(name)
        if k:
            out.add("|" + k)
            out.add(f"{str(lang).split('-')[0].lower()}|{k}")
    return sorted(out)


def _name_grams(query: str) -> dict[int, list[str]]:
    """The question's word sequences by length, longest first."""
    words = name_key(query).split()
    out: dict[int, list[str]] = {}
    for n in range(min(NAME_MAX_WORDS, len(words)), 0, -1):
        grams = sorted({" ".join(words[i:i + n]) for i in range(len(words) - n + 1)})
        if n == 1:
            grams = [g for g in grams if not (g.isascii() and len(g) < NAME_MIN_CHARS)]
        if grams:
            out[n] = grams
    return out


def search_names(collection, query: str, limit: int, langs: list[str] | None = None,
                 book_ids: list[int] | None = None, validity: dict | None = None) -> list[dict]:
    """The records one of whose names IS a word sequence of `query` — in any
    language, or in one of `langs` when the search is limited to languages —
    longest name first, then the most linked, as `search` returns hits (the
    record's first chunk). `[]` for a collection without names."""
    if not names_capable(collection) or limit <= 0:
        return []
    grams = _name_grams(query)
    if not grams:
        return []
    prefixes = [f"{l.split('-')[0].lower()}|" for l in langs] if langs else ["|"]
    out: list[dict] = []
    seen: set[int] = set()
    try:
        for n, gs in grams.items():
            keys = [p + g for p in prefixes for g in gs]
            band: list[dict] = []
            for group in existing_groups(collection):
                idx = get_index(group, collection)
                idx.reload()
                q = tantivy.Query.term_set_query(idx.schema, NAME_KEYS, keys)
                if book_ids:
                    q = tantivy.Query.boolean_query([
                        (tantivy.Occur.Must, q),
                        (tantivy.Occur.Must, tantivy.Query.term_set_query(idx.schema, "book_id", book_ids))])
                if validity:
                    from app import validity as validity_mod
                    for f in validity_mod.fts_filters(idx.schema, validity):
                        q = tantivy.Query.boolean_query([(tantivy.Occur.Must, q), (tantivy.Occur.Must, f)])
                searcher = idx.searcher()
                for pop, addr in searcher.search(q, limit, order_by_field=POPULARITY).hits:
                    doc = searcher.doc(addr)
                    band.append({"book_id": doc["book_id"][0], "chunk_id": doc["chunk_id"][0],
                                 "chapter": doc["chapter"][0] if doc["chapter"] else "",
                                 "lang": doc["lang"][0], "text": doc["body"][0],
                                 "popularity": int(pop), "source": f"names:{group}"})
            # the band in one order on every peer: the most linked, then the record
            band.sort(key=lambda h: (-h["popularity"], tie_key(h["chunk_id"])))
            for h in band:
                if h["book_id"] in seen:
                    continue
                seen.add(h["book_id"])
                h["score"] = float(n) + min(h["popularity"], 999_999) / 1_000_000
                out.append(h)
                if len(out) >= limit:
                    return out
    except Exception:
        return out
    return out


def section_languages(collection) -> tuple[str, ...]:
    """The languages a collection's `mul` records are written in, one section
    each, headed by the code: its plugin's declared `languages` (wikidata:
    `("en",)`; the Codex's list, fixed for the collection's life, 02 §2.7).
    `()` for a plugin that declares none — every other one today. The ONE
    source for the text leg here and for the legs that filter by the record's
    language (`retrieval.limited_langs`)."""
    plugin = collections_mod.PLUGIN_REGISTRY.get(getattr(collection, "plugin", None))
    return tuple(getattr(plugin, "languages", ()) or ())


def by_section_language(lang: str, chunks: list[dict],
                        languages: tuple[str, ...] = ()) -> list[tuple[str, list[dict]]]:
    """A document's chunks as (language, chunks) by the index group they go
    to. A `mul` record's sections are each written in ONE language, named by
    its heading: each is indexed in that language's group, stemmed as that
    language, with that language stored — so "in English" finds a wikidata
    entity's English text, and the English stemmer reads it (2026-10-24: all
    75.9 M of the node's sat in `other`, unstemmed, and every language-limited
    search skipped them).

    ONLY A HEADING THE PLUGIN DECLARES (`languages`, `section_languages`)
    names a language. The first form took any two or three letters for a
    code, and a `mul` record headed otherwise would have been filed under a
    language that is not one: pubmed's structured abstracts carry "AIM",
    gutenberg's chapters "II" and "IV" (caught 2026-10-24, before any node
    ran it). Any other heading keeps the record's `mul`, in `other`, as before.
    Every other record: one part, its own language, as before. The list is
    fixed for the collection's life, so a re-index never leaves a part in a
    group it has left. A heading `<language>:<part>` — wikidata's history
    section, `en:history` (parser 2) — is that language's text too."""
    if (lang or "") != MULTILINGUAL or not languages:
        return [(lang, chunks)]
    parts: dict[str, list[dict]] = {}
    for c in chunks:
        head = (c.get("chapter") or "").strip().split(":", 1)[0]
        parts.setdefault(head if head in languages else MULTILINGUAL, []).append(c)
    return list(parts.items()) or [(lang, chunks)]


def index_book(lang: str, book_id: int, chunks: list[dict], collection=None):
    """One document, one commit — for callers that genuinely have one."""
    index_books([(lang, book_id, chunks)], collection)

def delete_book(lang: str, book_id: int, collection=None):
    """A `mul` record's sections may be in several groups
    (`by_section_language`): it is deleted from each group its sections can
    be in, and from `other`."""
    base = (collection.fts_dir if collection is not None
            else collections_mod.default_collection().fts_dir)
    groups = [group_for(lang)]
    if (lang or "") == MULTILINGUAL:
        groups = list(dict.fromkeys([group_for(l) for l in section_languages(collection)] + groups))
    for group in groups:
        idx = get_index(group, collection)
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
    if names_capable(collection):
        # the names are indexed, not stored: a rewrite from the stored fields
        # would drop every one of them, silently. No such collection erases
        # (wikidata keeps its records); one that did rebuilds from its records
        raise RuntimeError(f"{group}: a text index with names (fts.search_names) is rebuilt from "
                           f"its records, never rewritten from its stored fields")
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
            for col in ("valid_from", "valid_to", "shown"):     # a versioned collection's columns
                if d.get(col):
                    fields[col] = int(d[col][0])
            fields.update(_indexed(algorithm, fields["chapter"], body))
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

#: BM25F (Robertson, Zaragoza & Taylor, "Simple BM25 extension to multiple
#: weighted fields", CIKM 2004), the heading's field weight. BM25F weights
#: each field's term frequency and length, sums them, and saturates ONCE with
#: one document frequency; with integer weights that is exactly BM25 over the
#: heading written k times plus the body once — which is what this indexes
#: (tests/test_fts_chapter_boost.py checks tantivy's score against the paper's
#: formula). The weight is tuned on held-out labelled queries, as the BM25F
#: literature does (Pérez-Iglesias et al., arXiv 0911.5046): the campaign.
#: 0 is the pre-BM25F schema: `Should(body) + Should(chapter)`, two
#: separately saturated scores — the sum the paper shows breaks BM25, and why
#: "white whale obsession captain" put Moby-Dick's first chunk #291 below
#: every chunk headed "The White Whale Shoal." (München, 2026-10-23). No knob
#: re-weights that sum (operator, 2026-10-23: do not reinvent the wheel).
#: Not a runtime knob: it decides what an index holds, so a value other than
#: the one an index was built with refuses to open it; a change rebuilds the
#: text indexes under a new FTS_VERSION. THE STANDARD ALTERNATIVE in full:
#: non-integer weights and a per-field b (Zaragoza et al., TREC 2004) need
#: per-field term frequencies at scoring time — Lucene's CombinedFieldQuery,
#: Elasticsearch's `combined_fields`; tantivy 0.26 has neither, and adopting
#: one means a scorer upstream or another engine.
HEADING_IN_BODY = 0

#: a tie group at the cut is read whole up to this many hits beyond `limit`
#: (times `limit`): bounded, so a query whose thousands of chunks score the
#: same costs a bounded read
TIE_READ_FACTOR = 8


def tie_key(chunk_id: str) -> tuple:
    """Equal scores order by the record's uid bytes, then the chunk's place
    in its record — the earliest first — then the id itself. A chunk id is
    `<uid>:<seq>:<version>` (app/chunking.py); the string alone put
    `5097:133:` before `5097:1:` and `g:10:` before `g:1:` (München,
    2026-10-20)."""
    text = str(chunk_id)
    parts = text.rsplit(":", 2)
    try:
        return (parts[0].encode("utf-8"), int(parts[1]), text)
    except (IndexError, ValueError):
        return (text.encode("utf-8"), 0, text)

def search(group: str, query: str, limit: int = 20, collection=None,
           book_ids: list[int] | None = None, langs: list[str] | None = None,
           validity: dict | None = None) -> list[dict]:
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
            if not HEADING_IN_BODY:
                # chapter titles are stored unstemmed, so they keep the raw
                # query — dropping them would quietly lose a matching signal
                chapter_q = idx.parse_query_lenient(clean, ["chapter"])[0]
                q = tantivy.Query.boolean_query(
                    [(tantivy.Occur.Should, q), (tantivy.Occur.Should, chapter_q)])
        elif HEADING_IN_BODY:
            # BM25F: the weighted heading is in body_idx already
            q = idx.parse_query_lenient(clean, ["body_idx"])[0]
        else:
            q = idx.parse_query_lenient(clean, ["body", "chapter"])[0]
        if book_ids:
            id_filter = tantivy.Query.term_set_query(idx.schema, "book_id", book_ids)
            q = tantivy.Query.boolean_query([(tantivy.Occur.Must, q), (tantivy.Occur.Must, id_filter)])
        if langs:
            lang_filter = tantivy.Query.term_set_query(idx.schema, "lang", list(langs))
            q = tantivy.Query.boolean_query([(tantivy.Occur.Must, q), (tantivy.Occur.Must, lang_filter)])
        if validity:
            # one version per Work, by key (app/validity.py): a range on the
            # id's validity prefix and on the derived end, both fast fields
            from app import validity as validity_mod
            for f in validity_mod.fts_filters(idx.schema, validity):
                q = tantivy.Query.boolean_query([(tantivy.Occur.Must, q), (tantivy.Occur.Must, f)])
        searcher = idx.searcher()
        # 05 §5.1 (amended 2026-10-20): a leg's list is the same list on
        # every peer with the same members. Tantivy returns the top `limit`
        # in its own order among EQUAL scores, which depends on how the
        # index was built: a tie group straddling the cut was cut
        # arbitrarily (München). One hit past the cut says whether the group
        # straddles it; if so it is read whole (bounded), then sorted, then
        # cut.
        n = limit + 1
        cap = max(limit + 1, limit * TIE_READ_FACTOR)
        while True:
            hits = searcher.search(q, n).hits
            if len(hits) < n or n >= cap or hits[-1][0] < hits[limit - 1][0]:
                break
            n = min(n * 2, cap)
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
        # deterministic secondary key so results are reproducible, then cut.
        out.sort(key=lambda h: (-h["score"], tie_key(h["chunk_id"])))
        return out[:limit]
    except Exception:
        return []


def search_records(group: str, query: str, depth: int, collection=None,
                   langs: list[str] | None = None, **restrict) -> list[dict]:
    """`search`, deep enough to hold the list's top `depth` RECORDS (05 §5.1,
    amended 2026-10-10; app/leg_depth.py): it starts at `depth` chunks and
    doubles while a book's many chunks crowd the list, bounded."""
    from . import leg_depth
    return leg_depth.to_records(
        lambda n: search(group, query, limit=n, collection=collection, langs=langs, **restrict),
        depth)

"""Bundle test helpers — building a collection with a real Lance table without
ever calling a live embed endpoint.

A separate module rather than conftest so tests can import it absolutely,
matching tests/acquire/mock_http_server.py's precedent (there are no
__init__.py files under tests/).
"""
import numpy as np

from app import collections as collections_mod, embeddings

DIM = 8


def make_collection(tmp_path, name="gutenberg", plugin="gutenberg"):
    root = tmp_path / name
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    return collections_mod.Collection(
        name=name, plugin=plugin, profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=root / "manifest.db",
        fts_dir=root / "fts", lance_dir=root / "lancedb")


def unit_vectors(n, dim=DIM, seed=0, adjacency=0.75):
    """Deterministic vectors that look like a real embedding space rather than
    noise, because the admission checks are built to reject noise:

      * L2-normalized, matching embeddings._embed_batch.
      * ANISOTROPIC — a shared positive component, so pairwise cosine has a
        clearly non-zero mean (architecture-v2.md §6: "embedding spaces are
        strongly anisotropic — foreign vectors betray themselves statistically").
      * ADJACENT-CORRELATED — each vector is a blend of its predecessor and
        fresh noise, standing in for the ~120 tokens consecutive chunks
        genuinely share. Without this the honest fixture would trip the
        overlap-invariant check.
    """
    rng = np.random.default_rng(seed)
    out = []
    previous = None
    for _ in range(n):
        noise = rng.normal(size=dim).astype(np.float32) + 0.6
        v = noise if previous is None else adjacency * previous + (1 - adjacency) * noise
        v = v.astype(np.float32)
        v = v / np.linalg.norm(v)
        out.append(v)
        previous = v
    return out


def isotropic_vectors(n, dim=DIM, seed=0):
    """Uniform random points on the unit sphere — structurally perfect and
    semantically meaningless. What a lazy forger produces."""
    rng = np.random.default_rng(seed)
    v = rng.normal(size=(n, dim)).astype(np.float32)
    return [row / np.linalg.norm(row) for row in v]


def chunkable_text(n_chunks: int, salt: str = "") -> str:
    """Text that app/chunking.py genuinely splits into ~n_chunks chunks.

    Each paragraph is sized just under the chunk window (CHUNK_TOKENS * 4
    chars), so adding a second would overflow it and each becomes its own
    chunk.

    `salt` makes every DOCUMENT's text distinct, not just every paragraph.
    Without it two documents produce identical chunk text, and any stub
    embedder keyed on text silently returns the other document's vectors —
    which looks exactly like a wrong-model bundle.
    """
    from app import config
    width = int(config.CHUNK_TOKENS * 4 * 0.9)
    paragraphs = []
    for i in range(max(1, n_chunks)):
        filler = f"{salt}para{i} lighthouse keeper tended the lamp nightly. "
        paragraphs.append((filler * (width // len(filler) + 1))[:width])
    return "\n\n".join(paragraphs)


def local_chunks(collection, uid, native_id):
    """The chunks this node derives from its own copy of the raw document —
    the same call the importer makes."""
    from app import chunking
    from pathlib import Path
    plugin = collection.plugin_instance()
    chapters = plugin.read_chapters(Path(collection.corpus_dir / str(native_id)),
                                     int(native_id))
    return list(chunking.chunk_book(uid, int(native_id), chapters))


def seed_document(con, collection, uid, native_id, *, text=None,
                  n_chunks=3, seed=0, lang="en", dtype=np.float32,
                  vectors=None):
    """Insert a document into the manifest, write its raw file, and add
    matching vectors to Lance — the state export() expects to find.

    The Lance rows are built from the document's REAL locally-derived chunks,
    not invented ids: the importer requires the bundle's chunk-id set to equal
    the set this node derives, so a fixture that invents ids would be refused
    (correctly) before reaching whatever the test is actually about.

    Note the layout: for Gutenberg a DocRef's `locator` (and therefore
    documents.path) is the book DIRECTORY, not the text file — GutenbergSource
    .fetch() calls pg_text.find_txt(Path(locator), native_id) to locate it.
    """
    book_dir = collection.corpus_dir / str(native_id)
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / f"pg{native_id}.txt").write_text(
        text if text is not None else chunkable_text(n_chunks, salt=f"doc{native_id} "),
        encoding="utf-8")
    con.execute(
        "INSERT OR REPLACE INTO documents(uid,source,native_id,path,lang,title,"
        "rdf_done,fts_done,embed_done,updated_at) VALUES(?,?,?,?,?,?,1,1,1,0)",
        (uid, collection.plugin, str(native_id), str(book_dir), lang, f"Doc {native_id}"))

    chunks = local_chunks(collection, uid, native_id)
    vecs = vectors if vectors is not None else unit_vectors(len(chunks), seed=seed)
    rows = [{
        "id": c["id"], "book_id": int(native_id), "lang": lang,
        "seq": c["seq"], "chapter": c["chapter"] or "", "text": c["text"],
        "vector": np.asarray(vecs[i], dtype=dtype),
    } for i, c in enumerate(chunks)]

    conn = embeddings._conn(collection)
    if embeddings.TABLE in conn.table_names():
        table = conn.open_table(embeddings.TABLE)
        table.delete(f"book_id = {int(native_id)}")
        table.add(rows)
    else:
        import pyarrow as pa
        value_type = pa.float16() if dtype is np.float16 else pa.float32()
        schema = pa.schema([
            pa.field("id", pa.string()), pa.field("book_id", pa.int64()),
            pa.field("lang", pa.string()), pa.field("seq", pa.int64()),
            pa.field("chapter", pa.string()), pa.field("text", pa.string()),
            pa.field("vector", pa.list_(value_type, DIM)),
        ])
        conn.create_table(embeddings.TABLE, data=rows, schema=schema)
    return rows

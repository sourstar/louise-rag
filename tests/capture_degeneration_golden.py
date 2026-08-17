"""One-time capture of golden retrieval.search() output from *today's
unmodified* code (before the P0.3 entity/reference layer lands) — the
degeneration test's regression baseline (docs/architecture-v2.md §4's
"no entities found -> plan degenerates to today's pipeline exactly").

Run this ONCE, before any P0.3 code lands, then commit the resulting
degeneration-golden.json. Do not re-run it after P0.3 lands — it exists to
freeze pre-entity-layer behavior, not track the current tree.

retrieval.search() does not yet accept a `collection` param at capture time
(that's one of P0.3's own additive changes) — collections.default_collection
is monkeypatched here instead, so this script exercises truly unmodified
retrieval.py.

Usage: python -m tests.capture_degeneration_golden
"""
import json
import shutil
import tempfile
from pathlib import Path

from app import chunking, collections as collections_mod, fts
from app import db, discovery, retrieval, worker
from app.plugins.base import DiscoverState

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_PATH = FIXTURES / "degeneration-golden.json"

# ~20 queries built from literal vocabulary already in the gutenberg-mini /
# pubmed-mini fixtures. None contain "BRCA2"/"BRCA1"/"TP53"/"KIT" (verified
# by grep against tests/fixtures before this list was written) — the whole
# point is that these must resolve zero entities, pre- and post-P0.3 alike.
QUERIES = [
    "committee meeting",
    "bright and steady morning",
    "a plain walk through the village",
    "old stone bridge",
    "heron in the shallows",
    "horloger et les montres",
    "chapitre deux",
    "fixtures and calibration",
    "widget calibration throughput",
    "gadget fatigue study",
    "record with no abstract",
    "widget durability wear",
    "gadget assembly reliability",
    "widget supply chain disruption",
    "gadget noise levels",
    "widget cost modeling maintenance",
    "gadget lifecycle testing",
    "reproducibility of results",
    "the meeting adjourned before dusk",
    "a peddler with buttons and thread",
]


def _index_gutenberg(collection) -> None:
    con = db.connect(collection)
    discovery.sync_collection(con, collection)
    for row in con.execute("SELECT * FROM documents ORDER BY uid").fetchall():
        worker.stage_rdf(con, row, collection)
    for row in con.execute("SELECT * FROM documents ORDER BY uid").fetchall():
        worker.stage_fts(con, row, collection)
    con.close()


def _index_pubmed(collection) -> None:
    """PubMed was never wired into worker.py's stage functions in P0.2 (no
    read_metadata/read_chapters helpers) — index directly via the plugin's
    own discover/fetch/parse + chunk_abstract, deduping by native_id the same
    way discovery.sync_collection does (last ref wins; tombstoned PMIDs are
    simply never indexed)."""
    con = db.connect(collection)
    discovery.sync_collection(con, collection)   # populates `documents`, including tombstones
    plugin = collection.plugin_instance()
    state = DiscoverState(corpus_root=collection.corpus_dir)
    by_native = {}
    for ref in plugin.discover(state):
        if ref.extra.get("kind") == "article":
            by_native[ref.native_id] = ref
    for native_id, ref in by_native.items():
        doc = plugin.parse(plugin.fetch(ref))
        chunks = list(chunking.chunk_abstract(doc.uid, int(native_id), doc.sections))
        fts.index_book("en", int(native_id), chunks, collection)
        con.execute(
            "UPDATE documents SET title=?,rdf_done=1,fts_done=1,chunks=?,tokens=? WHERE uid=?",
            (doc.title, len(chunks), sum(c["tokens"] for c in chunks), doc.uid))
    con.close()


def _capture(collection) -> dict:
    original = collections_mod.default_collection
    collections_mod.default_collection = lambda: collection
    try:
        con = db.connect(collection)
        out = {}
        for q in QUERIES:
            result = retrieval.search(con, q, k=12, mode="hybrid", translate=False)
            out[q] = [{"book_id": h["book_id"], "chunk_id": h["chunk_id"]} for h in result["hits"]]
        con.close()
    finally:
        collections_mod.default_collection = original
    return out


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        gutenberg_corpus = tmp / "gutenberg_corpus"
        shutil.copytree(FIXTURES / "gutenberg-mini" / "corpus", gutenberg_corpus)
        gutenberg_collection = collections_mod.Collection(
            name="gutenberg", plugin="gutenberg", profile=None, embed_mode_default=None,
            corpus_dir=gutenberg_corpus, manifest_path=tmp / "gutenberg" / "manifest.db",
            fts_dir=tmp / "gutenberg" / "fts", lance_dir=tmp / "gutenberg" / "lancedb")

        pubmed_collection = collections_mod.Collection(
            name="pubmed", plugin="pubmed", profile=None, embed_mode_default=None,
            corpus_dir=FIXTURES / "pubmed-mini", manifest_path=tmp / "pubmed" / "manifest.db",
            fts_dir=tmp / "pubmed" / "fts", lance_dir=tmp / "pubmed" / "lancedb")

        _index_gutenberg(gutenberg_collection)
        _index_pubmed(pubmed_collection)

        golden = {
            "gutenberg": _capture(gutenberg_collection),
            "pubmed": _capture(pubmed_collection),
        }
        GOLDEN_PATH.write_text(json.dumps(golden, indent=2, ensure_ascii=False, sort_keys=True))
        print(f"wrote {GOLDEN_PATH}")


if __name__ == "__main__":
    main()

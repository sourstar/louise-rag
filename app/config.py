"""Central configuration. Everything is overridable via environment variables."""
import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
CORPUS_DIR = Path(os.getenv("CORPUS_DIR", "/corpus"))  # gutenberg-epub root (one dir per book id)
# The well-known root where per-collection corpus datasets are mounted
# (deployment maps e.g. /mnt/tank/data/pubmed -> /corpora/pubmed). The
# collection-create UI offers exactly the folders present here — operators
# choose from real mounts instead of typing arbitrary container paths.
CORPUS_MOUNTS_DIR = Path(os.getenv("CORPUS_MOUNTS_DIR", "/corpora"))

DB_PATH = DATA_DIR / "manifest.db"
FTS_DIR = DATA_DIR / "fts"
LANCE_DIR = DATA_DIR / "lancedb"

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/")
# Optional separate Ollama instance for embeddings (parallel slots, small ctx).
EMBED_URL = (os.getenv("EMBED_URL") or OLLAMA_URL).rstrip("/")
EMBED_MODEL = os.getenv("EMBED_MODEL", "qwen3-embedding-8k:latest")
CHAT_MODEL = os.getenv("CHAT_MODEL", "qwen3.6-35b-a3b:latest")
RERANK_URL = os.getenv("RERANK_URL", "")  # optional TEI-style /rerank endpoint; empty = disabled

CHUNK_TOKENS = int(os.getenv("CHUNK_TOKENS", "1000"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "120"))
# Default 1 on purpose: llama-server corrupts long sequences when one
# /v1/embeddings request carries multiple texts (verified live 2026-08-17;
# deterministic per batch shape, and its prompt cache can replay the corrupted
# result for later single-text requests). Raise only for backends verified to
# batch correctly. Parallel single-text requests are safe — use
# EMBED_CONCURRENCY for throughput instead.
EMBED_BATCH = int(os.getenv("EMBED_BATCH", "1"))
EMBED_CONCURRENCY = int(os.getenv("EMBED_CONCURRENCY", "1"))  # parallel embed requests in flight
# Behavioral backend gate (app/verify/backend_gate.py): before writing any
# vectors the worker verifies the endpoint still reproduces its recorded
# probe baseline — the check the weak (name-derived) model hash cannot do.
# "off" disables it; there is deliberately no per-collection toggle.
BACKEND_GATE = os.getenv("BACKEND_GATE", "on")
EMBED_DTYPE = os.getenv("EMBED_DTYPE", "f32")  # f32 | f16 (halves vector storage)
EMBED_API = os.getenv("EMBED_API", "ollama")  # ollama (/api/embed) | openai (/v1/embeddings, e.g. llama-server)
EMBED_CTX = int(os.getenv("EMBED_CTX", "8192"))

# Bump these to force selective reprocessing of the corresponding stage.
CHUNK_VERSION = int(os.getenv("CHUNK_VERSION", "1"))
FTS_VERSION = int(os.getenv("FTS_VERSION", "1"))
EMBED_VERSION = int(os.getenv("EMBED_VERSION", "1"))
# Bump when text-cleaning logic (pg_text.read_clean and friends) changes in a
# way that alters chunk content — part of space_id (see app/spaces.py).
CLEANING_VERSION = int(os.getenv("CLEANING_VERSION", "1"))
# Bump to force re-tagging of doc_keys (app/entities.py) independent of FTS_VERSION.
ENTITY_VERSION = int(os.getenv("ENTITY_VERSION", "1"))
# Bump to force re-acquisition of already-fetched files (app/acquire/).
FETCH_VERSION = int(os.getenv("FETCH_VERSION", "1"))

# Federation (P0.5): per-collection candidate budget applied ahead of the
# cross-collection RRF fuse + optional rerank, only when >1 collection
# participates (architecture-v2.md §13: "per-collection candidate budgets
# and rerank-on-demand" are the named lever against rerank cost at
# federation scale). A single participating collection is unbounded by this
# and uses the existing max(k*4,40) rerank-input cutoff, unchanged — the
# default mirrors that same floor so it reads as "the same per-query
# candidate volume, now applied per collection."
FEDERATION_CANDIDATE_BUDGET = int(os.getenv("FEDERATION_CANDIDATE_BUDGET", "40"))

# Global reference-lookup store (HGNC, ClinVar-mini, ...) — one file shared
# across all collections, since these are standalone lookup layers, not
# corpus-specific (architecture-v2.md §4, §9). REFS_SRC_DIR is unset by
# default: sync is a no-op until an operator points it at TSV fixtures/dumps,
# so no new required volume mount.
REFS_DB_PATH = Path(os.getenv("REFS_DB_PATH", str(DATA_DIR / "refs.db")))
_refs_src_dir = os.getenv("REFS_SRC_DIR")
REFS_SRC_DIR = Path(_refs_src_dir) if _refs_src_dir else None

# Node signing key for vector bundles (app/bundles/). PATH ONLY — CLAUDE.md
# invariant 8 and architecture-v2.md §14 both say the node key's *location* is
# Tier-1 config and the key material itself never appears in an env value, a
# config value, a log, or an error message. There is deliberately no NODE_KEY
# env var and no node_key TOML field; if you find yourself adding one, don't.
# Unset = this node cannot export bundles (import needs only public keys).
_node_key_path = os.getenv("NODE_KEY_PATH")
NODE_KEY_PATH = Path(_node_key_path) if _node_key_path else None

# Single admin token gating librarian (/admin) routes (architecture-v2.md
# §14: "authenticated /admin API, strictly separate from the OpenAI-
# compatible query endpoint"). Tier 1 identity. Unset = admin routes refuse
# everything — fail closed, never a default-open backdoor.
#
# Two ways to supply it, and ADMIN_TOKEN_FILE (a PATH) wins: invariant 8
# wants secrets referenced by filesystem path, never carried as a value that
# `docker inspect`, a compose dump or a transcript can expose — the token
# leaked that way once on the reference deployment. ADMIN_TOKEN (the value
# itself) stays supported for existing deployments. The material is read
# once here and never logged; a set-but-unreadable file is a startup error
# naming the path, not a silently disabled admin.
def _resolve_admin_token(env) -> str:
    path = env.get("ADMIN_TOKEN_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise RuntimeError(
                f"ADMIN_TOKEN_FILE is set but unreadable: {path} "
                f"({type(e).__name__})") from None
    return env.get("ADMIN_TOKEN", "")


ADMIN_TOKEN = _resolve_admin_token(os.environ)

PORT = int(os.getenv("PORT", "8742"))
WORKER_IDLE_SLEEP = float(os.getenv("WORKER_IDLE_SLEEP", "5"))
# Per-collection time budget per worker round (seconds). Cheap stages (a
# pubmed rdf parse is ~0.1s) process many documents per round; an expensive
# stage (a 60s embed) still runs exactly once — fairness bounded by time,
# not by document count.
WORKER_ROUND_BUDGET_S = float(os.getenv("WORKER_ROUND_BUDGET_S", "10"))
# How long one parallel-embed call keeps REFILLING its worker window before
# draining and returning to the round loop. Long enough that the window is
# full almost all the time (a document is minutes), short enough that stop
# / state changes are honored within a reasonable delay.
EMBED_STREAM_S = float(os.getenv("EMBED_STREAM_S", "120"))
# Rescan the corpus for new/changed books: always at worker startup, then every
# N minutes while running (0 = only at startup / manual rescan).
DISCOVER_INTERVAL_MIN = int(os.getenv("DISCOVER_INTERVAL_MIN", "360"))
# Per-collection pipeline threads: each collection's rdf/fts stages run on
# their own thread (one per collection — never two writers on one tantivy
# index), while the main loop keeps embed (GPU-serial across collections by
# doctrine), genesis, bundles and control. 0 = legacy single-thread rounds.
WORKER_PIPELINE_THREADS = int(os.getenv("WORKER_PIPELINE_THREADS", "1"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# How many documents one fts round commits through a single tantivy writer.
# A writer costs a 64 MB allocation, a commit and a merge wait REGARDLESS of
# how much was written, so paying that once per document is the dominant cost
# on a corpus of small documents. Two caps, because collections differ by
# orders of magnitude: pubmed abstracts are 1-2 chunks and hit the document
# cap, while one genome contig is ~6,700 chunks and hits the chunk cap after
# three. The chunk cap is the memory guard — a batch holds its chunks in
# memory until the commit.
FTS_BATCH_DOCS = int(os.getenv("FTS_BATCH_DOCS", "200"))
FTS_BATCH_CHUNKS = int(os.getenv("FTS_BATCH_CHUNKS", "20000"))

# How many documents one rdf round handles inside a single transaction.
# Profiled on pubmed (33M rows): 95% of a warm rdf step was SQLite — the
# UPDATE on the wide documents table plus the catalog_fts FTS5 delete/insert,
# committed per document. Parsing was 0.8 ms. One transaction per batch
# writes each dirty page to the WAL once per batch instead of once per
# document, and FTS5 buffers its index writes within a transaction.
RDF_BATCH_DOCS = int(os.getenv("RDF_BATCH_DOCS", "200"))

# Local (corpus-resident) genesis archives read a whole parquet shard —
# ~437 MB, ~10 s — to import the documents in it that became embed-eligible
# since the last read. While rdf/fts still have work on the same thread, a
# shard is read only once it holds at least GENESIS_MIN_CANDIDATES such
# documents, or once its oldest one has waited GENESIS_MAX_WAIT_SEC — so the
# read is amortised over a batch instead of paid per fts commit, and no
# document waits longer than the cap for its free vectors. Once rdf/fts are
# exhausted every candidate is taken immediately.
GENESIS_MIN_CANDIDATES = int(os.getenv("GENESIS_MIN_CANDIDATES", "1000"))
GENESIS_MAX_WAIT_SEC = int(os.getenv("GENESIS_MAX_WAIT_SEC", "600"))
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "300"))

# ISO-639 code -> tantivy snowball stemmer language. Everything else uses the
# generic lowercase analyzer (still perfectly searchable, just unstemmed).
STEMMER_LANGS = {
    "en": "english", "fr": "french", "de": "german", "fi": "finnish",
    "nl": "dutch", "it": "italian", "es": "spanish", "pt": "portuguese",
    "sv": "swedish", "da": "danish", "hu": "hungarian", "el": "greek",
    "ru": "russian", "no": "norwegian", "ro": "romanian", "tr": "turkish",
    "ar": "arabic", "ta": "tamil",
}
# Languages stemmed by PyStemmer in our own code (app/stemming.py) instead
# of by tantivy's built-in filter.
#
# Greek must be here: tantivy compiles rust-stemmers 1.2.0 — the last
# release of a crate dormant since November 2019 — whose Greek algorithm
# PANICS on ordinary words. Verified with tantivy's API and none of our
# code in the path:
#     "παγαινετε"  -> PanicException: end byte index 18 is out of bounds
#                     of `παγαινετ`      (snowball_env.rs:249)
#     "πηγαινετε"  -> ok, stems to `πηγαινετ`
# The suffix removal shrinks the buffer to 16 bytes and a byte index
# computed against the original 18 is then reused. PyStemmer (libstemmer,
# released 2024-10-10) stems the same word without complaint and agrees
# with rust-stemmers everywhere rust-stemmers works.
#
# Adding a language here changes that group's index SCHEMA (it gains a
# body_stem field), so its index must be deleted and its documents
# re-indexed. That is why this is a short list rather than "all of them":
# migrating pubmed's 20M documents would cost weeks of CPU to fix a bug
# they do not have. The mechanism is general — the choice of who uses it
# is a cost decision, one language at a time.
PYSTEM_LANGS = {"el"}
# Languages whose scripts have no spaces: index as character bigrams.
CJK_LANGS = {"zh", "ja", "ko"}

def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FTS_DIR.mkdir(parents=True, exist_ok=True)
    LANCE_DIR.mkdir(parents=True, exist_ok=True)

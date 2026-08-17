"""Central configuration. Everything is overridable via environment variables."""
import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
CORPUS_DIR = Path(os.getenv("CORPUS_DIR", "/corpus"))  # gutenberg-epub root (one dir per book id)

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
EMBED_BATCH = int(os.getenv("EMBED_BATCH", "8"))
EMBED_CONCURRENCY = int(os.getenv("EMBED_CONCURRENCY", "1"))  # parallel embed requests in flight
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
# compatible query endpoint"). Tier 1 identity, env only (CLAUDE.md
# invariant 8: secrets by filesystem/env, never DB/logs). Unset = admin
# routes refuse everything — fail closed, never a default-open backdoor.
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

PORT = int(os.getenv("PORT", "8742"))
WORKER_IDLE_SLEEP = float(os.getenv("WORKER_IDLE_SLEEP", "5"))
# Rescan the corpus for new/changed books: always at worker startup, then every
# N minutes while running (0 = only at startup / manual rescan).
DISCOVER_INTERVAL_MIN = int(os.getenv("DISCOVER_INTERVAL_MIN", "360"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
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
# Languages whose scripts have no spaces: index as character bigrams.
CJK_LANGS = {"zh", "ja", "ko"}

def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FTS_DIR.mkdir(parents=True, exist_ok=True)
    LANCE_DIR.mkdir(parents=True, exist_ok=True)

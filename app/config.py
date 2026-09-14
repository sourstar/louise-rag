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
# Derived per-container packs for PubMed (plugins/pubmed/pack.py): a cache
# rebuilt from the gz XML, never exchanged. Default: a hidden `.pack/`
# directory beside the containers (same volume as the archive); set this to
# keep the working set on faster storage (STATUS 5i).
PUBMED_PACK_DIR = Path(os.getenv("PUBMED_PACK_DIR")) if os.getenv("PUBMED_PACK_DIR") else None
# Acquisition STAGING (STATUS 5i): streaming transports write here (fast
# storage) and a verified file is moved into its corpus in one sequential
# copy — an HDD pool then never sees the transport's chunk-by-chunk fsync'd
# writes. None = write in place. A file the area cannot hold lands in place.
ACQUIRE_STAGING_DIR = Path(os.getenv("ACQUIRE_STAGING_DIR")) if os.getenv("ACQUIRE_STAGING_DIR") else None

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
# How many chunks of ONE document are embedded before its vectors are
# written. A document is the unit of work but NOT a bounded amount of it:
# gutenberg 4656 chunks into 5,335 pieces, which is 5.3 h at this endpoint's
# measured 3.6 s/chunk. Writing only at the end meant every restart in that
# window discarded the lot (observed live). Smaller = less work at risk per
# restart and a tighter SIGTERM; larger = fewer Lance fragments to compact.
# Documents below this size are written in one go exactly as before.
EMBED_SLICE_CHUNKS = int(os.getenv("EMBED_SLICE_CHUNKS", "64"))
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
# Fair share across collections (app/sched.py, docs/notes/scheduling.md §10): the
# encoder rotation is deficit round-robin over the collections that actually
# have work, instead of a fixed budget per collection in list order. 0 =
# the previous behaviour, kept as an escape hatch while this is young.
SCHED_FAIR_SHARE = int(os.getenv("SCHED_FAIR_SHARE", "1"))
# The base quantum — the fair share itself. A flow's slot is never smaller
# than its own minimum efficient unit, which is MEASURED, not set here.
SCHED_QUANTUM_S = float(os.getenv("SCHED_QUANTUM_S", str(WORKER_ROUND_BUDGET_S)))
# ...and never larger than this, however expensive that collection's units
# turn out to be. An oversized unit is a unit to fix (it shows up as an
# overrun), not a reason to hand one collection the resource for minutes.
SCHED_FLOOR_CAP_S = float(os.getenv("SCHED_FLOOR_CAP_S", "60"))
# How long a batch should LAST (app/sizing.py). The counts below are the
# starting point; once a collection's real cost per document is measured,
# the batch is sized to hit this instead — the same setting then buys the
# same thing on a 400-word abstract and a 300-page book. A unit's duration
# is also the granularity at which a resource can be handed back, so this
# is what bounds B2/B3 in docs/notes/scheduling.md §10.
BATCH_TARGET_S = float(os.getenv("BATCH_TARGET_S", "3"))
# The re-index probe is a WINDOW over rowids, not a batch: its cost depends
# on how many rows the window happens to hold. Measured live at 498 ms
# (pubmed) for a fixed 200k window; the width is now adjusted to hit this.
REINDEX_PROBE_TARGET_S = float(os.getenv("REINDEX_PROBE_TARGET_S", "0.1"))
# The phase controller's low watermark (worker._archive_yields): when fewer
# than this many documents are waiting for a vector AND fts has work ready,
# fts takes the collection's thread ahead of an archive pass. Feeding the
# encoder is never wasted — genesis imports vectors only for fts_done
# documents, so fts is the archive's upstream too. 0 disables the rule.
PHASE_EMBED_LOW = int(os.getenv("PHASE_EMBED_LOW", "500"))
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
# Amendment 38 — remote archives are fetched like a corpus. Shards on disk
# that are not yet FINAL (fetched, or applied but still revisited as their
# documents clear fts) are bounded by this many bytes; one MedCPT shard is
# ~3.0 GB, so the default keeps a handful — the active shard and the next.
GENESIS_CACHE_BYTES = int(os.getenv("GENESIS_CACHE_BYTES", str(24 * 1024 ** 3)))
# a revisit pass that imports nothing doubles that shard's wait, up to this
GENESIS_PASS_BACKOFF_MAX_SEC = int(os.getenv("GENESIS_PASS_BACKOFF_MAX_SEC", "21600"))
# a remote shard's apply pass runs on the collection's own thread, beside its
# rdf/fts: it is cut after this many seconds (committing what it has) and
# resumes after an equal share for indexing — 1M documents per shard would
# otherwise hold indexing for hours (observed: ~150 docs/s on pubmed)
GENESIS_PASS_BUDGET_S = int(os.getenv("GENESIS_PASS_BUDGET_S", "300"))
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "300"))

# The PARSE POOL (app/parallel.py): fetch/parse/chunk per document is pure
# Python and every collection's pipeline thread shares ONE interpreter
# lock — measured on the reference node as one thread at 87 % and eleven
# idle cores. Batches are cut into sub-batches for this many low-priority
# child processes; 1 = in-process, serial (tests, small nodes).
PARSE_PROCESSES = int(os.getenv("PARSE_PROCESSES", str(max(1, (os.cpu_count() or 2) - 1))))
# a child is replaced after this many sub-batches: its plugin caches (row
# groups, streams) grow with what it has parsed — measured +4 GB across
# eleven children in fifteen minutes — and a fresh child starts small
PARSE_TASKS_PER_CHILD = int(os.getenv("PARSE_TASKS_PER_CHILD", "500"))
# How long a batch may wait for the pool before the caller computes it
# in-process instead. The pool is an OPTIMISATION: a pipeline thread that
# blocks on it forever stops a collection dead and cannot honour SIGTERM —
# observed live 2026-08-28, pubmed's thread parked in the executor with no
# children left alive. Generous (a child may be building a container pack),
# but finite.
PARSE_TIMEOUT_S = float(os.getenv("PARSE_TIMEOUT_S", "120"))
# Consecutive pool failures after which this process stops using children
# altogether and computes in-process until the next restart.
PARSE_MAX_FAILURES = int(os.getenv("PARSE_MAX_FAILURES", "3"))

# The local VECTOR INDEX (app/vector_index.py; pack-format.md §5, appendix
# A). Without one every semantic query is a flat scan of the Lance table —
# measured 13 s on 15.8M rows. An IVF index is built once a collection's
# table holds at least this many rows, and refreshed (new rows folded in)
# by the worker off the GPU loop when enough unindexed rows accumulate.
VECTOR_INDEX_MIN_ROWS = int(os.getenv("VECTOR_INDEX_MIN_ROWS", "100000"))
# ivf_flat (a second copy of the vectors, best recall) | ivf_pq (compact
# codes + re-ranking; the right choice on the biggest tables)
VECTOR_INDEX_TYPE = os.getenv("VECTOR_INDEX_TYPE", "ivf_pq")
VECTOR_INDEX_NPROBES = int(os.getenv("VECTOR_INDEX_NPROBES", "30"))
VECTOR_INDEX_REFINE = int(os.getenv("VECTOR_INDEX_REFINE", "30"))
VECTOR_INDEX_REFRESH_ROWS = int(os.getenv("VECTOR_INDEX_REFRESH_ROWS", "50000"))
# ...and at least this fraction of the table: a refresh compacts and
# re-indexes, which cost 41 minutes on the 16.8M-row wikivec table (HDD)
# for 115k new rows — it must not run every 50k rows there
VECTOR_INDEX_REFRESH_FRACTION = float(os.getenv("VECTOR_INDEX_REFRESH_FRACTION", "0.02"))
VECTOR_INDEX_REFRESH_S = int(os.getenv("VECTOR_INDEX_REFRESH_S", "1800"))
# How long Lance keeps SUPERSEDED versions of a vector table. This is not a
# tuning knob, it is a disk-usage decision, and the default was costing more
# than everything else on the pool: `optimize()` with no argument prunes
# versions older than SEVEN DAYS, and every archive flush (delete+add on 500
# book ids) makes a version. Measured 2026-08-28 on this node: wikivec held
# 522,650 versions = 177 GB of _versions beside 291 GB of live data, with
# 116 GB free on the pool.
#
# A derived vector store is rebuildable and nothing time-travels it, so the
# window only has to outlive a reader that is mid-query. Hours, not days.
VECTOR_INDEX_KEEP_VERSIONS_H = float(os.getenv("VECTOR_INDEX_KEEP_VERSIONS_H", "2"))
# Below this much free space, a vector-index refresh is SKIPPED rather than
# attempted. `optimize()` compacts before it prunes — it writes the new
# fragments first and deletes the old ones after — so on a nearly-full pool
# it makes things worse before (if ever) making them better. Measured on the
# reference node at 92% full: a wikivec compaction took free space from
# 116 GB down to 93 GB without releasing anything. Maintenance must never be
# the thing that fills the disk.
#: A small safety margin for the operation itself — NOT a judgement about
#: pool health. It used to be 150 GiB and it was the WHOLE test, which
#: deadlocked: below the floor nothing ran, so nothing was freed, so free
#: space never climbed back over it. A large absolute floor is that same
#: deadlock for every table at once, and a large floor applied to a SMALL
#: table is it in miniature — compacting a 100 MiB table needs about
#: 25 MiB, and refusing to do it because the pool is under 150 GiB free
#: forbids exactly the cheap reclamation that would help. What scales is
#: below.
VECTOR_INDEX_MIN_FREE_GB = float(os.getenv("VECTOR_INDEX_MIN_FREE_GB", "2"))

#: Free space a compaction of a table needs, as a fraction of that table.
#: Measured once: compacting a 291 GiB table took free space down by 23 GiB
#: before it released anything, about 8 %. 0.25 is that with margin.
VECTOR_INDEX_COMPACT_HEADROOM = float(os.getenv("VECTOR_INDEX_COMPACT_HEADROOM", "0.25"))

#: How often a collection drops its superseded Lance versions. Pruning is
#: cheap and frees space without needing headroom (vector_index.prune), so
#: this is a plain interval and NOT gated on free space or on the index
#: having work to do — the pair of those two conditions is what let one
#: table reach 13,657 versions and 267 GiB over 40 GiB of live data.
VECTOR_INDEX_PRUNE_S = int(os.getenv("VECTOR_INDEX_PRUNE_S", "3600"))
# The well-known root where PACKS are mounted (pack-format.md). The create
# dialog offered them to the `packed` plugin, boxed in attic/ (2026-09-14).
PACK_MOUNTS_DIR = Path(os.getenv("PACK_MOUNTS_DIR", "/packs"))

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


def louise_root() -> Path:
    """`<LOUISE_ROOT>` — the ONE directory of 07 §7.8, whose
    `collections/<name>/` holds each Codex collection's drag, state, index
    and vector table.

    Here rather than in app/collections.py because app/louise.py needs the
    same answer and importing that module would pull the whole plugin
    registry onto the path of every CLI invocation. Read at CALL time: a
    module-level constant freezes the environment at import, which is the
    defect this replaces — `app/louise.py` treated $LOUISE_ROOT as the
    collections directory ITSELF while app/collections.py treated it as the
    PARENT, so the CLI listed a different directory from the one the worker
    serves, in every configuration.
    """
    value = os.getenv("LOUISE_ROOT")
    return Path(value) if value else DATA_DIR

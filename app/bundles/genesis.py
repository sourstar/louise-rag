"""Genesis import — admit a PUBLISHED vector archive into a collection's
space. architecture-v2.md §8 + amendment 10: admission, not trust.

The point (and the pitch): if someone already paid the GPU-hours for a
corpus — NCBI's MedCPT embeddings of all PubMed, a community's Wikipedia
vectors — a node reuses that work instead of re-embedding, and any
"compatible embedded data" can join the platform the same way. Compatible
means: the collection's own [embed] recipe is the SAME function that
produced the archive, which is exactly what the sample re-encode proves.

Rules, all doc-mandated:
- possession-scoped: only vectors whose uids this node's manifest holds
  (RAG needs the text; §8's pointer-only rule covers the rest). The chunk
  count per document must match the archive's vectors per document exactly
  (medcpt: one per article) — partial coverage would fake embed_done.
- convert on import: f32 → the recipe dtype, L2-normalized client-side.
- admission: structural T0 (dim, NaN, dupes) + degenerate-vector check +
  sample re-encode through the collection's [embed] endpoint at
  cos >= RE_ENCODE_TAU. A shard that fails is refused WHOLE.
- provenance: one genesis_imports row per shard — cursor and audit record
  in one; re-running skips applied shards (invariant 2).

Archives are declared per plugin in ARCHIVES; a generic parquet adapter
(columns: id, vector) admits any third-party dataset that maps ids onto a
collection's uids.
"""
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from .. import (chunking, config, db, embeddings, licensing, parallel,
                plugin_io)
from ..collections import recipe_for
from . import ledger
from .checks import check_degenerate, check_structural
from .writer_lock import vector_write_lock

RE_ENCODE_TAU = 0.95      # same-function gate; calibrated tau supersedes later
# The proof is a QUORUM, not a worst-of: an archive is a snapshot of a
# living corpus, so a few sampled documents whose text changed since
# (a corrected abstract) reproduce badly under the SAME function. Live on
# pubmed: shard 0 proved and imported, shard 1 failed worst-of-8 at 0.874
# while every other sampled document sat at >= 0.993. The rule: at least
# RE_ENCODE_MIN_SAMPLE samples, a median >= tau AND RE_ENCODE_QUORUM of
# them >= tau proves the function; the samples below tau are STALE
# documents and are skipped (never imported), not evidence against the
# archive. A shard whose quorum fails is refused whole, as before.
RE_ENCODE_SAMPLE = 16
RE_ENCODE_MIN_SAMPLE = 1      # a shard with few held documents proves with what it has
RE_ENCODE_QUORUM = 0.75
# A verdict AGAINST an archive needs evidence: at least this many samples
# failing the quorum. Fewer are not a verdict. Live on pubmed (2026-08-26):
# shard 1 had proven and imported 950k vectors over thirty revisit passes,
# each skipping the same one stale document, until a pass whose ONLY
# eligible document was that stale one re-ran the proof on a sample of
# one — 0/1, REJECTED. The proof is therefore taken once per ARCHIVE (one
# producer, one function; persisted as `proven_at`) and later passes only
# screen for staleness; a small failing sample is stale documents under a
# proven archive and inconclusive (deferred) under an unproven one.
RE_ENCODE_REJECT_MIN = 8
APPLY_BATCH_DOCS = 500


class GenesisError(RuntimeError):
    """Terminal for this shard: recorded, shard refused whole."""


class GenesisUnavailable(RuntimeError):
    """The embed endpoint (needed for the same-function proof) or the
    archive source is unreachable — retry later, no verdict recorded."""


class GenesisInconclusive(GenesisUnavailable):
    """The same-function sample was too small to judge an UNPROVEN archive
    (below the quorum on fewer than RE_ENCODE_REJECT_MIN samples): nothing
    was written, no verdict recorded — retried when more documents are
    eligible. A GenesisUnavailable, so every retry path already applies."""


@dataclass(frozen=True)
class GenesisArchive:
    """Where a published embedding set lives and how to read it.

    `local=True` (amendment 11) marks a CORPUS-RESIDENT archive: the vectors
    live inside the collection's own corpus files (pre-embedded parquet),
    so shards are discovered on disk rather than declared, nothing is
    downloaded, corpus files are never deleted after import, and shards are
    REVISITED (oldest first) because documents become import-eligible
    gradually as rdf/fts progresses — instead of the once-only,
    wait-for-settle policy remote archives need."""
    archive_id: str
    base_url: str
    adapter: str                      # medcpt-npy | parquet | corpus-embedded
    shards: tuple[str, ...]           # shard names, adapter-interpreted; () + local → on-disk
    uid_prefix: str                   # archive id -> manifest uid
    local: bool = False
    # Amendment 38 — DECLARED COVERAGE for a remote archive whose shards
    # are contiguous id ranges: shard `str(i)` holds native ids
    # [i*ids_per_shard, (i+1)*ids_per_shard - 1]. Lets the worker know
    # which documents the archive CAN cover from the id alone (the local
    # encoder skips them under "all") and which shard a document waits
    # on, without downloading anything. None = coverage unknown.
    ids_per_shard: int | None = None
    shard_bytes: int = 0              # size hint per shard (cache bound, preflight)

    # ---- what the Codex credits (docs/06 §6.6, docs/09 §9.3) --------------
    # The registry is where an archive is declared, and `hold collection
    # create` copies the entry into the Codex's `credits` WHOLE — hashed
    # into `collection_id`, so neither the licence, the attribution nor the
    # coverage can be altered by a later build, and an importer neither
    # composes the acknowledgement nor is free to omit it.
    producer: str = ""                # who produced the vectors, as people say it
    license: str = ""                 # the licence the VECTORS carry, SPDX where there is one
    attribution: str = ""             # the credit line that licence requires
    # The keys this archive's bundles are signed with, if any. They live
    # HERE and not in the Codex (02 §2.7, 09 §9.3): a licence and an
    # attribution are permanent facts about whom a collection stands on
    # and are hashed into its identity, but a key is lost and reissued
    # (09 §9.2), and an archive that published nothing signed may begin.
    # Hashing one would fork the identity whenever it moved.
    #
    # 03 §3.5's refusal resolves an arriving `producer_pubkey` through this
    # set to an archive id, then refuses the bundle if the importing
    # collection's `credits` does not name that archive. An EMPTY set is
    # the ordinary case today — MedCPT is a directory of .npy files and
    # signs nothing — and it is not a defect: the refusal simply never
    # fires for such an archive, because no bundle can carry a key nobody
    # has, and what a peer converts from it that peer signs under its own
    # name (01, promise 3).
    keys: tuple[str, ...] = ()

    def credit(self) -> dict:
        """The archive's entry as 09 §9.3's credit object, ready to hash.

        The keys are NOT part of it; see the field above.
        """
        return {"kind": "archive", "archive": self.archive_id,
                "producer": self.producer,
                "license": self.license, "attribution": self.attribution,
                "coverage": self.coverage()}

    def coverage(self):
        """`"all"` when the archive covers the corpus, else the declared
        id ranges (09 §9.3)."""
        if self.ids_per_shard and self.shards:
            return {"ids_per_shard": self.ids_per_shard, "shards": len(self.shards)}
        return "all"

    def id_range_of(self, shard: str) -> tuple[int, int] | None:
        if not self.ids_per_shard or not shard.isdigit():
            return None
        i = int(shard)
        return i * self.ids_per_shard, (i + 1) * self.ids_per_shard - 1

    def shard_of_id(self, native_id: int) -> str | None:
        """The shard that would hold this id, or None when the archive's
        declared range ends before it (a post-snapshot document)."""
        if not self.ids_per_shard or native_id < 0:
            return None
        shard = str(native_id // self.ids_per_shard)
        return shard if shard in self.shards else None


# NCBI publishes MedCPT article embeddings for all of PubMed (public
# domain): per shard, embeds_chunk_N.npy (float32 [n, 768]) with
# pmids_chunk_N.json (n PMIDs, same order). The shards are EXACT PMID
# ranges — chunk i holds PMIDs i*10^6 .. i*10^6+999,999, strictly
# increasing (verified against the published id lists of chunks 0, 1 and
# 36 on 2026-08-24; ~977k-999k ids each: PMIDs without an abstract are
# simply absent). embeds_chunk_N.npy is ~3.0 GB.
MEDCPT_PUBMED = GenesisArchive(
    archive_id="medcpt-pubmed",
    base_url="https://ftp.ncbi.nlm.nih.gov/pub/lu/MedCPT/pubmed_embeddings",
    adapter="medcpt-npy",
    shards=tuple(str(i) for i in range(37)),
    uid_prefix="pubmed:",
    ids_per_shard=1_000_000,
    shard_bytes=3_070_000_000,
    producer="NCBI (National Library of Medicine)",
    license="public-domain",
    # The provider AND the corpus, by the same rule as Upstash below.
    attribution="MedCPT article embeddings over PubMed, NCBI/NLM (public domain)",
    # It signs nothing: a directory of .npy files on an FTP server.
    keys=(),
)

# The vectors travel WITH the corpus (format v1: data/{lang}/NNN.parquet
# rows of id/url/title/text/embedding): each corpus shard is a genesis
# shard, read in place.
UPSTASH_WIKIPEDIA = GenesisArchive(
    archive_id="upstash-wikipedia-2024-06",
    base_url="https://huggingface.co/datasets/Upstash/wikipedia-2024-06-bge-m3",
    adapter="corpus-embedded",
    # ITS DECLARED COVERAGE, by language block (docs/notes/wikipedia-two-
    # layers.md). The archive's articles are numbered ordinal × 10^10 + page
    # id (the frozen table, app/plugins/wikipedia/ids.py), so block i is
    # language i and the eleven are exactly its ids. For a LOCAL archive
    # `shards` names these blocks, not its files — those are found on disk
    # (`_local_shards`). The Codex's credit carries it and 06 §6.3's
    # `archive:` rule reads it: wikipedia's layer 1 is these ids and never
    # a dump-read page (numbered from 10^12). It was "all", which would have
    # put every document of the collection in layer 1.
    shards=tuple(str(i) for i in range(11)),
    ids_per_shard=10 ** 10,
    # Not what a local archive's uids carry: those are the COLLECTION's
    # (`import_shard`) — `embedded:` for the legacy wikivec, `wikipedia:`
    # for wikipedia's layer 1, over the same numbers.
    uid_prefix="embedded:",
    local=True,
    producer="Upstash",
    license="CC-BY-SA-4.0",           # the CONTENT is Wikipedia's; the vectors follow it
    # Both parties, in that order, because both are owed (operator, 2026-09-09).
    # CC BY-SA is Wikipedia's licence, not Upstash's: the vectors are a
    # derivative of the articles, so the clause that has to be satisfied names
    # the ARTICLES' authors, and Upstash is credited for the embedding work on
    # top of them. An attribution naming only the party we obtained the file
    # from would satisfy nobody's licence.
    attribution="Wikipedia contributors (CC BY-SA 4.0), embedded by Upstash "
                "as wikipedia-2024-06-bge-m3",
    keys=(),
)

ARCHIVES: dict[str, GenesisArchive] = {
    "medcpt-pubmed": MEDCPT_PUBMED,
    "upstash-wikipedia-2024-06": UPSTASH_WIKIPEDIA,
}

# The default-on wiring: creating a collection for one of these plugins
# declares its archive in the TOML unless the operator removes the line.
DEFAULT_ARCHIVE_BY_PLUGIN: dict[str, str] = {
    "embedded": "upstash-wikipedia-2024-06",
    "pubmed": "medcpt-pubmed",
}


# Residual candidates per (manifest path, shard): documents a read of that
# shard was asked to cover and could NOT — a shard-boundary straddler whose
# vectors are split across two files and so fail the chunk-shape gate, or a
# document whose chunk text would not load. They stay embed-eligible (the
# local encoder gap-fills them, amendment 11), so without this record the
# shard they point at would be re-read — ~437 MB, ~10 s — every round to
# import zero rows. A shard is re-read only for a candidate NOT in its
# residual set. Process-local, like the backend gate's verdict cache: a
# restart costs one re-read per residual shard, never a wrong import.
#
# Keyed on the manifest PATH, not the collection name: two Collection
# objects can share a name and point at different files (fts.get_index,
# the counts cache and the scan cursor each learned this separately).
_residual: dict[tuple[str, str], frozenset[str]] = {}
_residual_lock = threading.Lock()

# Amendment 38: bumped whenever a remote shard goes FINAL. The worker's
# coverage-aware backfill keys its rowid cursor on it — documents in a
# finalised range that the archive did not cover sit BELOW the cursor and
# become the local encoder's the moment the shard is final.
_coverage_epoch = 0


# A shard's id RANGE is answered by the browse index over
# CAST(native_id AS INTEGER) — and only by it. Left to choose, SQLite takes
# `idx_documents_scope (in_scope=?)` instead and filters the whole
# collection: measured live at 19.1 s to list one shard's eligible
# documents on pubmed (40M rows) and 9.5 s to count one shard's pending
# ones — ~28 s of every 300 s pass, on a plan that looks indexed. The hint
# is applied only when the index exists (the worker builds it at startup;
# `ensure_browse_index`), so a fresh manifest still works, just slowly.
_NATIVE_INT_INDEX = "idx_documents_native_int"


def _has_native_index(con) -> bool:
    try:
        return con.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                            (_NATIVE_INT_INDEX,)).fetchone() is not None
    except Exception:   # noqa: BLE001
        return False


def _hint(con) -> str:
    return f" INDEXED BY {_NATIVE_INT_INDEX}" if _has_native_index(con) else ""


def eligible_in_range_sql(con) -> str:
    """The documents of a shard's id range this node holds, indexed and
    still unembedded — what a revisit pass reads rows for."""
    return (f"SELECT uid FROM documents{_hint(con)} "
            f"WHERE CAST(native_id AS INTEGER) BETWEEN ? AND ? "
            f"AND in_scope=1 AND error IS NULL AND fts_done=1 AND embed_done=0")


def pending_in_range_sql(con) -> str:
    """How many documents of a shard's id range have not cleared fts —
    what decides whether the shard is FINAL."""
    return (f"SELECT COUNT(*) FROM documents{_hint(con)} "
            f"WHERE CAST(native_id AS INTEGER) BETWEEN ? AND ? "
            f"AND in_scope=1 AND error IS NULL AND fts_done=0")


def unserved_in_range_sql(con) -> str:
    """How many documents of a shard's id range are PAST fts and still
    without a vector — documents the archive could serve right now.

    The companion of `pending_in_range_sql`: that one counts demand that
    has not arrived yet, this one counts demand that arrived and was
    missed. `final` is decided by the first alone, which is the gap
    `reopen_finals_for_range` closes.
    """
    return (f"SELECT COUNT(*) FROM documents{_hint(con)} "
            f"WHERE CAST(native_id AS INTEGER) BETWEEN ? AND ? "
            f"AND in_scope=1 AND error IS NULL AND fts_done=1 AND embed_done=0")


def merge_spans(spans) -> list[tuple[int, int]]:
    """Sorted, with overlapping and touching id spans joined — MedCPT's 37
    contiguous one-million shards are one span."""
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    return merged


def declared_spans(archive: "GenesisArchive | None",
                   exclude=()) -> list[tuple[int, int]]:
    """The merged id spans the archive DECLARES (amendment 38), less the
    shards in `exclude`. [] for one that declares no coverage: whatever is
    held, nothing says where the archive ends. A LOCAL archive declares one
    too since wikipedia's two layers — its language blocks. One answer for
    the worker's covered ranges, the health report and the Library card, so
    the three cannot disagree about where the range is."""
    if archive is None or not archive.ids_per_shard:
        return []
    return merge_spans(rng for s in archive.shards if s not in exclude
                       for rng in (archive.id_range_of(s),) if rng)


def in_spans_sql(spans, *, named: bool = False) -> tuple[str, list[int] | dict[str, int]]:
    """`(CAST(native_id AS INTEGER) BETWEEN ? AND ? OR …)` over `spans`,
    with its parameters — positional, or `named` (`:span_lo0`, …) for a
    query that binds by name. The expression is the browse index's, so a
    query that can use the index may; one that scans pays per row."""
    if named:
        sql = " OR ".join(f"CAST(native_id AS INTEGER) BETWEEN :span_lo{i} AND :span_hi{i}"
                          for i in range(len(spans)))
        named_params = {}
        for i, (lo, hi) in enumerate(spans):
            named_params[f"span_lo{i}"], named_params[f"span_hi{i}"] = lo, hi
        return f"({sql})", named_params
    sql = " OR ".join("CAST(native_id AS INTEGER) BETWEEN ? AND ?" for _ in spans)
    return f"({sql})", [n for span in spans for n in span]


#: A native_id that is an integer written plainly. SQLite casts a
#: non-numeric string to 0 (`CAST('pubmed25n0001.xml.gz' AS INTEGER)` = 0,
#: `'12abc'` = 12), which puts it inside any range that starts at 0 —
#: MedCPT's does. Such an id belongs to no declared range
#: (`reopen_finals_for_range` skips it), so a COUNT that says "inside the
#: range" ANDs this in. The integer's own text must be the whole id: exact
#: for '', '12abc', '+5' and '007', and the cheapest form measured (4M-row
#: scan with the four range SUMs: 3.23 s, against 3.78 s for NOT GLOB
#: '*[^0-9]*' and 2.76 s with no check at all). For the counts only: the
#: expression index cannot answer it.
NUMERIC_ID_SQL = "(length(CAST(native_id AS INTEGER)) = length(native_id))"

#: The flush's credit check: which of a batch's chunks this shard already
#: delivered. One PK-index search per chunk id in ONE statement, on the
#: pages mark_imported's upsert then reads: +2.2 ms per 500-document flush
#: of re-imported documents, 35M synthetic provenance rows (a per-uid query
#: through the uid index cost +8.2 ms).
CREDITED_SQL = ("SELECT chunk_id FROM chunk_verification "
                "WHERE chunk_id IN ({marks}) AND bundle_id=?")


def coverage_epoch() -> int:
    return _coverage_epoch


def _bump_coverage_epoch() -> None:
    global _coverage_epoch
    _coverage_epoch += 1

# One page of the candidate walk; the walk is capped so one round never
# spends more than a bounded number of row lookups deciding there is
# nothing to read.
_CANDIDATE_PAGE = 2048
_CANDIDATE_SCAN_CAP = 65536

# Selection is the per-collection embed-candidate set, walked in index order
# — a prefix of idx_documents_scope (in_scope, rdf_done, fts_done,
# embed_done) with rowid as the tail key, so `rowid>? ORDER BY rowid` pages
# through the index range without a TEMP B-TREE. This is what replaced
# sweeping the shard LIST: the candidates' own locators name the shards
# worth reading, and a shard nobody is waiting for is never opened.
_CANDIDATES_PAGE_SQL = (
    "SELECT rowid, uid, path, updated_at FROM documents "
    "WHERE in_scope=1 AND rdf_done=1 AND fts_done=1 AND embed_done=0 "
    "AND error IS NULL AND rowid>? ORDER BY rowid LIMIT ?")


def _residual_of(collection, shard: str) -> frozenset[str]:
    with _residual_lock:
        return _residual.get((str(collection.manifest_path), shard), frozenset())


def _record_residual(collection, shard: str, uids) -> None:
    with _residual_lock:
        _residual[(str(collection.manifest_path), shard)] = frozenset(uids)


def reset_revisit_state() -> None:
    """Drop all residual bookkeeping (process-local, like the backend gate's
    verdict cache). Tests call it between cases; nothing in production
    needs it, since a restart starts empty anyway."""
    with _residual_lock:
        _residual.clear()


#: the residual key of what a local archive's candidate walk RELEASES
#: without a read — straddlers (known from their locators) and the
#: documents of a rejected shard: the local encoder's (amendment 11)
_RELEASED = "(released)"


def _add_residual(collection, shard: str, uids) -> None:
    key = (str(collection.manifest_path), shard)
    with _residual_lock:
        _residual[key] = _residual.get(key, frozenset()) | frozenset(uids)


def residual_uids(collection) -> list[str]:
    """Every document this process found a LOCAL archive cannot cover for
    this collection — straddlers, text that would not load, stale samples.
    They are the local encoder's (amendment 11): the archive's declared
    range keeps the encoder off everything else (worker._backfill_rows)."""
    key = str(collection.manifest_path)
    with _residual_lock:
        return sorted({u for (k, _shard), uids in _residual.items() if k == key
                       for u in uids})


@dataclass
class ShardDemand:
    """What one on-disk shard is being waited on for: the embed-eligible
    documents it holds that are not in its residual set."""
    shard: str
    uids: list[str]
    oldest: int            # smallest updated_at among them (fts commit time)
    locators: dict = field(default_factory=dict)   # uid -> locator: the rows to read


def _shard_of_locator(root: Path, locator: str, split_locator) -> str | None:
    """Corpus-relative POSIX path of the shard that holds a locator — the
    genesis_imports key — or None when no single shard read can cover it:

    - a STRADDLER (two segments: the article's rows span a shard boundary)
      — each shard is read independently and yields a partial vector list,
      so the chunk-shape gate refuses it from either side. Known from the
      locator alone, so it costs no read, ever: a residual found BY a read
      is process-local and would otherwise buy one re-read per straddler
      shard after every restart — ~one per shard, an hour of reads on a
      471-shard language once fts has passed them all. The walk RELEASES
      it instead (`_RELEASED`, rebuilt by the next walk at no cost), and
      the local encoder gap-fills it (amendment 11); a future adapter that
      reads both segments would lift this.
    - a DUMP page (wikipedia's layer 2): no archive shard at all.
    - a locator outside the corpus root (a moved corpus, before discovery
      repairs it) names no readable shard."""
    try:
        segments = split_locator(locator)
        if len(segments) != 1:
            return None
        path, _start, _end = segments[0]
        if not str(path).endswith(".parquet"):
            # the collection's corpus holds both sources, and a dump's
            # bz2 stream is no archive shard
            return None
        return Path(path).relative_to(root).as_posix()
    except (ValueError, IndexError):
        return None


def _straddles(locator: str, split_locator) -> bool:
    """An archive locator of two or more segments: the article's rows span
    a shard boundary, so no single read can cover it."""
    try:
        segments = split_locator(locator)
    except (ValueError, IndexError):
        return False
    return len(segments) > 1 and str(segments[0][0]).endswith(".parquet")


def _candidate_shards(con, collection) -> list[ShardDemand]:
    """For a LOCAL archive: the shards holding documents that are
    embed-eligible and not yet covered, found from the candidates' own
    locators — never by sweeping the shard list — in candidate index
    order. Empty when there is nothing the archive could do this round."""
    from ..plugins.wikipedia.archive import split_locator   # format v1 locators
    root = collection.corpus_dir
    demand: dict[str, ShardDemand] = {}
    # A REJECTED shard is never re-read (`genesis retry` puts it back): its
    # documents are the local encoder's, and only its — the archive's
    # declared range stays the archive's everywhere else (worker.
    # _covered_ranges). ≤ one row per shard read, on a primary-key prefix.
    rejected = {r["shard"] for r in con.execute(
        "SELECT shard FROM genesis_imports WHERE archive=? AND state='rejected'",
        (collection.genesis_archive or "",))}
    released: list[str] = []
    last_rowid, scanned = -1, 0
    while scanned < _CANDIDATE_SCAN_CAP:
        page = con.execute(_CANDIDATES_PAGE_SQL,
                           (last_rowid, _CANDIDATE_PAGE)).fetchall()
        if not page:
            break
        for r in page:
            shard = _shard_of_locator(root, r["path"], split_locator)
            if shard is None:
                if _straddles(r["path"], split_locator):
                    released.append(r["uid"])
                continue
            if shard in rejected:
                released.append(r["uid"])
                continue
            if r["uid"] in _residual_of(collection, shard):
                continue
            d = demand.get(shard)
            ts = int(r["updated_at"] or 0)
            if d is None:
                demand[shard] = ShardDemand(shard, [r["uid"]], ts, {r["uid"]: r["path"]})
            else:
                d.uids.append(r["uid"])
                d.locators[r["uid"]] = r["path"]
                d.oldest = min(d.oldest, ts)
        last_rowid = page[-1]["rowid"]
        scanned += len(page)
    if released:
        _add_residual(collection, _RELEASED, released)
    return list(demand.values())


def row_proven(row) -> bool:
    """A ledger row that carries a passed same-function proof: `proven_at`
    is set, or — rows written before that column existed — the shard
    imported documents, which only a passed proof allowed. A row selected
    without those columns falls back to its state; `applying` never counts
    (a pass cut by a crash before its proof)."""
    keys = row.keys()
    if "proven_at" in keys and row["proven_at"]:
        return True
    if "docs" in keys:
        return row["state"] in ("applied", "final") and int(row["docs"] or 0) > 0
    return row["state"] in ("applied", "final")


def archive_proven(rows) -> bool:
    """One shard's passed proof proves the ARCHIVE: one producer, one
    function — the proof is about the function, not the shard."""
    return any(row_proven(r) for r in rows)


def archive_stopped(rows) -> bool:
    """A rejected shard stops the archive ONLY while nothing has proven
    it: a systematically incompatible archive must not be retried shard
    after shard. Once one shard has passed the same-function proof and
    imported, a later rejection is that shard's problem (its range is
    released to the local encoder) — not evidence against the archive."""
    rows = list(rows)
    rejected = any(r["state"] == "rejected" for r in rows)
    return rejected and not archive_proven(rows)


def retry_rejected(con, archive_id: str | None = None, shard: str | None = None) -> list[tuple[str, str]]:
    """Put REJECTED shards back in line (`genesis retry`): state `fetched`,
    verdict cleared, revisit due now. Files still on disk apply at once;
    files gone are re-fetched. Returns the (archive, shard) pairs reset."""
    where, params = ["state='rejected'"], []
    if archive_id:
        where.append("archive=?"); params.append(archive_id)
    if shard:
        where.append("shard=?"); params.append(shard)
    clause = " AND ".join(where)
    rows = [(r["archive"], r["shard"]) for r in con.execute(
        f"SELECT archive, shard FROM genesis_imports WHERE {clause}", params)]
    if rows:
        con.execute(
            f"UPDATE genesis_imports SET state='fetched', detail=NULL, pending=NULL, "
            f"next_pass_at=0, updated_at=? WHERE {clause}", [int(time.time()), *params])
    return rows


def drain_next_shard(con, collection, *, stop=None, eager: bool = True) -> dict | None:
    """The worker's automatic path: import the next shard of the
    collection's declared archive that has embed work it could satisfy.
    Returns the shard result, or None when there is nothing to do (no
    declaration, no candidates, all shards applied, a shard was rejected —
    a systematically incompatible archive must not be retried forever — or
    a retryable failure is inside its backoff window).

    `eager=False` (the caller still has rdf/fts work on this thread) makes
    a LOCAL archive wait for a shard's demand to reach GENESIS_MIN_CANDIDATES
    documents or GENESIS_MAX_WAIT_SEC of age before paying the read, so the
    ~10 s shard read is amortised over a batch instead of charged per fts
    commit. Remote archives apply once post-settle and ignore it."""
    archive = ARCHIVES.get(collection.genesis_archive or "")
    if archive is None:
        return None
    rows = {r["shard"]: r for r in con.execute(
        "SELECT shard, state, updated_at, pending, next_pass_at, proven_at, docs "
        "FROM genesis_imports WHERE archive=?", (archive.archive_id,))}
    if archive_stopped(rows.values()):
        return None
    now = int(time.time())

    def in_backoff(shard: str) -> bool:
        row = rows.get(shard)
        return row is not None and row["state"] == "skipped" \
            and now - (row["updated_at"] or 0) < 600       # retryable: 10 min

    if archive.local:
        for d in _candidate_shards(con, collection):
            if in_backoff(d.shard):
                continue
            if not eager and len(d.uids) < config.GENESIS_MIN_CANDIDATES \
                    and now - d.oldest < config.GENESIS_MAX_WAIT_SEC:
                continue                # let the batch grow; rdf/fts run meanwhile
            try:
                # `stop=stop` — the remote branch below always passed it and
                # this one never did, so a LOCAL archive pass observed
                # neither SIGTERM nor its collection being retired.
                result = import_shard(con, collection, archive, d.shard, revisit=True,
                                      wanted=d.locators, stop=stop)
            except GenesisUnavailable:
                return None             # recorded as skipped; retried later
            except GenesisError:
                return None             # recorded as rejected; never retried
            # what this read was asked for and could not cover stays
            # eligible for the local encoder; the shard is not re-read for it
            _record_residual(collection, d.shard, result.get("residual_uids", ()))
            return result
        # nothing the archive can cover right now: YIELD the round — the
        # embed stage must be free to gap-fill what the archive cannot
        return None

    candidates = con.execute(
        "SELECT 1 FROM documents WHERE in_scope=1 AND rdf_done=1 AND fts_done=1 "
        "AND embed_done=0 AND error IS NULL LIMIT 1").fetchone()
    if candidates is None:
        return None
    # REMOTE archive, amendment 38: shards reach the cache through
    # genesis_fetch (its own thread, its own budget class) and are applied
    # from disk HERE — at once for the documents indexed so far, revisited
    # as more of their documents clear fts, and finalised (files deleted)
    # once no document in their id range still waits. The old post-settle
    # gate (apply once, after the whole collection had indexed) held
    # pubmed's 37 shards back for the months 40M documents take.
    # The budget's other half: after a pass was cut on time, THIS
    # COLLECTION's archive yields for the same span — per collection, not
    # per shard, or five shards on disk would each take their slice
    # back-to-back and rdf/fts (which the archive itself depends on)
    # would never get the thread.
    key = str(collection.manifest_path)
    if time.time() < _yield_until.get(key, 0.0):
        return None
    for shard in _remote_drain_order(archive.shards, rows, now, collection,
                                     archive, con):
        try:
            result = import_shard(con, collection, archive, shard,
                                  fetch=_not_on_disk, revisit=True, stop=stop)
        except GenesisUnavailable:
            return None                 # files gone: skipped, re-fetched later
        except GenesisError:
            return None                 # recorded as rejected; never retried
        if result.get("partial") and not (stop is not None and stop.is_set()):
            _yield_until[key] = time.time() + config.GENESIS_PASS_BUDGET_S
        return result
    return None


# per collection (manifest path): when its remote archive may run again
# after a budget-cut pass — process-local, like the residual sets
_yield_until: dict[str, float] = {}


#: per collection (manifest path): when a stalled archive may force one
#: backed-off shard to be re-checked. See `_remote_drain_order`.
_forced_revisit: dict[str, float] = {}


def _on_disk(collection, archive, shard: str) -> bool:
    """Are this shard's files actually here?

    `_remote_drain_order`'s docstring has always said "a shard not on disk
    ... is never walked here", and nothing checked: the walk read the
    LEDGER only. A shard whose files were deleted while its row still says
    `applied` was therefore selected every single round — `import_shard`
    raised GenesisUnavailable, `drain_next_shard` caught it and returned
    None, and the next round picked the same shard again. Silent, because
    GenesisUnavailable is "recorded as skipped; retried later", and
    permanent, because nothing about the shard ever changed.

    Measured on pubmed 2026-09-11: shards 8-12 `applied` with their files
    gone, the drain order returning ['9','10','11','12','13'], the import
    at 0 documents/s for twenty minutes with one core spinning and not one
    log line. 30M documents were waiting for vectors the archive already
    held.

    Unknown means NOT on disk. A shard that cannot be located is one the
    walk must skip, and guessing the other way is how this started.
    """
    if collection is None or archive is None:
        return True                     # no way to ask: preserve old behaviour
    try:
        from . import genesis_fetch
        cache = genesis_fetch.cache_dir(collection, archive)
        files = genesis_fetch.shard_files(archive, cache, shard)
        return bool(files) and all(p.is_file() for _, p in files)
    except Exception:       # noqa: BLE001 — a probe must never break the walk
        return False


def _cache_is_full(con, archive) -> bool:
    """True when no further shard can be fetched until one goes final —
    genesis_fetch's own condition, asked from this side."""
    try:
        from . import genesis_fetch
        resident = genesis_fetch.resident_bytes(con, archive)
        return bool(resident + (archive.shard_bytes or 0)
                    > config.GENESIS_CACHE_BYTES)
    except Exception:       # noqa: BLE001 — a stall check must never raise
        return False


def _remote_drain_order(shards, rows, now: int, collection=None,
                        archive=None, con=None) -> list[str]:
    """Fetched-never-applied shards first, in declared order — the free
    vectors land as early as possible — then applied shards that still
    have documents waiting (`pending`), soonest revisit first. A shard's
    `next_pass_at` is its per-shard backoff: a pass that imported nothing
    doubled it (import_shard), so the cost of revisiting follows the work
    available, never the length of the shard list. A shard not on disk
    (no row, fetching, skipped) or final is never walked here."""
    fresh = [s for s in shards
             if rows.get(s) is not None and rows[s]["state"] == "fetched"]
    # `applying` here means a pass was cut short by a crash or restart —
    # its files are on disk and its work resumes like a budgeted pause
    due = [s for s in shards
           if rows.get(s) is not None and rows[s]["state"] in ("applied", "applying")
           and (rows[s]["pending"] is None or rows[s]["pending"] > 0)
           and int(rows[s]["next_pass_at"] or 0) <= now
           and _on_disk(collection, archive, s)]
    due.sort(key=lambda s: int(rows[s]["next_pass_at"] or 0))
    if fresh or due or collection is None or archive is None or con is None:
        return fresh + due

    # THE STALL. Nothing is due, and if the cache is also full then nothing
    # can be fetched either — so the round does nothing, and will keep
    # doing nothing until a backoff expires. That backoff is up to
    # GENESIS_PASS_BACKOFF_MAX_SEC (6 h) and it DOUBLED because earlier
    # passes imported little; but they imported little because fts had not
    # reached those documents yet, and fts has been advancing ever since.
    # `pending` counts documents with fts_done=0, so the very condition the
    # backoff is waiting on changes continuously while the backoff does
    # not. Measured on pubmed 2026-09-11: eleven applied shards each
    # carrying a 9,600 s backoff and a stale `pending`, every document in
    # the ranges checked already indexed — so every one of them was
    # finalisable, 37 GiB sat resident against a 24 GiB bound, and the
    # import advanced in bursts hours apart instead of continuously.
    #
    # Revisiting is then the ONLY way forward, so the backoff is not
    # merely useless, it is the thing holding the pipeline shut. Force the
    # soonest-due shard, at most once per GENESIS_MAX_WAIT_SEC, which
    # bounds the cost to one `pending` recount (~9.5 s on pubmed) per ten
    # minutes and only while stalled.
    if not _cache_is_full(con, archive):
        return []
    key = str(collection.manifest_path)
    if now < _forced_revisit.get(key, 0.0):
        return []
    backed_off = [s for s in shards
                  if rows.get(s) is not None
                  and rows[s]["state"] in ("applied", "applying")
                  and (rows[s]["pending"] is None or rows[s]["pending"] > 0)
                  and _on_disk(collection, archive, s)]
    if not backed_off:
        return []
    backed_off.sort(key=lambda s: int(rows[s]["next_pass_at"] or 0))
    _forced_revisit[key] = now + config.GENESIS_MAX_WAIT_SEC
    return backed_off[:1]


def _drain_order(shards, rows, *, revisit: bool):
    """The explicit `genesis import` walk (CLI): pending shards in declared
    order, then — with `revisit` — applied ones oldest-first. The WORKER
    never walks an archive this way: a local archive reads the shards its
    candidates name (_candidate_shards), a remote one what is on disk
    (_remote_drain_order); sweeping the list cost one ~10 s parquet read
    per shard per round on a 959-shard corpus, importing nothing from 958."""
    pending = [s for s in shards
               if rows.get(s) is None or rows[s]["state"] not in ("applied", "final")]
    if not revisit:
        return pending
    applied = sorted((s for s in shards
                      if rows.get(s) is not None and rows[s]["state"] == "applied"),
                     key=lambda s: rows[s]["updated_at"] or 0)
    return pending + applied


def _local_shards(collection) -> tuple[str, ...]:
    """Corpus-resident shards, as corpus-relative POSIX paths (stable
    genesis_imports keys across remounts)."""
    root = collection.corpus_dir
    if not root.is_dir():
        return ()
    return tuple(sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*.parquet") if p.is_file()))


# -------- adapters: shard -> iterator of (uid, [vector, ...] by seq) --------
# Every adapter yields ONE entry per document with that document's vectors
# in chunk-seq order; single-chunk archives (medcpt) yield one-element lists.

def _read_medcpt_shard(cache_dir: Path, shard: str,
                        fetch: Callable[[str, Path], None],
                        uid_prefix: str, collection, wanted=None) -> tuple[Iterator, list[Path]]:
    """`wanted` (uids, or None for every row): a revisit pass names the
    documents that became eligible since, and reads ONLY their rows from
    the memory-mapped array — proportional to the new documents, not to
    the million-row shard. Live before this: every pass walked the shard
    from row 0 with a manifest lookup per row, was cut by its 300 s
    budget while re-skipping what earlier passes had imported, and each
    pass imported less than the one before (86k, 101k, 87k, 72k, 68k,
    58k, 49k) — half of pubmed's pipeline thread, spent re-reading."""
    npy = cache_dir / f"embeds_chunk_{shard}.npy"
    ids = cache_dir / f"pmids_chunk_{shard}.json"
    for name, path in ((f"embeds_chunk_{shard}.npy", npy),
                        (f"pmids_chunk_{shard}.json", ids)):
        if not path.is_file():
            fetch(name, path)
    pmids = json.loads(ids.read_text(encoding="utf-8"))
    vectors = np.load(npy, mmap_mode="r")   # 2.7 GB shards never enter RAM whole
    if len(pmids) != len(vectors):
        raise GenesisError(
            f"shard {shard}: {len(pmids)} ids vs {len(vectors)} vectors")

    def rows():
        if wanted is None:
            for pmid, vec in zip(pmids, vectors):
                yield f"{uid_prefix}{pmid}", [np.asarray(vec, dtype=np.float32)]
            return
        # one pass over the id list (strings, in memory) to find the rows;
        # then only those rows are touched in the mmap, in shard order
        want = set(wanted)
        for i, pmid in enumerate(pmids):
            uid = f"{uid_prefix}{pmid}"
            if uid in want:
                yield uid, [np.asarray(vectors[i], dtype=np.float32)]
    return rows(), [npy, ids]


def _read_parquet_shard(cache_dir: Path, shard: str,
                         fetch: Callable[[str, Path], None],
                         uid_prefix: str, collection, wanted=None) -> tuple[Iterator, list[Path]]:
    """Generic adapter: any parquet with (id, vector) columns — the
    "bring your own compatible embeddings" path."""
    import pyarrow.parquet as pq
    path = cache_dir / shard
    if not path.is_file():
        fetch(shard, path)
    table = pq.read_table(path, columns=["id", "vector"])

    def rows():
        for rid, vec in zip(table.column("id").to_pylist(),
                             table.column("vector").to_pylist()):
            yield f"{uid_prefix}{rid}", [np.asarray(vec, dtype=np.float32)]
    return rows(), [path]


def _row_groups_for(pf, ranges: list[tuple[int, int]]) -> list[int]:
    """The parquet row groups that hold any of the [start, end) row ranges."""
    groups = []
    offset = 0
    for g in range(pf.num_row_groups):
        n = pf.metadata.row_group(g).num_rows
        lo, hi = offset, offset + n
        if any(start < hi and end > lo for start, end in ranges):
            groups.append(g)
        offset = hi
    return groups


def _read_corpus_embedded_shard(cache_dir: Path, shard: str,
                                 fetch: Callable[[str, Path], None],
                                 uid_prefix: str, collection, wanted=None) -> tuple[Iterator, list[Path]]:
    """Amendment 11: the corpus IS the archive. `shard` is a corpus-relative
    parquet path (format v1: data/{lang}/NNN.parquet); rows of one article
    are consecutive with id "{article}_{seq}". Nothing is fetched and the
    file is part of the corpus — import_shard never deletes it.

    `wanted` ({uid: locator}, or None for the whole shard): a candidate
    read touches only the ROW GROUPS the candidates' locators name —
    live, every 1,000-document fts batch triggered a read of the whole
    437 MB shard to import those 1,000 documents (~20 % of each batch's
    wall time)."""
    import pyarrow.parquet as pq
    from ..plugins.wikipedia.archive import split_locator, split_row_id
    from ..plugins.wikipedia.ids import LANG_ORDINAL, native_id_of
    path = collection.corpus_dir / shard
    if not path.is_file():
        raise GenesisUnavailable(f"corpus shard missing: {path}")
    lang = Path(shard).parts[1] if len(Path(shard).parts) >= 3 else ""
    if lang not in LANG_ORDINAL:
        raise GenesisError(f"shard {shard}: unrecognized language directory")
    pf = pq.ParquetFile(path)
    groups: list[int] | None = None
    want: set[str] | None = None
    if wanted is not None:
        want = set(wanted)
        ranges: list[tuple[int, int]] = []
        for uid, locator in wanted.items():
            for seg_path, start, end in (split_locator(locator) if locator else ()):
                if Path(seg_path) == path:
                    ranges.append((start, end))
        groups = _row_groups_for(pf, ranges) if ranges else list(range(pf.num_row_groups))

    def batches():
        if groups is None:
            yield from pf.iter_batches(columns=["id", "embedding"], batch_size=1024)
            return
        for g in groups:
            yield from pf.read_row_group(g, columns=["id", "embedding"]).to_batches(max_chunksize=1024)

    def rows():
        # An article straddling two shard files surfaces here with a PARTIAL
        # vector list (each shard is read independently), fails the
        # chunk-shape gate, and gap-fills locally instead — ~one article per
        # shard boundary, and never a wrong import.
        current: str | None = None
        vecs: list[np.ndarray] = []
        for batch in batches():
            for rid, vec in zip(batch.column(0).to_pylist(), batch.column(1).to_pylist()):
                article, _seq = split_row_id(rid)
                if article != current:
                    if current is not None:
                        uid = f"{uid_prefix}{native_id_of(lang, int(current), archive=True)}"
                        if want is None or uid in want:
                            yield uid, vecs
                    current, vecs = article, []
                vecs.append(np.asarray(vec, dtype=np.float32))
        if current is not None:
            uid = f"{uid_prefix}{native_id_of(lang, int(current), archive=True)}"
            if want is None or uid in want:
                yield uid, vecs
    return rows(), [path]


_ADAPTERS = {"medcpt-npy": _read_medcpt_shard, "parquet": _read_parquet_shard,
             "corpus-embedded": _read_corpus_embedded_shard}


# ---------------- import ---------------------------------------------------

def _https_fetcher(archive: GenesisArchive):
    from ..acquire.spec import (Endpoint, RemoteFile, Transport, TransferAborted,
                                TransportError)
    from ..acquire.transport_https import HttpsTransport
    transport = HttpsTransport()
    endpoint = Endpoint(transport=Transport.HTTPS, url=archive.base_url)

    def fetch(name: str, dest: Path) -> None:
        part = dest.with_name(dest.name + ".part")
        resume = part.stat().st_size if part.is_file() else 0
        try:
            transport.fetch_one(endpoint, RemoteFile(rel_path=name), dest, resume)
        except TransferAborted:
            raise                       # a stop request is not unavailability
        except TransportError as e:
            raise GenesisUnavailable(str(e)) from e
    return fetch


def _not_on_disk(name: str, dest: Path) -> None:
    """The worker's remote-archive fetch seam (amendment 38): shards reach
    the cache through genesis_fetch on their own thread and budget class;
    the embed thread never downloads. A shard recorded `fetched` whose
    files are gone is unavailable — the row goes back to `skipped` and the
    fetcher lands it again."""
    raise GenesisUnavailable(f"{name} is not in the archive cache")


def _digest_files(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        with open(p, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
    return h.hexdigest()


def _local_chunk_texts(collection, row) -> list[tuple[str, str]]:
    """[(chunk_id, text)] in seq order for one document.

    The apply loop no longer calls this per document — it chunks a whole
    batch through the parse pool (`parallel.chunks_for_rows`), which is the
    same computation and eleven times the cores. Kept because it IS that
    computation for one document, and tools and tests read it as such."""
    from .. import layers as layers_mod
    plugin = collection.plugin_instance()
    sections = plugin_io.sections(plugin, row)
    return [(c["id"], c["text"])
            for c in layers_mod.chunks_for_document(collection, row, sections)]


def _sample_re_encode(collection, sampled: list[tuple[str, str, np.ndarray]],
                      embed_fn, *, proven: bool = False) -> list[str]:
    """The same-function proof: the collection's own encoder must reproduce
    the archive's vectors on held texts. This is what makes later gap-filling
    coherent — and what keeps an INCOMPATIBLE dataset out, however well-formed.

    `sampled` is [(uid, text, archived vector)]. Passes by QUORUM (see the
    constants) and returns the uids of the samples below tau — stale
    documents the caller must not import.

    `proven`: the archive's function is already proven (another shard, or an
    earlier pass of this one). The sample is then a STALENESS SCREEN, not a
    proof: anything below tau is a document whose text changed since the
    snapshot, and a failing quorum on fewer than RE_ENCODE_REJECT_MIN samples
    is that many stale documents — never a verdict. On an UNPROVEN archive a
    small failing sample is inconclusive (GenesisInconclusive: deferred,
    nothing written). A failing sample of RE_ENCODE_REJECT_MIN or more is the
    verdict either way: GenesisError, the shard refused whole."""
    if len(sampled) < RE_ENCODE_MIN_SAMPLE:
        if proven:
            return []
        raise GenesisInconclusive(
            f"only {len(sampled)} re-encode sample(s) within the local truncation "
            f"budget (need {RE_ENCODE_MIN_SAMPLE}) — cannot prove the archive "
            f"matches this collection's encoder yet; retried when more documents "
            f"are eligible")
    texts, archived = [], []
    for _uid, text, vec in sampled:
        texts.append(text)
        archived.append(vec / (np.linalg.norm(vec) or 1.0))
    try:
        fresh = embed_fn(texts)
    except Exception as e:
        raise GenesisUnavailable(
            f"embed endpoint needed for the same-function proof is unavailable "
            f"({type(e).__name__})") from e
    cosines = []
    for a, f in zip(archived, fresh):
        f = np.asarray(f, dtype=np.float64)
        f /= (np.linalg.norm(f) or 1.0)
        cosines.append(float(a @ f))
    below = [uid for (uid, _t, _v), c in zip(sampled, cosines) if c < RE_ENCODE_TAU]
    passing = len(cosines) - len(below)
    median = float(np.median(cosines))
    if median >= RE_ENCODE_TAU and passing >= RE_ENCODE_QUORUM * len(cosines):
        return below
    verdict = (f"archive vs local encoder median cos {median:.4f}, "
               f"{passing}/{len(cosines)} >= {RE_ENCODE_TAU}")
    if len(cosines) >= RE_ENCODE_REJECT_MIN:
        raise GenesisError(
            f"sample re-encode failed: {verdict} — the archive was NOT produced "
            f"by this collection's [embed] recipe; importing it would mix spaces")
    if proven:
        return below            # that many stale documents under a proven function
    raise GenesisInconclusive(
        f"sample re-encode inconclusive: {verdict} on {len(cosines)} sample(s) "
        f"(a verdict needs {RE_ENCODE_REJECT_MIN}) — retried when more documents "
        f"are eligible")


def import_shard(con, collection, archive: GenesisArchive, shard: str, *,
                 embed_fn=None, fetch=None, keep_cache: bool = False,
                 revisit: bool = False, stop=None, wanted=None) -> dict:
    """Admit one shard. Idempotent: an applied shard short-circuits, unless
    `revisit` (local archives) — re-running only touches documents that
    became eligible since, via the same delete-then-insert.

    `wanted` narrows the read to named documents ({uid: locator} from a
    local archive's candidates). For a REMOTE archive with declared
    coverage a revisit pass narrows itself: one indexed range query on
    the manifest names the eligible documents of the shard's id range,
    and only their rows are read — the pass costs the new documents."""
    # INVARIANT 9, and the half of it that was missing. `genesis_fetch`
    # gates the FETCH — before the first byte moves from the origin — but
    # a shard already on disk never passes through it: a local archive's
    # vectors travel with the corpus, a hand-populated cache directory is
    # not a fetch, and a peer copy is not a fetch either. The invariant is
    # explicit that those are not a bypass ("hand-populated folders and
    # peer copies are not a bypass — observed live: a peer-copied shard was
    # indexed and imported ack-free"), and USE is what it gates, not
    # transport. So the check belongs here too, at the chokepoint every
    # path reaches: `import_archive` walks through this function and
    # `app/cli.py` is the only caller of either.
    licensing.require_ack(collection, use="an archive's vectors are imported")

    existing = con.execute(
        "SELECT state, updated_at, next_pass_at, proven_at, docs FROM genesis_imports "
        "WHERE archive=? AND shard=?", (archive.archive_id, shard)).fetchone()
    if existing and existing["state"] == "final":
        return {"shard": shard, "state": "already-applied"}   # complete: files gone
    if existing and existing["state"] == "applied" and not revisit:
        return {"shard": shard, "state": "already-applied"}
    # the proof is per ARCHIVE and persists: any shard's passed proof (or
    # its imports, for rows older than `proven_at`) makes this pass a
    # staleness screen rather than a trial
    archive_is_proven = archive_proven(con.execute(
        "SELECT state, proven_at, docs FROM genesis_imports WHERE archive=?",
        (archive.archive_id,)))
    # the revisit interval this pass inherits (per-shard backoff, remote)
    prev_interval = max(0, int((existing["next_pass_at"] or 0) - (existing["updated_at"] or 0))) \
        if existing else 0

    recipe = recipe_for(collection)
    if embed_fn is None:
        embed_fn = lambda texts: embeddings.embed_texts(texts, recipe=recipe)  # noqa: E731
    fetch = fetch or _https_fetcher(archive)
    cache_dir = collection.manifest_path.parent / "genesis" / archive.archive_id
    cache_dir.mkdir(parents=True, exist_ok=True)

    if wanted is None and revisit and not archive.local and archive.id_range_of(shard):
        lo, hi = archive.id_range_of(shard)
        wanted = {r["uid"]: None for r in con.execute(eligible_in_range_sql(con), (lo, hi))}
    # A LOCAL archive's rows become the COLLECTION's documents, and a uid is
    # `<plugin>:<native_id>` (02 §2.3): `embedded:` for the legacy wikivec,
    # `wikipedia:` for wikipedia's layer 1 — one archive, the same ids.
    uid_prefix = f"{collection.plugin}:" if archive.local else archive.uid_prefix
    rows_iter, files = _ADAPTERS[archive.adapter](cache_dir, shard, fetch,
                                                    uid_prefix, collection,
                                                    wanted=wanted)
    # the fetcher digested a remote shard when it landed (3 GB, ~10 s);
    # a revisit pass on the embed thread must not pay that again
    prior_digest = con.execute(
        "SELECT content_digest FROM genesis_imports WHERE archive=? AND shard=?",
        (archive.archive_id, shard)).fetchone()
    digest = (prior_digest["content_digest"]
              if prior_digest and prior_digest["content_digest"] and revisit
              else _digest_files(files))
    con.execute(
        "INSERT INTO genesis_imports(archive,shard,source_url,content_digest,"
        "state,updated_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(archive,shard) DO UPDATE SET state=excluded.state,"
        "content_digest=excluded.content_digest,updated_at=excluded.updated_at",
        (archive.archive_id, shard, archive.base_url, digest, "applying",
         int(time.time())))   # autocommit connection (isolation_level=None)

    np_dtype = np.float16 if recipe.dtype == "f16" else np.float32
    docs = chunks_written = skipped_absent = skipped_shape = 0
    # remote archive WITHOUT declared coverage: the pass counts, per id it
    # saw and could not import, whether the document is held and still
    # waiting for fts (a declared range answers this with one count)
    pending_probe = {"n": 0}
    probe_pending = revisit and not archive.local and archive.ids_per_shard is None
    # A remote shard's first pass imports up to a million documents on the
    # collection's own thread — the thread that also runs its rdf/fts. The
    # pass is BUDGETED (config.GENESIS_PASS_BUDGET_S): it stops on time or
    # on stop (SIGTERM: the unit is a batch), commits what it has, and
    # resumes next time — after an equal share for rdf/fts. Every already-
    # imported document is skipped by its row lookup on resume.
    t_start = time.time()
    budget = config.GENESIS_PASS_BUDGET_S if (revisit and not archive.local) else None
    partial = stopped = False
    # candidates this read was asked for and could not cover — reported so
    # drain_next_shard stops re-reading the shard on their account
    residual_uids: list[str] = []
    sample: list[tuple[str, str, np.ndarray]] = []      # (uid, text, archived vector)
    batch_rows: list[dict] = []
    batch_native: list[int] = []
    batch_uids: list[str] = []
    batch_chunk_ids: dict[str, list[str]] = {}
    header_dim: int | None = None
    checked_structural = False
    proven = False
    # Provenance identity for these vectors. There is no signing key: an
    # archive is admitted by the same-function proof, not a signature, so
    # the producer is the archive itself and the "bundle" is one shard of it.
    producer = f"genesis:{archive.archive_id}"
    pseudo_bundle = f"genesis:{archive.archive_id}/{shard}"

    def flush():
        nonlocal batch_rows, batch_native, batch_uids, batch_chunk_ids
        if not batch_rows:
            return
        with vector_write_lock(con):
            conn = embeddings._conn(collection)
            import pyarrow as pa
            if embeddings.TABLE in conn.table_names():
                table = conn.open_table(embeddings.TABLE)
                # ONE delete per batch, not one per document: a Lance delete
                # scans the table for its filter, so a million per-document
                # deletes over a table growing to a million rows was the
                # quadratic trap in a new coat (pubmed's first shard pass:
                # ~150 documents/s). Idempotence (invariant 2: delete-then-
                # insert keyed on the document) is unchanged — the ids are
                # forced through int() exactly as _book_filter does.
                ids = ",".join(str(int(n)) for n in batch_native)
                table.delete(f"book_id IN ({ids})")
                table.add(batch_rows)
            else:
                value_type = pa.float16() if recipe.dtype == "f16" else pa.float32()
                schema = pa.schema([
                    pa.field("id", pa.string()), pa.field("book_id", pa.int64()),
                    pa.field("lang", pa.string()), pa.field("seq", pa.int64()),
                    pa.field("chapter", pa.string()), pa.field("text", pa.string()),
                    pa.field("vector", pa.list_(value_type, header_dim)),
                ])
                conn.create_table(embeddings.TABLE, data=batch_rows, schema=schema)
        now = int(time.time())
        # ONE transaction per flush (invariant 2: marks land after the
        # durable Lance write, together): on the autocommit connection
        # every UPDATE used to be its own commit — 2 × APPLY_BATCH_DOCS
        # fsyncs per batch, a million per MedCPT shard. BEGIN IMMEDIATE:
        # a writer that reads first must not upgrade under WAL.
        own_tx = not con.in_transaction
        if own_tx:
            con.execute("BEGIN IMMEDIATE")
        try:
            # CREDITED ONCE PER DOCUMENT: a document this shard already
            # delivered comes back whenever something resets embed_done and
            # keeps its provenance (a reindex, a stage reset, a changed
            # document, a requeue) and a revisit re-selects it. mark_imported
            # upserts on chunk_id, so provenance counts it once; so must the
            # ledger. Read inside this transaction, before the upserts.
            all_chunk_ids = [c for u in batch_uids for c in batch_chunk_ids.get(u, ())]
            credited: set[str] = set()
            for i in range(0, len(all_chunk_ids), 900):
                part = all_chunk_ids[i:i + 900]
                credited.update(r[0] for r in con.execute(
                    CREDITED_SQL.format(marks=",".join("?" * len(part))),
                    (*part, pseudo_bundle)))
            new_docs = sum(1 for u in batch_uids
                           if not credited.intersection(batch_chunk_ids.get(u, ())))
            new_chunks = len(set(all_chunk_ids) - credited)
            for uid in batch_uids:
                con.execute(
                    "UPDATE documents SET embed_done=1, embed_version=?, updated_at=? "
                    "WHERE uid=?", (config.EMBED_VERSION, now, uid))
                # These vectors were NOT computed here. Without this the Ask
                # drawer badged every imported chunk "local ✓" — a false
                # provenance claim, and provenance is what quarantine and §6's
                # proof-of-forgery rest on. Marking them 'imported' also makes
                # them eligible for verify-on-use, which is exactly right.
                ledger.mark_imported(con, batch_chunk_ids.get(uid, []), uid,
                                      producer, pseudo_bundle)
            # THE LEDGER COUNTS HERE, in the transaction that marks the
            # documents — never at the end of the pass (invariant 2). It was
            # counted once, after the walk, so a pass that committed flushes
            # and then ended any other way — a commit conflict deferred, a
            # busy manifest, a rejection mid-shard, a kill, a failure in the
            # final count — left those documents imported and never counted,
            # and the resumed pass skips them (embed_done=1) as not held.
            # Live on pubmed 2026-09-14: shards 8, 9 and 13 had 80,654 more
            # embedded documents in their ranges than their rows said —
            # 53,000 in 13 is exactly 106 flushes of 500.
            #
            # WHAT IT COUNTS: the documents (and chunks) this shard has
            # delivered, each once — its provenance rows, which is what
            # `recount_ledger` recomputes. Not vectors held now: a document
            # the local encoder later re-embeds loses its provenance
            # (`ledger.clear_verification`) and keeps its count here until
            # a recount.
            con.execute(
                "UPDATE genesis_imports SET docs=COALESCE(docs,0)+?, "
                "chunks=COALESCE(chunks,0)+? WHERE archive=? AND shard=?",
                (new_docs, new_chunks, archive.archive_id, shard))
            if own_tx:
                con.execute("COMMIT")
        except BaseException:
            if own_tx and con.in_transaction:
                con.execute("ROLLBACK")
            raise
        batch_rows, batch_native, batch_uids = [], [], []
        batch_chunk_ids = {}

    def prove() -> None:
        """The same-function proof (first pass of an unproven archive) or
        the staleness screen (every later pass), over the sample gathered
        so far; runs BEFORE the first flush, so a refusal writes nothing.
        A passed proof is persisted on the row — the archive is proven
        from here on, for this shard's later passes and every other shard."""
        nonlocal proven, archive_is_proven
        evict(_sample_re_encode(collection, sample, embed_fn,
                                proven=archive_is_proven))
        if not archive_is_proven:
            con.execute("UPDATE genesis_imports SET proven_at=? WHERE archive=? AND shard=?",
                        (int(time.time()), archive.archive_id, shard))
            archive_is_proven = True
        proven = True

    def evict(stale: list[str]) -> None:
        """Drop the STALE sample documents (below tau under a proven
        function: their text changed since the snapshot) from the pending
        batch before it is written. They stay embed-eligible for the
        local encoder, like a chunk-shape mismatch."""
        nonlocal batch_rows, batch_native, batch_uids, batch_chunk_ids, docs, chunks_written
        bad = set(stale)
        if not bad:
            return
        natives = {native for u, native in zip(batch_uids, batch_native) if u in bad}
        before = len(batch_rows)
        batch_rows = [r for r in batch_rows if r["book_id"] not in natives]
        chunks_written -= before - len(batch_rows)
        docs -= sum(1 for u in batch_uids if u in bad)
        batch_native = [n for u, n in zip(batch_uids, batch_native) if u not in bad]
        batch_uids = [u for u in batch_uids if u not in bad]
        for u in bad:
            batch_chunk_ids.pop(u, None)
            residual_uids.append(u)
        db.log_event(con, "info", "genesis",
                     f"{archive.archive_id}/{shard}: {len(bad)} sampled document(s) "
                     f"stale under a proven function — skipped, left to the encoder")

    # Truncation-aware sampling (amendment 11): the local serving stack may
    # truncate shorter than the producer did, so the same-function proof
    # samples chunks inside the LOCAL budget — where the two functions must
    # agree. Over-budget chunks still import: their archived vector is
    # strictly better than anything the local encoder could produce.
    sample_char_budget = recipe.ctx * 4

    try:
        # The documents whose vectors are in hand, waiting for their chunk
        # TEXTS: fetch+parse+chunk is per-document Python and was called one
        # document at a time inside this loop — 2.87 ms each, 51.7 s of a
        # 150 s wikivec pass, on one core with eleven idle (measured
        # 2026-08-28). It goes to the PARSE POOL a batch at a time; the
        # writes, the proof and the ordering below are untouched.
        held: list[tuple] = []            # (row, vecs, units)

        def drain_held():
            """Chunk the held documents (pooled) and append their rows."""
            nonlocal skipped_absent, skipped_shape, chunks_written, held
            if not held:
                return
            computed = parallel.chunks_for_rows(collection, [r for r, _, _ in held])
            for (row, vecs, units), (ok, chunks) in zip(held, computed):
                uid = row["uid"]
                if not ok:
                    skipped_absent += 1
                    residual_uids.append(uid)
                    continue
                chunk_texts = [(c["id"], c["text"]) for c in chunks]
                if len(chunk_texts) != len(vecs):
                    # vectors bind to chunks 1:1; a count mismatch would leave
                    # the document half-embedded (or mis-attached) — skip it,
                    # local gap-fill covers it in the same proven space
                    skipped_shape += 1
                    residual_uids.append(uid)
                    continue
                _append_document(row, chunk_texts, vecs, units)
            held = []

        def _append_document(row, chunk_texts, vecs, units):
            nonlocal chunks_written, docs, checked_structural
            uid = row["uid"]
            for seq, ((chunk_id, text), vec, unit) in enumerate(
                    zip(chunk_texts, vecs, units)):
                if not proven and len(sample) < RE_ENCODE_SAMPLE \
                        and len(text) <= sample_char_budget:
                    # the proof itself runs at the first flush, over the
                    # whole sample: stale samples are then evicted from
                    # the batch before anything is written
                    sample.append((uid, text, np.asarray(vec, dtype=np.float64)))
                batch_rows.append({
                    "id": chunk_id, "book_id": int(row["native_id"]),
                    "lang": row["lang"] or "en", "seq": seq, "chapter": "",
                    "text": text, "vector": unit,
                })
                batch_chunk_ids.setdefault(uid, []).append(chunk_id)
                chunks_written += 1
            batch_native.append(int(row["native_id"]))
            batch_uids.append(uid)
            docs += 1
            if len(batch_rows) >= APPLY_BATCH_DOCS:
                # ALL admission runs before the first flush — "refused
                # whole" must mean nothing was written. Structural is
                # cheap; the same-function proof runs here on a partial
                # sample if the full quota never accumulated (truncation-
                # aware filter), and an EMPTY sample on an unproven archive
                # DEFERS the shard: vectors admitted without proof would be
                # trust, not admission — and a refusal without evidence
                # would be a verdict without one.
                if not checked_structural:
                    ids = [r["id"] for r in batch_rows]
                    batch_vecs = [np.asarray(r["vector"], dtype=np.float32)
                                  for r in batch_rows]
                    for result in (check_structural(ids, batch_vecs, header_dim,
                                                     recipe.dtype),
                                    check_degenerate(batch_vecs)):
                        if not result.passed:
                            raise GenesisError(f"admission: {result.detail}")
                    checked_structural = True
                if not proven:
                    prove()
                flush()

        for uid, vecs in rows_iter:
            # The interrupt is INDEPENDENT of the budget. Nested under
            # `budget is not None`, a pass started without one — which is
            # every local-archive pass — could not be stopped at all: not by
            # SIGTERM, not by its collection being retired. wikivec drained
            # for hours past both.
            stopped = stop is not None and stop.is_set()
            if stopped or (budget is not None and time.time() - t_start > budget):
                partial = True
                break
            row = con.execute(
                "SELECT uid, native_id, path, lang FROM documents "
                "WHERE uid=? AND error IS NULL AND fts_done=1 AND embed_done=0",
                (uid,)).fetchone()
            if row is None:
                skipped_absent += 1
                if probe_pending:
                    held_row = con.execute(
                        "SELECT 1 FROM documents WHERE uid=? AND in_scope=1 "
                        "AND error IS NULL AND fts_done=0", (uid,)).fetchone()
                    if held_row is not None:
                        pending_probe["n"] += 1
                continue

            units = []
            for vec in vecs:
                if header_dim is None:
                    header_dim = int(vec.shape[0])
                    if recipe.dim is not None and header_dim != recipe.dim:
                        raise GenesisError(
                            f"archive dim {header_dim} != recipe dim {recipe.dim}")
                norm = float(np.linalg.norm(vec))
                if not np.isfinite(vec).all() or norm == 0.0:
                    raise GenesisError(f"{uid}: non-finite or zero archive vector")
                units.append((vec / norm).astype(np_dtype))

            held.append((dict(row), vecs, units))
            if len(held) >= APPLY_BATCH_DOCS:
                drain_held()
        drain_held()
        if chunks_written and not proven:
            prove()
        flush()
    except GenesisInconclusive as e:
        # not a verdict, nothing written: the shard waits for more eligible
        # documents like a pass that imported nothing (per-shard backoff on
        # the remote revisit path; the retryable 10-minute backoff elsewhere)
        now = int(time.time())
        interval = min(config.GENESIS_PASS_BACKOFF_MAX_SEC,
                       max(config.GENESIS_MAX_WAIT_SEC, prev_interval * 2))
        con.execute(
            "UPDATE genesis_imports SET state=?, detail=?, pending=NULL, "
            "next_pass_at=?, updated_at=? WHERE archive=? AND shard=?",
            ("applied" if (revisit and not archive.local) else "skipped",
             str(e)[:500], now + interval, now, archive.archive_id, shard))
        db.log_event(con, "info", "genesis",
                     f"{archive.archive_id}/{shard}: {e}"[:400])
        raise
    except GenesisError as e:
        con.execute(
            "UPDATE genesis_imports SET state='rejected', detail=?, updated_at=? "
            "WHERE archive=? AND shard=?",
            (str(e)[:500], int(time.time()), archive.archive_id, shard))
        db.log_event(con, "error", "genesis",
                     f"{archive.archive_id}/{shard} REJECTED: {e}"[:400])
        raise
    except GenesisUnavailable:
        con.execute(
            "UPDATE genesis_imports SET state='skipped', detail='retryable', "
            "updated_at=? WHERE archive=? AND shard=?",
            (int(time.time()), archive.archive_id, shard))
        raise
    except Exception as e:      # noqa: BLE001 — narrowed immediately, LAST so the
        # specific handlers above keep their state transitions
        from ..worker import vector_store_contended
        if not vector_store_contended(e):
            raise
        # the index maintainer (or another writer) committed first: what
        # this pass already wrote stands, the shard keeps its state and the
        # pass is retried — a deferral, like a busy manifest
        con.execute(
            "UPDATE genesis_imports SET state='skipped', detail='vector store busy', "
            "updated_at=? WHERE archive=? AND shard=?",
            (int(time.time()), archive.archive_id, shard))
        db.log_event(con, "warn", "genesis",
                     f"{archive.archive_id}/{shard}: pass deferred — vector store "
                     f"busy ({type(e).__name__}); resumes next round")
        raise GenesisUnavailable(f"vector store busy: {type(e).__name__}") from e

    now = int(time.time())
    state = "applied"
    pending: int | None = None
    next_pass_at = 0
    if partial:
        # budget or stop: the walk did not reach the end of the shard, so
        # nothing is known about its coverage yet (pending NULL: due). A
        # budget cut resumes after an equal share of the thread for
        # rdf/fts; a stop (SIGTERM) resumes the moment the worker is back
        next_pass_at = now if stopped else now + int(budget or 0)
    elif revisit and not archive.local:
        # REMOTE, amendment 38: how many documents this shard could still
        # cover are waiting for fts? None → FINAL (files deleted); some →
        # revisit later, with a per-shard backoff that doubles after a
        # pass that imported nothing, so an idle shard costs less and
        # less while the active one is read as often as it pays.
        pending = _pending_for_shard(con, archive, shard, pending_probe)
        if pending == 0:
            state = "final"
            _bump_coverage_epoch()
        else:
            base = config.GENESIS_MAX_WAIT_SEC
            interval = base if docs else min(config.GENESIS_PASS_BACKOFF_MAX_SEC,
                                             max(base, prev_interval * 2))
            next_pass_at = now + interval
    # docs and chunks are NOT touched here: every flush added its own to
    # the row in the transaction that marked them. The ledger only grows —
    # the non-revisit branch used to OVERWRITE it with this one pass's
    # count (`docs=?`), erasing every earlier pass of the same shard.
    con.execute(
        "UPDATE genesis_imports SET state=?, detail=NULL, "
        "pending=?, next_pass_at=?, updated_at=? WHERE archive=? AND shard=?",
        (state, pending, next_pass_at, now, archive.archive_id, shard))
    db.log_event(con, "info", "genesis",
                 f"{archive.archive_id}/{shard}: imported {chunks_written} vectors "
                 f"across {docs} documents ({skipped_absent} not held locally, "
                 f"{skipped_shape} chunk-shape mismatches)"
                 + (f"; paused after {int(time.time() - t_start)}s, resumes in "
                    f"{next_pass_at - now}s" if partial
                    else f"; {pending} still indexing, next pass in {next_pass_at - now}s"
                    if state == "applied" and pending is not None
                    else "; FINAL" if state == "final" else ""))
    if not keep_cache and not archive.local and (state == "final" or not revisit):
        # local archives' files ARE the corpus — never delete them; a
        # remote shard that will be revisited stays on disk (cache-bounded
        # by genesis_fetch) until it is final
        for p in files:
            p.unlink(missing_ok=True)
        if state == "final":
            con.execute("UPDATE genesis_imports SET bytes=0 WHERE archive=? AND shard=?",
                        (archive.archive_id, shard))
    return {"shard": shard, "state": state, "docs": docs,
            "chunks": chunks_written, "skipped_absent": skipped_absent,
            "skipped_shape": skipped_shape, "residual_uids": residual_uids,
            "pending": pending, "partial": partial}


def _pending_for_shard(con, archive: GenesisArchive, shard: str,
                       pending_probe: dict) -> int:
    """Documents the shard could still cover that have not cleared fts.
    With DECLARED coverage it is one range count over the browse index
    (CAST(native_id AS INTEGER), built by the worker at startup); without
    it, the pass itself probed every id it saw (pending_probe)."""
    id_range = archive.id_range_of(shard)
    if id_range is None:
        return int(pending_probe.get("n", 0))
    lo, hi = id_range
    return int(con.execute(pending_in_range_sql(con), (lo, hi)).fetchone()[0])


def import_archive(con, collection, archive: GenesisArchive, *,
                   shards: list[str] | None = None, embed_fn=None,
                   fetch=None, stop=None, keep_cache: bool = False) -> dict:
    """Walk the archive's shards through import_shard. Resumable by
    construction (applied shards short-circuit); a rejected shard stops the
    walk — a systematically incompatible archive must not burn 36 more
    downloads to prove itself 36 more times."""
    results = []
    walk = shards or (list(_local_shards(collection)) if archive.local
                      else archive.shards)
    for shard in walk:
        if stop is not None and stop.is_set():
            break
        results.append(import_shard(con, collection, archive, shard,
                                     embed_fn=embed_fn, fetch=fetch,
                                     keep_cache=keep_cache,
                                     revisit=archive.local))
    return {"shards": results,
            "docs": sum(r.get("docs", 0) for r in results),
            "chunks": sum(r.get("chunks", 0) for r in results)}


#: Every archive-imported chunk's provenance row, in uid order — the one
#: walk `recount_ledger` makes. INDEXED BY the uid index, so the ORDER BY is
#: the index's own order (no TEMP B-TREE) and a document's chunks arrive
#: together, which is what counts DOCUMENTS without holding a set of
#: 35M uids. There is no index on bundle_id: every row is read once.
RECOUNT_SQL = ("SELECT uid, bundle_id FROM chunk_verification "
               "INDEXED BY idx_chunk_verification_uid "
               "WHERE substr(bundle_id, 1, 8) = 'genesis:' ORDER BY uid")


@dataclass(frozen=True)
class LedgerRecount:
    archive: str
    shard: str
    docs: int                # what the ledger says
    chunks: int
    held_docs: int           # what chunk_verification says
    held_chunks: int

    @property
    def changed(self) -> bool:
        return (self.docs, self.chunks) != (self.held_docs, self.held_chunks)


def recount_ledger(con, *, write: bool = False) -> list[LedgerRecount]:
    """Recompute every ledger row's docs and chunks from the provenance
    the flushes wrote — chunk_verification rows whose bundle_id is
    `genesis:<archive>/<shard>` — and, with `write`, set them.

    THE ONE-SHOT REPAIR for rows counted before the ledger moved into the
    flush's transaction: those passes lost every flush of a pass that did
    not reach its end (pubmed 2026-09-14: shards 8, 9 and 13, 80,654
    documents). chunk_verification is written in the SAME transaction as
    embed_done, so it is the record of what the archive actually delivered
    — a document the local encoder re-embedded has lost its rows
    (`ledger.clear_verification`) and is not credited. A shard with no
    provenance rows recounts to zero.

    THE LEDGER IS ALSO PROOF EVIDENCE: `row_proven` reads an applied or
    final row with docs > 0 as proven when `proven_at` is NULL (rows from
    before that column, and shards other than the one that proved). A
    recount that lowers such a row to zero would un-prove the archive, and
    one rejected shard would then STOP it (`archive_stopped`: the drain,
    the fetcher, and the worker's covered ranges). So before lowering, the
    proof is persisted: `proven_at` takes the row's `updated_at`.

    `write` takes BEGIN IMMEDIATE BEFORE the walk, so no flush lands
    between the count and the write; run it with the worker stopped. The
    walk reads every chunk_verification row once, in uid order (the uid
    index, then the row for its bundle_id): 58.7 s over 35M synthetic rows
    (6.5 GiB manifest, warm page cache, a laptop, 2026-09-14) — a floor for
    the node, not its figure. The node's figure is the dry run's, which the
    CLI prints; --yes holds the write lock for as long."""
    own_tx = write and not con.in_transaction
    if own_tx:
        con.execute("BEGIN IMMEDIATE")
    try:
        rows = {f"genesis:{r['archive']}/{r['shard']}": r for r in con.execute(
            "SELECT archive, shard, state, docs, chunks, proven_at, updated_at "
            "FROM genesis_imports ORDER BY archive, shard")}
        held_docs = dict.fromkeys(rows, 0)
        held_chunks = dict.fromkeys(rows, 0)
        last_uid, seen = None, set()
        for uid, bundle in con.execute(RECOUNT_SQL):
            if bundle not in rows:
                continue            # a shard with no ledger row: nothing to set
            if uid != last_uid:
                last_uid, seen = uid, set()
            held_chunks[bundle] += 1
            if bundle not in seen:
                seen.add(bundle)
                held_docs[bundle] += 1
        out = [LedgerRecount(r["archive"], r["shard"], int(r["docs"] or 0),
                             int(r["chunks"] or 0), held_docs[b], held_chunks[b])
               for b, r in rows.items()]
        if write:
            now = int(time.time())
            for rc in out:
                row = rows[f"genesis:{rc.archive}/{rc.shard}"]
                if rc.changed and not row["proven_at"] and row_proven(row):
                    con.execute(
                        "UPDATE genesis_imports SET proven_at=? WHERE archive=? AND shard=?",
                        (int(row["updated_at"] or now), rc.archive, rc.shard))
                if rc.changed:
                    # docs and chunks only: updated_at and next_pass_at are
                    # the revisit backoff's clock, not this repair's
                    con.execute(
                        "UPDATE genesis_imports SET docs=?, chunks=? "
                        "WHERE archive=? AND shard=?",
                        (rc.held_docs, rc.held_chunks, rc.archive, rc.shard))
            if any(rc.changed for rc in out):
                db.log_event(con, "info", "genesis",
                             "ledger recounted from chunk_verification: " + ", ".join(
                                 f"{rc.archive}/{rc.shard} docs {rc.docs}->{rc.held_docs}"
                                 for rc in out if rc.changed)[:360])
            if own_tx:
                con.execute("COMMIT")
    except BaseException:
        if own_tx and con.in_transaction:
            con.execute("ROLLBACK")
        raise
    return out


def reopen_finals_for_range(con, collection, native_ids) -> list[str]:
    """A FINAL shard whose id range just took a document through fts is
    due again. Returns the shards reopened.

    THE GAP THIS CLOSES. `final` means `_pending_for_shard` read zero —
    no document in the shard's range was still upstream of fts at that
    instant. It is set at the END of a pass, and the pass walked the
    shard's ids BEFORE it. A document that clears fts between the walk
    and the count is therefore counted as "not waiting" (it is past fts)
    while never having been offered the shard's vector — and a final
    shard is never walked again, so the vector the archive holds for it
    can never arrive. It falls to the local encoder instead, which is
    precisely the trade genesis exists to avoid, and under `embed = off`
    it gets nothing at all.

    Measured on pubmed 2026-09-13: all 37 shards final, 38,324 documents
    inside the archive's own id range unembedded, concentrated in the
    shards fts was working through as the archive drained (5,784 in shard
    36, 5,240 in 28, 4,423 in 33; 160 in shard 20, which fts had long
    left behind). No path existed by which any of them could be served.

    WHY AN EVENT AND NOT A POLL. The poll — recount each final shard's
    range and reopen the ones with unserved documents — costs a range
    count per shard per round and, worse, cannot converge: a document the
    archive genuinely does not contain is unserved forever, so the shard
    reopens forever while `_covered_ranges` keeps the local encoder out of
    its range. The event has neither problem. It fires only when fts
    actually crosses the range, which is the only way new demand can
    appear; the re-pass then imports what it can and finalises again, and
    a document the archive lacks generates no further event. Cost on the
    fts path is one read of a ≤ shard-count table with a primary key.

    Reopening is a ledger write only: `applied` with `pending` unknown is
    exactly the state the drain treats as due and the fetcher re-fetches
    when the files are gone (finalising deletes them), so both halves of
    amendment 38 pick it up with no other change. The coverage epoch is
    bumped because the range belongs to the archive again and the
    encoder's backfill cursors must re-reserve it.
    """
    archive = ARCHIVES.get(getattr(collection, "genesis_archive", "") or "")
    if archive is None or archive.local or not archive.ids_per_shard:
        return []
    ids = []
    for native in native_ids:
        try:
            ids.append(int(native))
        except (TypeError, ValueError):
            continue            # a non-numeric id belongs to no declared range
    if not ids:
        return []
    lo, hi = min(ids), max(ids)
    finals = [r["shard"] for r in con.execute(
        "SELECT shard FROM genesis_imports WHERE archive=? AND state='final'",
        (archive.archive_id,))]
    hit = []
    for shard in finals:
        rng = archive.id_range_of(shard)
        if rng is None or rng[1] < lo or hi < rng[0]:
            continue            # ranges cannot overlap this batch at all
        if any(rng[0] <= i <= rng[1] for i in ids):
            hit.append(shard)
    if not hit:
        return []
    con.execute(
        "UPDATE genesis_imports SET state='applied', pending=NULL, "
        "next_pass_at=0, updated_at=? WHERE archive=? AND state='final' "
        f"AND shard IN ({','.join('?' * len(hit))})",
        (int(time.time()), archive.archive_id, *hit))
    _bump_coverage_epoch()
    return hit


def reopen_finals_with_unserved(con, collection) -> dict[str, int]:
    """One-shot repair for shards that finalised BEFORE
    `reopen_finals_for_range` existed: count each final shard's unserved
    documents and reopen the ones with any. Returns {shard: count}.

    Deliberately NOT on any round's path — it pays one indexed range count
    per final shard, which is the cost the event above exists to avoid.
    `louise dev genesis-recheck` runs it once.
    """
    archive = ARCHIVES.get(getattr(collection, "genesis_archive", "") or "")
    if archive is None or archive.local or not archive.ids_per_shard:
        return {}
    finals = [r["shard"] for r in con.execute(
        "SELECT shard FROM genesis_imports WHERE archive=? AND state='final'",
        (archive.archive_id,))]
    found: dict[str, int] = {}
    for shard in finals:
        rng = archive.id_range_of(shard)
        if rng is None:
            continue
        n = int(con.execute(unserved_in_range_sql(con), rng).fetchone()[0])
        if n:
            found[shard] = n
    if found:
        con.execute(
            "UPDATE genesis_imports SET state='applied', pending=NULL, "
            "next_pass_at=0, updated_at=? WHERE archive=? AND state='final' "
            f"AND shard IN ({','.join('?' * len(found))})",
            (int(time.time()), archive.archive_id, *found))
        _bump_coverage_epoch()
    return found

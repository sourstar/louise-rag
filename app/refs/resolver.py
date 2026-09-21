"""Regex + dictionary entity resolution — cheap, deterministic (CLAUDE.md
invariant 3): a pure function of the reference tables' current content and
the query/doc text.

The symbol index is MEMOISED on the reference data's own version stamp, so
"pure function of the reference tables' current content" still holds exactly:
the cache key is that content's version. Only the recomputation is removed,
never a dependency. See _symbol_index.
"""
import re
import threading
from dataclasses import dataclass

from ..plugins.base import Entity, RefLookup

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]{1,15}")

#: A clinical significance, most actionable first: the ACMG/AMP tiers and the
#: compound terms ClinVar adds between them. `expand` already reads the
#: `significance` column for a fact's label; this is only its order. Anything
#: else — "not provided", a term not listed — comes after all of these.
CLINICAL_ORDER = (
    "Pathogenic",
    "Pathogenic/Likely pathogenic",
    "Likely pathogenic",
    "Conflicting classifications of pathogenicity",
    "Uncertain significance",
    "Likely benign",
    "Benign/Likely benign",
    "Benign",
)
_CLINICAL_RANK = ("CASE significance "
                  + " ".join(f"WHEN '{t}' THEN {i}" for i, t in enumerate(CLINICAL_ORDER))
                  + f" ELSE {len(CLINICAL_ORDER)} END")


@dataclass(frozen=True)
class Reached:
    """One declared link walked from a resolved entity: the rows pinned, and
    how many the link reaches in all — so what is not pinned is named in the
    answer, never dropped in silence (docs/05)."""
    table: str
    entities: tuple[Entity, ...]
    total: int


_cache_lock = threading.Lock()
_symbol_cache: tuple | None = None        # (stamp, symbols)


def invalidate_cache() -> None:
    """Drop the memoised symbol index. The stamp makes this unnecessary in
    normal operation; it exists so a test can force a rebuild."""
    global _symbol_cache
    with _cache_lock:
        _symbol_cache = None


def _stamp(refs: RefLookup):
    """Version of every reference source, via the RefLookup protocol itself
    (no extra method to implement). `sync_source` writes ref_meta whenever a
    source is reprocessed, so this changes exactly when the data does."""
    return tuple(sorted(
        (r["source_id"], r["ref_version"], r["synced_at"])
        for r in refs.rows("ref_meta")))


def _symbol_index(refs: RefLookup) -> dict:
    """hgnc_gene keyed by symbol, built once per reference-data version.

    This dict was previously rebuilt on EVERY call, which meant every
    document of every corpus. Measured live on this deployment: 45,040 HGNC
    rows loaded and hashed in 61.7 ms to perform 0.029 ms of matching — a
    2,149x waste, and 64% of the entire fts stage. At PubMed scale that is
    weeks of CPU spent rebuilding an identical dictionary.

    A RefLookup with no ref_meta (a hand-rolled test double) simply gets the
    old uncached behaviour rather than a stale answer.
    """
    global _symbol_cache
    try:
        stamp = _stamp(refs)
    except Exception:   # noqa: BLE001 — no ref_meta: correctness over speed
        return {r["symbol"]: r for r in refs.rows("hgnc_gene")}
    with _cache_lock:
        cached = _symbol_cache
    if cached is not None and cached[0] == stamp:
        return cached[1]
    # built outside the lock: two threads may duplicate the work once, which
    # costs a rebuild and can never produce a wrong answer, where holding the
    # lock for 60 ms would stall every pipeline thread behind it
    symbols = {r["symbol"]: r for r in refs.rows("hgnc_gene")}
    with _cache_lock:
        _symbol_cache = (stamp, symbols)
    return symbols


def _entity(refs: RefLookup, table: str, row: dict) -> Entity:
    """A row a link reached, as the entity `expand` and `expand_bounded` both
    return — one construction, so the two cannot disagree on a fact."""
    owner = refs.owner_of(table)
    return Entity(entity_id=f"{owner}:{row[refs.pk_of(table)]}", kind=table,
                  source_id=owner, label=row.get("significance") or row.get("name"))


class DictResolver:
    """Exact-case symbol matching against hgnc_gene. Case sensitivity is a
    deliberate precision choice: gene symbols routinely collide with
    ordinary English words (SET, WARS, CAT, KIT, ...) at full HGNC scale;
    requiring the exact stored case (all-caps, as HGNC symbols are) avoids
    the common false-positive failure mode at negligible recall cost, since
    genuinely gene-mentioning queries almost always use the symbol's
    canonical case. A production-scale resolver would need more (minimum
    length, stopword list, adjacency to a coordinate/rsID token) — out of
    scope here.
    """

    def resolve(self, query: str, refs: RefLookup) -> list[Entity]:
        symbols = _symbol_index(refs)
        seen: set[str] = set()
        out: list[Entity] = []
        for tok in _TOKEN.findall(query):
            if tok in symbols and tok not in seen:
                seen.add(tok)
                row = symbols[tok]
                out.append(Entity(entity_id=f"hgnc:{row['hgnc_id']}", kind="hgnc_gene",
                                   source_id="hgnc", label=row["symbol"]))
        return out

    def expand(self, entity: Entity, refs: RefLookup) -> list[Entity]:
        """Traverses declared links only. Only the "walk from a table toward
        the tables that reference it" direction is implemented (entity.kind
        as a link's to_table) — the only direction any current RefLink/test
        needs (gene -> its ClinVar variants); the reverse (variant -> its
        gene) would need entity.kind as a link's from_table and isn't wired
        up since nothing exercises it yet."""
        _, _, native = entity.entity_id.partition(":")
        row = refs.get(entity.kind, native)
        if row is None:
            return []
        out: list[Entity] = []
        for link in refs.links_for(entity.kind):
            if link.to_table != entity.kind:
                continue
            for r in refs.rows(link.from_table, {link.from_column: row[link.to_column]}):
                out.append(_entity(refs, link.from_table, r))
        return out

    def expand_bounded(self, entity: Entity, refs: RefLookup, limit: int) -> list[Reached]:
        """`expand`, one declared link at a time, with at most `limit` rows
        materialised per link — the most clinically actionable first where
        the table carries a `significance`, else by primary key — and the
        link's full count beside them.

        `expand` materialises every row a link reaches. For BRCA2 that is
        21,961 ClinVar variants (plus 1 gnomAD row and 6 HomoloGene), 668 ms
        on the node — spent every time a search named the gene, to pin facts
        nobody could read. Bounded, the same three links cost 24.6 ms
        (2026-09-21). ClinVar's order still sorts that gene's rows (no index
        serves a CASE), so it grows as n log n with the gene: tens of ms."""
        _, _, native = entity.entity_id.partition(":")
        row = refs.get(entity.kind, native)
        if row is None:
            return []
        out: list[Reached] = []
        for link in refs.links_for(entity.kind):
            if link.to_table != entity.kind:
                continue
            table = link.from_table
            where = {link.from_column: row[link.to_column]}
            pk = refs.pk_of(table)
            order = f"{_CLINICAL_RANK}, {pk}" if "significance" in refs.columns(table) else pk
            out.append(Reached(
                table=table,
                entities=tuple(_entity(refs, table, r)
                               for r in refs.rows(table, where, order_by=order, limit=limit)),
                total=refs.count(table, where)))
        return out


def llm_fallback_resolve(query: str, refs: RefLookup) -> list[Entity]:
    """Named hook for architecture-v2.md §4's LLM fallback ("ten lines where
    llm.translate_query lives") for queries with no explicit identifier.
    Explicit NON-GOAL for P0.3: never called by default. An LLM call is
    inherently non-deterministic and must not be allowed to silently affect
    invariant-3 determinism or the degeneration test's guarantee. Raises
    until a future task wires it behind an explicit opt-in.
    """
    raise NotImplementedError(
        "LLM entity fallback is a non-goal for P0.3 (architecture-v2.md §4)")

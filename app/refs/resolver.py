"""Regex + dictionary entity resolution — cheap, deterministic (CLAUDE.md
invariant 3): a pure function of the reference tables' current content and
the query/doc text, rebuilt on every call, no hidden state.
"""
import re

from ..plugins.base import Entity, RefLookup

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]{1,15}")


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
        symbols = {r["symbol"]: r for r in refs.rows("hgnc_gene")}
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
                table = link.from_table
                pk = refs.pk_of(table)
                out.append(Entity(
                    entity_id=f"{refs.owner_of(table)}:{r[pk]}",
                    kind=table,
                    source_id=refs.owner_of(table),
                    label=r.get("significance") or r.get("name")))
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

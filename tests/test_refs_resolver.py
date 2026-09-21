"""app/refs/resolver.py: DictResolver.resolve()/expand() against synced
hgnc/clinvar-mini fixtures.
"""
from pathlib import Path

from app import refs
from app.plugins.base import Entity
from app.refs import store as refs_store

FIXTURE_REFS = Path(__file__).parent / "fixtures" / "refs-mini"


def _synced_store(tmp_path):
    con = refs.connect(tmp_path / "refs.db")
    refs_store.sync_all(con, FIXTURE_REFS)
    return refs.RefStore(con, [s.schema() for s in refs.REFS_REGISTRY.values()])


def test_resolve_finds_gene_symbol(tmp_path):
    store = _synced_store(tmp_path)
    entities = refs.RESOLVER.resolve("What is known about BRCA2 c.658_659del?", store)
    assert entities == [Entity("hgnc:1101", "hgnc_gene", "hgnc", "BRCA2")]


def test_resolve_no_match_returns_empty(tmp_path):
    store = _synced_store(tmp_path)
    assert refs.RESOLVER.resolve("a quiet walk through the village", store) == []


def test_resolve_is_case_sensitive(tmp_path):
    store = _synced_store(tmp_path)
    assert refs.RESOLVER.resolve("brca2 lowercase should not match", store) == []


def test_resolve_dedups_repeated_symbol(tmp_path):
    store = _synced_store(tmp_path)
    entities = refs.RESOLVER.resolve("BRCA2 and BRCA2 again", store)
    assert len(entities) == 1


def test_expand_follows_variant_to_gene_link(tmp_path):
    store = _synced_store(tmp_path)
    gene = Entity("hgnc:1101", "hgnc_gene", "hgnc", "BRCA2")
    expanded = refs.RESOLVER.expand(gene, store)
    assert len(expanded) == 1
    variant = expanded[0]
    assert variant.entity_id == "clinvar-mini:55074"
    assert variant.kind == "clinvar_variant"
    assert variant.source_id == "clinvar-mini"
    assert variant.label == "Pathogenic"


def test_expand_unknown_entity_returns_empty(tmp_path):
    store = _synced_store(tmp_path)
    fake = Entity("hgnc:999999", "hgnc_gene", "hgnc", "NOPE")
    assert refs.RESOLVER.expand(fake, store) == []


# ---- expand_bounded: docs/05's one hop, bounded -------------------------------------------
#
# The node, 2026-09-21: BRCA2 expanded to 21,968 entities (21,961 ClinVar
# variants, 1 gnomAD, 6 HomoloGene), every one a pinned fact, 668 ms to
# materialise. docs/05 wants the hop; nobody can read 21,969 cards.

def _brca2_with_variants(tmp_path, significances):
    store = _synced_store(tmp_path)
    store.con.executemany(
        "INSERT INTO clinvar_variant(variant_id, gene_symbol, significance, description) "
        "VALUES (?, 'BRCA2', ?, 'synthetic')",
        [(str(900 + i), s) for i, s in enumerate(significances)])
    store.con.commit()
    gene = refs.RESOLVER.resolve("BRCA2", store)[0]
    return store, gene


def _clinvar(reached):
    return next(r for r in reached if r.table == "clinvar_variant")


def test_the_most_actionable_come_first_and_the_rest_are_counted(tmp_path):
    store, gene = _brca2_with_variants(
        tmp_path, ["Benign", "not provided", "Likely pathogenic", "Uncertain significance",
                   "Pathogenic/Likely pathogenic", "Conflicting classifications of pathogenicity"])
    link = _clinvar(refs.RESOLVER.expand_bounded(gene, store, 3))
    assert [e.label for e in link.entities] == [
        "Pathogenic",                       # the fixture's own 55074
        "Pathogenic/Likely pathogenic",
        "Likely pathogenic"]
    assert link.total == 7, "all seven counted, three pinned"


def test_within_one_significance_the_order_is_the_primary_key(tmp_path):
    store, gene = _brca2_with_variants(tmp_path, ["Pathogenic"] * 4)
    ids = [e.entity_id for e in _clinvar(refs.RESOLVER.expand_bounded(gene, store, 5)).entities]
    assert ids == sorted(ids), "same query, same facts: a deterministic order (invariant 3)"


def test_each_link_is_bounded_on_its_own(tmp_path):
    store, gene = _brca2_with_variants(tmp_path, ["Benign"] * 10)
    reached = {r.table: r for r in refs.RESOLVER.expand_bounded(gene, store, 2)}
    assert set(reached) == {"clinvar_variant", "gnomad_constraint", "homologene"}
    assert len(reached["clinvar_variant"].entities) == 2 and reached["clinvar_variant"].total == 11
    for r in reached.values():
        assert len(r.entities) == min(2, r.total)


def test_never_more_than_the_bound_is_read(tmp_path):
    """The cost, not only the answer: `expand` read every row a link reached.
    Every read here carries the bound, so 21,961 variants cost 25 rows."""
    store, gene = _brca2_with_variants(tmp_path, ["Benign"] * 30)
    reads = []
    rows = store.rows

    def recording(table, where=None, **kw):
        reads.append((table, kw.get("limit")))
        return rows(table, where, **kw)

    store.rows = recording
    refs.RESOLVER.expand_bounded(gene, store, 4)
    assert reads and all(limit == 4 for _, limit in reads), reads


def test_expand_itself_is_unchanged_and_still_reaches_everything(tmp_path):
    store, gene = _brca2_with_variants(tmp_path, ["Benign"] * 5)
    everything = refs.RESOLVER.expand(gene, store)
    bounded = refs.RESOLVER.expand_bounded(gene, store, 10_000)
    assert {e.entity_id for e in everything} == {e.entity_id for r in bounded for e in r.entities}

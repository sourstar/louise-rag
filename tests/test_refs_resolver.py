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

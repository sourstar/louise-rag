"""The entities a query names: pinned as facts, and — 05 §5.2 — never a boost.

The node, 2026-09-21: a PubMed search never came back. The boost (+1/60 on
the fused score of a candidate the query's entities tag) fetched EVERY
document tagged with them before searching; on a manifest nobody had
ANALYZEd the planner drove the loop from all 40.0 M documents, and one
search was still running after 75 minutes. Asked for the candidates alone
after that, it cost nothing — and it was still a boost, which 05 §5.2 never
allowed. Measured on TREC Precision Medicine 2017–18 (80 topics, NIST's
judgements; tools/measure/entity_boost_trecpm.py, 2026-10-25) it lost:
NDCG@10 0.3073 against 0.3143 without it. It is gone.

And the expansion fed it: "BRCA2 breast cancer risk" resolved to the gene and
expanded to 21,968 ClinVar variants — each a pinned fact in the answer, and
not ONE of them tagging a single PubMed document. So the legs are handed
only what the query names (what 05 §5.2's restriction would take, if the
operator keeps it). The facts keep docs/05's one hop — P0.3 accepted "a
BRCA2 query returns pinned HGNC/ClinVar facts" — bounded per link, the rest
named in `facts_withheld` (operator, the same night: follow docs/05,
bounded).
"""
import shutil
from pathlib import Path

from app import collections as collections_mod, db, discovery, retrieval
from app.plugins.base import Entity, RefLink
from app.refs.resolver import Reached

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"
GENE_ID = "hgnc:HGNC:1101"


def _manifest(tmp_path):
    corpus = tmp_path / "boost_corpus"          # isolated_config already owns "corpus"
    shutil.copytree(FIXTURE_CORPUS, corpus)
    c = collections_mod.Collection(
        name="pubmedish", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / "m" / "manifest.db",
        fts_dir=tmp_path / "m" / "fts", lance_dir=tmp_path / "m" / "lancedb")
    con = db.connect(c)
    discovery.sync_collection(con, c)
    return c, con


def _more_documents(con, c, n: int, first: int = 90_000) -> list[int]:
    ids = list(range(first, first + n))
    con.executemany("INSERT INTO documents(uid, source, native_id, path) VALUES (?,?,?,?)",
                    [(f"{c.plugin}:{i}", c.plugin, str(i), "synthetic") for i in ids])
    con.commit()
    return ids


def _tag(con, c, book_ids, entity_id: str = GENE_ID) -> None:
    con.executemany("INSERT OR IGNORE INTO doc_keys(uid, entity_id) VALUES (?, ?)",
                    [(f"{c.plugin}:{b}", entity_id) for b in book_ids])
    con.commit()


class _Plan:
    """One language group, searched with the query as typed."""
    core = "BRCA2"
    translations: dict = {}
    query_lang = None
    only = ()

    def searches(self, group):
        return True

    def variants(self, group, held):
        return [self.core]

    def lang_filter(self, group):
        return None


# ---- nothing boosts -------------------------------------------------------------------------

def test_a_candidate_the_named_entity_tags_keeps_its_fused_score(isolated_config, monkeypatch, tmp_path):
    """05 §5.2: "a filter restricts; nothing boosts". The third candidate
    carries the gene the query names; it stays third, at 1/(60+3), exactly
    as when the query names nothing — the boost would have put it first."""
    c, con = _manifest(tmp_path)
    ids = _more_documents(con, c, 3)
    _tag(con, c, ids[2:])
    hits = [{"chunk_id": f"{c.plugin}:{b}:0:1", "book_id": b, "source": "fts:g"} for b in ids]
    monkeypatch.setattr(retrieval.fts, "existing_groups", lambda coll: ["g"])
    monkeypatch.setattr(retrieval.fts, "group_languages", lambda group, coll: ["en"])
    monkeypatch.setattr(retrieval.fts, "names_capable", lambda coll: False)
    monkeypatch.setattr(retrieval.fts, "search_records", lambda group, q, limit, **kw: [dict(h) for h in hits])
    monkeypatch.setattr(retrieval.collections_mod, "reranker_for", lambda coll: None)

    def fused(entity_ids):
        out = retrieval._search_one_collection(con, c, "BRCA2", 5, "fts", False, entity_ids, _Plan())
        return [(m["book_id"], m["rrf"]) for m in out["merged"]]

    plain = fused([])
    assert plain == [(b, 1.0 / (retrieval.RRF_K + r)) for r, b in enumerate(ids, 1)]
    assert fused([GENE_ID]) == plain


# ---- the legs are handed what the query names; the facts take docs/05's bounded hop ---------

GENE = Entity(entity_id=GENE_ID, kind="hgnc_gene", source_id="hgnc", label="BRCA2")
PINNED = tuple(Entity(entity_id=f"clinvar-mini:{i}", kind="clinvar_variant",
                      source_id="clinvar-mini", label="Pathogenic")
               for i in range(retrieval.FACTS_PER_LINK))
TO_GENE = RefLink(name="variant_to_gene", from_table="clinvar_variant", from_column="gene_symbol",
                  to_table="hgnc_gene", to_column="symbol")
ORTHOLOG = RefLink(name="ortholog_to_gene", from_table="homologene", from_column="gene_symbol",
                   to_table="hgnc_gene", to_column="symbol")
VIA_GENE = {"from": GENE_ID, "link": "variant_to_gene",
            "on": "clinvar_variant.gene_symbol → hgnc_gene.symbol"}


class _Store:
    def get(self, kind, native):
        return {"kind": kind}


def _names_brca2(monkeypatch):
    """BRCA2 as the node has it: 21,961 ClinVar variants behind one link, of
    which the bound pins FACTS_PER_LINK."""
    monkeypatch.setattr(retrieval.refs, "get_store", lambda: _Store())
    monkeypatch.setattr(retrieval.refs.RESOLVER, "resolve", lambda q, s: [GENE])
    monkeypatch.setattr(retrieval.refs.RESOLVER, "expand_bounded", lambda e, s, limit: [
        Reached(table="clinvar_variant", entities=PINNED[:limit], total=21_961, link=TO_GENE),
        Reached(table="homologene", entities=(), total=0, link=ORTHOLOG)])
    named: list[list[str]] = []

    def one_collection(con, c, q, k, mode, tr, entity_ids, plan=None, depth=0):
        named.append(list(entity_ids))
        return {"catalog": [], "merged": [], "translations": {},
                "timings": {"catalog": 0.0}, "scope": {}}

    monkeypatch.setattr(retrieval, "_search_one_collection", one_collection)
    monkeypatch.setattr(retrieval, "_attach_work_fields", lambda con, c, hits, q: hits)
    monkeypatch.setattr(retrieval, "_attach_metadata_and_enqueue", lambda con, c, hits: None)
    return named


def _check(out, named) -> None:
    assert named == [[GENE_ID]], "the legs: the gene alone, not its variants"
    facts = [f["entity_id"] for f in out["facts"]]
    assert facts[0] == GENE_ID and len(facts) == 1 + retrieval.FACTS_PER_LINK, \
        "the facts: the gene, and docs/05's one hop — bounded"
    assert out["facts_withheld"] == [{"entity_id": GENE_ID, "table": "clinvar_variant",
                                      "reached": 21_961, "pinned": retrieval.FACTS_PER_LINK}], \
        "what the bound left out is named, never dropped in silence"
    assert out["facts"][0]["via"] is None, "the query named the gene"
    assert [f["via"] for f in out["facts"][1:]] == [VIA_GENE] * retrieval.FACTS_PER_LINK, \
        "every fact expand reached names the link it was reached through (05 §5.4)"


def test_search_hands_the_legs_the_named_gene_and_pins_a_bounded_hop(
        isolated_config, monkeypatch, tmp_path):
    c, con = _manifest(tmp_path)
    named = _names_brca2(monkeypatch)
    _check(retrieval.search(con, "BRCA2 breast cancer risk", k=5, collection=c,
                            translate=False), named)


def test_federated_search_follows_the_same_rule(isolated_config, monkeypatch, tmp_path):
    """The two paths came in a pair; fixing one would have left the other
    pinning every variant."""
    c, _ = _manifest(tmp_path)
    named = _names_brca2(monkeypatch)
    monkeypatch.setattr(retrieval, "_rerank_budget_s", lambda depth=50: 0.4)
    _check(retrieval.federated_search([c], "BRCA2 breast cancer risk", k=5, translate=False),
           named)


def test_nothing_is_withheld_when_nothing_is_left_out(isolated_config, monkeypatch, tmp_path):
    c, con = _manifest(tmp_path)
    monkeypatch.setattr(retrieval.refs, "get_store", lambda: None)
    monkeypatch.setattr(retrieval.refs.RESOLVER, "resolve", lambda q, s: [])
    out = retrieval.search(con, "a quiet walk through the village", k=5, collection=c,
                           translate=False)
    assert out["facts"] == [] and out["facts_withheld"] == []


def test_an_entity_the_query_names_is_named_even_when_a_link_also_reaches_it(monkeypatch):
    """One fact per entity, and its `via` says what is true of the QUESTION:
    named outright, it did not reach the answer through anything."""
    variant = PINNED[0]
    monkeypatch.setattr(retrieval.refs.RESOLVER, "resolve", lambda q, s: [GENE, variant])
    monkeypatch.setattr(retrieval.refs.RESOLVER, "expand_bounded", lambda e, s, limit: [
        Reached(table="clinvar_variant", entities=(variant,), total=1, link=TO_GENE)]
        if e is GENE else [])
    _, facts, _ = retrieval._entity_facts("BRCA2 and one of its variants", _Store())
    assert [(f.entity_id, f.via) for f in facts] == [(GENE_ID, None), (variant.entity_id, None)]

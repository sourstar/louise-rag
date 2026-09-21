"""The entity boost: which of a search's candidates the named entities tag.

The node, 2026-09-21: a PubMed search never came back. The boost fetched
EVERY document tagged with the query's entities before searching, joining
`doc_keys` to `documents`; a manifest nobody has ANALYZEd has no statistics,
so the planner took `d.source = ?` for a selective filter and drove the loop
from all 40.0 M documents. One search was still running after 75 minutes,
its caller long gone. ONE entity was enough to do it.

Fixing the join order took BRCA2 to 2.5 s — and left the cost scaling with
the entity: BRCA2 tags 12,053 documents, the heaviest gene 265,237. The boost
only ever asks whether a CANDIDATE is tagged, so that is now the question,
put after the merge, for the candidates alone.

And the expansion fed it: "BRCA2 breast cancer risk" resolved to the gene and
expanded to 21,968 ClinVar variants — each a pinned fact in the answer, and
not ONE of them tagging a single PubMed document (the gene alone and all
21,969 tag the same 12,053). So the boost takes only what the query names.
The facts keep docs/05's one hop — P0.3 accepted "a BRCA2 query returns
pinned HGNC/ClinVar facts" — bounded per link, the rest named in
`facts_withheld` (operator, the same night: follow docs/05, bounded).

Plans are checked on a fixture manifest, which has no statistics either: the
planner guesses as it did on the node, and the fixture reproduced the node's
exact plan when the old query was put back.
"""
import shutil
from pathlib import Path

from app import collections as collections_mod, db, discovery, retrieval
from app.plugins.base import Entity
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


class _Recording:
    """The connection, noting every statement: the plan is taken of the SQL
    the function really runs, never of a copy of it in this file."""

    def __init__(self, con) -> None:
        self.con, self.seen = con, []

    def execute(self, sql, params=()):
        self.seen.append((sql, list(params)))
        return self.con.execute(sql, params)


def _plan_rows(con, sql, params) -> list[str]:
    return [str(r[-1]).upper() for r in con.execute("EXPLAIN QUERY PLAN " + sql, params)]


# ---- the question, put for the candidates ------------------------------------------------

def test_the_loop_runs_over_the_candidates_never_the_collection(tmp_path):
    c, con = _manifest(tmp_path)
    rec = _Recording(con)
    retrieval._tagged_among(rec, c, [GENE_ID], [10001, 10002, 10003])
    (sql, params), = rec.seen
    plan = _plan_rows(con, sql, params)

    assert "IDX_DOCUMENTS_NATIVE (SOURCE=? AND NATIVE_ID=?)" in plan[0], \
        f"the outer loop must be the candidates, one row each: {plan}"
    assert not any(row.endswith("(SOURCE=?)") for row in plan), \
        f"a search on `source` alone is a walk of the whole collection: {plan}"
    assert not any("TEMP B-TREE" in row or "SCAN" in row for row in plan), plan


def test_the_cost_does_not_grow_with_how_many_documents_the_entity_tags(tmp_path):
    """Counted in SQLite's own VM steps. 400 documents tagged or one, the
    same three candidates cost the same: the heaviest gene on the node tags
    265,237 documents, and a boost that paid per tagged document paid that."""
    c, con = _manifest(tmp_path)
    ids = _more_documents(con, c, 400)
    candidates = ids[:3]

    def steps() -> int:
        n = [0]

        def tick() -> int:
            n[0] += 1
            return 0
        con.set_progress_handler(tick, 1)
        try:
            found = retrieval._tagged_among(con, c, [GENE_ID], candidates)
        finally:
            con.set_progress_handler(None, 1)
        assert found == {candidates[0]}
        return n[0]

    _tag(con, c, [candidates[0]])
    one = steps()
    _tag(con, c, ids[3:])                       # 397 more, none of them a candidate
    many = steps()
    assert many <= one * 1.2, f"tagging 397 more documents cost {many} steps against {one}"


def test_a_tagged_candidate_is_found_once_and_nothing_else_is(tmp_path):
    c, con = _manifest(tmp_path)
    ids = _more_documents(con, c, 5)
    _tag(con, c, ids[:1])
    _tag(con, c, ids[:1], "hgnc:HGNC:1100")     # a second key on the same document
    _tag(con, c, ids[4:])                       # tagged, but not a candidate
    assert retrieval._tagged_among(con, c, [GENE_ID, "hgnc:HGNC:1100"], ids[:3]) \
        == {ids[0]}, "no DISTINCT in SQL: the set collapses the repeat"
    assert retrieval._tagged_among(con, c, [], ids) == set()
    assert retrieval._tagged_among(con, c, [GENE_ID], []) == set()


# ---- the boost takes what the query names; the facts take docs/05's bounded hop -------------

GENE = Entity(entity_id=GENE_ID, kind="hgnc_gene", source_id="hgnc", label="BRCA2")
PINNED = tuple(Entity(entity_id=f"clinvar-mini:{i}", kind="clinvar_variant",
                      source_id="clinvar-mini", label="Pathogenic")
               for i in range(retrieval.FACTS_PER_LINK))


class _Store:
    def get(self, kind, native):
        return {"kind": kind}


def _names_brca2(monkeypatch):
    """BRCA2 as the node has it: 21,961 ClinVar variants behind one link, of
    which the bound pins FACTS_PER_LINK."""
    monkeypatch.setattr(retrieval.refs, "get_store", lambda: _Store())
    monkeypatch.setattr(retrieval.refs.RESOLVER, "resolve", lambda q, s: [GENE])
    monkeypatch.setattr(retrieval.refs.RESOLVER, "expand_bounded", lambda e, s, limit: [
        Reached(table="clinvar_variant", entities=PINNED[:limit], total=21_961),
        Reached(table="homologene", entities=(), total=0)])
    boosted: list[list[str]] = []

    def one_collection(con, c, q, k, mode, tr, entity_ids, plan=None, depth=0):
        boosted.append(list(entity_ids))
        return {"catalog": [], "merged": [], "translations": {},
                "timings": {"catalog": 0.0}, "scope": {}}

    monkeypatch.setattr(retrieval, "_search_one_collection", one_collection)
    monkeypatch.setattr(retrieval, "_attach_work_fields", lambda con, c, hits, q: hits)
    monkeypatch.setattr(retrieval, "_attach_metadata_and_enqueue", lambda con, c, hits: None)
    return boosted


def _check(out, boosted) -> None:
    assert boosted == [[GENE_ID]], "the boost: the gene alone, not its variants"
    facts = [f["entity_id"] for f in out["facts"]]
    assert facts[0] == GENE_ID and len(facts) == 1 + retrieval.FACTS_PER_LINK, \
        "the facts: the gene, and docs/05's one hop — bounded"
    assert out["facts_withheld"] == [{"entity_id": GENE_ID, "table": "clinvar_variant",
                                      "reached": 21_961, "pinned": retrieval.FACTS_PER_LINK}], \
        "what the bound left out is named, never dropped in silence"


def test_search_boosts_the_named_gene_and_pins_a_bounded_hop(
        isolated_config, monkeypatch, tmp_path):
    c, con = _manifest(tmp_path)
    boosted = _names_brca2(monkeypatch)
    _check(retrieval.search(con, "BRCA2 breast cancer risk", k=5, collection=c,
                            translate=False), boosted)


def test_federated_search_follows_the_same_rule(isolated_config, monkeypatch, tmp_path):
    """The two paths came in a pair; fixing one would have left the other
    pinning and boosting every variant."""
    c, _ = _manifest(tmp_path)
    boosted = _names_brca2(monkeypatch)
    monkeypatch.setattr(retrieval, "_rerank_budget_s", lambda depth=50: 0.4)
    _check(retrieval.federated_search([c], "BRCA2 breast cancer risk", k=5, translate=False),
           boosted)


def test_nothing_is_withheld_when_nothing_is_left_out(isolated_config, monkeypatch, tmp_path):
    c, con = _manifest(tmp_path)
    monkeypatch.setattr(retrieval.refs, "get_store", lambda: None)
    monkeypatch.setattr(retrieval.refs.RESOLVER, "resolve", lambda q, s: [])
    out = retrieval.search(con, "a quiet walk through the village", k=5, collection=c,
                           translate=False)
    assert out["facts"] == [] and out["facts_withheld"] == []

"""MEASUREMENT (not product code): the entity boost (`retrieval.ENTITY_BOOST`,
1/60 added to the fused score of a candidate the query's entity tags) against
05 §5.2's "a filter restricts; nothing boosts" — task #33, found by PR #4.

The labelled set is a standard one, not ours: TREC Precision Medicine 2017
and 2018, scientific abstracts (topics naming a disease and a gene or
variant; relevance 0/1/2 judged by NIST assessors on PubMed abstracts,
independent of Louise's tagging). The files are TREC's own, as ir_datasets
catalogues them (`medline/trec-pm-2017`, `medline/trec-pm-2018`):
  https://trec.nist.gov/data/precmed/topics2017.xml
  https://trec.nist.gov/data/precmed/qrels-final-abstracts.txt
  http://www.trec-cds.org/topics2018.xml
  https://trec.nist.gov/data/precmed/qrels-treceval-abstracts-2018-v2.txt
Judged documents that are not PubMed (2017's AACR/ASCO abstracts) are left
out. The query is the participants' common baseline: disease, then gene.

Two steps:
  1. prepare (anywhere): `python tools/measure/entity_boost_trecpm.py prepare
     --pm 2017 topics2017.xml qrels-final-abstracts.txt
     --pm 2018 topics2018.xml qrels-treceval-abstracts-2018-v2.txt > data.json`
  2. search (on the node, the deployed path, reranker on): the script reaches
     the node on stdin with `DATA = <data.json>` prepended, as the campaign's
     other harnesses do. Each topic is searched once per ARM (the boost as
     deployed, and 0), the order alternating per topic; nothing is written.
One JSON line per topic on stdout; the summary on stderr.

Metrics per topic and arm, at k = 10 unless said: NDCG (trec_eval's
ndcg_cut: gain = the grade, log2 discount, unjudged counted as 0), the
CONDENSED NDCG (Sakai 2007: unjudged documents removed before ranking — the
standard answer to a pool Louise did not contribute to), P (grade >= 1), RR,
the share of the top 10 that is judged, and R@50. A topic counts only when
both arms were reranked; the boost can only differ where it APPLIED (a
candidate tagged with an entity the query named), and that subset is
reported apart.
"""
from __future__ import annotations

import json
import math
import re
import sys
import xml.etree.ElementTree as ET

BOOSTS = {"boost": None, "none": 0.0}      # None: as deployed
K = 10
DEPTH = 50


# ---- prepare: TREC's files -> DATA -------------------------------------------------------

def parse_topics(xml_text: str, year: int) -> list[dict]:
    """A TREC PM topics file -> [{qid, disease, gene, query}]; `qid` is
    `<year>-<number>` so two years never collide."""
    out = []
    for t in ET.fromstring(xml_text).iter("topic"):
        disease = (t.findtext("disease") or "").strip()
        gene = (t.findtext("gene") or "").strip()
        out.append({"qid": f"{year}-{t.get('number')}", "disease": disease, "gene": gene,
                    "query": " ".join(x for x in (disease, gene) if x)})
    return out


def parse_qrels(text: str, year: int) -> dict[str, dict[str, int]]:
    """TREC qrels (`topic iteration docid grade`) -> {qid: {pmid: grade}};
    a docid that is not a PubMed id is left out."""
    out: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 4 or not parts[2].isdigit():
            continue
        out.setdefault(f"{year}-{parts[0]}", {})[parts[2]] = int(parts[3])
    return out


def prepare(pairs: list[tuple[int, str, str]]) -> list[dict]:
    """[(year, topics text, qrels text)] -> DATA: the topics with judged
    PubMed documents, each with its `rels`."""
    data = []
    for year, topics, qrels in pairs:
        rels = parse_qrels(qrels, year)
        for t in parse_topics(topics, year):
            if any(g > 0 for g in rels.get(t["qid"], {}).values()):
                data.append({**t, "rels": rels[t["qid"]]})
    return data


# ---- metrics --------------------------------------------------------------------------------

def _dcg(grades: list[int]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(grades))


def metrics(ranked: list[str], rels: dict[str, int], k: int = K, depth: int = DEPTH) -> dict:
    """One ranked list of PMIDs against one topic's judgements."""
    grades = [rels.get(p, 0) for p in ranked[:k]]
    ideal = sorted((g for g in rels.values() if g > 0), reverse=True)[:k]
    idcg = _dcg(ideal)
    judged = [p for p in ranked if p in rels]
    cgrades = [rels[p] for p in judged[:k]]
    rr = next((1.0 / (i + 1) for i, p in enumerate(ranked[:k]) if rels.get(p, 0) > 0), 0.0)
    relevant = {p for p, g in rels.items() if g > 0}
    return {"ndcg": round(_dcg(grades) / idcg, 4) if idcg else 0.0,
            "ndcg_condensed": round(_dcg(cgrades) / idcg, 4) if idcg else 0.0,
            "p": round(sum(1 for g in grades if g > 0) / k, 4),
            "rr": round(rr, 4),
            "judged": round(sum(1 for p in ranked[:k] if p in rels) / k, 4),
            "r_depth": round(len(relevant & set(ranked[:depth])) / len(relevant), 4) if relevant else 0.0}


def summarise(rows: list[dict]) -> dict:
    """Means per arm over the topics both arms reranked; the same over the
    topics where the boost applied; and per metric, the topics where the
    boost won, lost or tied."""
    both = [r for r in rows if not r.get("error") and all(r["reranked"].values())]
    applied = [r for r in both if r["applied"]]
    out = {"topics": len(rows), "counted": len(both), "boost_applied": len(applied)}
    for name, subset in (("all", both), ("applied", applied)):
        block = {}
        for m in ("ndcg", "ndcg_condensed", "p", "rr", "judged", "r_depth"):
            a = [r["arms"]["boost"][m] for r in subset]
            b = [r["arms"]["none"][m] for r in subset]
            if not subset:
                continue
            block[m] = {"boost": round(sum(a) / len(a), 4), "none": round(sum(b) / len(b), 4),
                        "boost_won": sum(x > y for x, y in zip(a, b)),
                        "boost_lost": sum(x < y for x, y in zip(a, b)),
                        "tied": sum(x == y for x, y in zip(a, b))}
        out[name] = block
    return out


def pmid_of(hit: dict) -> str:
    """A pubmed hit's PMID: its record uid is `pubmed:<pmid>`."""
    record = str(hit["chunk_id"]).rsplit(":", 2)[0]
    return record.split(":", 1)[1] if ":" in record else record


# ---- search: on the node ------------------------------------------------------------------

def run(data: list[dict], collection: str = "pubmed") -> list[dict]:
    from app import query_service, retrieval
    deployed = retrieval.ENTITY_BOOST
    seen: dict = {}
    orig_tagged, orig_reranked = retrieval._tagged_among, retrieval._reranked

    def tagged(*a, **kw):
        got = orig_tagged(*a, **kw)
        seen["applied"] = seen.get("applied", False) or bool(got)
        return got

    def reranked(*a, **kw):
        out = orig_reranked(*a, **kw)
        seen["reranked"] = seen.get("reranked", True) and bool(out[1])
        seen.setdefault("why", out[2])
        return out
    retrieval._tagged_among, retrieval._reranked = tagged, reranked
    rows = []
    try:
        for n, topic in enumerate(data):
            row = {"qid": topic["qid"], "query": topic["query"], "arms": {}, "reranked": {},
                   "applied": False, "error": None}
            order = list(BOOSTS) if n % 2 == 0 else list(reversed(BOOSTS))
            for arm in order:
                retrieval.ENTITY_BOOST = deployed if BOOSTS[arm] is None else BOOSTS[arm]
                seen.clear()
                try:
                    hits = query_service.run_search([collection], topic["query"], DEPTH, "hybrid", True)["hits"]
                except Exception as e:   # noqa: BLE001 — recorded, the next topic goes on
                    row["error"] = f"{arm}: {type(e).__name__}: {e}"[:200]
                    break
                row["arms"][arm] = metrics([pmid_of(h) for h in hits], topic["rels"])
                row["reranked"][arm] = seen.get("reranked", False)
                row["applied"] = row["applied"] or seen.get("applied", False)
                if not row["reranked"][arm]:
                    row.setdefault("why", {})[arm] = seen.get("why")
            print(json.dumps(row, ensure_ascii=False), flush=True)
            rows.append(row)
    finally:
        retrieval.ENTITY_BOOST = deployed
        retrieval._tagged_among, retrieval._reranked = orig_tagged, orig_reranked
    print(json.dumps(summarise(rows), indent=1), file=sys.stderr)
    return rows


def _cli(argv: list[str]) -> int:
    if not argv or argv[0] != "prepare":
        print(__doc__, file=sys.stderr)
        return 2
    pairs, rest = [], argv[1:]
    while rest:
        if rest[0] != "--pm" or len(rest) < 4 or not re.fullmatch(r"\d{4}", rest[1]):
            print("usage: prepare --pm <year> <topics.xml> <qrels.txt> [--pm …]", file=sys.stderr)
            return 2
        year, topics, qrels, rest = int(rest[1]), rest[2], rest[3], rest[4:]
        with open(topics, encoding="utf-8") as t, open(qrels, encoding="utf-8") as q:
            pairs.append((year, t.read(), q.read()))
    json.dump(prepare(pairs), sys.stdout)
    return 0


if "DATA" in globals():
    run(DATA)  # noqa: F821 — prepended by the caller
elif __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))

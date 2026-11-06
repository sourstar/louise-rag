"""MEASUREMENT (not product code): what a resolved identifier should do to the
legs, against 05 §5.2's "a filter restricts; nothing boosts" — task #33,
found by PR #4.

RESULT (München, 2026-10-25, 80 topics, all reranked, bit-reproducible on a
second run; docs/notes/query-campaign.md): NDCG@10 none 0.3143, the boost
0.3073, any 0.3002, all 0.2983; both restrictions ~2 s slower. The boost
(1/60 added to the fused score of a candidate the query's entity tags) was
removed from the search after it; its arm went with it. The two
restriction arms stay, to measure again whatever 05 §5.2 becomes.

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

THE ARMS (each topic searched once per arm, the order rotating per topic):
- `none`: as deployed — no restriction, nothing boosts;
- `any`: 05 §5.2 as written, a resolved identifier RESTRICTS every leg to
  the records tagged with it (`doc_keys`, the tagger's stand-in for a
  record's `keys`) — tagged with ANY of the named identifiers;
- `all`: the same, tagged with ALL of them (the spec says "a resolved
  identifier restricts", and leaves several open).
The restriction is the facet filters' own prefilter (`book_ids`), so the
arm runs the code path a filter runs. An identifier tagging nothing here
restricts to nothing and returns nothing, as 05 §5.2 says a filter does.

Two steps:
  1. prepare (anywhere): `python tools/measure/entity_boost_trecpm.py prepare
     --pm 2017 topics2017.xml qrels-final-abstracts.txt
     --pm 2018 topics2018.xml qrels-treceval-abstracts-2018-v2.txt > data.json`
  2. search (on the node, the deployed path, reranker on): the script reaches
     the node on stdin with `DATA = <data.json>` prepended, as the campaign's
     other harnesses do. Each topic is searched once per ARM, the order
     rotating per topic; nothing is written.
One JSON line per topic on stdout; the summary on stderr.

Metrics per topic and arm, at k = 10 unless said: NDCG (trec_eval's
ndcg_cut: gain = the grade, log2 discount, unjudged counted as 0), the
CONDENSED NDCG (Sakai 2007: unjudged documents removed before ranking — the
standard answer to a pool Louise did not contribute to), P (grade >= 1), RR,
the share of the top 10 that is judged, and R@50. A topic counts only when
every arm was reranked; a restriction can only differ where it APPLIED (a
record tagged with an entity the query named), and that subset is reported
apart.
"""
import json
import math
import re
import sys
import xml.etree.ElementTree as ET

#: arm -> its restriction: None (as deployed) / "any" / "all"
ARMS = {"none": None, "any": "any", "all": "all"}
BASELINE = "none"
K = 10
DEPTH = 50
METRICS = ("ndcg", "ndcg_condensed", "p", "rr", "judged", "r_depth")


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
    """Per arm, its means over the topics every arm reranked, and per metric
    the topics it won, lost and tied against the baseline (`none`); the same
    over the topics where a named identifier tagged something (`applied`)."""
    counted = [r for r in rows if not r.get("error") and r["reranked"] and all(r["reranked"].values())]
    applied = [r for r in counted if r["applied"]]
    out = {"topics": len(rows), "counted": len(counted), "applied": len(applied)}
    arms = sorted({a for r in counted for a in r["arms"]}, key=list(ARMS).index)
    for name, subset in (("all_topics", counted), ("applied_topics", applied)):
        block: dict = {}
        for arm in arms:
            if not subset:
                continue
            per = {}
            for m in METRICS:
                a = [r["arms"][arm][m] for r in subset]
                b = [r["arms"][BASELINE][m] for r in subset]
                per[m] = {"mean": round(sum(a) / len(a), 4)}
                if arm != BASELINE:
                    per[m].update(won=sum(x > y for x, y in zip(a, b)),
                                  lost=sum(x < y for x, y in zip(a, b)),
                                  tied=sum(x == y for x, y in zip(a, b)))
            ms = [r["ms"][arm] for r in subset if r.get("ms", {}).get(arm) is not None]
            per["ms_median"] = sorted(ms)[len(ms) // 2] if ms else None
            block[arm] = per
        out[name] = block
    return out


def tagged(con, plugin: str, entity_ids: list[str], mode: str) -> set[int]:
    """The records of `plugin` tagged with ANY (`mode="any"`) or ALL of the
    named identifiers: the restriction 05 §5.2 describes, from `doc_keys`
    (driven by its entity index — the order the 2026-09-21 hang taught)."""
    marks = ",".join("?" * len(entity_ids))
    sql = (f"SELECT d.native_id FROM doc_keys k CROSS JOIN documents d ON d.uid = k.uid "
           f"WHERE k.entity_id IN ({marks}) AND d.source = ?")
    if mode == "all":
        sql += f" GROUP BY d.native_id HAVING COUNT(DISTINCT k.entity_id) = {len(set(entity_ids))}"
    return {int(r[0]) for r in con.execute(sql, [*entity_ids, plugin]) if str(r[0]).isdigit()}


def pmid_of(hit: dict) -> str:
    """A pubmed hit's PMID: its record uid is `pubmed:<pmid>`."""
    record = str(hit["chunk_id"]).rsplit(":", 2)[0]
    return record.split(":", 1)[1] if ":" in record else record


# ---- search: on the node ------------------------------------------------------------------

def run(data: list[dict], collection: str = "pubmed") -> list[dict]:
    import time
    from app import query_service, retrieval
    seen: dict = {}
    restriction: list = [None]
    orig_one, orig_reranked = retrieval._search_one_collection, retrieval._reranked

    def one(con, coll, query, k, mode, translate, entity_ids, *args, **kw):
        if entity_ids:
            seen["applied"] = seen.get("applied", False) or bool(
                tagged(con, coll.plugin, list(entity_ids), "any"))
            if restriction[0] and kw.get("book_ids") is None:
                kw["book_ids"] = tagged(con, coll.plugin, list(entity_ids), restriction[0])
                seen["restricted_to"] = len(kw["book_ids"])
        return orig_one(con, coll, query, k, mode, translate, entity_ids, *args, **kw)

    def reranked(*a, **kw):
        out = orig_reranked(*a, **kw)
        seen["reranked"] = seen.get("reranked", True) and bool(out[1])
        seen.setdefault("why", out[2])
        return out
    retrieval._search_one_collection, retrieval._reranked = one, reranked
    rows = []
    names = list(ARMS)
    try:
        for n, topic in enumerate(data):
            row = {"qid": topic["qid"], "query": topic["query"], "arms": {}, "reranked": {},
                   "ms": {}, "restricted_to": {}, "applied": False, "error": None}
            order = names[n % len(names):] + names[:n % len(names)]
            for arm in order:
                restriction[0] = ARMS[arm]
                seen.clear()
                t0 = time.monotonic()
                try:
                    hits = query_service.run_search([collection], topic["query"], DEPTH, "hybrid", True)["hits"]
                except Exception as e:   # noqa: BLE001 — recorded, the next topic goes on
                    row["error"] = f"{arm}: {type(e).__name__}: {e}"[:200]
                    break
                row["ms"][arm] = round((time.monotonic() - t0) * 1000)
                row["arms"][arm] = metrics([pmid_of(h) for h in hits], topic["rels"])
                # an arm restricted to nothing returns nothing, and has nothing to rerank
                row["reranked"][arm] = seen.get("reranked", False) or (not hits and "restricted_to" in seen)
                row["restricted_to"][arm] = seen.get("restricted_to")
                row["applied"] = row["applied"] or seen.get("applied", False)
                if not row["reranked"][arm]:
                    row.setdefault("why", {})[arm] = seen.get("why")
            print(json.dumps(row, ensure_ascii=False), flush=True)
            rows.append(row)
    finally:
        retrieval._search_one_collection, retrieval._reranked = orig_one, orig_reranked
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

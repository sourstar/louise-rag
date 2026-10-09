"""Measure whether Wikidata can group gutenberg's books into works.

READ-ONLY measurement (docs/notes/wikidata-work-grouping.md). Relations are
taken only as Wikidata states them (docs/05 §5.5): P2034 (Project Gutenberg
ebook ID) on an item, P629 (edition or translation of) from that item to a
work, P747 (has edition or translation) from a work to that item. Nothing is
matched by title.

    python tools/measure/wikidata_grouping.py fetch    # SPARQL -> out/
    python tools/measure/wikidata_grouping.py analyze  # out/ -> tables

Input: tools/measure/out/gutenberg_books.tsv (native_id<TAB>lang, the node's
in-scope gutenberg documents).
"""
from __future__ import annotations

import collections
import csv
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"
BOOKS = OUT / "gutenberg_books.tsv"
RAW = OUT / "wikidata_p2034_raw.json"
ENDPOINT = "https://query.wikidata.org/sparql"
UA = "louise-rag-measurement/1.0 (contact via repo owner)"
PAGE = 2000

# Every P2034 statement of any rank; per item the edition-side facts, per
# work (P629 target, or a work that states P747 -> the item) the work-side
# facts. GROUP_CONCAT keeps one row per (item, PG id statement).
QUERY = """
SELECT ?item ?pg ?rank ?itemLabel ?title
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?cls), "entity/"); separator="|") AS ?itemP31)
  (GROUP_CONCAT(DISTINCT ?ilang; separator="|") AS ?itemLang)
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?iauth), "entity/"); separator="|") AS ?itemAuthor)
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?w629), "entity/"); separator="|") AS ?p629)
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?w747), "entity/"); separator="|") AS ?p747)
WHERE {
  { SELECT ?item ?pg ?rank WHERE {
      ?item p:P2034 ?st . ?st ps:P2034 ?pg ; wikibase:rank ?rank .
    } ORDER BY ?item ?pg LIMIT %(limit)d OFFSET %(offset)d }
  OPTIONAL { ?item wdt:P31 ?cls }
  OPTIONAL { ?item wdt:P407 ?il . OPTIONAL { ?il wdt:P218 ?il1 } OPTIONAL { ?il wdt:P220 ?il3 }
             BIND(COALESCE(?il1, ?il3, STRAFTER(STR(?il), "entity/")) AS ?ilang) }
  OPTIONAL { ?item wdt:P50 ?iauth }
  OPTIONAL { ?item wdt:P1476 ?title }
  OPTIONAL { ?item wdt:P629 ?w629 }
  OPTIONAL { ?w747 wdt:P747 ?item }
  OPTIONAL { ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel) = "en") }
}
GROUP BY ?item ?pg ?rank ?itemLabel ?title
"""

WORK_QUERY = """
SELECT ?work ?workLabel
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?cls), "entity/"); separator="|") AS ?workP31)
  (GROUP_CONCAT(DISTINCT ?l407; separator="|") AS ?workP407)
  (GROUP_CONCAT(DISTINCT ?l364; separator="|") AS ?workP364)
  (GROUP_CONCAT(DISTINCT ?authLabel; separator="|") AS ?workAuthor)
WHERE {
  VALUES ?work { %(values)s }
  OPTIONAL { ?work wdt:P31 ?cls }
  OPTIONAL { ?work wdt:P407 ?a . OPTIONAL { ?a wdt:P218 ?a1 } OPTIONAL { ?a wdt:P220 ?a3 }
             BIND(COALESCE(?a1, ?a3, STRAFTER(STR(?a), "entity/")) AS ?l407) }
  OPTIONAL { ?work wdt:P364 ?b . OPTIONAL { ?b wdt:P218 ?b1 } OPTIONAL { ?b wdt:P220 ?b3 }
             BIND(COALESCE(?b1, ?b3, STRAFTER(STR(?b), "entity/")) AS ?l364) }
  OPTIONAL { ?work wdt:P50 ?auth . OPTIONAL { ?auth rdfs:label ?authLabel . FILTER(LANG(?authLabel) = "en") } }
  OPTIONAL { ?work rdfs:label ?workLabel . FILTER(LANG(?workLabel) = "en") }
}
GROUP BY ?work ?workLabel
"""


def sparql(query: str) -> list[dict[str, str]]:
    body = urllib.parse.urlencode({"query": query}).encode()
    for attempt in range(8):
        req = urllib.request.Request(ENDPOINT, data=body, headers={
            "User-Agent": UA, "Accept": "application/sparql-results+json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.load(resp)
            return [{k: v["value"] for k, v in b.items()}
                    for b in data["results"]["bindings"]]
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
            wait = int(e.headers.get("Retry-After") or 0) or 2 ** (attempt + 2)
            print(f"  HTTP {e.code}, waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("SPARQL endpoint kept refusing")


def fetch() -> None:
    rows: list[dict[str, str]] = []
    offset = 0
    while True:
        page = sparql(QUERY % {"limit": PAGE, "offset": offset})
        print(f"offset {offset}: {len(page)} rows", file=sys.stderr)
        rows += page
        if len({(r["item"], r["pg"], r["rank"]) for r in page}) < PAGE:
            break
        offset += PAGE
        time.sleep(1)
    works = sorted({w for r in rows for k in ("p629", "p747")
                    for w in r.get(k, "").split("|") if w})
    work_rows: list[dict[str, str]] = []
    for i in range(0, len(works), 200):
        values = " ".join(f"wd:{w}" for w in works[i:i + 200])
        work_rows += sparql(WORK_QUERY % {"values": values})
        time.sleep(1)
    RAW.write_text(json.dumps({
        "fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "query": QUERY, "work_query": WORK_QUERY,
        "items": rows, "works": work_rows}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"{len(rows)} item rows, {len(work_rows)} works -> {RAW}", file=sys.stderr)


def qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def split(s: str | None) -> list[str]:
    return [x for x in (s or "").split("|") if x]


@dataclass
class Group:
    key: str
    books: list[str] = field(default_factory=list)


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.1f}%" if b else "-"


def analyze() -> None:
    books: dict[str, str] = {}
    with BOOKS.open(encoding="utf-8") as f:
        for line in f:
            nid, _, lang = line.rstrip("\n").partition("\t")
            books[nid] = lang or "(none)"
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    works = {qid(w["work"]): w for w in raw["works"]}

    # PG id -> item(s), best rank only for the grouping; deprecated noted.
    pg_items: dict[str, set[str]] = collections.defaultdict(set)
    deprecated: set[tuple[str, str]] = set()
    item: dict[str, dict[str, str]] = {}
    for r in raw["items"]:
        i = qid(r["item"])
        item[i] = r
        if r["rank"].endswith("DeprecatedRank"):
            deprecated.add((r["pg"], i))
            continue
        pg_items[r["pg"]].add(i)

    def works_of(i: str, with_747: bool) -> set[str]:
        r = item[i]
        ws = set(split(r.get("p629")))
        if with_747:
            ws |= set(split(r.get("p747")))
        return ws

    lang_counts = collections.Counter(books.values())
    top = [l for l, _ in lang_counts.most_common(20)]
    show = top + [l for l in ("la", "el", "grc", "ang", "enm") if l not in top]

    matched = {b for b in books if b in pg_items}
    stated = {b for b in matched if any(works_of(i, False) for i in pg_items[b])}
    stated_any = {b for b in matched if any(works_of(i, True) for i in pg_items[b])}

    out: list[str] = []
    w = out.append
    w(f"fetched {raw['fetched']}; Wikidata P2034 statements {len({(r['item'], r['pg'], r['rank']) for r in raw['items']})} "
      f"(deprecated {len(deprecated)}), distinct PG ids (best rank) {len(pg_items)}")
    w(f"collection books {len(books)}; PG ids on Wikidata not in the collection "
      f"{len(set(pg_items) - set(books))}")
    w("")
    w("| lang | books | (a) on Wikidata | (b) P629 stated | (b') P629 or P747 |")
    w("|---|---:|---:|---:|---:|")
    for l in ["ALL"] + show:
        bs = [b for b in books if l == "ALL" or books[b] == l]
        n = len(bs)
        a = sum(b in matched for b in bs)
        s = sum(b in stated for b in bs)
        s2 = sum(b in stated_any for b in bs)
        w(f"| {l} | {n} | {a} ({pct(a, n)}) | {s} ({pct(s, n)}) | {s2} ({pct(s2, n)}) |")

    sampled: list[str] = []
    for mode, with_747, item_as_work in (
            ("strict: P629", False, False),
            ("P629 or P747", True, False),
            ("broad: P629/P747, else the item itself", True, True)):
        groups: dict[str, set[str]] = collections.defaultdict(set)
        for b in matched:
            for i in pg_items[b]:
                ws = works_of(i, with_747)
                if not ws and item_as_work:
                    ws = {i}
                for wk in ws:
                    groups[wk].add(b)
        multi = {k: v for k, v in groups.items() if len(v) >= 2}
        cross = {k: v for k, v in multi.items() if len({books[b] for b in v}) >= 2}
        grouped_books = set().union(*multi.values()) if multi else set()
        sizes = collections.Counter(len(v) for v in multi.values())
        csizes = collections.Counter(len(v) for v in cross.values())

        def orig(k: str) -> list[str]:
            if k in works:
                return split(works[k].get("workP407")) or split(works[k].get("workP364"))
            return split(item[k].get("itemLang")) if k in item else []

        with_orig = sum(bool(orig(k)) for k in multi)
        w("")
        w(f"### {mode}")
        w(f"works with >=1 collection book {len(groups)}; works grouping >=2: "
          f"{len(multi)} covering {len(grouped_books)} books ({pct(len(grouped_books), len(books))}); "
          f"cross-language {len(cross)} covering "
          f"{len(set().union(*cross.values())) if cross else 0} books")
        w(f"size distribution (>=2): {dict(sorted(sizes.items()))}; "
          f"cross-language: {dict(sorted(csizes.items()))}")
        w(f"(d) grouped works whose original language is stated: {with_orig}/{len(multi)} "
          f"({pct(with_orig, len(multi))}); cross-language: "
          f"{sum(bool(orig(k)) for k in cross)}/{len(cross)}")
        langpairs = collections.Counter(
            "+".join(sorted({books[b] for b in v})) for v in cross.values())
        w(f"cross-language language sets: {dict(langpairs.most_common(15))}")
        for l in ("la", "el"):
            ls = [b for b in books if books[b] == l]
            hit = [b for b in ls if any(
                any(books[o] == "en" for o in v) for v in groups.values() if b in v)]
            ing = [b for b in ls if any(b in v for v in multi.values())]
            w(f"(e) {l}: {len(ls)} books, on Wikidata {sum(b in matched for b in ls)}, "
              f"in a >=2 group {len(ing)}, in a work with an English collection book {len(hit)}")
        if item_as_work:
            # (f) the hand-check sample: 30 random cross-language groups.
            rnd = random.Random(20261009)
            keys = sorted(cross)
            pick = rnd.sample(keys, min(30, len(keys)))
            rows = []
            for k in pick:
                lbl = works.get(k, {}).get("workLabel") or item.get(k, {}).get("itemLabel", "")
                for b in sorted(cross[k], key=int):
                    for i in sorted(pg_items[b]):
                        r = item[i]
                        rows.append({"group": k, "group_label": lbl, "orig_lang": "|".join(orig(k)), "pg": b,
                                     "lang": books[b], "item": i,
                                     "item_label": r.get("itemLabel", ""),
                                     "title": r.get("title", ""),
                                     "item_lang": r.get("itemLang", ""),
                                     "p31": r.get("itemP31", "")})
            with (OUT / "wikidata_crosslang_sample.csv").open("w", newline="", encoding="utf-8") as f:
                cw = csv.DictWriter(f, fieldnames=list(rows[0]))
                cw.writeheader()
                cw.writerows(rows)
            sampled = pick
            # every cross-language group, for the record
            with (OUT / "wikidata_crosslang_all.csv").open("w", newline="", encoding="utf-8") as f:
                cw = csv.writer(f)
                cw.writerow(["group", "label", "orig_lang", "pg:lang..."])
                for k in sorted(cross):
                    lbl = works.get(k, {}).get("workLabel") or item.get(k, {}).get("itemLabel", "")
                    cw.writerow([k, lbl, "|".join(orig(k))] + [f"{b}:{books[b]}" for b in sorted(cross[k], key=int)])

    # (f) duplicates
    multi_item = {p: v for p, v in pg_items.items() if len(v) >= 2}
    multi_629 = [i for i in item if len(split(item[i].get("p629"))) >= 2]
    multi_pg = collections.Counter(i for p, v in pg_items.items() for i in v)
    w("")
    w(f"(f) PG ids on >=2 items: {len(multi_item)} (in collection "
      f"{sum(p in books for p in multi_item)}); items with >=2 P629: {len(multi_629)}; "
      f"items carrying >=2 PG ids: {sum(1 for c in multi_pg.values() if c >= 2)} "
      f"(max {max(multi_pg.values()) if multi_pg else 0})")
    w(f"PG ids on several items: {dict(list(sorted((p, sorted(v)) for p, v in multi_item.items()))[:20])}")
    w(f"items with several P629: {[(i, item[i].get('p629')) for i in multi_629][:20]}")
    p31 = collections.Counter(c for i in item for c in split(item[i].get("itemP31")) or ["(none)"])
    w(f"P31 of P2034 items (top 10): {dict(p31.most_common(10))}")
    w(f"sampled groups: {sampled}")
    text = "\n".join(out)
    (OUT / "wikidata_grouping_report.txt").write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    {"fetch": fetch, "analyze": analyze}[sys.argv[1]]()

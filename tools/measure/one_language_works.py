"""MEASUREMENT (not product code), step 2 of docs/notes/one-language-per-work.md:
group gutenberg's books into WORKS from what the origin states, no similarity.

  wikidata  records.jsonl -> wiki_map.json     each "Wikipedia page about this
            book" URL -> its Wikidata item (the Wikipedia API's pageprops
            wikibase_item, redirects followed, <=50 titles a call, sequential,
            backoff on 429), then each item's P407 / P364 (language of work)
  groups    records.jsonl + wiki_map.json -> works.json, works_report.json
            a work = a Wikidata item, unioned with PG's editions notes; its
            ORIGINAL language = the lang of a book whose LCSH translation
            heading names the work's source language ("Epic poetry, Greek --
            Translations into English" -> the work is Greek: the book in el),
            else the item's P407/P364, else unknown

Usage (Docker, network on):
  python one_language_works.py wikidata --out tools/measure/out/onelang
  python one_language_works.py groups   --out tools/measure/out/onelang
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

UA ="louise-rag-measure/0.1 (research measurement; https://github.com/sourstar)"

#: a language item's English label -> manifest code. The labels are read from
#: Wikidata (the `wikidata` phase stores them); a variety maps to the code PG
#: tags it with (PG tags ancient Greek `el`, every Latin `la`), except the
#: historical stages PG keeps apart (Middle English `enm`, Old English `ang`).
LABEL_CODE = [
    (r"^Old English", "ang"), (r"^Middle English", "enm"), (r"English", "en"),
    (r"^Old French", "fro"), (r"^Middle French", "frm"), (r"French", "fr"),
    (r"Greek", "el"), (r"Latin", "la"), (r"Chinese", "zh"),
    (r"German", "de"), (r"Italian|Tuscan", "it"), (r"Hebrew", "he"),
    (r"Dutch|Flemish", "nl"), (r"Norse|Icelandic", "is"), (r"Spanish|Castilian", "es"),
    (r"Portuguese", "pt"), (r"Swedish", "sv"), (r"Danish", "da"),
    (r"Norwegian|Bokmål|Nynorsk", "no"), (r"Finnish", "fi"), (r"Hungarian", "hu"),
    (r"Czech", "cs"), (r"Polish", "pl"), (r"Russian", "ru"), (r"Japanese", "ja"),
    (r"Persian", "fa"), (r"Turkish", "tr"), (r"Esperanto", "eo"), (r"Sanskrit", "sa"),
    (r"Arabic", "ar"), (r"Catalan", "ca"), (r"Welsh", "cy"), (r"Irish", "ga"),
    (r"Scots$", "sco"), (r"Romanian", "ro"), (r"Galician", "gl"), (r"Slovene", "sl"),
    (r"Afrikaans", "af"), (r"Tagalog", "tl"), (r"Occitan|Provençal", "oc"),
    (r"Breton", "br"), (r"Bulgarian", "bg"), (r"Serbian", "sr"), (r"Croatian", "hr"),
    (r"Ukrainian", "uk"), (r"Korean", "ko"), (r"Bangla|Bengali", "bn"), (r"Hindi", "hi"),
    (r"Yiddish", "yi"), (r"Frisian", "fy"), (r"Armenian", "hy"), (r"Lithuanian", "lt"),
    (r"Estonian", "et"), (r"Latvian", "lv"), (r"Slovak", "sk"), (r"Belarusian", "be"),
]


def label_code(label: str | None) -> str | None:
    for pat, code in LABEL_CODE:
        if label and re.search(pat, label):
            return code
    return None


def http_json(url: str, *, tries: int = 8) -> dict:
    delay = 2.0
    for i in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 502, 500) and i < tries - 1:
                ra = e.headers.get("Retry-After")
                wait = float(ra) if ra and ra.isdigit() else delay
                print(f"  {e.code}, waiting {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
                delay = min(delay * 2, 120)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if i < tries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            raise
    raise RuntimeError("unreachable")


def read_records(out: Path) -> list[dict]:
    return [json.loads(l) for l in (out / "records.jsonl").open(encoding="utf-8") if l.startswith("{")]


def split_wiki(url: str) -> tuple[str, str] | None:
    """('en.wikipedia.org', 'Title with spaces') or None."""
    u = urllib.parse.urlparse(url.strip())
    host = u.netloc.lower()
    if not host.endswith("wikipedia.org"):
        return None
    host = host.replace(".m.wikipedia.org", ".wikipedia.org")
    if u.path.startswith("/wiki/"):
        title = urllib.parse.unquote(u.path[len("/wiki/"):])
    else:
        q = urllib.parse.parse_qs(u.query)
        if "title" not in q:
            return None
        title = q["title"][0]
    title = title.split("#", 1)[0].replace("_", " ").strip()
    return (host, title) if title else None


def phase_wikidata(out: Path) -> None:
    path = out / "wiki_map.json"
    state = json.loads(path.read_text("utf-8")) if path.exists() else {"titles": {}, "items": {}}
    recs = read_records(out)
    by_host: dict[str, set[str]] = defaultdict(set)
    for r in recs:
        for u in r.get("wiki") or []:
            s = split_wiki(u)
            if s:
                by_host[s[0]].add(s[1])
    print({h: len(t) for h, t in sorted(by_host.items(), key=lambda x: -len(x[1]))}, file=sys.stderr)
    for host, titles in sorted(by_host.items()):
        todo = sorted(t for t in titles if f"{host}|{t}" not in state["titles"])
        for i in range(0, len(todo), 50):
            batch = todo[i:i + 50]
            url = (f"https://{host}/w/api.php?action=query&format=json&formatversion=2"
                   f"&redirects=1&prop=pageprops&ppprop=wikibase_item&titles="
                   + urllib.parse.quote("|".join(batch)))
            data = http_json(url)
            q = data.get("query") or {}
            fwd: dict[str, str] = {t: t for t in batch}
            for n in q.get("normalized") or []:
                for k, v in list(fwd.items()):
                    if v == n["from"]:
                        fwd[k] = n["to"]
            for rd in q.get("redirects") or []:
                for k, v in list(fwd.items()):
                    if v == rd["from"]:
                        fwd[k] = rd["to"]
            page_item = {}
            for p in q.get("pages") or []:
                page_item[p.get("title")] = ((p.get("pageprops") or {}).get("wikibase_item")
                                             if not p.get("missing") else None)
            for t in batch:
                state["titles"][f"{host}|{t}"] = page_item.get(fwd[t])
            path.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
            time.sleep(0.3)
        print(f"{host}: {len(titles)} titles", file=sys.stderr)
    items = sorted({q for q in state["titles"].values() if q} - set(state["items"]))
    for i in range(0, len(items), 50):
        batch = items[i:i + 50]
        url = ("https://www.wikidata.org/w/api.php?action=wbgetentities&format=json"
               "&props=claims|labels&languages=en&ids=" + "|".join(batch))
        data = http_json(url)
        for qid, ent in (data.get("entities") or {}).items():
            claims = ent.get("claims") or {}

            def vals(pid):
                return [((c.get("mainsnak") or {}).get("datavalue") or {}).get("value", {}).get("id")
                        for c in claims.get(pid, []) if c.get("rank") != "deprecated"]
            state["items"][qid] = {
                "label": ((ent.get("labels") or {}).get("en") or {}).get("value"),
                "P31": [v for v in vals("P31") if v], "P407": [v for v in vals("P407") if v],
                "P364": [v for v in vals("P364") if v], "P629": [v for v in vals("P629") if v],
                "redirect_to": ent.get("redirects", {}).get("to") if ent.get("redirects") else None}
        path.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
        time.sleep(0.3)
    langs = sorted({q for it in state["items"].values() for q in it["P407"] + it["P364"]})
    state.setdefault("lang_labels", {})
    todo = [q for q in langs if q not in state["lang_labels"]]
    for i in range(0, len(todo), 50):
        url = ("https://www.wikidata.org/w/api.php?action=wbgetentities&format=json"
               "&props=labels&languages=en&ids=" + "|".join(todo[i:i + 50]))
        for qid, ent in (http_json(url).get("entities") or {}).items():
            state["lang_labels"][qid] = ((ent.get("labels") or {}).get("en") or {}).get("value")
        time.sleep(0.3)
    path.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    unmapped = Counter(state["lang_labels"].get(q) for it in state["items"].values()
                       for q in it["P407"] + it["P364"] if not label_code(state["lang_labels"].get(q)))
    print("unmapped language items:", unmapped.most_common(30), file=sys.stderr)
    print(f"titles {len(state['titles'])}, resolved {sum(1 for v in state['titles'].values() if v)}, "
          f"items {len(state['items'])}", file=sys.stderr)


#: LCSH adjective of a language -> manifest code, for "<X>, <Lang> -- Translations into <Lang2>"
#: and "<Lang> poetry -- Translations into ..." headings
LCSH_LANG = {
    "Greek": "el", "Latin": "la", "French": "fr", "German": "de", "Italian": "it",
    "Spanish": "es", "Portuguese": "pt", "Dutch": "nl", "Russian": "ru", "Polish": "pl",
    "Swedish": "sv", "Danish": "da", "Norwegian": "no", "Finnish": "fi", "Hungarian": "hu",
    "Czech": "cs", "Chinese": "zh", "Japanese": "ja", "Sanskrit": "sa", "Arabic": "ar",
    "Persian": "fa", "Hebrew": "he", "English": "en", "Icelandic": "is", "Old Norse": "is",
    "Catalan": "ca", "Provençal": "oc", "Esperanto": "eo", "Welsh": "cy", "Irish": "ga",
    "Romanian": "ro", "Turkish": "tr", "Yiddish": "yi", "Bengali": "bn", "Hindi": "hi",
    "Tamil": "ta", "Pali": "pi", "Egyptian": "egy", "Akkadian": "akk", "Ukrainian": "uk",
    "Serbian": "sr", "Croatian": "hr", "Bulgarian": "bg", "Slovak": "sk", "Slovenian": "sl",
    "Estonian": "et", "Lithuanian": "lt", "Latvian": "lv", "Afrikaans": "af", "Tagalog": "tl",
}
TRANS_RE = re.compile(r"Translations? into ([A-Z][a-zé]+(?: [A-Z][a-z]+)?)")


def trans_langs(heading: str) -> tuple[str | None, str | None]:
    """(source code, target code) from an LCSH translation heading."""
    m = TRANS_RE.search(heading)
    tgt = LCSH_LANG.get(m.group(1)) if m else None
    head = heading.split("--")[0]
    src = None
    for name, code in LCSH_LANG.items():
        if re.search(rf"\b{re.escape(name)}\b", head):
            src = code
            break
    return src, tgt


class DSU:
    def __init__(self):
        self.p: dict = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def phase_groups(out: Path) -> None:
    recs = read_records(out)
    wm = json.loads((out / "wiki_map.json").read_text("utf-8"))
    by_uid = {r["uid"]: r for r in recs}
    lang_of = {r["uid"]: (r.get("lang") or "und") for r in recs}
    dsu = DSU()
    book_items: dict[str, set[str]] = defaultdict(set)
    n_wiki = n_wiki_resolved = n_ed = n_multi_item = 0
    for r in recs:
        uid = r["uid"]
        urls = r.get("wiki") or []
        if urls:
            n_wiki += 1
        for u in urls:
            s = split_wiki(u)
            q = wm["titles"].get(f"{s[0]}|{s[1]}") if s else None
            if q:
                it = wm["items"].get(q) or {}
                q = it.get("redirect_to") or q
                book_items[uid].add(q)
        if book_items[uid]:
            n_wiki_resolved += 1
            items = sorted(book_items[uid])
            if len(items) == 1:
                dsu.union(("item", items[0]), ("book", uid))
            else:
                # a book naming SEVERAL works' pages is an anthology or an
                # omnibus, not an edition of one of them: grouping through it
                # chained King Lear to Grillparzer and Götz to the Paradiso
                n_multi_item += 1
                dsu.find(("book", uid))
        if r.get("editions"):
            n_ed += 1
            dsu.find(("book", uid))
            for e in r["editions"]:
                other = f"gutenberg:{e}"
                if other in by_uid:
                    dsu.union(("book", uid), ("book", other))
    comps: dict = defaultdict(lambda: {"books": set(), "items": set()})
    for node in list(dsu.p):
        root = dsu.find(node)
        kind, val = node
        comps[root]["books" if kind == "book" else "items"].add(val)
    works = []
    for root, c in comps.items():
        books = sorted(c["books"], key=lambda u: int(u.split(":")[1]))
        if not books:
            continue
        langs = sorted({lang_of[b] for b in books})
        # original language: a translation heading's source among the books' langs
        src_votes = Counter()
        heading_src = Counter()
        for b in books:
            for h in by_uid[b].get("trans_subjects") or []:
                s, _t = trans_langs(h)
                if s:
                    heading_src[s] += 1
        orig, how = None, "unknown"
        if heading_src:
            s = heading_src.most_common(1)[0][0]
            orig, how = s, "lcsh"
        if orig is None:
            p = Counter()
            for q in c["items"]:
                it = wm["items"].get(q) or {}
                for lq in it.get("P407", []) + it.get("P364", []):
                    code = label_code(wm.get("lang_labels", {}).get(lq))
                    if code:
                        p[code] += 1
            if p:
                # several languages stated (Kidnapped: English and Scots): the
                # most stated, and among equals one the collection holds
                top = max(p.values())
                tied = [c for c, n in p.items() if n == top]
                orig = next((c for c in tied if c in langs), tied[0])
                how = "p407"
        works.append({"work": "W" + (sorted(c["items"])[0] if c["items"] else books[0].split(":")[1]),
                      "items": sorted(c["items"]), "books": books, "langs": langs,
                      "original": orig, "original_how": how,
                      "original_held": orig in langs if orig else None,
                      "labels": [(wm["items"].get(q) or {}).get("label") for q in sorted(c["items"])]})
    works.sort(key=lambda w: (-len(w["books"]), w["work"]))
    (out / "works.json").write_text(json.dumps(works, ensure_ascii=False, indent=0), "utf-8")

    multi = [w for w in works if len(w["books"]) >= 2]
    cross = [w for w in multi if len(w["langs"]) >= 2]
    tot_lang = Counter(lang_of.values())
    in_multi = Counter(lang_of[b] for w in multi for b in w["books"])
    in_cross = Counter(lang_of[b] for w in cross for b in w["books"])
    wiki_lang = Counter(lang_of[u] for u in book_items if book_items[u])
    pairs = Counter("+".join(w["langs"]) for w in cross)
    sizes = Counter(len(w["books"]) for w in multi)
    report = {
        "books": len(recs), "books_with_wiki_url": n_wiki, "books_wiki_resolved": n_wiki_resolved,
        "books_with_editions_note": n_ed, "books_naming_several_items": n_multi_item,
        "works_total": len(works), "works_ge2": len(multi), "books_in_ge2": sum(len(w["books"]) for w in multi),
        "works_cross_lang": len(cross), "books_in_cross": sum(len(w["books"]) for w in cross),
        "sizes": dict(sorted(sizes.items())), "cross_lang_combos": pairs.most_common(40),
        "original_how_multi": Counter(w["original_how"] for w in multi),
        "original_how_cross": Counter(w["original_how"] for w in cross),
        "original_held_cross": Counter(str(w["original_held"]) for w in cross),
        "original_lang_cross": Counter(w["original"] or "?" for w in cross).most_common(30),
        "per_lang": {l: {"books": n, "wiki": wiki_lang[l], "in_ge2": in_multi[l], "in_cross": in_cross[l]}
                     for l, n in tot_lang.most_common(30)},
    }
    (out / "works_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=dict), "utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1, default=dict))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["wikidata", "groups"])
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    {"wikidata": phase_wikidata, "groups": phase_groups}[a.phase](a.out)


if __name__ == "__main__":
    main()

"""MEASUREMENT (not product code), step 1 of docs/notes/one-language-per-work.md:
read gutenberg's records READ-ONLY and print, per book, what the ORIGIN states
about which work it is an edition of. Stdout only; writes nothing.

Run on the node:
  docker exec -i ix-louise-rag-worker-1 python - < one_language_records.py > records.jsonl

One JSON line per in-scope record: uid, lang, title, creators, languages, and —
only where present — the Wikipedia URL(s) of the book from PG's RDF
description ("Wikipedia page about this book: <url>"), the editions note's
ebook ids ("Project Gutenberg has several editions of this eBook: #N ..."),
the LCSH translation headings ("... -- Translations into English"), the
translator credit (role trl), the LCSH set, and the MARC 520 summary
(truncated) used to write queries.
"""
import ast
import json
import re
import sys
from pathlib import Path

from app.pack import records
from app.plugins import drag

WIKI = re.compile(r"Wikipedia page about this book:\s*(https?://\S+)")
EDITIONS = re.compile(r"Project Gutenberg has several editions of this eBook")
EBOOK_NUM = re.compile(r"#(\d+)")


def balanced(u: str) -> str:
    """Drop a trailing ")" that closes nothing in the URL (prose around it)."""
    while u.endswith(")") and u.count(")") > u.count("("):
        u = u[:-1]
    return u


def lit(v):
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return ast.literal_eval(v)
        except (ValueError, SyntaxError):
            return v
    return v


def main() -> None:
    files = drag.shard_files(Path("/data/collections/gutenberg/drag"))
    n = 0
    for p in files:
        for r in records.iter_records(p):
            ex = r.get("extra") or {}
            desc = ex.get("description") or ""
            out = {"uid": r["uid"], "lang": r.get("lang"), "title": (r.get("title") or "")[:200],
                   "creators": (r.get("creators") or [])[:6]}
            langs = lit(ex.get("languages"))
            if isinstance(langs, list) and len(langs) > 1:
                out["languages"] = langs
            wiki = [balanced(u.rstrip(".,;")) for u in WIKI.findall(desc)]
            if wiki:
                out["wiki"] = wiki
            if EDITIONS.search(desc):
                para = next((s for s in desc.split("\n\n") if EDITIONS.search(s)), desc)
                out["editions"] = sorted({int(x) for x in EBOOK_NUM.findall(para)})
                out["editions_note"] = para[:600]
            subs = [s for s in r.get("subjects") or [] if not re.fullmatch(r"[A-Z]{1,3}", s)]
            tr = [s for s in subs if "Translations into" in s]
            if tr:
                out["trans_subjects"] = tr
            cd = lit(ex.get("creators_detail"))
            if isinstance(cd, list):
                trl = [c.get("name") for c in cd if isinstance(c, dict) and c.get("role") == "trl"]
                if trl:
                    out["translators"] = trl
                auth = [c.get("agent_id") for c in cd if isinstance(c, dict)
                        and c.get("role") in ("creator", "aut") and c.get("agent_id")]
                if auth:
                    out["author_agents"] = auth
            if wiki or "editions" in out or tr:
                out["subjects"] = subs[:12]
                marc = lit(ex.get("marc"))
                if isinstance(marc, dict) and marc.get("520"):
                    out["summary"] = str(marc["520"][0])[:700]
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            n += 1
    print(f"# {n} records from {len(files)} parts", file=sys.stderr)


main()

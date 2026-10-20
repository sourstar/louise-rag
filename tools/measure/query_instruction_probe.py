"""MEASUREMENT (not product code): the vector leg of gutenberg with the query
encoded bare, with Qwen3-Embedding's default instruction, and with a
library-specific one (docs/notes/query-campaign.md, run 2; A1f).

Per query, `embeddings.search_records(query, 100)` under each form — the
recipe's `query_instruction` replaced in-process, nothing written — and the
rank of the query's work (any of its books, works.json) among the distinct
records, plus how many of the top 10 are carried by a chunk under 200
characters. Runs in a read-only container of the web's image (the script on
stdin, `DATA` prepended: [{qid, kind, lang, query, books}], built from
one_language_titles.tsv, out/onelang_live/queries.jsonl and works.json).
Run it while the query encoder is resident: on 2026-10-20 the node swapped
it out for normattiva's corpus embedding after 88 queries, and every probe
query then cost a reload on both sides (stopped there).
"""
import dataclasses, json, sys, time
from app import collections as cm, embeddings
col = cm.by_name("gutenberg")
orig = cm.recipe_for
FORMS = {
    "bare": None,
    "default": "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:",
    "library": "Instruct: Given a search for a book by its title, its author or a description of its content, retrieve passages of that book\nQuery:",
}
for q in DATA:
    want = set(q["books"])
    row = {"qid": q["qid"], "kind": q["kind"], "lang": q["lang"]}
    for name, instr in FORMS.items():
        cm.recipe_for = (lambda c, _i=instr: dataclasses.replace(orig(c), query_instruction=_i)) if instr else orig
        t = time.time()
        recs = embeddings.search_records(q["query"], 100, collection=col)
        ids, lens = [], []
        for h in recs:
            if h["book_id"] not in ids:
                ids.append(h["book_id"]); lens.append(len(h["text"]))
        rank = next((i + 1 for i, b in enumerate(ids) if b in want), None)
        row[name] = {"rank": rank, "frag10": sum(1 for l in lens[:10] if l < 200), "s": round(time.time() - t, 2)}
    cm.recipe_for = orig
    print(json.dumps(row), flush=True)

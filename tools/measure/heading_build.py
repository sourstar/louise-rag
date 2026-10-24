# MEASUREMENT (not product code): gutenberg's en word-match index rebuilt into a
# scratch fts_dir with fts.HEADING_IN_BODY = K (9db4e3a: the heading folded K times
# into ONE indexed field with the body), then the fts-leg measurement of the
# heading-weight table on it. Prepended: K, OUT, DATA.
import copy, json, time
from pathlib import Path
from app import collections as cm, db, fts, worker

fts.HEADING_IN_BODY = K  # noqa: F821
c = [x for x in cm.load_all() if x.name == "gutenberg"][0]
c2 = copy.copy(c)
object.__setattr__(c2, "fts_dir", Path(OUT))  # noqa: F821
Path(OUT).mkdir(parents=True, exist_ok=True)  # noqa: F821
con = db.connect(c)
rows = con.execute("SELECT * FROM documents WHERE lang='en' AND error IS NULL AND fts_done=1 ORDER BY rowid").fetchall()
print(json.dumps({"K": K, "documents": len(rows)}), flush=True)  # noqa: F821
t0 = time.time(); done = chunks_n = 0; batch = []
for r in rows:
    try:
        ch = worker._load_chunks(r, c)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"skip": r["uid"], "why": f"{type(e).__name__}: {e}"[:120]}), flush=True)
        continue
    if ch:
        batch.append((r["lang"], int(r["native_id"]), ch)); chunks_n += len(ch)
    done += 1
    if len(batch) >= 200:
        fts.index_books(batch, c2); batch = []
    if done % 2000 == 0:
        print(json.dumps({"K": K, "done": done, "chunks": chunks_n, "s": round(time.time() - t0), "docs_per_s": round(done / (time.time() - t0), 1)}), flush=True)
if batch:
    fts.index_books(batch, c2)
print(json.dumps({"K": K, "built": done, "chunks": chunks_n, "s": round(time.time() - t0)}), flush=True)

data = [d for d in DATA if d["lang"] == "en"]  # noqa: F821
def rank(recs, books):
    for i, r in enumerate(recs, 1):
        if f"gutenberg:{int(r.get('book_id') or 0)}" in books: return i
    return None
per = {}
for d in data:
    per.setdefault(d["kind"], []).append(rank(fts.search_records("en", d["query"], 50, collection=c2), set(d["books"])))
md = rank(fts.search_records("en", "white whale obsession captain", 500, collection=c2), {"gutenberg:2701", "gutenberg:2489", "gutenberg:15"})
out = {"K": K, "moby_record_rank": md}
for k, rs in per.items():
    n = len(rs)
    out[k] = {"n": n, "mrr": round(sum(1 / r for r in rs if r) / n, 3), "r10": round(sum(1 for r in rs if r and r <= 10) / n, 3), "r50": round(sum(1 for r in rs if r) / n, 3)}
print(json.dumps(out), flush=True)

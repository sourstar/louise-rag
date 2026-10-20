"""reembed-verify.py — normattiva's re-embedded vectors against a fresh encode
of the same texts, BYTE FOR BYTE (CLAUDE.md invariant 12): the production
path end to end — the worker's batches and concurrency, the carry by text
hash (06 §6.3, amended 2026-10-21), the cast to the table's dtype — checked
by the function the worker encodes with. Read-only; run in the web container:
    docker exec -i -w /srv -e PYTHONPATH=/srv ix-louise-rag-web-1 python - < reembed-verify.py
Exit 1 when one stored vector differs."""
import random
import sqlite3
import sys

import lancedb
import numpy as np

from app import collections as cm, config, embeddings

N_DOCS, PER_DOC = 12, 2
c = cm.by_name("normattiva")
con = sqlite3.connect(f"file:{c.manifest_path}?mode=ro", uri=True)
left = con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1 AND embed_version=?",
                   (embeddings.REPLACED_VERSION,)).fetchone()[0]
done = [int(r[0]) for r in con.execute(
    "SELECT native_id FROM documents WHERE embed_done=1 AND embed_version=?",
    (config.EMBED_VERSION,))]
print(f"re-embedded {len(done)}, still marked {left}")
if not done:
    sys.exit(0)
rng = random.Random(20261021)
tbl = lancedb.connect(str(c.lance_dir)).open_table(embeddings.TABLE)
dtype = embeddings._np_dtype_of_table(tbl)
rows = []
for nid in rng.sample(done, min(N_DOCS, len(done))):
    got = (tbl.search().where(f"book_id = {nid}", prefilter=True)
           .select(["id", "text", "vector"]).limit(100_000).to_list())
    rows += rng.sample(got, min(PER_DOC, len(got)))
fresh = embeddings.embed_chunks(c, [r["text"] for r in rows])
same = [np.asarray(f, dtype=dtype).tobytes() == np.asarray(r["vector"], dtype=dtype).tobytes()
        for f, r in zip(fresh, rows)]
print(f"{sum(same)}/{len(rows)} stored vectors bit-identical to a fresh encode ({dtype})")
for ok, r in zip(same, rows):
    if not ok:
        print("  differs:", r["id"])
sys.exit(0 if all(same) else 1)

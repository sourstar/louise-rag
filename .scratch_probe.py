import numpy as np, tempfile, pathlib
from app import collections as cm, config, embeddings, vector_index
d = pathlib.Path(tempfile.mkdtemp())
c = cm.Collection(name="vp", plugin="gutenberg", profile=None, embed_mode_default=None,
                  corpus_dir=d/"c", manifest_path=d/"m.db", fts_dir=d/"f", lance_dir=d/"l",
                  embed=cm.EmbedRecipe(url="http://x:1", api="openai", model="m",
                                       dtype="f32", ctx=512, dim=8))
conn = embeddings._conn(c)
for i in range(12):
    rows = [{"id": f"u{i}:{j}:1", "book_id": i, "text": "t",
             "vector": np.random.default_rng(i*100+j).standard_normal(8).astype("float32").tolist()}
            for j in range(4)]
    if embeddings.TABLE not in conn.table_names():
        conn.create_table(embeddings.TABLE, data=rows)
    else:
        conn.open_table(embeddings.TABLE).add(rows)
tbl = vector_index._table(c)
print("versions before:", len(tbl.list_versions()))
from datetime import timedelta
try:
    st = tbl.cleanup_old_versions(older_than=timedelta(hours=0))
    print("stat:", st, type(st))
    print("attrs:", [a for a in dir(st) if not a.startswith('_')])
except Exception as e:
    print("RAISED:", type(e).__name__, e)
print("versions after:", len(vector_index._table(c).list_versions()))

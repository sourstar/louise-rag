""""Clear errors & retry" writes only the rows it changes.

It matched `error IS NULL OR <stage error>`, so every healthy document was
rewritten too: on pubmed 40M rows in one transaction, minutes under the
manifest's write lock — the worker starved behind it and the request could
end before the reset did, so the button read as doing nothing (2026-09-15).
"""
from app import collections as collections_mod, db


def test_clear_errors_writes_only_the_rows_it_changes(isolated_config):
    con = db.connect(collections_mod.default_collection())
    rows = [(f"g:{i}", "g", str(i), "/x", 1, None, 0) for i in range(1000)]   # healthy
    rows += [("g:e1", "g", "e1", "/x", 1, "embed: embed server HTTP 503", 1),
             ("g:e2", "g", "e2", "/x", 1, "fts: boom", 2),
             ("g:e3", "g", "e3", "/x", 1, "rdf: bad", 9),
             ("g:r1", "g", "r1", "/x", 1, None, 3),           # retries spent, no error
             ("g:u1", "g", "u1", "/x", 1, "no txt file", 1)]  # a corpus fact: kept
    con.executemany("INSERT INTO documents(uid,source,native_id,path,in_scope,error,"
                    "retries) VALUES(?,?,?,?,?,?,?)", rows)

    before = con.total_changes
    assert db.apply_reset(con, "errors", []) == 4
    assert con.total_changes - before == 4, "healthy documents are not rewritten"
    left = {r["uid"]: r["error"] for r in con.execute(
        "SELECT uid, error FROM documents WHERE error IS NOT NULL OR retries > 0")}
    assert left == {"g:u1": "no txt file"}, "stage errors cleared, a corpus fact kept"

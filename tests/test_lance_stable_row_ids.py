"""Every vector table is created with Lance's STABLE ROW IDS
(app/embeddings.py NEW_TABLE_STORAGE).

Without them a compaction that moves indexed rows re-maps the whole vector
index into new files: wikipedia's twins repair of 2026-10-30 left eight full
IVF_PQ generations, 142 GiB, written in four hours. With them the index
stays and a deleted row is masked. Measured the same way here."""
from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pytest

lancedb = pytest.importorskip("lancedb")
pytest.importorskip("lance")

from app import embeddings  # noqa: E402

APP = Path(__file__).resolve().parent.parent / "app"


def _rows(n: int, dim: int = 16) -> list[dict]:
    rng = np.random.default_rng(7)
    return [{"id": f"{i // 4}:{i % 4}:1", "book_id": i // 4, "lang": "en", "seq": i % 4,
             "chapter": "", "text": f"chunk {i}",
             "vector": rng.standard_normal(dim).astype(np.float16)} for i in range(n)]


def _index_dirs(root: Path) -> list[str]:
    return sorted(p.name for p in (root / f"{embeddings.TABLE}.lance" / "_indices").iterdir())


def test_a_compaction_after_deletes_keeps_the_one_index(tmp_path):
    from lancedb.index import IvfPq
    db = lancedb.connect(str(tmp_path))
    tbl = embeddings._create_table(db, _rows(2048))
    tbl.add(_rows(4096)[2048:])
    tbl.create_index("vector", config=IvfPq(distance_type="l2", num_partitions=4,
                                            num_sub_vectors=4), replace=True)
    before = _index_dirs(tmp_path)
    tbl.delete("book_id < 100")                   # delete-then-insert's first half
    tbl.to_lance().optimize.compact_files(materialize_deletions=True,
                                          materialize_deletions_threshold=0.0)
    assert _index_dirs(tmp_path) == before, "the index is not re-mapped into a new copy"
    tbl = db.open_table(embeddings.TABLE)
    q = np.random.default_rng(1).standard_normal(16).astype(np.float32)
    hits = tbl.search(q).limit(20).nprobes(4).to_list()
    assert hits and min(h["book_id"] for h in hits) >= 100, "a deleted row is never found"


def test_every_table_creation_in_app_names_the_option():
    missing = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "create_table"
                    and not any(k.arg == "storage_options" for k in node.keywords)):
                missing.append(f"{path.relative_to(APP.parent)}:{node.lineno}")
    assert not missing, f"a vector table created without stable row ids: {missing}"

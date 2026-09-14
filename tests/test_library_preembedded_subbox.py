"""Wikipedia's card shows its pre-embeddings like PubMed's (operator,
2026-09-14): the same sub-box, the same rows, the same badge.

Only a STAND-ALONE pre-embedded collection — wikivec, the `embedded`
plugin, whose card IS the archive — keeps the one-line box. Every other
collection with pre-embeddings gets the sub-box, and its archive's files
all being on disk read as all fetched, the way a remote archive's landed
files do.
"""
from dataclasses import replace

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from app import collections as collections_mod, db, web

ARCHIVE = "upstash-wikipedia-2024-06"


def _shard(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "id": ["10_0"], "url": ["https://en.wikipedia.org/wiki/Moon"],
        "title": ["Moon"], "text": ["para one"],
        "embedding": [np.ones(4, dtype=np.float32).tolist()]}), path)


def _collection(tmp_path, plugin: str):
    corpus = tmp_path / plugin
    _shard(corpus / "data" / "en" / "000.parquet")
    _shard(corpus / "data" / "de" / "000.parquet")
    return replace(collections_mod.default_collection(), name=plugin, plugin=plugin,
                   corpus_dir=corpus, manifest_path=tmp_path / plugin / "manifest.db",
                   genesis_archive=ARCHIVE)


def test_wikipedias_pre_embeddings_get_pubmeds_sub_box(isolated_config, tmp_path):
    c = _collection(tmp_path, "wikipedia")
    g = web._genesis_state(db.connect(c), c, None,
                           fetched={"complete": False, "active": True})
    assert g["standalone"] is False, "wikipedia's card is not the archive"
    assert (g["fetched"], g["shard_total"]) == (2, 2), "every archive file is on disk"
    assert g["fetch_state"] == "done", \
        "the archive's badge speaks for the archive, not the dumps being fetched"


def test_wikivec_stays_stand_alone(isolated_config, tmp_path):
    c = _collection(tmp_path, "embedded")
    g = web._genesis_state(db.connect(c), c, None,
                           fetched={"complete": False, "active": True})
    assert g["standalone"] is True
    assert g["fetch_state"] == "active", "a stand-alone card reads its own fetch"


def test_the_template_keys_the_sub_box_on_standalone():
    """The sub-box, the main row's label and the backfill note all follow
    `standalone`, not the archive's kind."""
    from pathlib import Path
    html = (Path(web.__file__).parent / "templates" / "library.html").read_text(encoding="utf-8")
    assert "{% if c.genesis and not c.genesis.standalone %}" in html
    assert "not c.genesis.local" not in html and "c.genesis and c.genesis.local" not in html

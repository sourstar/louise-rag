"""The streaming writer's memory bound: the vector table is never
materialized whole — pass A samples documents from an id-only stream, pass B
streams (id, vector) once, and the bootstrap runs on the capped document
sample while the envelope covers every vector. At production scale the old
whole-table load was a ~36 GB OOM, not a calibration.
"""
import json

import pytest
from nacl import signing

from app.verify import artifact as artifact_mod
from tests.bundles.helpers import seed_document
from tools import build_space_artifact

# The space these bundles are cut in is the default collection's
# (`spaces.gather_components()` with no collection): the test node's Codex
# gutenberg, never whatever `collections/` the working directory holds.
pytestmark = pytest.mark.usefixtures("isolated_config")

KEY = signing.SigningKey(bytes(range(32)))


@pytest.fixture
def corpus(con, collection):
    for native_id in range(1, 13):
        seed_document(con, collection, f"gutenberg:{native_id}", native_id,
                      n_chunks=4, seed=native_id)
    return collection


def test_subsampled_bootstrap_still_produces_a_loadable_artifact(
        corpus, tmp_path, monkeypatch):
    """With the sample capped below the corpus size, the writer must keep
    only the sampled documents' chunks in RAM yet publish a full-corpus
    envelope and honest counts."""
    monkeypatch.setattr(build_space_artifact, "SAMPLE_DOCS", 6)
    monkeypatch.setattr(build_space_artifact, "CAP_CHUNKS", 24)
    out = tmp_path / "artifact-out"
    result = build_space_artifact.build(corpus, out, replicates=6000,
                                         signing_key=KEY)
    assert result["chunks"] == 48 and result["documents"] == 12
    assert result["sample_documents"] <= 6
    assert result["sample_chunks"] <= 24

    loaded = artifact_mod.load(out)
    assert loaded.envelope is not None
    raw = json.loads((out / artifact_mod.ARTIFACT_NAME).read_text())
    ref = raw["reference_corpus"]
    assert ref["documents"] == 12 and ref["chunks"] == 48, \
        "artifact metadata reports the FULL corpus, not the sample"
    assert ref["calibration_sample"]["documents"] <= 6
    buckets = raw["envelope"]["moment_statistic"]["null_quantiles_by_bucket"]
    assert buckets, "the sampled bootstrap must still publish a null"


def test_streaming_never_materializes_the_table(corpus, tmp_path, monkeypatch):
    """Structural guard: build() must go through the batch iterator, never
    a whole-table to_arrow()/to_pylist() load."""
    calls = []
    real = build_space_artifact._batches

    def counting(collection, columns):
        calls.append(tuple(columns))
        return real(collection, columns)
    monkeypatch.setattr(build_space_artifact, "_batches", counting)

    build_space_artifact.build(corpus, tmp_path / "out", replicates=6000)
    assert ("id",) in calls, "pass A (document sampling) must stream ids"
    assert ("id", "vector") in calls, "pass B must stream id+vector batches"

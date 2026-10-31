"""The stored CPU reference (operator, 2026-10-31; docs/notes/near-exact-runs.md
§2): the CPU twin's answers to the 512 calibration items, made once and held
against by every gate."""
from __future__ import annotations

import pytest

from app.hold import calibration as cal, cpu_reference as cpu_ref, embedder as emb
from app.hold.codex import CodexError
from tests.hold.conftest import a_stated_run, fake_embed
from tests.hold.test_embedder import codex_768, perturbed, reference

CPU_RUN = {"driver": "cpu", "engine": {"name": "localai/llama-cpp-cpu"}, "weights": "sha256:" + "aa" * 32}


def test_a_reference_is_kept_whole_read_back_and_found_by_its_weights_and_set(tmp_path):
    vectors = reference()
    path = cpu_ref.write(tmp_path, "c", "document", run_id="sha256:" + "11" * 32,
                         run=CPU_RUN, set_id="sha256:set", vectors=vectors)
    got = cpu_ref.read(path)
    assert got.run_id == "sha256:" + "11" * 32 and got.weights == CPU_RUN["weights"]
    assert got.vectors[0] == pytest.approx(vectors[0], abs=0) and len(got.vectors) == cal.SIZE
    assert cpu_ref.find(tmp_path, "c", "document", weights=CPU_RUN["weights"],
                        set_id="sha256:set").path == path
    assert cpu_ref.find(tmp_path, "c", "document", weights="sha256:" + "bb" * 32,
                        set_id="sha256:set") is None, "other weights: another function"
    assert cpu_ref.find(tmp_path, "c", "document", weights=CPU_RUN["weights"],
                        set_id="sha256:other") is None
    assert cpu_ref.find(tmp_path, "c", "query", weights=CPU_RUN["weights"],
                        set_id="sha256:set") is None


def test_a_card_run_is_not_a_cpu_reference_and_a_tampered_file_is_not_read(tmp_path):
    with pytest.raises(CodexError, match="not the CPU"):
        cpu_ref.write(tmp_path, "c", "document", run_id="sha256:" + "22" * 32,
                      run=dict(CPU_RUN, driver="xe 1.3 / level-zero 1.21"),
                      set_id="s", vectors=reference())
    path = cpu_ref.write(tmp_path, "c", "document", run_id="sha256:" + "33" * 32,
                         run=CPU_RUN, set_id="s", vectors=reference())
    import json
    doc = json.loads(path.read_text())
    doc["sha256"] = "00" * 32
    path.write_text(json.dumps(doc))
    with pytest.raises(CodexError, match="digest"):
        cpu_ref.read(path)
    assert cpu_ref.find(tmp_path, "c", "document", weights=CPU_RUN["weights"],
                        set_id="s") is None, "an unreadable file is passed over, never used"


def _gate(codex, got, cpu_vectors):
    ref = reference()
    return emb.run_gate(codex, reference=ref, embed=lambda texts: got, seed=b"\x09" * 32,
                        stated=a_stated_run(codex), embed_one=fake_embed, contribute=fake_embed,
                        cpu_reference=("sha256:" + "44" * 32, cpu_vectors))


def test_the_gate_holds_every_answer_to_the_stored_cpu_reference():
    """At or above the review line (0.995, operator 2026-10-18) the run passes
    with its numbers kept; below it on ANY of the 512 it fails, whatever the
    sampled sixteen say."""
    codex = codex_768()
    ref = reference()
    close = perturbed(ref, indices=(5,), to=0.9991)
    run = _gate(codex, close, ref)
    assert run.ok and run.cpu_run == "sha256:" + "44" * 32
    assert run.cpu_worst == pytest.approx(0.9991, abs=1e-4) and run.cpu_median == pytest.approx(1.0)
    far = perturbed(ref, indices=(400,), to=0.99)
    run = _gate(codex, far, ref)
    assert not run.ok and "below the review line" in run.why()
    assert run.passed >= run.min_pass, "the sampled quorum alone would have passed it"


def test_a_statement_says_how_it_stands_against_the_cpu_reference(monkeypatch):
    from tests.hold.test_backend_engine import BACKEND, _vega_run
    from tests.hold.test_embedder import a_key
    codex = codex_768(version=5)
    ref = reference()
    run = emb.run_gate(codex, reference=ref, embed=lambda texts: ref, seed=b"\x09" * 32,
                       stated=_vega_run(codex), embed_one=fake_embed, contribute=fake_embed,
                       cpu_reference=("sha256:" + "44" * 32, perturbed(ref, indices=(3,), to=0.9995)))
    body = emb.read_statement(emb.statement(run, codex, a_key(), now="2026-10-31T00:00:00Z"))
    assert body["version"] == 5 and body["exactness"] == "exact" and body["loads"] == 1
    assert body["cpu_reference"]["run"] == "sha256:" + "44" * 32
    assert body["cpu_reference"]["worst"].startswith("0.9995")


def _v5(**over):
    from app.hold import runs as hold_runs
    codex = codex_768(version=5)
    body = {"version": 5, "passed": 16, "median": "1.000000", "exactness": "exact",
            "loads": 1, "load_variation": "1", "cpu_reference": None, **over}
    return hold_runs.passing(body, codex)


def test_a_version_5_statement_is_admitted_by_how_exact_it_is():
    """The gate's own-bytes verdict is in the statement from version 5, and a
    write is admitted by the statement: before it, a run that did not give
    back its own bytes passed `passing` all the same."""
    assert _v5()
    assert not _v5(load_variation="0.999998"), "exact means byte-equal"
    assert not _v5(cpu_reference={"run": "r", "worst": "0.994", "median": "0.9999"}), \
        "below the review line against the stored CPU reference"
    assert _v5(cpu_reference={"run": "r", "worst": "0.996", "median": "0.9999"})
    near = dict(exactness="near", loads=3, load_variation="0.999995",
                cpu_reference={"run": "r", "worst": "0.9995", "median": "1.000000"})
    assert _v5(**near)
    assert not _v5(**dict(near, loads=1)), "one load could be the lucky one"
    assert not _v5(**dict(near, load_variation="0.99990"))
    assert not _v5(**dict(near, cpu_reference=None)), "near needs the CPU to stand against"
    assert not _v5(**dict(near, cpu_reference={"run": "r", "worst": "0.9985",
                                               "median": "1"})), "near needs 0.999"


def test_a_run_that_does_not_give_back_its_own_bytes_is_not_admitted():
    from app.hold import runs as hold_runs
    from tests.hold.test_backend_engine import _vega_run
    from tests.hold.test_embedder import a_key
    codex = codex_768(version=5)
    ref = reference()
    calls = {"n": 0}

    def drifting(texts):
        calls["n"] += 1
        return fake_embed(texts, tag=b"" if calls["n"] == 1 else b"drift")
    run = emb.run_gate(codex, reference=ref, embed=lambda texts: ref, seed=b"\x09" * 32,
                       stated=_vega_run(codex), embed_one=drifting, contribute=fake_embed)
    assert run.reproducible is False and run.load_variation < 1
    body = emb.read_statement(emb.statement(run, codex, a_key(), now="2026-10-31T00:00:00Z"))
    assert int(body["passed"]) >= codex.space.calibration.min_pass
    assert not hold_runs.passing(body, codex), "the statement now carries the verdict"

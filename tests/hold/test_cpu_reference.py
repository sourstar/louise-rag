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


# ---- the bound on a CPU twin's request (München, 2026-11-01) --------------------
# wikidata's bge-m3 space (ctx 8,191) timed out three times at the card's flat
# 300 s: 16 texts a request (its `[encoder] batch`) of up to 8,191 tokens.

def test_a_request_to_the_cpu_twin_is_bounded_by_the_spaces_window():
    from app.hold import cpu_reference as cpu_ref
    assert cpu_ref.request_timeout(1024, 1) == 300.0
    assert cpu_ref.request_timeout(None, 1) == 300.0, "no window named: one 1,024"
    assert cpu_ref.request_timeout(8191, 1) == 2400.0
    assert cpu_ref.request_timeout(8191, 2) == 4800.0, "one queued behind another"


def test_the_reference_asks_one_text_a_request_under_that_bound(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app import config, encoder_use, louise, run_check
    from app import collections as collections_mod
    from app.hold import collection as col, cpu_reference as cpu_ref, embedder as emb
    from app.hold import fingerprint
    recipe = collections_mod.EmbedRecipe(url="http://localai:8080", api="vega",
                                         model="vega-bge-m3-cpu", dtype="f16", ctx=8191,
                                         batch=16)
    codex = SimpleNamespace(space=SimpleNamespace(query=SimpleNamespace(encoder=None),
                                                  calibration=SimpleNamespace(set="s")))
    monkeypatch.setattr(col, "resolve", lambda root, name: ("wikidata", codex))
    monkeypatch.setattr(louise, "_recipe_for", lambda c, n: recipe)
    monkeypatch.setattr(louise, "_as_candidate", lambda r, a: r)
    monkeypatch.setattr(run_check, "calibration_items", lambda *a: ["x"] * 3)
    monkeypatch.setattr(run_check, "probe_for", lambda *a: "louise")
    stated = SimpleNamespace(driver="cpu", run_id="sha256:" + "c" * 64, engine={}, weights="w")
    monkeypatch.setattr(run_check, "stated_run", lambda *a, **k: stated)
    monkeypatch.setattr(emb, "gate_texts", lambda c, role, items: list(items))
    monkeypatch.setattr(emb, "reproduces", lambda a, b: True)
    monkeypatch.setattr(fingerprint, "indices", lambda texts: [0])
    monkeypatch.setattr(config, "EMBED_CONCURRENCY", 2)
    asked = []

    def answers(r, **kw):
        asked.append((r.batch, kw.get("timeout")))
        fn = lambda texts: [[1.0, 0.0] for _ in texts]
        fn.runs = {stated.run_id}
        return fn
    monkeypatch.setattr(run_check, "answers", answers)
    monkeypatch.setattr(cpu_ref, "write", lambda *a, **k: tmp_path / "ref.json")

    class Held:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(encoder_use, "held", lambda *a, **k: Held())
    a = SimpleNamespace(root=tmp_path, collection="wikidata", model="vega-bge-m3-cpu",
                        query_model=None)
    assert louise.cmd_embedder_cpu_reference(a) == 0
    assert asked == [(1, 4800.0), (1, 4800.0)], "one text a request, the window's bound"


# ---- a review line per quantization level, measured (operator, 2026-11-01) ----
# "Calculate a reasonable threshold per quantization level, do not just assume
# it is the same as the full precision." The CPU rounds activations to 8 bits for
# every quantised weight type, so each level is measured card against its CPU
# twin and its line computed by one rule; an unmeasured level has none.

@pytest.mark.parametrize("model,level", [
    ("Qwen3-Embedding-4B-Q4_K_M.gguf", "Q4_K_M"),
    ("/models/x-IQ4_XS.gguf", "IQ4_XS"),
    ("model-q8_0.gguf", "Q8_0"),
    ("bge-m3-Q6_K.gguf", "Q6_K"),
    ("bge-m3-FP16.gguf", "FP16"),
    ("MedCPT-Query-Encoder-f16.gguf", "F16"),
    ("Q40.gguf", None),
    (None, None),
])
def test_the_level_is_the_type_tag_of_the_weights_localai_loaded(model, level):
    engine = {"options": {"Model": model}} if model is not None else {"name": "x"}
    assert emb.quant_level(engine) == level


def test_the_rule_gives_headroom_over_the_worst_measured_and_never_tightens_full_precision():
    assert emb.cpu_line_from(0.989292) == 0.983, "gutenberg's Q4_K_M: 1 - 1.5 x 0.010708"
    assert emb.cpu_line_from(0.99) == 0.985
    assert emb.cpu_line_from(0.99993) == 0.995, "never stricter than the operator's 0.995"
    assert emb.cpu_line_from(0.95) == 0.925


@pytest.mark.parametrize("model,line", [
    ("Qwen3-Embedding-4B-Q4_K_M.gguf", 0.983),     # measured: worst 0.989292
    ("bge-m3-FP16.gguf", 0.995),
    ("MedCPT-Query-Encoder-f16.gguf", 0.995),
    ("dar-legal-it", 0.995),                       # no tag: a full-precision backend
    ("bge-m3-Q8_0.gguf", None),                    # quantised, not measured: no line
    ("x-IQ4_XS.gguf", None),
])
def test_each_level_has_its_own_line_or_none_until_measured(model, line):
    assert emb.cpu_review_line({"options": {"Model": model}}) == line


def _engine(model):
    from tests.hold.test_backend_engine import BACKEND
    from dataclasses import replace
    return replace(BACKEND, options={"Model": model, "ModelFile": "${WEIGHTS}"})


def _gated(codex, engine, cpu):
    from tests.hold.test_backend_engine import _vega_run
    ref = reference()
    return emb.run_gate(codex, reference=ref, embed=lambda texts: ref, seed=b"\x09" * 32,
                        stated=_vega_run(codex, engine), embed_one=fake_embed,
                        contribute=fake_embed, cpu_reference=("sha256:" + "44" * 32, cpu))


def test_a_level_is_held_to_its_own_measured_line():
    codex = codex_768()
    ref = reference()
    gutenberg = perturbed(ref, indices=(235,), to=0.989292)
    assert _gated(codex, _engine("Qwen3-Embedding-4B-Q4_K_M.gguf"), gutenberg).ok
    full = _gated(codex, _engine("bge-m3-FP16.gguf"), gutenberg)
    assert not full.ok and "below the review line 0.995" in full.why()
    broken = _gated(codex, _engine("Qwen3-Embedding-4B-Q4_K_M.gguf"),
                    perturbed(ref, indices=(7,), to=0.126))
    assert not broken.ok and "below the review line 0.983" in broken.why(), \
        "a broken engine still fails"


def test_an_unmeasured_level_is_never_judged_on_another_levels_line():
    codex = codex_768()
    ref = reference()
    run = _gated(codex, _engine("bge-m3-Q8_0.gguf"), perturbed(ref, indices=(3,), to=0.9999))
    assert run.cpu_line is None and run.quant == "Q8_0"
    assert not run.ok and "not measured yet" in run.why() and "cross_vendor_check" in run.why()
    from tests.hold.test_backend_engine import _vega_run
    no_reference = emb.run_gate(codex, reference=ref, embed=lambda texts: ref, seed=b"\x09" * 32,
                                stated=_vega_run(codex, _engine("bge-m3-Q8_0.gguf")),
                                embed_one=fake_embed, contribute=fake_embed)
    assert no_reference.ok, "without a CPU reference there is nothing to judge: exact passes"


def test_a_statement_is_admitted_at_its_levels_line():
    gutenberg = {"run": "r", "worst": "0.989292", "median": "0.998358"}
    q4 = {"options": {"Model": "Qwen3-Embedding-4B-Q4_K_M.gguf"}}
    assert _v5(engine=q4, cpu_reference=gutenberg)
    assert not _v5(cpu_reference=gutenberg), "full precision keeps 0.995"
    assert not _v5(engine=q4, cpu_reference={"run": "r", "worst": "0.982", "median": "0.99"})
    q8 = {"options": {"Model": "bge-m3-Q8_0.gguf"}}
    assert not _v5(engine=q8, cpu_reference={"run": "r", "worst": "0.9999", "median": "1"}), \
        "an unmeasured level is not admitted against a CPU reference"
    assert _v5(engine=q8), "and nothing to judge without one"

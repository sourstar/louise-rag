"""`create` holds the runs that embedded its reference to the recipe's engine
and weights; `hold embedder run` prints an endpoint's stated run (07 §7.3;
docs/notes/v5-migration.md G5).

The Codex names the engine its reference vectors were computed with, written
out so anyone can compute them again (02 §2.4, 09 §9.3). An `api = "vega"`
endpoint states the run of every answer (its receipts): a reference computed
by another engine than the recipe's would make that field false, and it is
refused before anything is written.
"""
import json

import pytest

from app import embeddings, louise, run_check
from app.bundles import keys as node_keys
from app.hold.codex import CodexError, canonical_json, digest
from app.hold.embedder import StatedRun
from tests.hold.conftest import KEY, PLATFORM, Q_WEIGHTS, WEIGHTS, fake_embed
from tests.hold.test_template import ENGINE_TABLE, SPACE, recipe

URL = "http://localai.invalid:8080"


def a_run(engine=None, weights=WEIGHTS) -> StatedRun:
    engine = dict(engine or ENGINE_TABLE)
    obj = {"driver": "xe; libze 26.05", "engine": engine, "weights": weights}
    return StatedRun(run_id=digest(canonical_json(obj)), engine=engine, weights=weights,
                     driver=obj["driver"])


def _recipe(**kw):
    from app.collections import EmbedRecipe
    return EmbedRecipe(url=URL, api="vega", model="vega-m", dtype="f16", ctx=512, dim=768, **kw)


def test_the_runs_that_embedded_the_reference_are_the_recipes_engine(monkeypatch):
    run = a_run()
    monkeypatch.setattr(run_check, "describe_run", lambda recipe, rid, **kw: run)
    louise.check_reference_runs(SPACE, _recipe(), {run.run_id})


@pytest.mark.parametrize("what,run", [
    ("engine", a_run(engine={**ENGINE_TABLE, "build": "b99999"})),
    ("weights", a_run(weights="sha256:" + "5e" * 32)),
])
def test_another_engine_or_other_weights_are_refused(monkeypatch, what, run):
    monkeypatch.setattr(run_check, "describe_run", lambda recipe, rid, **kw: run)
    with pytest.raises(CodexError, match=f"{what} .*Nothing was created"):
        louise.check_reference_runs(SPACE, _recipe(), {run.run_id})


def test_an_answer_that_stated_no_run_is_refused():
    with pytest.raises(CodexError, match="stated no run"):
        louise.check_reference_runs(SPACE, _recipe(), {None})
    with pytest.raises(CodexError, match="stated no run"):
        louise.check_reference_runs(SPACE, _recipe(), set())


def test_the_query_side_is_held_to_its_own_weights_on_the_document_sides_engine(monkeypatch):
    """09 §9.3: a query encoder that names no engine runs on the document
    side's."""
    run = a_run(weights=Q_WEIGHTS)
    monkeypatch.setattr(run_check, "describe_run", lambda recipe, rid, **kw: run)
    louise.check_reference_runs(SPACE, _recipe(), {run.run_id}, query=True)
    with pytest.raises(CodexError, match="weights"):
        louise.check_reference_runs(SPACE, _recipe(), {run.run_id})


def _create(tmp_path, monkeypatch, run):
    seen = {}

    def embed_calibration(recipe, texts, *, runs=None, **kw):
        seen.setdefault("runs", []).append(runs)
        if runs is not None:
            runs.add(run.run_id)
        return fake_embed(texts)
    monkeypatch.setattr(embeddings, "embed_calibration", embed_calibration)
    monkeypatch.setattr(run_check, "describe_run",
                        lambda recipe, rid, **kw: run if rid == run.run_id else None)
    monkeypatch.setattr(node_keys, "load_signing_key", lambda *a, **k: KEY)
    r = recipe(space={**SPACE, "query": {"encoder": None, "instruction": None}})
    return seen, lambda: louise.create_from(tmp_path, "gutenberg", r, platform_image=PLATFORM,
                                            encoder=URL, api="vega", embed_model="vega-m",
                                            name="checked")


def test_create_refuses_a_reference_from_another_engine_before_anything_is_written(
        tmp_path, monkeypatch):
    _seen, create = _create(tmp_path, monkeypatch, a_run(engine={**ENGINE_TABLE, "build": "b1"}))
    with pytest.raises(CodexError, match="engine"):
        create()
    assert not (tmp_path / "checked").exists() and not (tmp_path / "checked.toml").exists()


def test_create_with_the_recipes_engine_goes_ahead(tmp_path, monkeypatch):
    seen, create = _create(tmp_path, monkeypatch, a_run())
    codex, held = create()
    assert codex.space.encoder.engine.to_dict() == ENGINE_TABLE
    assert held.is_file() and all(r is not None for r in seen["runs"]), \
        "a vega endpoint's runs are collected on every embedding of the set"


def test_embedder_run_prints_the_stated_run(monkeypatch, capsys):
    run = a_run()
    asked = {}

    def stated(recipe, *, query=False, probe_item=run_check.PROBE):
        asked.update(api=recipe.api, model=recipe.model, query=query, probe=probe_item)
        return run
    monkeypatch.setattr(run_check, "stated_run", stated)
    assert louise.main(["hold", "embedder", "run", "--encoder", URL, "--api", "vega",
                        "--model", "vega-m", "--pair"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"run": run.run_id, "engine": run.engine, "weights": run.weights,
                   "driver": run.driver}
    assert asked == {"api": "vega", "model": "vega-m", "query": False,
                     "probe": run_check.PAIR_PROBE}


def test_embedder_run_needs_a_model_and_one_form(capsys):
    assert louise.main(["hold", "embedder", "run", "--encoder", URL]) == 2
    assert "--model" in capsys.readouterr().err
    assert louise.main(["hold", "embedder", "run", "--encoder", URL, "--model", "m",
                        "--query", "--pair"]) == 2

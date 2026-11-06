"""`hold collection new`: a new collection, one workflow, every input from
the recipe (operator, 2026-11-06: "A mess up in a collection embedding is
the most severe flaw we can have, this is not acceptable. I expected that a
clear workflow for the creation of a new collection had been implemented").

The steps' own machinery is tested where it lives (calibrate, create,
cpu-reference, the gate); here, that `new` feeds them the recipe's inputs
and nothing else, holds each step to the one before, and stops at the
first that fails."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import louise, run_check
from app.hold import calibrate as calib
from app.hold import collection as col

WEIGHTS = "sha256:" + "ab" * 32
SET = "sha256:" + "5e" * 32
ENGINE = {"name": "localai/llama-cpp", "image": "sha256:" + "11" * 32}


def _recipe(tmp_path, **space_over) -> str:
    space = {"encoder": {"weights": WEIGHTS, "engine": ENGINE}, "ctx": 8191, "cut": "tokens",
             "document": {"instruction": "passage: "}, "calibration": {"set": SET},
             "query": {"instruction": "query: "}}
    space.update(space_over)
    p = tmp_path / "recipe.json"
    p.write_text(json.dumps({"space": space}), encoding="utf-8")
    return str(p)


@pytest.fixture
def steps(monkeypatch, tmp_path):
    """Every step stood in for, recording what it was given."""
    seen: dict = {"order": []}

    monkeypatch.setattr(calib, "load_set", lambda set_id: (set_id, ["t1", "t2"]))
    monkeypatch.setattr(louise.cx, "engine_from_dict", lambda d, where: SimpleNamespace(engine_id="e", **d)
                        if isinstance(d, dict) else d)

    def stated_run(recipe, probe_item=None):
        seen["twin"] = recipe
        return SimpleNamespace(driver=seen.get("driver", "cpu"), weights=seen.get("twin_weights", WEIGHTS),
                               engine=dict(ENGINE), run_id="sha256:run-twin")
    monkeypatch.setattr(run_check, "stated_run", stated_run)

    def http_embed(ctx, *, document_instruction=None, set_id=None, cut="characters"):
        seen["calibrate"] = {"ctx": ctx, "instruction": document_instruction, "set": set_id, "cut": cut}
        return lambda ep, texts: None
    monkeypatch.setattr(calib, "http_embed", http_embed)

    def measure(reference, against, *, set_id, texts, embed):
        seen["order"].append("calibrate")
        seen["endpoints"] = (reference, against)
        return {"tolerance": seen.get("measured", "0.999995"), "set": set_id}
    monkeypatch.setattr(calib, "measure", measure)

    def create_from(root, plugin, recipe, **kw):
        seen["order"].append("create")
        seen["created"] = (plugin, recipe, kw)
        (root / "wd" / "state").mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(collection_id="sha256:" + "cc" * 32), root / "wd.toml"
    monkeypatch.setattr(louise, "create_from", create_from)
    monkeypatch.setattr(col, "name_of", lambda root, ref: "wd")

    def cpu_reference(a):
        seen["order"].append("cpu-reference")
        seen["cpu_args"] = a
        return seen.get("cpu_rc", 0)
    monkeypatch.setattr(louise, "cmd_embedder_cpu_reference", cpu_reference)

    def gate(a):
        seen["order"].append("gate")
        return seen.get("gate_rc", 0)
    monkeypatch.setattr(louise, "cmd_embedder_check", gate)
    return seen


def _new(tmp_path, recipe, *extra):
    return louise.main(["hold", "collection", "new", "wikidata", "--root", str(tmp_path), "--recipe", recipe,
                        "--encoder", "http://localai:8080", "--api", "vega", "--embed-model", "vega-nemo",
                        "--cpu-model", "vega-nemo-cpu", *extra])


def test_every_input_comes_from_the_recipe_and_the_steps_run_in_order(tmp_path, steps):
    """The calibration is measured at the recipe's ctx, cut, document
    instruction and set: the inputs the gate sends (2026-11-06: by
    characters, for a space that cuts by tokens)."""
    assert _new(tmp_path, _recipe(tmp_path)) == 0
    assert steps["calibrate"] == {"ctx": 8191, "instruction": "passage: ", "set": SET, "cut": "tokens"}
    assert (steps["twin"].model, steps["twin"].cut, steps["twin"].ctx) == ("vega-nemo-cpu", "tokens", 8191)
    reference, against = steps["endpoints"]
    assert (reference.model, reference.weights, against[0].model) == ("vega-nemo", WEIGHTS, "vega-nemo-cpu")
    assert steps["order"] == ["calibrate", "create", "cpu-reference", "gate"]
    assert steps["created"][1]["space"]["tolerance"] == "0.999995", "the measured tolerance, into create"
    assert steps["cpu_args"].refresh and not steps["cpu_args"].again, \
        "the collection's copy written again from the store, never recomputed"
    kept = json.loads((tmp_path / "wd" / "state" / "calibration.json").read_text())
    assert (kept["tolerance"], kept["ctx"], kept["cut"], kept["instruction"]) == ("0.999995", 8191, "tokens",
                                                                                  "passage: ")


def test_a_tolerance_the_recipe_states_must_be_the_measured_one(tmp_path, steps):
    assert _new(tmp_path, _recipe(tmp_path, tolerance="0.9999")) == 2
    assert "create" not in steps["order"], "refused before anything is made"
    assert _new(tmp_path, _recipe(tmp_path, tolerance="0.999995")) == 0


def test_a_twin_that_is_not_the_cpu_or_not_the_weights_is_refused(tmp_path, steps):
    for key, value in (("driver", "sycl"), ("twin_weights", "sha256:" + "cd" * 32)):
        steps.pop("driver", None), steps.pop("twin_weights", None)
        steps[key] = value
        assert _new(tmp_path, _recipe(tmp_path)) == 2
        assert "calibrate" not in steps["order"]


def test_a_failed_step_stops_the_workflow_and_the_gate_decides_the_exit(tmp_path, steps):
    steps["cpu_rc"] = 1
    assert _new(tmp_path, _recipe(tmp_path)) == 1
    assert "gate" not in steps["order"]
    steps["order"].clear()
    steps["cpu_rc"], steps["gate_rc"] = 0, 1
    assert _new(tmp_path, _recipe(tmp_path)) == 1


def test_a_collection_already_made_is_held_to_the_recipe(tmp_path, steps, monkeypatch):
    sp = SimpleNamespace(ctx=8191, cut="characters", document_instruction="passage: ",
                         calibration=SimpleNamespace(set=SET), tolerance="0.999995",
                         encoder=SimpleNamespace(weights=WEIGHTS, engine=ENGINE))
    codex = SimpleNamespace(space=sp)
    monkeypatch.setattr(col, "resolve", lambda root, name: ("wd", codex))
    assert _new(tmp_path, _recipe(tmp_path), "--name", "wd") == 2
    assert "create" not in steps["order"] and "cpu-reference" not in steps["order"]
    sp.cut = "tokens"
    steps["order"].clear()
    assert _new(tmp_path, _recipe(tmp_path), "--name", "wd") == 0
    assert steps["order"] == ["calibrate", "cpu-reference", "gate"], "kept, not created again"
    sp.encoder = SimpleNamespace(weights="sha256:" + "ef" * 32, engine=ENGINE)
    steps["order"].clear()
    assert _new(tmp_path, _recipe(tmp_path), "--name", "wd") == 2, "another encoder's collection: refused"

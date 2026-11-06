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
from app.hold import workflow as wf

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

    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", "sha256:" + "9a" * 32)
    monkeypatch.setenv("LOUISE_ROOT", str(tmp_path / "louise"))
    monkeypatch.setattr(col, "dry_codex", lambda plugin, recipe, **kw: _codex(**recipe["space"]))
    monkeypatch.setattr(calib, "load_set", lambda set_id: (set_id, ["t1", "t2"]))
    monkeypatch.setattr(louise.cx, "engine_from_dict", lambda d, where: SimpleNamespace(engine_id="e", **d)
                        if isinstance(d, dict) else d)

    def stated_run(recipe, probe_item=None):
        if recipe.model.endswith("-cpu"):
            seen["twin"] = recipe
            return SimpleNamespace(driver=seen.get("driver", "cpu"), weights=seen.get("twin_weights", WEIGHTS),
                                   engine=dict(ENGINE), run_id="sha256:run-twin")
        seen["card"] = recipe
        return SimpleNamespace(driver="sycl", weights=WEIGHTS, engine=dict(ENGINE),
                               run_id=seen.get("card_run", "sha256:run-card"))
    monkeypatch.setattr(run_check, "stated_run", stated_run)

    def http_embed(ctx, *, document_instruction=None, set_id=None, cut="characters"):
        seen["calibrate"] = {"ctx": ctx, "instruction": document_instruction, "set": set_id, "cut": cut}
        return lambda ep, texts: None
    monkeypatch.setattr(calib, "http_embed", http_embed)

    def measure(reference, against, *, set_id, texts, embed):
        seen["order"].append("calibrate")
        seen["endpoints"] = (reference, against)
        return {"tolerance": seen.get("measured", "0.999995"), "set": set_id,
                "reference": {"runs": [seen.get("card_run", "sha256:run-card")]}}
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


def _codex(*, encoder=None, ctx=8191, cut="characters", document=None, calibration=None, tolerance=None,
           layers="one", **_):
    """A Codex as `new` reads it: its space, and a `space_id` that moves with
    what is inside it (02 §2.4) and not with the tolerance or calibration."""
    encoder = encoder or {"weights": WEIGHTS, "engine": ENGINE}
    engine = SimpleNamespace(engine_id="engine:" + json.dumps(encoder["engine"], sort_keys=True))
    sp = SimpleNamespace(ctx=ctx, cut=cut, document_instruction=(document or {}).get("instruction"),
                         calibration=SimpleNamespace(set=(calibration or {}).get("set", SET)),
                         tolerance=tolerance, encoder=SimpleNamespace(weights=encoder["weights"], engine=engine))
    sid = "sha256:" + json.dumps([encoder["weights"], ctx, cut, sp.document_instruction, layers])
    return SimpleNamespace(space=sp, space_id=sid, collection_id="sha256:" + "dd" * 32)


def _new(tmp_path, recipe, *extra):
    """`new` with the test's recipe; `--name wd` unless `extra` gives the name."""
    return louise.main(["hold", "collection", "new", "wikidata", "--root", str(tmp_path), "--recipe", recipe,
                        "--encoder", "http://localai:8080", "--api", "vega", "--embed-model", "vega-nemo",
                        "--cpu-model", "vega-nemo-cpu", *(extra or ("--name", "wd"))])


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


def test_a_collection_already_made_is_held_to_the_recipe_by_space_id(tmp_path, steps, monkeypatch, capsys):
    """By `space_id`, its engine and its calibration set — not five fields
    (the review of 2026-11-06: engine, dim, dtype, pooling and the layers
    went unchecked)."""
    held = {"document": {"instruction": "passage: "}, "tolerance": "0.999995"}
    monkeypatch.setattr(col, "resolve", lambda root, name: ("wd", _codex(cut="characters", **held)))
    assert _new(tmp_path, _recipe(tmp_path), "--name", "wd") == 2
    assert steps["order"] == [], "refused before any measurement"
    assert "cut 'characters', the recipe's 'tokens'" in capsys.readouterr().err
    monkeypatch.setattr(col, "resolve", lambda root, name: ("wd", _codex(cut="tokens", **held)))
    assert _new(tmp_path, _recipe(tmp_path), "--name", "wd") == 0
    assert steps["order"] == ["calibrate", "cpu-reference", "gate"], "kept, not created again"
    for other, says in (({"encoder": {"weights": "sha256:" + "ef" * 32, "engine": ENGINE}}, "encoder weights"),
                        ({"encoder": {"weights": WEIGHTS, "engine": {**ENGINE, "image": "sha256:" + "22" * 32}}},
                         "engine"),
                        ({"layers": "two"}, "its layers or cleaning"),
                        ({"calibration": {"set": "sha256:" + "77" * 32}}, "calibration set")):
        monkeypatch.setattr(col, "resolve", lambda root, name, other=other: ("wd", _codex(cut="tokens", **held, **other)))
        steps["order"].clear()
        assert _new(tmp_path, _recipe(tmp_path), "--name", "wd") == 2, other
        assert steps["order"] == [] and says in capsys.readouterr().err, other


def test_an_existing_collection_of_another_tolerance_is_refused_after_the_measurement(tmp_path, steps,
                                                                                     monkeypatch):
    monkeypatch.setattr(col, "resolve", lambda root, name: ("wd", _codex(
        cut="tokens", document={"instruction": "passage: "}, tolerance="0.99999")))
    assert _new(tmp_path, _recipe(tmp_path)) == 2
    assert steps["order"] == ["calibrate"], "measured, then refused: not changed in place"


def test_a_question_encoder_is_case_b_and_refused_before_anything_is_asked(tmp_path, steps, capsys):
    """Any `space.query.encoder`, its weights the document side's or not:
    `create` would need the question side's reference, after the card had
    measured (the review of 2026-11-06). Case B is phase 3."""
    for weights in (WEIGHTS, "sha256:" + "ef" * 32):
        recipe = _recipe(tmp_path, query={"instruction": "query: ", "encoder": {"weights": weights}})
        assert _new(tmp_path, recipe) == 2
        assert "case B" in capsys.readouterr().err
    assert steps["order"] == [] and "twin" not in steps, "not even the twin was asked"


def test_a_stopped_run_resumes_and_a_done_calibration_is_checked_not_redone(tmp_path, steps):
    steps["cpu_rc"] = 1
    assert _new(tmp_path, _recipe(tmp_path)) == 1
    kept = json.loads(wf.progress_path("wd").read_text())
    assert kept["case"] == "A" and set(kept["steps"]) == {"calibrate", "tolerance", "make"}, \
        "kept after each step's own effect; cpu-reference failed, not kept"
    steps["order"].clear()
    steps["cpu_rc"] = 0
    assert _new(tmp_path, _recipe(tmp_path)) == 0
    assert steps["order"] == ["create", "cpu-reference", "gate"], "the card states the calibrated run: kept"
    assert steps["card"].model == "vega-nemo" and steps["card"].cut == "tokens"
    steps["order"].clear()
    steps["card_run"] = "sha256:another-run"
    assert _new(tmp_path, _recipe(tmp_path)) == 0
    assert steps["order"][0] == "calibrate", "another run on the card: measured again"


def test_another_request_under_the_same_name_starts_afresh(tmp_path, steps, capsys):
    steps["cpu_rc"] = 1
    assert _new(tmp_path, _recipe(tmp_path)) == 1
    steps["order"].clear()
    assert _new(tmp_path, _recipe(tmp_path, ctx=4096)) == 1
    assert steps["order"][0] == "calibrate" and "started afresh" in capsys.readouterr().err


def test_a_passed_gate_says_what_would_still_stop_embedding(tmp_path, steps, capsys):
    """RULE 2 (2026-11-06): wikidata was turned on after `new` ended on "the
    gate PASSED" and sat paused on its own for hours, carried over by a
    rename; nothing had said so."""
    from app import node_state
    node_state.set_worker_state("running")
    node_state.set_paused("wd", True, by="rename wikidata -> wd")
    assert _new(tmp_path, _recipe(tmp_path)) == 0
    err = capsys.readouterr().err
    assert "wd: the gate PASSED; embedding stays off until the operator turns it on" in err
    assert "wd: turned on, it still embeds nothing until this is lifted:\n" \
           '  wd is paused on its own, by "rename wikidata -> wd"' in err, err
    assert "`louise node resume wd`" in err
    assert 'embed setting is "off"' not in err, "the line above says it"
    steps["gate_rc"] = 1
    assert _new(tmp_path, _recipe(tmp_path)) == 1
    assert "still embeds nothing" not in capsys.readouterr().err, "a failed gate embeds nothing anyway"
    steps["gate_rc"] = 0
    node_state.set_paused("wd", False, by="louise node resume wd")
    assert _new(tmp_path, _recipe(tmp_path)) == 0
    assert "still embeds nothing" not in capsys.readouterr().err, "nothing stands: nothing said"


def test_the_steps_are_the_workflows_numbered_in_case_as_order(tmp_path, steps, capsys):
    assert _new(tmp_path, _recipe(tmp_path)) == 0
    said = [line.split()[1] for line in capsys.readouterr().err.splitlines() if line[:2] in
            {f"{i}/" for i in range(1, 10)}]
    assert said == list(wf.ordered(wf.case("A")))


def test_the_dry_codex_is_the_space_create_makes(tmp_path):
    """`col.dry_codex`, which `new` holds an existing collection to, gives
    the `space_id` and the engine `create` writes."""
    from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed, fake_query_embed
    from tests.hold.test_template import recipe
    root = tmp_path / "collections"
    root.mkdir()
    made = col.create_from_recipe(root, "gutenberg", recipe(), platform_image=PLATFORM, embed=fake_embed,
                                  embed_query=fake_query_embed, name="g", key=KEY, now=NOW)
    dry = col.dry_codex("gutenberg", recipe(), platform_image=PLATFORM)
    assert dry.space_id == made.space_id
    assert dry.space.encoder.engine.engine_id == made.space.encoder.engine.engine_id
    assert louise._differs_from(made, dry) == []


def test_the_name_is_the_operators_and_is_asked_for(tmp_path, steps):
    """RULE 2: the local name is the one thing only the operator knows."""
    with pytest.raises(SystemExit):
        louise.main(["hold", "collection", "new", "wikidata", "--root", str(tmp_path),
                     "--recipe", _recipe(tmp_path), "--encoder", "http://localai:8080",
                     "--cpu-model", "vega-nemo-cpu"])
    assert steps["order"] == []

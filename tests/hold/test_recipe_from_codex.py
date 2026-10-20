"""tools/recipe_from_codex.py: the recipe a root Codex is rebuilt from, byte for
byte, and its token-cut twin (char-cut-truncation.md §5)."""
import json

import pytest

from app.hold import codex as cx
from tools import recipe_from_codex as rfc

from .conftest import PLATFORM
from .test_backend_engine import _backend_recipe
from .test_template import from_recipe


@pytest.fixture
def live():
    """A version-6 root, as pubmed's live Codex is."""
    c = from_recipe("gutenberg", _backend_recipe(), platform_image=PLATFORM)
    assert c.version == 6 and c.relation == "root"
    return c


def test_the_recipe_rebuilds_the_codex_byte_for_byte(live):
    recipe = rfc.recipe_of(live)
    for computed in ("envelope",):
        assert computed not in recipe["space"]
    assert "vectors" not in recipe["space"]["calibration"]
    assert rfc.build(live, recipe).canonical() == live.canonical()


def test_the_cut_twin_is_version_8_in_its_own_space(live, tmp_path):
    src, out = tmp_path / "codex.json", tmp_path / "recipe.json"
    src.write_bytes(live.canonical())
    assert rfc.main([str(src), str(out), "--cut", "tokens"]) == 0
    recipe = json.loads(out.read_text(encoding="utf-8"))
    assert recipe["space"]["cut"] == "tokens"
    twin = rfc.build(live, recipe)
    assert twin.version == 8 and twin.space_id != live.space_id
    a, b = live.to_dict(), twin.to_dict()
    assert {k for k in a if a[k] != b.get(k)} == {"space", "version"}
    assert {k for k in a["space"] if a["space"][k] != b["space"].get(k)} | \
        (set(b["space"]) - set(a["space"])) == {"cut"}, "nothing else in the space moves"


def test_the_instruction_twin_moves_the_query_instruction_alone(live, tmp_path):
    """A1f's target: the space's query prefix set, nothing else in the Codex moved."""
    src, out = tmp_path / "codex.json", tmp_path / "recipe.json"
    src.write_bytes(live.canonical())
    instruction = "Instruct: Given a search for a book, retrieve passages of that book\nQuery:"
    assert rfc.main([str(src), str(out), "--instruction", instruction]) == 0
    recipe = json.loads(out.read_text(encoding="utf-8"))
    twin = rfc.build(live, recipe)
    assert twin.space.query.instruction == instruction and twin.space_id != live.space_id
    a, b = live.to_dict(), twin.to_dict()
    assert {k for k in a if a[k] != b.get(k)} == {"space"}
    assert {k for k in a["space"] if a["space"][k] != b["space"][k]} == {"query"}
    assert {k for k in a["space"]["query"] if a["space"]["query"][k] != b["space"]["query"][k]} == \
        {"instruction"}
    assert rfc.main([str(src), str(out), "--instruction", ""]) == 0
    assert rfc.build(live, json.loads(out.read_text(encoding="utf-8"))).canonical() == live.canonical(), \
        "an empty instruction is none, as the live Codex holds"


def test_a_codex_that_is_not_a_root_has_no_recipe(live):
    from dataclasses import replace
    sister = replace(live, derived_from="sha256:" + "ab" * 32)
    with pytest.raises(cx.CodexError, match="makes a root"):
        rfc.recipe_of(sister)


def test_a_side_takes_the_engine_its_endpoint_states_and_never_other_weights(live):
    recipe = rfc.recipe_of(live)
    run = {"run": "sha256:" + "11" * 32, "weights": live.space.encoder.weights,
           "engine": dict(live.space.encoder.engine.to_dict(), build="cafe0000beef")}
    rfc.with_run(recipe, run)
    twin = rfc.build(live, recipe)
    assert twin.space.encoder.engine.build == "cafe0000beef"
    assert twin.space_id == live.space_id, "the engine is outside space_id"
    with pytest.raises(cx.CodexError, match="another space"):
        rfc.with_run(recipe, dict(run, weights="sha256:" + "22" * 32))

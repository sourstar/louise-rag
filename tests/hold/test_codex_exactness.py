"""A Codex may REQUIRE bit-exact runs (operator, 2026-11-01: "if it useful add a
parameter in the codex specifying if the collection requires bit exactness or
near bit exactness, we don't know yet what the futures will bring"; 09 §9.3
`space.exactness`, EXACTNESS_VERSION; CLAUDE.md invariant 12).

Absent is the near-exact rule, so no Codex already written moves; "exact" is
the one value, written at version 10 (version 9 plus the field), outside
`space_id` and inside `collection_id`; a near-exact run contributes nothing to
such a collection, and the node's own setting can only be stricter."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.hold import codex as cx, embedder as emb, runs as hold_runs
from app.hold.codex import CodexError
from tests.hold.conftest import pubmed
from tests.hold.conftest import PLATFORM
from tests.hold.test_template import SPACE, from_recipe, recipe


def build(r):
    return from_recipe("gutenberg", r, platform_image=PLATFORM)


def _v10(**space_over) -> dict:
    d = dict(json.loads(pubmed(version=5).canonical()), version=10)
    d["slicing"] = dict(d["slicing"], container="jsonl")
    d["space"] = dict(d["space"], exactness="exact", **space_over)
    return d


def test_a_space_that_requires_exact_runs_is_version_10_and_keeps_its_space():
    base = pubmed(version=5)
    ten = cx.from_dict(_v10())
    cx.validate(ten)
    assert ten.version == 10 and ten.space.exactness == "exact"
    assert ten.space_id == base.space_id, "it admits runs; it is not the function"
    assert ten.collection_id != base.collection_id, "it is the collection's rule"
    assert cx.loads(ten.canonical()) == ten
    assert cx.written_version(ten.space, 5) == cx.EXACTNESS_VERSION
    assert base.space.exactness is None and "exactness" not in json.loads(base.canonical())["space"], \
        "absent — the near-exact rule — so no Codex already written moves"


@pytest.mark.parametrize("change,match", [
    (lambda d: dict(d, space={k: v for k, v in d["space"].items() if k != "exactness"}),
     "exactness"),
    (lambda d: dict(d, space=dict(d["space"], exactness="near")), "requires bit-exact runs"),
])
def test_version_10_declares_exact_and_nothing_else(change, match):
    with pytest.raises(CodexError, match=match):
        cx.from_dict(change(_v10()))


def test_below_version_10_the_key_is_unknown():
    d = json.loads(pubmed(version=5).canonical())
    d["space"] = dict(d["space"], exactness="exact")
    with pytest.raises(CodexError):
        cx.from_dict(d)


def test_create_writes_it_at_version_10_with_the_container():
    codex = build(recipe(space=dict(SPACE, exactness="exact")))
    assert codex.version == cx.EXACTNESS_VERSION and codex.space.exactness == "exact"
    assert codex.slicing["container"] == "jsonl"
    cx.validate(codex)
    plain = build(recipe())
    assert plain.version < cx.EXACTNESS_VERSION and plain.space.exactness is None
    assert plain.space_id == codex.space_id


def _near_body(codex) -> dict:
    return {"version": 5, "passed": 16, "median": "1.000000", "exactness": emb.NEAR,
            "loads": emb.NEAR_LOADS, "load_variation": "0.999995",
            "cpu_reference": {"run": "sha256:" + "44" * 32, "worst": "0.9995", "median": "0.9999"}}


def test_a_near_exact_run_contributes_nothing_to_a_collection_that_requires_exact():
    near = cx.from_dict(_v10())
    rule = pubmed(version=5)
    assert hold_runs.passing(_near_body(rule), rule), "the rule admits it"
    assert not hold_runs.passing(_near_body(near), near), "the Codex requires bytes"
    exact = dict(_near_body(near), exactness=emb.EXACT, load_variation="1")
    assert hold_runs.passing(exact, near)
    assert hold_runs.requires_exact(near) and not hold_runs.requires_exact(rule)


def test_the_gate_holds_a_node_to_the_codex_whatever_its_own_setting(monkeypatch, tmp_path):
    from app import run_check
    from types import SimpleNamespace
    seen = {}
    monkeypatch.setattr(run_check, "reference", lambda *a, **k: [[1.0]] * 512)
    monkeypatch.setattr(run_check, "stated_run", lambda *a, **k: SimpleNamespace(weights="w"))
    monkeypatch.setattr(run_check, "calibration_items", lambda *a, **k: [])
    monkeypatch.setattr(run_check, "_mark", lambda *a, **k: None)
    monkeypatch.setattr(run_check, "contributing", lambda r: None)
    from app.hold import cpu_reference
    monkeypatch.setattr(cpu_reference, "find", lambda *a, **k: None)
    monkeypatch.setattr(run_check.runs_mod, "validate",
                        lambda *a, **k: seen.setdefault("exactness", k["exactness"]) and {"run": "r"})
    recipe_ = SimpleNamespace(exactness="near", api="openai", url="http://e", model="m")
    run_check._validate(tmp_path, "c", cx.from_dict(_v10()), recipe_, key=object())
    assert seen["exactness"] == emb.EXACT
    seen.clear()
    run_check._validate(tmp_path, "c", pubmed(version=5), recipe_, key=object())
    assert seen["exactness"] == "near"

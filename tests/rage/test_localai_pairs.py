"""Text pairs through the front over LocalAI (docs/notes/input-forms.md, RAGE;
app/rage/pairs.py): `input_pairs` reaches LocalAI as one marked string per pair
only for a model whose YAML declares `pairs:true`, which is then in the run;
every other model, and every plain input carrying the marker, is refused —
a pair is never flattened."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.rage import pairs
from app.rage.front_localai import yaml_config
from app.rage.wire import RUN_HEADER, Refused

from .localai_fake import hand_written, pinned
from .test_localai_front import _put, node  # noqa: F401 — the fixture

ROOT = Path(__file__).resolve().parents[2]
PAIRED = "medcpt-article-ov"


def _pair_model(node, options=("parallel:1", "pairs:true")):
    hand_written(node.fake.models_dir, PAIRED,
                 pinned("bge-m3.gguf", options=list(options)))
    node.fake.reload_configs()


def _embeddings_sent(node) -> list[dict]:
    return [body for _m, path, body in node.fake.calls if path == "/v1/embeddings"]


def _ask(node, name, **body):
    return node.http.post(f"/upstream/{name}/v1/embeddings", json=body)


# ---- the translation ---------------------------------------------------------------------

def test_a_pair_reaches_localai_as_one_marked_string_and_the_run_names_pairs(node):
    _pair_model(node)
    r = _ask(node, PAIRED, model="whatever",
             input_pairs=[["Aspirin and cancer.", "We studied \"aspirin\".\n\nResults: ü"],
                          ["Title only", ""]])
    assert r.status_code == 200, r.text
    (sent,) = _embeddings_sent(node)
    assert pairs.FIELD not in sent and sent["model"] == PAIRED
    assert sent["input"] == [
        pairs.MARK + json.dumps(["Aspirin and cancer.", "We studied \"aspirin\".\n\nResults: ü"],
                                ensure_ascii=False),
        pairs.MARK + json.dumps(["Title only", ""])]
    run_id = r.headers[RUN_HEADER]
    run = node.http.get(f"/louise/runs/{run_id}").json()
    assert "pairs:true" in run["engine"]["options"]["Options"], \
        "the declaration is in the run, so in its run_id"


def test_a_plain_input_to_a_pair_model_passes_as_it_came(node):
    _pair_model(node)
    r = _ask(node, PAIRED, model=PAIRED, input=["a query"])
    assert r.status_code == 200 and RUN_HEADER in r.headers
    assert _embeddings_sent(node)[-1]["input"] == ["a query"]


# ---- the refusals ------------------------------------------------------------------------

def test_a_model_that_does_not_declare_pairs_refuses_them_never_flattening(node):
    _put(node)                                       # bge-m3: options ["parallel:1"]
    r = _ask(node, "bge-m3", input_pairs=[["a", "b"]])
    assert r.status_code == 400
    assert "pairs:true" in r.json()["error"] and "never flattened" in r.json()["error"]
    assert _embeddings_sent(node) == [], "nothing reached LocalAI"


@pytest.mark.parametrize("why,setup", [
    ("no model YAML", lambda node: None),
    ("cannot be read as JSON", lambda node: (node.fake.models_dir / f"{PAIRED}.yaml")
     .write_text("name: x\noptions:\n  - pairs:true\n", encoding="utf-8")),
])
def test_a_model_whose_options_cannot_be_read_refuses_pairs(node, why, setup):
    setup(node)
    r = _ask(node, PAIRED, input_pairs=[["a", "b"]])
    assert r.status_code == 400 and why in r.json()["error"]
    assert _embeddings_sent(node) == []


@pytest.mark.parametrize("name", ["bge-m3", PAIRED])
@pytest.mark.parametrize("given", [pairs.MARK + '["a", "b"]', ["ok", "x" + pairs.MARK]])
def test_a_plain_input_carrying_the_marker_is_refused_for_every_model(node, name, given):
    _put(node)
    _pair_model(node)
    r = _ask(node, name, input=given)
    assert r.status_code == 400 and "marker" in r.json()["error"]
    assert _embeddings_sent(node) == []


@pytest.mark.parametrize("body,why", [
    ({"input": ["a"], "input_pairs": [["a", "b"]]}, "one or the other"),
    ({"input_pairs": []}, "non-empty list"),
    ({"input_pairs": [["a"]]}, "non-empty list"),
    ({"input_pairs": [["a", 1]]}, "non-empty list"),
    ({"input_pairs": "a"}, "non-empty list"),
    ({"input_pairs": [["a", pairs.MARK]]}, "marker"),
])
def test_a_malformed_pair_request_is_refused(node, body, why):
    _pair_model(node)
    r = _ask(node, PAIRED, **body)
    assert r.status_code == 400 and why in r.json()["error"]
    assert _embeddings_sent(node) == []


def test_pairs_belong_to_embeddings_only(node):
    _pair_model(node)
    r = node.http.post(f"/upstream/{PAIRED}/v1/chat/completions",
                       json={"input_pairs": [["a", "b"]]})
    assert r.status_code == 400 and "embeddings" in r.json()["error"]


def test_a_process_loaded_without_pairs_never_answers_a_pair(node):
    """The YAML hand-edited to declare pairs while the model ran without:
    the answer would be a marked string embedded as text."""
    _pair_model(node, options=("parallel:1",))
    assert _ask(node, PAIRED, input=["load it"]).status_code == 200
    hand_written(node.fake.models_dir, PAIRED,
                 pinned("bge-m3.gguf", options=["parallel:1", "pairs:true"]))
    r = _ask(node, PAIRED, input_pairs=[["a", "b"]])
    assert r.status_code == 502 and "pairs:true" in r.json()["error"]


# ---- the pieces ----------------------------------------------------------------------------

def test_the_option_is_read_exactly():
    assert pairs.declares_pairs(["device:xpu", "pairs:true"])
    assert pairs.declares_pairs(["pairs: true"])
    for no in (["pairs:false"], ["pairs"], [], None, "pairs:true", ["xpairs:true"]):
        assert not pairs.declares_pairs(no), no


def test_the_translation_refuses_without_touching_a_model(tmp_path):
    body = {"input_pairs": [["a", "b"]]}
    with pytest.raises(Refused) as e:
        pairs.translate(body, "m", None, "why it is unknown")
    assert e.value.status == 400 and "why it is unknown" in str(e.value)
    assert body == {"input_pairs": [["a", "b"]]}


def test_the_repos_yamls_read_as_the_front_reads_them():
    path = ROOT / "deploy" / "rage" / "localai-models" / f"{PAIRED}.yaml"
    assert pairs.declares_pairs(yaml_config(path)["options"])
    others = [p for p in path.parent.glob("*.yaml") if p != path]
    assert others and not any(pairs.declares_pairs(yaml_config(p).get("options"))
                              for p in others), "only the pair backend's model declares pairs"


def test_the_front_and_the_backends_share_one_marker():
    spec = importlib.util.spec_from_file_location(
        "lb_pairs", ROOT / "deploy" / "localai" / "backends" / "louise_backend.py")
    lb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lb)
    assert lb.PAIR_MARK == pairs.MARK
    first, second = 'Título "x"', "line1\n\nline2 \x00 [] {}"
    assert lb.decode_pair(pairs.encode(first, second)) == (first, second)
    assert lb.decode_pair("plain text") == "plain text"
    for bad in (pairs.MARK + "not json", pairs.MARK + '["one"]', pairs.MARK + '["a", 2]',
                "text " + pairs.MARK):
        with pytest.raises(ValueError):
            lb.decode_pair(bad)

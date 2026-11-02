"""A space may prefix its DOCUMENTS (operator, 2026-11-02: Nemotron-3-Embed for
wikidata, gutenberg and wikipedia; 09 §9.3 `space.document`, DOCUMENT_VERSION;
docs/notes/nemotron-embed.md).

Nemotron is trained with "passage: " before every document, and a document
embedded without it is not the function the card and our measurements
describe. `space.document.instruction` is prepended to every document input by
the one client that sends documents (`embeddings._sent`), and nowhere else; it
changes every document vector, so it is INSIDE `space_id`. Absent where a space
declares none, so no Codex, space or `collection_id` already written moves;
written at version 11, version 10 plus the field."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app import collections as collections_mod, embeddings
from app.hold import carry, codex as cx
from app.hold.codex import CodexError
from tests.hold.conftest import PLATFORM, pubmed
from tests.hold.test_template import SPACE, from_recipe, recipe

PASSAGE = "passage: "


def _v11(**document) -> dict:
    d = dict(json.loads(pubmed(version=5).canonical()), version=11)
    d["slicing"] = dict(d["slicing"], container="jsonl")
    d["space"] = dict(d["space"], exactness="exact",
                      document=document or {"instruction": PASSAGE})
    return d


def _v10() -> dict:
    d = _v11()
    d["version"] = 10
    d["space"] = {k: v for k, v in d["space"].items() if k != "document"}
    return d


def test_a_document_instruction_is_version_11_and_inside_the_space():
    ten = cx.from_dict(_v10())
    eleven = cx.from_dict(_v11())
    cx.validate(eleven)
    assert eleven.version == cx.DOCUMENT_VERSION and eleven.space.document_instruction == PASSAGE
    assert eleven.space_id != ten.space_id, "every document vector is a function of it"
    assert cx.loads(eleven.canonical()) == eleven
    assert json.loads(eleven.canonical())["space"]["document"] == {"instruction": PASSAGE}
    assert cx.written_version(eleven.space, 5) == cx.DOCUMENT_VERSION
    assert ten.space.document_instruction is None
    assert "document" not in json.loads(ten.canonical())["space"], \
        "absent where none is declared, so no space_id already written moves"
    other = cx.from_dict(_v11(instruction="search_document: "))
    assert other.space_id not in (eleven.space_id, ten.space_id)


def test_the_query_instruction_alone_still_shares_document_vectors_but_not_this():
    eleven = cx.from_dict(_v11())
    assert eleven.admitted_spaces[0] == eleven.space_id
    ten = cx.from_dict(_v10())
    assert ten.space_id not in eleven.admitted_spaces, \
        "a space without the document prefix holds other document vectors"


@pytest.mark.parametrize("change,match", [
    (lambda d: dict(d, space={k: v for k, v in d["space"].items() if k != "document"}),
     "document"),
    (lambda d: dict(d, space=dict(d["space"], document=None)), "document instruction"),
    (lambda d: dict(d, space=dict(d["space"], document={"instruction": ""})), "non-empty"),
    (lambda d: dict(d, space=dict(d["space"], document={"instruction": PASSAGE, "x": 1})),
     "unknown field"),
    (lambda d: dict(d, space=dict(d["space"], document={})), "missing field"),
])
def test_version_11_declares_a_document_instruction_and_nothing_else(change, match):
    with pytest.raises(CodexError, match=match):
        cx.from_dict(change(_v11()))


def test_below_version_11_the_key_is_unknown():
    d = _v10()
    d["space"] = dict(d["space"], document={"instruction": PASSAGE})
    with pytest.raises(CodexError, match="unknown field"):
        cx.from_dict(d)


def test_a_pair_layer_takes_no_document_instruction():
    eleven = cx.from_dict(_v11())
    paired = replace(eleven, layers=tuple(replace(l, input=dict(cx.INPUT_PAIR))
                                          for l in eleven.layers))
    with pytest.raises(CodexError, match="feed the encoder pairs"):
        cx.validate(paired)


def test_create_writes_it_at_version_11():
    space = dict(SPACE, exactness="exact", document={"instruction": PASSAGE})
    codex = from_recipe("gutenberg", recipe(space=space), platform_image=PLATFORM)
    cx.validate(codex)
    assert codex.version == cx.DOCUMENT_VERSION and codex.space.document_instruction == PASSAGE
    plain = from_recipe("gutenberg", recipe(space=dict(SPACE, exactness="exact")),
                        platform_image=PLATFORM)
    assert plain.version == cx.EXACTNESS_VERSION and plain.space_id != codex.space_id


def test_a_carry_never_crosses_a_document_instruction():
    eleven, ten = cx.from_dict(_v11()), cx.from_dict(_v10())
    assert carry.space_differences(eleven, ten) == ["document.instruction"]
    assert "document.instruction" in carry.ENCODER_SIDE, \
        "a collection holding no vector may change it (A1h), like the encoder"


# ---- the one client that sends documents --------------------------------------

def _recipe(**over):
    return collections_mod.EmbedRecipe(url="http://e", api="openai", model="m", dtype="f16",
                                       ctx=512, **over)


def test_every_document_gets_the_prefix_and_no_query_does():
    r = _recipe(document_instruction=PASSAGE, query_instruction="query: ")
    assert embeddings._sent(["a", "b"], r, False, False) == ["passage: a", "passage: b"]
    assert embeddings._sent(["a", "b"], r, False, True) == ["passage: a", "passage: b"], \
        "`asked` is the query side's; the gate asks documents with it set"
    assert embeddings._sent(["q"], r, True, False) == ["query: q"]
    assert embeddings._sent(["query: q"], r, True, True) == ["query: q"]
    bare = _recipe(query_instruction="query: ")
    assert embeddings._sent(["a"], bare, False, False) == ["a"], "no prefix where none is declared"


def test_a_pair_document_is_refused_a_prefix():
    r = _recipe(document_instruction=PASSAGE)
    with pytest.raises(embeddings.InputFormError, match="prefixes a text"):
        embeddings._sent([("t", "a")], r, False, False)


def test_the_client_sends_the_prefixed_text_on_both_paths(monkeypatch):
    sent = []

    def batch(b, timeout, recipe, raw=False, runs=None):
        sent.extend(b)
        return [[1.0, 0.0] for _ in b]
    monkeypatch.setattr(embeddings, "_embed_batch", batch)
    r = _recipe(document_instruction=PASSAGE)
    embeddings.embed_calibration(r, ["x"])
    embeddings.embed_calibration(r, ["y"], one_at_a_time=True, asked=True)
    assert sent == ["passage: x", "passage: y"]


def test_a_collection_file_cannot_state_it():
    for key in ("document_instruction", "document_prefix", "passage_prefix"):
        with pytest.raises(ValueError, match="not a node's to state"):
            collections_mod.parse_recipe({"url": "http://e", key: PASSAGE}, where="t")


def test_create_embeds_the_reference_with_it(monkeypatch):
    from app import louise
    seen = []
    monkeypatch.setattr(embeddings, "embed_calibration",
                        lambda recipe, texts, **k: seen.append(recipe) or [[1.0]] * len(texts))
    space = dict(SPACE, document={"instruction": PASSAGE})
    louise._embed_for_recipe(space, "http://e/upstream/m", "m")(["t"])
    louise._embed_for_recipe(space, "http://e/upstream/m", "m", query=True)(["t"])
    assert [r.document_instruction for r in seen] == [PASSAGE, None], \
        "the document reference with it; the query side never"

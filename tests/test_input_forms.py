"""Input forms: what an encoder is fed is part of the space.

docs/notes/input-forms.md (operator, 2026-10-08); 02 §2.4 and 09 §9.3 as
amended. Phase 1: a version-5 Codex declares each layer's `input`, inside
`space_id`, and a version-4 Codex keeps its bytes and both its ids. Phase 2:
ONE entry point (`embeddings.embed_chunks`) builds a chunk's encoder input
from its layer's form; a pair travels as `input_pairs`.
"""
from dataclasses import replace

import pytest

from app import chunking, collections as collections_mod, config, embeddings
from app import layers as layers_mod
from app.hold import codex as cx, records as rec
from app.hold.codex import Chunker, CodexError, Layer
from tests.hold.conftest import PLATFORM, pubmed
from tests.hold.test_codex import WORKED_EXAMPLE, _h
from tests.hold.test_template import from_recipe, recipe as a_recipe

PAIR = {"form": "pair", "split": "first-section"}
TEXT = {"form": "text"}
ABSTRACT = Chunker("abstract", 1, {})


def _v5(form=None, chunker=ABSTRACT) -> cx.Codex:
    return pubmed(version=5, layers=(Layer(chunker, "default", form),))


# ---- phase 1: the Codex ------------------------------------------------------

def test_a_version_4_codex_keeps_its_bytes_and_both_its_ids():
    """The amendment moves nothing already written: the worked example (a
    version-4 Codex) hashes exactly as 09 §9.3 prints it, with no `input`."""
    v4 = pubmed()
    assert v4.version == 4
    assert all("input" not in l for l in v4.to_dict()["layers"])
    assert v4.collection_id == _h({k: v for k, v in WORKED_EXAMPLE.items()
                                   if k != "descriptive"})
    assert v4.space_id == _h({
        "cleaning_version": 1,
        "encoder": {"weights": v4.space.encoder.weights},
        "layers": [{"chunker": {"id": "abstract", "version": 1, "params": {}},
                    "rule": "default"}],
        "space": {"ctx": 512, "dim": 768, "dtype": "f16", "epoch": "0",
                  "normalise": True, "pooling": "cls",
                  "query": {"encoder": {"weights": v4.space.query.encoder.weights},
                            "instruction": None}}})
    # a construction naming the text form below 5 is the same Codex
    spelled = pubmed(layers=(Layer(ABSTRACT, "default", dict(TEXT)),))
    assert (spelled.collection_id, spelled.space_id) == (v4.collection_id, v4.space_id)
    assert cx.loads(cx.dumps(v4)).collection_id == v4.collection_id


def test_a_new_codex_is_version_5_and_every_layer_says_its_form():
    assert cx.CODEX_VERSION == cx.INPUT_VERSION == 5
    c = _v5()
    assert c.layers[0].input == TEXT and c.layers[0].form == "text"
    assert c.to_dict()["layers"][0]["input"] == TEXT
    assert cx.loads(cx.dumps(c)).to_dict() == c.to_dict()


def test_the_form_is_inside_space_id():
    text, pair = _v5(), _v5(PAIR)
    assert text.space_id != pair.space_id
    assert text.space_id != pubmed().space_id, "a version-5 text layer is another space"
    sp = pair.space
    assert pair.space_id == _h({
        "cleaning_version": 1,
        "encoder": {"weights": sp.encoder.weights},
        "layers": [{"chunker": {"id": "abstract", "version": 1, "params": {}},
                    "rule": "default", "input": PAIR}],
        "space": {"ctx": 512, "dim": 768, "dtype": "f16", "epoch": "0",
                  "normalise": True, "pooling": "cls",
                  "query": {"encoder": {"weights": sp.query.encoder.weights},
                            "instruction": None}}})
    assert cx.loads(cx.dumps(pair)).layers[0].input == PAIR


@pytest.mark.parametrize("form,chunker,match", [
    (PAIR, Chunker("fixed", 1, {"tokens": 12, "overlap": 4}), "one chunk per record"),
    ({"form": "triple"}, ABSTRACT, "unknown input form"),
    ({"form": "pair", "split": "last-section"}, ABSTRACT, "unknown pair split"),
    ({"form": "pair"}, ABSTRACT, "carries exactly"),
    ({"form": "text", "split": "first-section"}, ABSTRACT, "carries exactly"),
])
def test_a_form_the_platform_cannot_build_is_refused(form, chunker, match):
    with pytest.raises(CodexError, match=match):
        cx.validate(_v5(form, chunker))


def test_below_version_5_a_pair_cannot_be_said():
    with pytest.raises(CodexError, match="needs version 5"):
        pubmed(layers=(Layer(ABSTRACT, "default", dict(PAIR)),))


def test_the_key_follows_the_version_when_read():
    d4 = pubmed().to_dict()
    d4["layers"][0]["input"] = TEXT
    with pytest.raises(CodexError, match="unknown field"):
        cx.from_dict(d4)
    d5 = _v5().to_dict()
    del d5["layers"][0]["input"]
    with pytest.raises(CodexError, match="missing field"):
        cx.from_dict(d5)


def test_a_recipe_may_declare_a_layer_s_input():
    layer = {"chunker": {"id": "abstract", "version": 1}, "input": PAIR}
    codex = from_recipe("gutenberg", a_recipe(layers=[layer]), platform_image=PLATFORM)
    assert codex.version == 5 and codex.layers[0].input == PAIR
    cx.validate(codex)
    plain = from_recipe("gutenberg", a_recipe(layers=[{"chunker": layer["chunker"]}]),
                        platform_image=PLATFORM)
    assert plain.layers[0].input == TEXT, "a recipe that names none gets text"
    bad = dict(layer, chunker={"id": "fixed", "version": 1,
                               "params": {"tokens": 1000, "overlap": 120}})
    with pytest.raises(CodexError, match="one chunk per record"):
        cx.validate(from_recipe("gutenberg", a_recipe(layers=[bad]),
                                platform_image=PLATFORM))


def test_every_plugin_template_defaults_to_text():
    from app.hold import template
    for name in ("gutenberg", "pubmed", "wikipedia", "normattiva"):
        for l in template.default_layers(template.source_for(name)):
            assert l.input is None or l.input == TEXT
            assert l.form == "text"


def test_the_platform_layer_carries_the_form():
    assert layers_mod.layers_for(_v5(PAIR))[0].input == PAIR
    assert layers_mod.layers_for(pubmed())[0].input == TEXT, "below 5 a layer reads as text"
    parsed = layers_mod.parse_layers([{"chunker": "abstract", "input": PAIR}], where="t")
    assert parsed[0].form == "pair"
    with pytest.raises(layers_mod.LayerError, match="one chunk per record"):
        layers_mod.parse_layers([{"chunker": "cdc", "input": PAIR}], where="t")


# ---- the chunker: `fields` beside the text, never in the id or the root -------

SECTIONS = [("title", "BRCA2 and repair"), ("abstract", "First part."),
            ("conclusions", "Second part.")]
ROW = {"uid": "pubmed:7", "native_id": 7}


def test_abstract_chunks_carry_fields_outside_id_text_and_root():
    """Golden: the id, the text and `chunks_root` are what they were before
    the amendment; a pair layer's chunk adds `fields`, a text layer's
    carries none."""
    pair = layers_mod.chunks_for_document(_v5(PAIR), ROW, SECTIONS)
    text = layers_mod.chunks_for_document(_v5(), ROW, SECTIONS)
    old = layers_mod.chunks_for_document(pubmed(), ROW, SECTIONS)
    assert [c["id"] for c in pair] == ["pubmed:7:0:1"]
    assert pair[0]["text"] == "BRCA2 and repair\n\nFirst part.\n\nSecond part."
    assert pair[0]["fields"] == ("BRCA2 and repair", "First part.\n\nSecond part.")
    assert "fields" not in text[0] and "fields" not in old[0]
    assert {k: v for k, v in pair[0].items() if k != "fields"} == text[0] == old[0]
    root = rec.chunks_root((c["id"], c["text"]) for c in pair)
    assert root == rec.chunks_root((c["id"], c["text"]) for c in text) == \
        "sha256:" + __import__("hashlib").sha256(
            b"pubmed:7:0:1" + __import__("hashlib").blake2b(
                pair[0]["text"].encode(), digest_size=16).digest()).hexdigest()


# ---- phase 2: one entry point ------------------------------------------------

RECIPE = collections_mod.EmbedRecipe(url="http://enc:1", api="openai", model="m",
                                     dtype="f32", ctx=512)


@pytest.fixture
def posts(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(config, "EMBED_BATCH", 16)
    monkeypatch.setattr(config, "EMBED_CONCURRENCY", 1)

    class Response:
        status_code = 200
        headers: dict = {}

        def __init__(self, n):
            self.n = n

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"index": i, "embedding": [1.0, float(i)]}
                             for i in range(self.n)]}

    def post(url, json=None, timeout=None, headers=None):
        calls.append(json)
        return Response(len(json.get("input") or json.get("input_pairs")))
    monkeypatch.setattr(embeddings.httpc, "post", post)
    return calls


def test_a_text_layer_sends_the_text(posts):
    chunks = layers_mod.chunks_for_document(_v5(), ROW, SECTIONS)
    assert len(embeddings.embed_chunks(_v5(), chunks, recipe=RECIPE)) == 1
    assert posts == [{"input": [chunks[0]["text"]], "model": "m"}]


def test_a_pair_layer_sends_input_pairs(posts):
    codex = _v5(PAIR)
    chunks = layers_mod.chunks_for_document(codex, ROW, SECTIONS)
    vectors = embeddings.embed_chunks(codex, chunks, recipe=RECIPE)
    assert len(vectors) == 1
    assert posts == [{"input_pairs": [["BRCA2 and repair", "First part.\n\nSecond part."]],
                      "model": "m"}]


def test_a_pair_layer_chunk_without_fields_is_refused(posts):
    codex = _v5(PAIR)
    with pytest.raises(embeddings.InputFormError, match="fields"):
        embeddings.embed_chunks(codex, ["a stored text"], recipe=RECIPE)
    with pytest.raises(embeddings.InputFormError, match="fields"):
        embeddings.embed_chunks(codex, [{"text": "no fields"}], recipe=RECIPE)
    assert posts == [], "nothing is flattened into a string and sent"


def test_a_pair_on_an_api_without_input_pairs_is_refused(posts):
    codex = _v5(PAIR)
    chunks = layers_mod.chunks_for_document(codex, ROW, SECTIONS)
    with pytest.raises(embeddings.InputFormError, match="ollama"):
        embeddings.embed_chunks(codex, chunks, recipe=replace(RECIPE, api="ollama"))
    assert posts == []


def test_several_layers_keep_order_and_never_mix_a_batch(posts):
    """A pair layer beside a text one: each chunk in its own layer's form,
    one request per form, vectors back in the chunks' order."""
    codex = pubmed(version=5, layers=(
        Layer(Chunker("verbatim", 1, {}), "field:type=review", None),
        Layer(ABSTRACT, "default", dict(PAIR))))
    items = [{"text": "plain one"},
             {"text": "t\n\na", "fields": ("t", "a")},
             {"text": "plain two"}]
    vectors = embeddings.embed_chunks(codex, items, recipe=RECIPE)
    assert posts == [{"input_pairs": [["t", "a"]], "model": "m"},
                     {"input": ["plain one", "plain two"], "model": "m"}]
    # the second text is index 1 of its request: in third place, not second
    assert vectors[0] == vectors[1] == [1.0, 0.0] and vectors[2][1] > 0


def test_a_pair_is_truncated_on_its_second_segment():
    assert embeddings._cut(("title", "x" * 50), 10) == ("title", "xxxxx")
    assert embeddings._cut("y" * 50, 10) == "y" * 10


def test_queries_stay_single_text(posts):
    q = replace(RECIPE, query_url="http://query:2", query_instruction="query: ")
    embeddings.embed_query(_v5(PAIR), "brca2", recipe=q)
    assert posts == [{"input": ["query: brca2"]}]


def test_calibration_texts_are_text_whatever_the_layer(posts):
    embeddings.embed_calibration(RECIPE, ["one", "two"])
    embeddings.embed_calibration(RECIPE, ["three"], one_at_a_time=True)
    assert posts == [{"input": ["one", "two"], "model": "m"},
                     {"input": ["three"], "model": "m"}]


def test_chunk_input_hands_a_text_layer_its_text():
    text = layers_mod.chunks_for_document(_v5(), ROW, SECTIONS)[0]
    pair = layers_mod.chunks_for_document(_v5(PAIR), ROW, SECTIONS)[0]
    assert embeddings.chunk_input(text) == text["text"]
    assert embeddings.chunk_input(pair) is pair
    assert chunking.ABSTRACT in layers_mod.PAIR_CHUNKERS

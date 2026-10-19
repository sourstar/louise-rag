"""tests/test_plugins_wikidata_chunker.py — the `wikidata-text` chunker (06
§6.3): Wikimedia's `chunk_item_text` rule over our rendered text and a
tokenizer named by sha256 — the whole text when it fits, else head + the
instance-of line + the longest run of statements that fits, an over-long
statement cut at the token that fits; ids across sections; the slicing
scheme `range=`."""
import json
from pathlib import Path

import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

from app import chunking, layers
from app.pack import slicing
from app.plugins.wikidata import chunker, ids, textify

FIXTURES = Path(__file__).parent / "fixtures" / "wikidata-mini"
SHA = "sha256:" + "ab" * 32


def _q3331_text() -> str:
    line = [l for l in (FIXTURES / "dump-sample.jsonl").read_bytes().split(b"\n") if b'"id":"Q3331"' in l][0]
    obj = json.loads(line.rstrip(b",\n"))
    labels = {"P31": "instance of", "P17": "country", "P131": "located in", "P625": "coordinate location",
              "P1619": "date of official opening", "P137": "operator", "P580": "start time", "P373": "Commons category",
              "P18": "image", "P646": "Freebase ID", "P454": "Structurae ID", "P856": "official website",
              "P1766": "place name sign", "P2505": "carries", "P2581": "BabelNet ID", "P407": "language",
              "Q2354973": "road tunnel", "Q142": "France", "Q3351": "Lyon", "Q3337": "Lyon Metropolis",
              "Q16665897": "Métropole de Lyon", "Q1044097": "Boulevard de la Croix-Rousse", "Q150": "French"}
    return textify.entity_text(obj, "en", lambda e, lang: labels.get(e))


@pytest.fixture(autouse=True)
def word_tokenizer():
    """One token per whitespace-separated word, offsets kept: enough to make
    `max_tokens` a count of words."""
    tok = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    chunker.register(SHA, tok)
    yield tok
    chunker._tokenizers.pop(SHA, None)


def test_the_instance_of_line_comes_first_and_the_text_splits_into_head_and_statements():
    text = _q3331_text()
    head, lines = chunker.split_text(text)
    assert head == "tunnel de la Croix-Rousse, road tunnel in Lyon. Attributes include:"
    assert lines[0] == "instance of: road tunnel", "the sticky line, rendered first by textify"
    assert "date of official opening: 19 April 1952" in lines and len(lines) >= 10


def test_a_text_that_fits_is_one_chunk_and_a_long_one_repeats_head_and_sticky_line():
    text = _q3331_text()
    words = len(text.split())
    one = list(chunker.chunk_wikidata_text("wikidata:3331", 3331, [("en", text)],
                                           params={"tokenizer": SHA, "max_tokens": words + 1}))
    assert len(one) == 1 and one[0]["text"] == text and one[0]["id"] == "wikidata:3331:0:1" and one[0]["chapter"] == "en"
    many = list(chunker.chunk_wikidata_text("wikidata:3331", 3331, [("en", text), ("fr", text)],
                                            params={"tokenizer": SHA, "max_tokens": 40}))
    assert len(many) > 2
    head, lines = chunker.split_text(text)
    for c in many:
        assert c["text"].startswith(head + "\n- instance of: road tunnel"), "every window says what it is about"
        assert len(c["text"].split()) < 40
    # every statement appears exactly once across the windows of a section
    en = [c for c in many if c["chapter"] == "en"]
    seen = []
    for c in en:
        _, ls = chunker.split_text(c["text"])
        seen += ls[1:]
    assert seen == lines[1:], "the statements in order, each once, no overlap beyond head and sticky line"
    assert [c["id"] for c in many] == [f"wikidata:3331:{i}:1" for i in range(len(many))], "seq runs across sections"
    assert [c["chapter"] for c in many] == ["en"] * len(en) + ["fr"] * (len(many) - len(en))
    assert many == list(chunker.chunk_wikidata_text("wikidata:3331", 3331, [("en", text), ("fr", text)],
                                                    params={"tokenizer": SHA, "max_tokens": 40})), "deterministic"


def test_an_over_long_statement_is_cut_at_the_token_that_fits_and_the_rules_refuse_what_they_must():
    head = "Thing, a thing. Attributes include:"
    sticky = "instance of: thing"
    long_line = "description: " + " ".join(f"w{i}" for i in range(60))
    text = f"{head}\n- {sticky}\n- {long_line}\n- country: France"
    got = list(chunker.chunk_wikidata_text("wikidata:1", 1, [("en", text)], params={"tokenizer": SHA, "max_tokens": 20}))
    assert len(got) == 2
    assert got[0]["text"].startswith(f"{head}\n- {sticky}\n- description: w0") and len(got[0]["text"].split()) == 20, \
        "upstream's _trim_to_length keeps exactly max_tokens tokens (a whole window keeps fewer)"
    assert got[1]["text"] == f"{head}\n- {sticky}\n- country: France"
    with pytest.raises(chunker.ChunkerError, match="sha256"):
        list(chunker.chunk_wikidata_text("wikidata:1", 1, [("en", text)], params={"max_tokens": 20}))
    with pytest.raises(chunker.ChunkerError, match="not in"):
        chunker.tokenizer_for("sha256:" + "cd" * 32)
    with pytest.raises(chunker.ChunkerError, match="sha256"):
        chunker.tokenizer_for("not-a-digest")


def test_the_chunker_is_registered_and_the_layer_parses(tmp_path):
    assert chunking._CHUNKERS[chunking.WIKIDATA_TEXT] is not None
    assert chunking.chunk_version_for(chunking.WIKIDATA_TEXT) == chunker.CHUNK_VERSION == 1
    (layer,) = layers.parse_layers([{"chunker": "wikidata-text", "params": {"max_tokens": 1024, "tokenizer": SHA}}],
                                   where="test")
    assert layer.chunker == "wikidata-text" and layer.version == 1 and layer.params["tokenizer"] == SHA


def test_a_real_tokenizer_file_is_loaded_by_its_sha256(tmp_path, monkeypatch, word_tokenizer):
    from app import config
    path = tmp_path / "tok.json"
    word_tokenizer.save(str(path))
    sha = chunker.sha256_of(path)
    monkeypatch.setattr(config, "TOKENIZERS_DIR", tmp_path)
    (tmp_path / f"{sha[7:]}.json").write_bytes(path.read_bytes())
    chunker._tokenizers.pop(sha, None)
    tok = chunker.tokenizer_for(sha)
    assert tok.encode("a b c", add_special_tokens=False).offsets == [(0, 1), (2, 3), (4, 5)]
    (tmp_path / f"{sha[7:]}.json").write_bytes(b"{}")
    chunker._tokenizers.pop(sha, None)
    with pytest.raises(chunker.ChunkerError, match="not the pinned tokenizer"):
        chunker.tokenizer_for(sha)


def test_the_slicing_scheme_is_the_id_range():
    scheme = slicing.scheme_for("wikidata")
    assert scheme.keys == ("range",) and scheme.records_per_shard == 20_000
    assert scheme.slice_of(None, 42) == "range=0" and scheme.slice_of(None, 23_456_789) == "range=2"
    assert scheme.slice_of(None, ids.PROPERTY_BASE + 31) == "range=100"
    assert slicing.slice_hint("wikidata", {"native_id": "149999999"}) == "range=14"
    assert slicing.slice_hint("wikidata", {"native_id": str(ids.PROPERTY_BASE + 2)}) == "range=100"
    assert scheme.describe == {"keys": ["range"], "range": {"field": "native_id", "bin": 10_000_000, "properties": 100}}


def test_a_layer_without_its_tokenizer_is_refused_before_a_codex_is_minted():
    with pytest.raises(layers.LayerError, match="sha256"):
        layers.parse_layers([{"chunker": "wikidata-text", "params": {"max_tokens": 1024}}], where="t")
    with pytest.raises(layers.LayerError, match="sha256"):
        layers.parse_layers([{"chunker": "wikidata-text", "params": {"max_tokens": 1024, "tokenizer": "abc"}}], where="t")
    with pytest.raises(layers.LayerError, match="max_tokens"):
        layers.parse_layers([{"chunker": "wikidata-text", "params": {"max_tokens": 4, "tokenizer": SHA}}], where="t")
    layers.check_params({"tokens": 1000}, chunker="fixed", where="t")       # other chunkers: unchanged
    # the plugin's default layer carries no tokenizer: a recipe must name one
    from app.hold import template
    (layer,) = template.default_layers(template.source_for("wikidata"))
    assert layer.chunker.id == "wikidata-text" and "tokenizer" not in layer.chunker.params
    with pytest.raises(layers.LayerError, match="recipe states it"):
        layers.check_params(layer.chunker.params, chunker=layer.chunker.id, where="layers[0]")

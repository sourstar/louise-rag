"""06 §6.3: `cdc` takes `{target, min, max}` (characters) from its layer's
params, which 09 §9.3 hashes into `space_id`. A layer that names none cuts
at the version-1 constants, byte for byte, so no pinned space moves."""
import random

import pytest

from app import chunking
from app.chunking import ChunkerError, cdc_split, chunk_cdc
from app.hold import template
from app.plugins.wikipedia import SOURCE as WIKIPEDIA

V1 = {"target": chunking.CDC_TARGET_CHARS, "min": chunking.CDC_MIN_CHARS,
      "max": chunking.CDC_MAX_CHARS}


def _text(n: int) -> str:
    rng = random.Random(7)
    words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]
    return " ".join(rng.choice(words) for _ in range(n))


def test_no_params_and_the_v1_values_cut_identically():
    text = _text(20_000)
    assert cdc_split(text) == cdc_split(text, {}) == cdc_split(text, V1)
    assert [c["text"] for c in chunk_cdc("w:1", 1, [("", text)], 1, {})] == \
        [c["text"] for c in chunk_cdc("w:1", 1, [("", text)], 1, V1)]


def test_the_layers_sizes_are_honoured():
    text = _text(20_000)
    small = {"target": 600, "min": 200, "max": 900}
    pieces = cdc_split(text, small)
    assert "".join(pieces) == text, "nothing lost, nothing added"
    assert max(map(len, pieces)) <= 900
    assert all(len(p) > 200 for p in pieces[:-1])
    assert len(pieces) > len(cdc_split(text)), "smaller sizes, more pieces"


@pytest.mark.parametrize("bad", [
    {"target": 4000, "min": 1000},                              # missing max
    {"target": 4000, "min": 1000, "max": 8000, "mx": 1},        # unknown key
    {"target": 1000, "min": 1000, "max": 8000},                 # min == target
    {"target": 9000, "min": 1000, "max": 8000},                 # target > max
    {"target": "x", "min": 1000, "max": 8000},
])
def test_a_bad_layer_is_refused_before_any_chunk(bad):
    with pytest.raises(ChunkerError):
        list(chunk_cdc("w:1", 1, [("", _text(10))], 1, bad))


def test_wikipedia_declares_the_manuals_cdc_sizes():
    """06 §6.3's own worked example for Wikipedia."""
    layer = template.default_layers(WIKIPEDIA)[-1]
    assert (layer.chunker.id, layer.rule) == ("cdc", "default")
    assert layer.chunker.params == {"target": 4000, "min": 1000, "max": 8000}

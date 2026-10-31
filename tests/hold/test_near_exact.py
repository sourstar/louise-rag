"""Near-exact runs (operator, 2026-10-31, choice C; docs/notes/near-exact-runs.md
§3): the gate measures several fresh loads itself; a run whose answers vary
is admitted only where its collection asked for near-exact, within 0.99999,
and against a stored CPU reference at 0.999."""
from __future__ import annotations

import numpy as np
import pytest

from app.hold import embedder as emb, runs as hold_runs
from tests.hold.conftest import fake_embed
from tests.hold.test_backend_engine import _vega_run
from tests.hold.test_embedder import a_key, codex_768, perturbed, reference


def _nudged(v, cos):
    """`v` turned to cosine `cos` of itself."""
    x = np.asarray(v, dtype="float64")
    x /= np.linalg.norm(x)
    u = np.zeros_like(x)
    u[0], u[1] = -x[1], x[0]
    u /= np.linalg.norm(u)
    return list((cos * x + np.sqrt(max(0.0, 1 - cos * cos)) * u).astype("float32"))


class Card:
    """A model that answers like `fake_embed` but, on load `flip_on`, answers
    `flip_text` slightly otherwise (cosine `cos`) — flash attention's flips."""

    def __init__(self, *, flip_on=None, cos=0.999999):
        self.loads, self.flip_on, self.cos = 0, flip_on, cos
        self.flipped = []

    def reload(self):
        self.loads += 1

    def one(self, texts):
        out = fake_embed(texts)
        if self.flip_on == self.loads:
            k = max(range(len(texts)), key=lambda i: len(texts[i]))   # a long one
            out[k] = _nudged(out[k], self.cos)
            self.flipped.append(texts[k])
        return out


def _gate(card, *, exactness, cpu=True):
    codex = codex_768(version=5)
    ref = reference()
    return codex, emb.run_gate(
        codex, reference=ref, embed=lambda t: ref, seed=b"\x09" * 32,
        stated=_vega_run(codex), embed_one=card.one, contribute=card.one,
        cpu_reference=("sha256:" + "44" * 32, perturbed(ref, indices=(3,), to=0.9995)) if cpu else None,
        reload=card.reload, exactness=exactness)


def test_the_gate_measures_fresh_loads_and_an_exact_model_stays_exact():
    card = Card()
    _codex, run = _gate(card, exactness="exact")
    assert card.loads == emb.NEAR_LOADS, "every gate reloads the model NEAR_LOADS times"
    assert run.ok and run.exactness == "exact" and run.loads == emb.NEAR_LOADS
    assert run.load_variation == 1.0


def test_a_flip_across_loads_fails_an_exact_run_and_says_how_to_ask_for_near():
    card = Card(flip_on=2)
    _codex, run = _gate(card, exactness="exact")
    assert card.flipped, "the flip happened on a fresh load, not within one"
    assert run.reproducible is True, "within one load it gave back its own bytes"
    assert not run.ok and run.exactness == "exact"
    assert "near" in run.why() and run.load_variation < 1.0


def test_a_near_run_within_the_bounds_is_admitted_and_its_statement_says_so():
    card = Card(flip_on=2, cos=0.999995)
    codex, run = _gate(card, exactness="near")
    assert run.ok and run.exactness == "near" and run.loads == emb.NEAR_LOADS
    assert emb.NEAR_VARIATION <= run.load_variation < 1.0
    body = emb.read_statement(emb.statement(run, codex, a_key(), now="2026-10-31T00:00:00Z"))
    assert body["exactness"] == "near" and body["loads"] == emb.NEAR_LOADS
    assert body["load_variation"] != "1" and not body["load_variation"].startswith("1")
    assert hold_runs.passing(body, codex)


@pytest.mark.parametrize("cos,cpu,why", [
    (0.9999, True, "needs 3 loads within"),            # varies too much
    (0.999995, False, "needs a stored CPU reference"),  # nothing to stand against
])
def test_a_near_run_outside_the_bounds_fails(cos, cpu, why):
    card = Card(flip_on=2, cos=cos)
    _codex, run = _gate(card, exactness="near", cpu=cpu)
    assert not run.ok and why in run.why()


def test_a_near_collection_whose_model_measures_exact_is_stated_exact():
    _codex, run = _gate(Card(), exactness="near")
    assert run.ok and run.exactness == "exact", "never weaker than measured"


def test_the_sweep_reaches_every_length_band_and_a_variation_never_reads_as_1():
    texts = ["short", " ".join(f"w{i}" for i in range(600))]
    got = emb.sweep(texts, 32, 512)
    assert len(got) == 32 and len(got[0].split()) == 1 and len(got[-1].split()) == 512
    assert emb._decimal(1.0) == "1"
    assert emb._decimal(0.99999999999) == "0.999999999"
    assert emb._decimal(0.9999951) == "0.999995100"


def test_exactness_is_read_from_the_collections_encoder_block():
    from app import collections as collections_mod
    base = {"url": "http://localai:8080", "embed_model": "m", "query_model": "m"}
    assert collections_mod.parse_recipe(base, where="t").exactness == "exact"
    assert collections_mod.parse_recipe(dict(base, exactness="near"), where="t").exactness == "near"
    with pytest.raises(ValueError, match="exactness"):
        collections_mod.parse_recipe(dict(base, exactness="roughly"), where="t")


def test_only_a_vega_encoder_is_reloaded_by_the_gate():
    from types import SimpleNamespace
    from app import run_check
    assert run_check._reloader(SimpleNamespace(api="openai"), query=False) is None
    assert callable(run_check._reloader(SimpleNamespace(api="vega"), query=False))

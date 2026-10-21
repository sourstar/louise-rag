"""The query instruction is the SPACE's: `space.query.instruction` (02 §2.4,
09 §9.3). "The query side is part of the function: a query is … prefixed
with `space.query.instruction` when that is not null; a document is embedded
by `space.encoder` and never prefixed."

What went wrong (25b30f3, 2026-09-20): search read its prefix from a
node-local `query_prefix` in the collection file, outside `space_id`, while
calibration and the gate prefixed with the Codex's. So a space with an
instruction was gated on one function and searched with another, and nothing
would have said so — both are honest vectors of the right width. The
instruction now reaches the serving side from the Codex and only from it.
"""
from dataclasses import replace

import pytest

from app import collections as collections_mod, embeddings
from app.hold import calibration as cal
from app.hold.codex import Query
from tests.hold.conftest import fake_query_embed, pubmed, replaced

DAR = "query: "                     # DAR-legal-it's; the trailing space is the model's


def _with_query(codex, query: Query):
    return replaced(codex, space=replace(codex.space, query=query))


def _bi_encoder(instruction):
    """PubMed's shape (09 §9.3's worked example): a separate query encoder."""
    codex = pubmed()
    return _with_query(codex, Query(codex.space.query.encoder, instruction))


def _one_model(instruction):
    """DAR-legal-it's shape: ONE model for documents and queries, and an
    instruction on the query side only — the case 25b30f3 was written for."""
    return _with_query(pubmed(), Query(None, instruction))


def _recipe(codex, **encoder):
    return collections_mod._codex_encoder({"url": "http://models:30437", "embed_model": "m",
                                           "query_model": "m", **encoder},
                                          codex, "normattiva.toml")


# ---- it is the space's --------------------------------------------------------------------

@pytest.mark.parametrize("shape", [_bi_encoder, _one_model])
def test_the_instruction_moves_the_space_id(shape):
    """Inside `space_id`, as 02 §2.4 has it: two spaces that differ only in
    their instruction are two functions, and must never share vectors."""
    assert shape(DAR).space_id != shape(None).space_id
    assert shape(DAR).space_id != shape("Instruct: x\nQuery: ").space_id


@pytest.mark.parametrize("shape", [_bi_encoder, _one_model])
def test_the_recipe_takes_it_from_the_codex_byte_for_byte(shape):
    assert _recipe(shape(DAR)).query_instruction == DAR, "the trailing space is kept"
    assert _recipe(shape("Instruct: t\nQuery: ")).query_instruction == "Instruct: t\nQuery: "
    assert _recipe(shape(None)).query_instruction is None, "PubMed v1's case: none"


def test_a_collection_file_cannot_state_another():
    """The node file names WHERE this node reaches the encoder, never what
    the space is — refused like a `dim` that disagrees (`_codex_encoder`)."""
    with pytest.raises(ValueError, match="not a node's to state"):
        _recipe(_one_model(DAR), query_prefix=DAR)


# ---- search runs the function the gate passed --------------------------------------------

def test_search_sends_exactly_what_calibration_embedded(monkeypatch):
    """The whole point: the texts a search puts on the wire for its query
    side are, byte for byte, the texts calibration built the space's query
    reference from (09 §9.12). One character of separator either way and the
    gate would be vouching for a function search never runs."""
    codex = _bi_encoder("Instruct: find statutes\nQuery: ")
    texts = [f"domanda {i}" for i in range(cal.SIZE)]

    calibrated: list[str] = []

    def query_side(batch):
        calibrated.extend(batch)
        return fake_query_embed(batch)

    cal.build_query_member(texts, query_side, instruction=codex.space.query.instruction,
                           normalise=True, dim=codex.space.dim)

    sent: list[str] = []

    class Answer:
        status_code = 200

        def __init__(self, n):
            self.n = n

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(self.n)]}

    def post(url, json=None, timeout=None, headers=None):
        sent.extend(json["input"])
        return Answer(len(json["input"]))

    monkeypatch.setattr(embeddings.httpc, "post", post)
    embeddings._embed_texts(texts, recipe=_recipe(codex), query=True)

    assert sent == calibrated
    assert sent[0] == "Instruct: find statutes\nQuery: domanda 0"


# ---- the gate and the re-check run the function search runs (2026-10-21) -----------------

def _wire(monkeypatch, dim):
    """The endpoint: records every input as it reaches the wire."""
    sent: list[str] = []

    class Answer:
        status_code = 200

        def __init__(self, n):
            self.n = n

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"index": i, "embedding": [1.0] + [0.0] * (dim - 1)}
                             for i in range(self.n)]}

    def post(url, json=None, timeout=None, headers=None):
        sent.extend(json["input"])
        return Answer(len(json["input"]))

    monkeypatch.setattr(embeddings.httpc, "post", post)
    return sent


def test_the_gate_and_the_recheck_send_the_instruction_once_as_calibration_did(monkeypatch):
    """Perugia, 2026-10-21: the gate's query worst was 0.993243 on 16 texts,
    and the same card against the same reference gave 0.997380 over all 512.
    The gate prefixed the instruction (hold/embedder.run_gate), then the
    client prefixed it again (embeddings._asked): every query run was gated
    and re-checked on `instruction + instruction + text`, a function no search
    runs, and every test held one link of the chain, never the chain."""
    from app import run_check
    from app.hold import embedder as emb, runs as hold_runs
    codex = _bi_encoder("Instruct: find statutes\nQuery: ")    # a query run of its own
    recipe = _recipe(codex)
    items = [f"domanda {i}" for i in range(cal.SIZE)]

    calibrated: list[str] = []
    cal.build_query_member(items, lambda b: calibrated.extend(b) or fake_query_embed(b),
                           instruction=codex.space.query.instruction, normalise=True,
                           dim=codex.space.dim)
    reference = [[1.0] + [0.0] * (codex.space.dim - 1)] * cal.SIZE

    for how in ("normalised", "one", "contribute"):
        sent = _wire(monkeypatch, codex.space.dim)
        emb.run_gate(codex, reference=reference, role="query", items=items,
                     embed=run_check.answers(recipe, query=True, how=how))
        assert sent == calibrated, f"the gate's {how} path sends what calibration embedded"

    sent = _wire(monkeypatch, codex.space.dim)
    sampled = list(range(codex.space.calibration.n))
    ok, _passed, _median = hold_runs.recheck(
        codex, {"sampled": sampled}, reference=reference, role="query", items=items,
        embed=run_check.answers(recipe, query=True))
    assert ok and sent == [calibrated[i] for i in sampled], "the re-check too"

    # and search, for the same texts, sends the same bytes (once)
    sent = _wire(monkeypatch, codex.space.dim)
    embeddings._embed_texts(items[:3], recipe=recipe, query=True)
    assert sent == calibrated[:3]
    assert sent[0].count("Instruct: find statutes") == 1

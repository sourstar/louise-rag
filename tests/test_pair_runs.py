"""tests/test_pair_runs.py — a pair space's run is validated on pairs, on the wire.

docs/notes/input-forms.md (operator, 2026-10-08): "a pair space's
calibration set is 512 title-and-abstract pairs from the collection's own
records, so the gate and every probe embed pairs". Against a stand-in for
RAGE that answers `input_pairs` and `input` alike (tests/test_run_check.py's,
taught pairs), this holds the node's side to it: the probe that asks which
run answers, the gate's every pass, the re-check and a check's challenge all
send `input_pairs` to the document encoder; the query encoder gets one text
per pair; and a client that flattened a pair would fail the gate. The
behavioural backend gate (Codex below version 3) and the importer's liveness
probe ask in pairs too.
"""
import json

import pytest

from app import collections as collections_mod, db, embeddings, run_check
from app.hold import calibration as cal, collection as col, shard as shard_mod, export as exp, \
    runs as hold_runs
from app.verify import backend_gate
from tests.hold.conftest import KEY, NOW, PLATFORM
from tests.hold.test_pair_calibration import PAIR_BYTES, PAIRS, _key, pair_recipe
from tests.test_run_check import DOC_URL, QUERY_URL, R1, Rage, opening, opening_query


def pair_opening(items, tag: bytes = b""):
    """The document encoder of a pair space, as `create` meets it and as the
    stand-in below answers: a pair is keyed as a pair, a text as a text."""
    return opening([_key(x) for x in items], tag=tag)


class PairRage(Rage):
    """RAGE serving a `pairs:true` model: `input_pairs` is embedded as pairs.
    `flatten` stands for a front that turned each pair into one string."""

    def __init__(self) -> None:
        super().__init__()
        self.bodies: list[tuple[str, dict]] = []
        self.flatten = False

    def post(self, url, *, json=None, timeout=None, headers=None):      # noqa: A002
        self.bodies.append((url, json))
        if "input_pairs" in json:
            pairs = [tuple(p) for p in json["input_pairs"]]
            items = [f"{a}\n\n{b}" for a, b in pairs] if self.flatten else pairs
            json = {"input": [_key(x) for x in items]}
        elif url.startswith(DOC_URL):
            json = {"input": [_key(t) for t in json["input"]]}
        return super().post(url, json=json, timeout=timeout, headers=headers)


@pytest.fixture
def rage(monkeypatch):
    r = PairRage()
    monkeypatch.setattr(embeddings.httpc, "post", r.post)
    monkeypatch.setattr(run_check.httpx, "get", r.get)
    return r


@pytest.fixture
def node(tmp_path, monkeypatch, rage):
    """A LOUISE_ROOT holding one version-5 PAIR collection reaching its
    encoder through RAGE."""
    root = tmp_path / "louise"
    (root / "collections").mkdir(parents=True)
    codex = col.create_from_recipe(
        root / "collections", "gutenberg", pair_recipe(), platform_image=PLATFORM,
        embed=pair_opening, embed_query=opening_query, name="pairs", key=KEY, now=NOW,
        pairs=PAIR_BYTES)
    assert codex.version == 5 and cal.form(codex) == "pair"
    cfg = tmp_path / "config" / "collections"
    cfg.mkdir(parents=True)
    (cfg / "pairs.toml").write_text(
        f'codex = "{codex.collection_id}"\nembed = "on-demand"\n'
        f'[encoder]\nurl = "{DOC_URL}"\nquery_url = "{QUERY_URL}"\n'
        'embed_model = "medcpt-article"\nquery_model = "medcpt-query"\n', encoding="utf-8")
    monkeypatch.setenv("LOUISE_ROOT", str(root))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(collections_mod.config, "DATA_DIR", root)
    from app.bundles import keys as node_keys
    monkeypatch.setattr(node_keys, "load_signing_key", lambda path=None: KEY)
    run_check.reset_cache()
    backend_gate.reset_cache()
    hold_runs._read.clear()
    collection = next(c for c in collections_mod.load_all() if c.name == "pairs")
    return root / "collections", collection, codex


def _sent(rage, url):
    return [b for u, b in rage.bodies if u.startswith(url)]


def test_the_gate_of_a_pair_space_sends_pairs_and_passes(node, rage):
    root, collection, codex = node
    recipe = collections_mod.recipe_for(collection)
    body = run_check.validate(root, "pairs", codex, recipe, key=KEY)
    assert hold_runs.passing(body, codex) and body["run"] == R1
    assert body["batch_invariant"] is True
    doc = _sent(rage, DOC_URL)
    assert doc and all("input_pairs" in b and "input" not in b for b in doc), \
        "the probe, the passes, the fingerprint and the batches: every one a pair"
    assert doc[0]["input_pairs"] == [list(run_check.PAIR_PROBE)]
    sent = [tuple(p) for b in doc[1:] for p in b["input_pairs"]]
    assert _as_sent(PAIRS) <= set(sent), "each pair, cut to the window as a chunk is"
    assert not _sent(rage, QUERY_URL)


def _as_sent(pairs):
    """The pairs as the client sends them: the title whole, the abstract cut
    to what the window leaves (`embeddings._cut`)."""
    budget = 512 * 4
    return {(a, b[:max(0, budget - len(a))]) for a, b in pairs}


def test_the_query_run_of_a_pair_space_is_gated_on_one_text_per_pair(node, rage):
    root, collection, codex = node
    recipe = collections_mod.recipe_for(collection)
    body = run_check.validate(root, "pairs", codex, recipe, role="query", key=KEY)
    assert hold_runs.passing(body, codex) and body["role"] == "query"
    query = _sent(rage, QUERY_URL)
    assert query and all("input" in b and "input_pairs" not in b for b in query)
    asked = {t for b in query[1:] for t in b["input"]}
    assert {f"{a}\n\n{b}"[:512 * 4] for a, b in PAIRS} <= asked, \
        "one text per pair, cut to the window as a chunk is"


def test_a_front_that_flattens_pairs_fails_the_gate(node, rage):
    """The defect the pair form exists for: the same model fed the flattened
    text is another function. The run is kept as a failure and writes
    nothing (03 §3.3)."""
    root, collection, codex = node
    rage.flatten = True
    body = run_check.validate(root, "pairs", codex,
                              collections_mod.recipe_for(collection), key=KEY)
    assert body["passed"] == 0 and not hold_runs.passing(body, codex)


def test_the_worker_settles_a_pair_space_and_re_checks_on_pairs(node, rage):
    root, collection, codex = node
    con = db.connect(collection)
    assert backend_gate.embed_allowed(con, collection) is True
    before = len(rage.bodies)
    run_check.reset_cache()                      # a new process: re-check, never re-gate
    assert backend_gate.embed_allowed(con, collection) is True
    doc = [b for u, b in rage.bodies[before:] if u.startswith(DOC_URL)]
    assert doc and all("input_pairs" in b for b in doc)
    assert sum(len(b["input_pairs"]) for b in doc) == 1 + codex.space.calibration.n


def test_a_check_s_challenge_goes_out_as_pairs_under_a_validated_run(node, rage):
    root, collection, codex = node
    recipe = collections_mod.recipe_for(collection)
    embed = run_check.checking(root, "pairs", codex, recipe, key=KEY)
    member = root / "pairs" / "shard" / shard_mod.calibration_paths(codex.space.calibration.set)[0]
    before = len(rage.bodies)
    rows = exp.challenge_rows(codex, embed, member)
    assert len(rows) == 16
    # the first sixteen pairs sent are the challenge (as many requests as the
    # node's EMBED_BATCH makes of them); the gate of the new run follows
    sent = [tuple(p) for u, b in rage.bodies[before:] if u.startswith(DOC_URL)
            for p in b["input_pairs"]]
    assert set(sent[:16]) == _as_sent(PAIRS[:16])


def test_the_probe_of_a_text_space_is_still_a_text():
    from tests.hold.conftest import pubmed
    assert run_check.probe_for(pubmed()) == run_check.PROBE
    assert run_check.probe_for(pubmed(), "query") == run_check.PROBE


# ---- the behavioural backend gate and the importer's probe -----------------------------

def _legacy(tmp_path, layers=None):
    from app import layers as layers_mod
    d = tmp_path / "corpus"
    d.mkdir(parents=True, exist_ok=True)
    return collections_mod.Collection(
        name="p", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=d, manifest_path=tmp_path / "manifest.db", fts_dir=tmp_path / "fts",
        lance_dir=tmp_path / "lancedb",
        layers=layers if layers is not None else (
            layers_mod.Layer("abstract", 1, "default", input=dict(layers_mod.INPUT_PAIR)),))


def test_the_backend_gate_probes_a_pair_collection_with_pairs(tmp_path):
    collection = _legacy(tmp_path)
    seen = []
    result = backend_gate.check(collection, lambda items: seen.extend(items)
                                or pair_opening(items))
    assert result["recorded"] is True
    assert seen and all(isinstance(x, tuple) for x in seen)
    assert seen == list(backend_gate.PAIR_PROBES.values())
    # and a front that began flattening them is caught on the next check
    with pytest.raises(backend_gate.BackendGateError, match="no longer reproduces"):
        backend_gate.check(collection, lambda items: pair_opening(
            [f"{a}\n\n{b}" for a, b in items]))
    plain = _legacy(tmp_path / "t", layers=())
    seen.clear()
    backend_gate.check(plain, lambda items: seen.extend(items) or opening(items))
    assert seen == list(backend_gate.PROBES.values()), "a text collection: texts, as ever"
    assert backend_gate._text_hash(("a", "b")) != backend_gate._text_hash("a\n\nb")


def test_the_importer_s_liveness_probe_asks_a_pair_collection_in_pairs(tmp_path, monkeypatch):
    from app.bundles import importer
    asked = []
    monkeypatch.setattr(collections_mod, "recipe_for", lambda c: "a recipe")
    monkeypatch.setattr(embeddings, "embed_calibration",
                        lambda recipe, items, **kw: asked.append(items) or [[1.0]])
    importer._probe_fn(_legacy(tmp_path))(["probe"])
    importer._probe_fn(_legacy(tmp_path / "t", layers=()))(["probe"])
    assert asked == [[("probe", "probe")], ["probe"]]


def test_a_pair_is_refused_on_the_query_side_and_by_a_text_collection():
    from tests.hold.conftest import pubmed
    recipe = collections_mod.EmbedRecipe(url="http://x", api="openai", model="", dtype="f32",
                                         ctx=512)
    with pytest.raises(embeddings.InputFormError, match="one text"):
        embeddings.embed_calibration(recipe, [("a", "b")], query=True)
    with pytest.raises(embeddings.InputFormError, match="no layer"):
        embeddings.embed_chunks(pubmed(), [("a", "b")], recipe=recipe)

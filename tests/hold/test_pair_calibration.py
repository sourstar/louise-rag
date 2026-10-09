"""tests/hold/test_pair_calibration.py — the gate measures the form the
pipeline writes.

docs/notes/input-forms.md ("Before any pair collection exists", operator
2026-10-08); docs/09-formats.md §9.12 and §9.18, docs/03-checking.md §3.3, as
amended 2026-10-08. A space whose layers feed the encoder tokenizer text
PAIRS is calibrated on 512 title-and-abstract pairs drawn from the
collection's own records, carried in its member as the `pairs` tensor, and
every user of the set embeds them as pairs: `create`, `hold embedder
calibrate`, the gate with its fingerprint and batches, the re-check, the
export's challenge and `hold check calibration`. The query side, one text in
every space, gets each pair as `first\\n\\nsecond`.

AND NO TEXT SPACE MOVES. The pins in the first section were computed by the
code before this change (commit 838c005) over the same fixtures: the member
and signature bytes of a text space's `create`, the statement and the
fingerprint of its gate, its fingerprint texts and its export challenge.
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from safetensors.numpy import load_file

from app import layers as layers_mod
from app.hold import (calibrate as calib, calibration as cal, check as chk, codex as cx,
                      collection as col, drag, embedder as emb, export as exp,
                      fingerprint as fp, runs as hold_runs)
from app.hold.calibration import CalibrationError
from app.hold.codex import CodexError
from tests.hold import test_embedder as te
from tests.hold.conftest import (CAL_QVECS, CAL_VECS, ENVELOPE, KEY, NOW, PLATFORM,
                                 a_stated_run, fake_embed, fake_query_embed)
from tests.hold.test_calibration_member import created
from tests.hold.test_template import SPACE, recipe

PAIR = {"form": "pair", "split": "first-section"}
PAIR_LAYER = {"chunker": {"id": "abstract", "version": 1}, "input": PAIR}


# ---- the fixtures: a pair set and a pair-fed encoder ----------------------------

def a_pair_set() -> list[tuple[str, str]]:
    """512 title-and-abstract pairs, every length from a sentence to past a
    512-token window, some abstracts in two sections as structured ones are."""
    pairs = []
    for i in range(cal.SIZE):
        title = f"Study {i} of the lighthouse effect in cohort {i % 37}"
        body = f"Background {i}. " + "We measured the beam at dusk. " * (1 + (i * 7) % 90)
        if i % 5 == 0:
            body += "\n\nMETHODS: a second section, joined as the chunker joins it."
        pairs.append((title, body.strip()))
    return pairs


PAIRS = a_pair_set()
PAIR_BYTES = cal.render_pairs(PAIRS)
PAIR_SET = cal.set_id(PAIR_BYTES)


def _key(item) -> str:
    """A stand-in encoder's key for an input: a pair is NOT its flattened text
    — `("a", "b")` and `"a\\n\\nb"` land in different places, as they do for
    MedCPT (docs/notes/input-forms.md: 0.989 median, not 1)."""
    if isinstance(item, str):
        return "T:" + item[:120]
    first, second = item
    return "P:" + json.dumps([first[:60], second[:60]])


def pair_embed(items, *, tag: bytes = b""):
    return fake_embed([_key(x) for x in items], tag=tag)


def flattening_embed(items, *, tag: bytes = b""):
    """A backend that flattens each pair into one string — the defect the pair
    form exists to keep out."""
    return pair_embed([x if isinstance(x, str) else f"{x[0]}\n\n{x[1]}" for x in items],
                      tag=tag)


def pair_recipe(**over):
    space = dict(SPACE, calibration={"set": PAIR_SET, "quorum": {"n": 16, "min_pass": 12}})
    return recipe(layers=[PAIR_LAYER], space=space, **over)


def created_pairs(root: Path, *, embed=pair_embed, **kw):
    codex = col.create_from_recipe(root, "gutenberg", pair_recipe(), platform_image=PLATFORM,
                                   embed=embed, embed_query=fake_query_embed, key=KEY,
                                   now=NOW, pairs=PAIR_BYTES, name="pairs", **kw)
    return codex, root / "pairs" / "drag"


def member_path(drag_dir: Path, codex) -> Path:
    return drag_dir / drag.calibration_paths(codex.space.calibration.set)[0]


# ---- text spaces keep their bytes (pins computed by 838c005's code) ---------------

TEXT_MEMBER = "30acba3d88f7e6ed584f498b3e5aee096ce42c62966da0eaaa957d868c9dcce5"
TEXT_SIG = "63d5c92c4064a4891c5715e5ca0e71cfe74995c1d91812fe7cd1dcaf433ca17e"
TEXT_STATEMENT = {"document": "63bf2e9ab35ae9e53c3cdfee23f14f29226c027a7d4540c52ab6ea2a684159ba",
                  "query": "b026702992e777c1e3268c72a372fa710c3a967f80fbee14b8e3c3e027f5a705"}
TEXT_FINGERPRINT = "sha256:3995fe7e3d17446134c8777ea4b6464fb86a02da791bb85809ea7834aaa630b9"
TEXT_INDICES = [0, 46, 29, 458, 220, 103, 69, 143, 62, 337, 299, 278, 364, 386, 383, 375]
TEXT_CHALLENGE = "5042dec0f196d857a01c1daef46467d5df033e6c9669425e812b99d793af9d39"


def test_a_text_space_create_writes_the_same_member_and_signature(tmp_path):
    codex, d = created(tmp_path)
    member, sig = drag.calibration_paths(codex.space.calibration.set)
    assert hashlib.sha256((d / member).read_bytes()).hexdigest() == TEXT_MEMBER
    assert hashlib.sha256((d / sig).read_bytes()).hexdigest() == TEXT_SIG
    assert (codex.collection_id, codex.space_id) == (
        "sha256:f00120b08f95c78898babebfed5bdcba7a70edf690b129e4f86b468c7fbe8031",
        "sha256:3a1937eb9e109ad70c692dd2c8ff83c0d37f926290551a5de55102c67d6754e4")
    assert (codex.space.calibration.vectors, codex.space.envelope,
            codex.space.calibration.query_vectors) == (CAL_VECS, ENVELOPE, CAL_QVECS)
    assert cal.PAIRS not in load_file(str(d / member))
    assert json.loads((d / sig).read_text(encoding="utf-8"))["body"]["version"] == 2


@pytest.mark.parametrize("role", ["document", "query"])
def test_a_text_space_gate_states_the_same_statement_and_fingerprint(role):
    codex, ref = te.codex_768(), te.reference()
    run = emb.run_gate(codex, reference=ref, embed=lambda t: ref, seed=b"\x09" * 32,
                       stated=a_stated_run(codex), embed_one=fake_embed,
                       contribute=fake_embed, role=role)
    body = dict(emb.statement(run, codex, te.a_key(), now=te.NOW)["body"])
    assert body["version"] == 3, "the peer's statement kept its version"
    body.pop("tokens_per_s")                    # a wall-clock rate, never the same twice
    assert hashlib.sha256(cx.canonical_json(body)).hexdigest() == TEXT_STATEMENT[role]
    assert run.fingerprint == TEXT_FINGERPRINT


def test_a_text_space_s_fingerprint_texts_and_challenge_are_unchanged():
    _, texts = cal.load("text")
    assert fp.indices(texts) == TEXT_INDICES
    rows = exp.challenge_rows(te.codex_768(), lambda t: fake_embed(t))
    assert hashlib.sha256(json.dumps(rows).encode()).hexdigest() == TEXT_CHALLENGE
    assert cal.items(te.codex_768()) == texts, "a text space's set is the platform's, as ever"


# ---- the pair set ----------------------------------------------------------------

def test_a_pair_set_is_one_compact_line_per_pair():
    first = PAIR_BYTES.split(b"\n")[0]
    assert first == json.dumps({"i": 0, "pair": list(PAIRS[0])}, ensure_ascii=False,
                               separators=(",", ":")).encode()
    assert PAIR_BYTES.count(b"\n") == cal.SIZE, "an abstract's line feeds are escaped"
    assert cal.pairs_of(PAIR_BYTES, PAIR_SET) == PAIRS


@pytest.mark.parametrize("bad,match", [
    (lambda p: p[:511], "512 pairs"),
    (lambda p: [("", p[0][1])] + p[1:], "first segment is empty"),
    (lambda p: [(p[0][0], "  ")] + p[1:], "second segment is empty"),
    (lambda p: [("Café", p[0][1])] + p[1:], "not NFC"),
    (lambda p: [(p[0][0], "a\tb")] + p[1:], "U\\+0009"),
    (lambda p: [p[1]] + p[1:], "identical"),
])
def test_a_pair_set_that_could_not_do_its_job_is_refused(bad, match):
    with pytest.raises(CalibrationError, match=match):
        cal.check_pairs(bad(list(PAIRS)))


def test_a_pair_set_that_is_not_the_one_named_is_refused():
    with pytest.raises(CalibrationError, match="hashes to"):
        cal.pairs_of(PAIR_BYTES, "sha256:" + "ab" * 32)
    with pytest.raises(CalibrationError, match="keys are exactly"):
        list(cal.parse_pairs(b'{"i":0,"text":"x"}\n'))


def _candidates(n: int, *, dupes: int = 0, bad: int = 0):
    out = [(f"pubmed:{i}", f"Title {i}", "x" * (10 + (i * 37) % 997)) for i in range(n)]
    out += [(f"pubmed:dup{i}", out[i][1], out[i][2]) for i in range(dupes)]
    out += [(f"pubmed:bad{i}", f"Bad {i}", "") for i in range(bad)]
    return out


def test_the_draw_spans_the_lengths_and_is_a_function_of_the_records():
    pool = _candidates(2000, dupes=40, bad=30)
    calls = []

    def candidates():
        calls.append(1)
        return iter(pool)

    got = cal.draw_pairs(candidates)
    assert len(calls) == 2, "an index pass, then a pass that fetches only the drawn"
    assert len(got) == cal.SIZE and len({k for k, _, _ in got}) == cal.SIZE
    lengths = [cal.item_bytes((a, b)) for _, a, b in got]
    assert lengths == sorted(lengths), "written in length order"
    admissible = sorted((cal.item_bytes((a, b)), k) for k, a, b in pool[:2000])
    assert lengths[0] == admissible[0][0] and lengths[-1] == admissible[-1][0], \
        "the shortest and the longest are both in"
    assert not any(k.startswith(("pubmed:dup", "pubmed:bad")) for k, _, _ in got)
    assert cal.draw_pairs(lambda: iter(pool)) == got
    assert cal.draw_pairs(lambda: iter(list(reversed(pool[:2000])) + pool[2000:])) == got, \
        "ties and order are the rule's, not the walk's"
    with pytest.raises(CalibrationError, match="only 100 admissible"):
        cal.draw_pairs(lambda: iter(_candidates(100)))


def test_a_record_s_pair_is_cut_by_its_layer():
    """`pair_candidates` cuts through `layers.chunks_for_document`, so the pair is
    exactly what `embed_chunks` will feed the encoder."""
    pair_layer = layers_mod.Layer("abstract", 1, "default", input=dict(PAIR))
    text_layer = layers_mod.Layer("abstract", 1, "default")
    rec = {"uid": "pubmed:7", "native_id": 7,
           "sections": [["title", " A title "], ["abstract", "One."], ["methods", "Two."]]}

    class C:
        layers = (pair_layer,)
        name = "p"

    assert list(cal.pair_candidates(C, [rec])) == [("pubmed:7", "A title", "One.\n\nTwo.")]
    C.layers = (text_layer,)
    assert list(cal.pair_candidates(C, [rec])) == []


def test_a_space_whose_layers_feed_two_forms_has_no_set():
    two = recipe(layers=[dict(PAIR_LAYER, rule="field:lang=en"),
                         {"chunker": {"id": "fixed", "version": 1,
                                      "params": {"tokens": 1000, "overlap": 120}},
                          "rule": "default"}],
                 space=pair_recipe()["space"])
    with pytest.raises(CalibrationError, match="two forms"):
        col.create_from_recipe(Path("/nonexistent"), "gutenberg", two, platform_image=PLATFORM,
                               embed=pair_embed, embed_query=fake_query_embed, key=KEY,
                               now=NOW, pairs=PAIR_BYTES)


# ---- create ----------------------------------------------------------------------

def test_create_embeds_the_pairs_as_pairs_and_the_member_carries_them(tmp_path):
    seen = {}

    def embed(items):
        seen["doc"] = items
        return pair_embed(items)

    def embed_query(texts):
        seen["query"] = texts
        return fake_query_embed(texts)

    codex = col.create_from_recipe(tmp_path, "gutenberg", pair_recipe(), platform_image=PLATFORM,
                                   embed=embed, embed_query=embed_query, key=KEY, now=NOW,
                                   pairs=PAIR_BYTES, name="pairs")
    assert codex.layers[0].input == PAIR and codex.space.calibration.set == PAIR_SET
    assert seen["doc"] == PAIRS, "every item embedded as a pair, in set order"
    assert seen["query"] == [f"{a}\n\n{b}" for a, b in PAIRS], "a query is one text"
    member = member_path(tmp_path / "pairs" / "drag", codex)
    tensors = load_file(str(member))
    assert tensors[cal.PAIRS].dtype == np.uint8 and tensors[cal.PAIRS].tobytes() == PAIR_BYTES
    assert cal.items(codex, member) == PAIRS
    vectors, _ = cal.read_member(member, expect_vectors=codex.space.calibration.vectors,
                                 expect_envelope=codex.space.envelope)
    np.testing.assert_allclose(vectors, np.asarray(pair_embed(PAIRS), dtype=np.float32),
                               atol=1e-6)
    sig = json.loads(member.with_name(member.name.replace(".safetensors", ".sig.json"))
                     .read_text(encoding="utf-8"))["body"]
    assert sig["kind"] == "louise-calibration" and sig["version"] == 3
    assert sig["set"] == PAIR_SET


def test_a_pair_space_needs_its_set_and_a_text_space_refuses_one(tmp_path):
    with pytest.raises(CodexError, match="--calibration-set"):
        col.create_from_recipe(tmp_path, "gutenberg", pair_recipe(), platform_image=PLATFORM,
                               embed=pair_embed, embed_query=fake_query_embed, key=KEY, now=NOW)
    with pytest.raises(CodexError, match="feed the\\s+encoder text"):
        col.create_from_recipe(tmp_path, "gutenberg", recipe(), platform_image=PLATFORM,
                               embed=fake_embed, embed_query=fake_query_embed, key=KEY, now=NOW,
                               pairs=PAIR_BYTES)
    other = cal.render_pairs([(a + "!", b) for a, b in PAIRS])
    with pytest.raises(CalibrationError, match="hashes to"):
        col.create_from_recipe(tmp_path, "gutenberg", pair_recipe(), platform_image=PLATFORM,
                               embed=pair_embed, embed_query=fake_query_embed, key=KEY, now=NOW,
                               pairs=other)
    assert not list(tmp_path.iterdir()), "nothing is created"


def test_a_member_without_its_pairs_is_not_a_pair_space_s(tmp_path):
    codex, d = created_pairs(tmp_path)
    with pytest.raises(CalibrationError, match="travels in its member"):
        cal.items(codex)
    text_member = created(tmp_path / "t")[1]
    some = next((text_member / "calibration").glob("*.safetensors"))
    with pytest.raises(CalibrationError, match="no `pairs` tensor"):
        cal.items(codex, some)
    with pytest.raises(CalibrationError, match="hashes to"):
        cal.write_member(tmp_path / "x.safetensors", np.zeros((2, 2), "float32"),
                         np.zeros((2, 2), "float32"), set_id_="sha256:" + "00" * 32,
                         encoder_weights="w", key="k", encoder_engine="e", pairs=PAIR_BYTES)


def test_create_takes_the_pairs_from_their_file_or_from_the_space_it_copies(tmp_path,
                                                                             monkeypatch):
    """`hold collection create --calibration-set FILE`; and `--space-from` a
    pair space, whose member carries the set, embeds the very same pairs."""
    from app import louise
    from app.bundles import keys as node_keys
    root = tmp_path / "collections"
    monkeypatch.setattr(node_keys, "load_signing_key", lambda *a, **k: KEY)
    monkeypatch.setattr(louise, "_embed_for_recipe",
                        lambda space, url, model=None, **kw: fake_query_embed if "query" in url else pair_embed)
    f = tmp_path / "pairs.jsonl"
    f.write_bytes(PAIR_BYTES)
    urls = {"encoder": "http://enc.invalid/upstream/article",
            "query_encoder": "http://enc.invalid/upstream/query"}
    first, _ = louise.create_from(root, "gutenberg", pair_recipe(), platform_image=PLATFORM,
                                  name="first", calibration_set=str(f), **urls)
    assert cal.items(first, member_path(root / "first" / "drag", first)) == PAIRS
    second, _ = louise.create_from(root, "gutenberg", recipe(work={"tokens": 6_000_000}),
                                   platform_image=PLATFORM, name="second",
                                   space_from=first.collection_id, **urls)
    assert second.collection_id != first.collection_id
    assert second.space_id == first.space_id and cal.form(second) == "pair"
    assert cal.items(second, member_path(root / "second" / "drag", second)) == PAIRS


def test_a_sister_in_the_same_pair_space_carries_the_member_and_its_pairs(tmp_path):
    from app.hold.codex import Update
    codex, d = created_pairs(tmp_path)
    sister = col.derive(tmp_path, codex.collection_id, update=Update("weekly", 3, 7))
    assert sister.space_id == codex.space_id
    member = member_path(tmp_path / col.name_of(tmp_path, sister.collection_id) / "drag", sister)
    assert cal.items(sister, member) == PAIRS


# ---- the gate --------------------------------------------------------------------

def _pair_codex(tmp_path):
    codex, d = created_pairs(tmp_path)
    member = member_path(d, codex)
    ref = cal.read_member(member, expect_vectors=codex.space.calibration.vectors,
                          expect_envelope=codex.space.envelope)[0].tolist()
    return codex, member, ref


def test_the_gate_embeds_every_pair_as_a_pair(tmp_path):
    codex, member, ref = _pair_codex(tmp_path)
    seen = {"embed": [], "one": [], "contribute": []}

    def watch(name):
        def fn(items):
            seen[name].extend(items)
            return pair_embed(items)
        return fn

    run = emb.run_gate(codex, reference=ref, embed=watch("embed"), seed=b"\x01" * 32,
                       stated=a_stated_run(codex), embed_one=watch("one"),
                       contribute=watch("contribute"), items=cal.items(codex, member))
    assert run.ok and run.passed == 16 and run.batch_invariant is True
    assert seen["embed"] == PAIRS
    sixteen = [PAIRS[i] for i in fp.indices(PAIRS)]
    assert seen["one"] == seen["contribute"] == sixteen
    assert all(isinstance(x, tuple) for x in seen["one"])
    by_len = sorted(range(cal.SIZE), key=lambda i: (cal.item_bytes(PAIRS[i]), i))
    assert fp.indices(PAIRS)[0] == by_len[0] and fp.indices(PAIRS)[-1] == by_len[-1]
    assert run.fingerprint == fp.of(pair_embed(sixteen))


def test_a_backend_that_flattens_pairs_fails_the_gate(tmp_path):
    """The defect this exists for: plain text through the same model is ANOTHER
    function, and a gate over plain texts would have passed it."""
    codex, member, ref = _pair_codex(tmp_path)
    run = emb.run_gate(codex, reference=ref, embed=flattening_embed, seed=b"\x01" * 32,
                       items=cal.items(codex, member))
    assert not run.ok and run.passed == 0


def test_the_query_side_gets_one_text_per_pair(tmp_path):
    codex, member, _ = _pair_codex(tmp_path)
    seen = []
    qref = cal.read_query_vectors(member, expect=codex.space.calibration.query_vectors)
    run = emb.run_gate(codex, reference=qref.tolist(),
                       embed=lambda t: seen.extend(t) or fake_query_embed(t),
                       seed=b"\x01" * 32, role="query", items=cal.items(codex, member))
    assert run.ok and seen == [f"{a}\n\n{b}" for a, b in PAIRS]


def test_without_its_set_a_pair_gate_does_not_run(tmp_path):
    codex, _, ref = _pair_codex(tmp_path)
    with pytest.raises(emb.GateError, match="travels in its member"):
        emb.run_gate(codex, reference=ref, embed=pair_embed)


def test_the_re_check_embeds_the_sampled_pairs(tmp_path):
    codex, member, ref = _pair_codex(tmp_path)
    run = emb.run_gate(codex, reference=ref, embed=pair_embed, seed=b"\x02" * 32,
                       stated=a_stated_run(codex), embed_one=pair_embed,
                       contribute=pair_embed, items=cal.items(codex, member))
    body = emb.statement(run, codex, te.a_key(), now=te.NOW)["body"]
    seen = []
    ok, passed, _ = hold_runs.recheck(codex, body, reference=ref,
                                      embed=lambda t: seen.extend(t) or pair_embed(t),
                                      items=cal.items(codex, member))
    assert ok and passed == 16 and seen == [PAIRS[i] for i in body["sampled"]]
    flat, _, _ = hold_runs.recheck(codex, body, reference=ref, embed=flattening_embed,
                                   items=cal.items(codex, member))
    assert flat is False


def test_hold_check_calibration_reads_the_pairs_from_the_member(tmp_path):
    codex, member, ref = _pair_codex(tmp_path)
    ctx = chk.Ctx(tmp_path, "pairs", codex, None, b"\x03" * 32, embed=pair_embed,
                  reference=ref)
    assert chk.check_calibration(ctx).result == chk.PASS
    ctx = chk.Ctx(tmp_path, "pairs", codex, None, b"\x03" * 32, embed=flattening_embed,
                  reference=ref)
    assert chk.check_calibration(ctx).result == chk.FAIL


def test_the_export_challenge_is_the_first_sixteen_pairs_as_pairs(tmp_path):
    codex, member, _ = _pair_codex(tmp_path)
    seen = []
    rows = exp.challenge_rows(codex, lambda t: seen.extend(t) or pair_embed(t), member)
    assert seen == PAIRS[:16]
    assert [cid for cid, _ in rows] == [f"calibration:{i}" for i in range(16)]


def test_calibrate_measures_a_pair_space_over_its_pairs(tmp_path, monkeypatch, capsys):
    """`hold embedder calibrate --pairs FILE`, through the real client: every
    lane's every request is a batch of pairs, which `_body` sends as
    `input_pairs`."""
    from app import embeddings
    from tests.hold import test_embedder_calibrate as tec

    f = tmp_path / "pairs.jsonl"
    f.write_bytes(PAIR_BYTES)
    assert calib.load_pairs(f) == (PAIR_SET, PAIRS)
    bodies = []

    def fake(batch, timeout, recipe, runs=None):
        assert all(isinstance(x, tuple) for x in batch), batch[:1]
        bodies.append(embeddings._body(recipe, batch))
        keys = [json.dumps(list(x)) for x in batch]
        if runs is not None:
            runs.add("sha256:run")
        if recipe.url == tec.REF_URL:
            return [tec._base(k).tolist() for k in keys]
        return [tec._perturbed(k, 1e-3).tolist() for k in keys]

    monkeypatch.setattr(embeddings, "_request_vectors", fake)
    ref = tec._file(tmp_path, "ref.toml", url=tec.REF_URL)
    other = tec._file(tmp_path, "other.toml", url=tec.OTHER_URL, weights=tec.W_OTHER)
    assert tec._run(ref, other, extra=["--pairs", str(f)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["set"] == PAIR_SET and out["texts"] == cal.SIZE
    assert bodies and all("input_pairs" in b and "input" not in b for b in bodies)
    assert sum(len(b["input_pairs"]) for b in bodies) == 2 * cal.SIZE

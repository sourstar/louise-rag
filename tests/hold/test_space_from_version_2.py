"""A version-2 space copied with `--space-from` stays that space.

Operator, 2026-09-21. pubmed's v1.0 root takes the development collection's
space so that its ~40M vectors stay admissible. Written as version 3 the
same space was another `space_id`: the engine amendment moved the encoder
image out of the hash, so the copy became another space and every vector
of the first inadmissible to the second. 02 §2.7's `space_from` exists to
put two collections in ONE space by construction, so a copied version-2
space is written back as it was, in a Codex of that version.
"""
import pytest

from app import louise
from app.bundles import keys as node_keys
from app.hold import codex as cx, collection as col, template
from tests.hold.conftest import KEY, PLATFORM, fake_embed, fake_query_embed, pubmed_v2
from tests.hold.test_template import recipe as a_recipe


def _create(tmp_path, monkeypatch, source, *, embed=fake_embed, **kw):
    root = tmp_path / "collections"
    held = col.create(root, source, name="pubmed-dev")
    monkeypatch.setattr(node_keys, "load_signing_key", lambda: KEY)
    monkeypatch.setattr(louise, "_embed_for_recipe",
                        lambda space, url, model=None: fake_query_embed if "query" in url else embed)
    r = a_recipe(cleaning_version=source.cleaning_version)
    codex, _ = louise.create_from(root, "gutenberg", r, platform_image=PLATFORM,
                                  space_from=held.collection_id,
                                  encoder="http://enc.invalid/upstream/article",
                                  query_encoder="http://enc.invalid/upstream/query", **kw)
    return held, codex


def test_a_version_2_space_copied_is_the_same_space(tmp_path, monkeypatch):
    source, codex = _create(tmp_path, monkeypatch, pubmed_v2())
    assert codex.version == 2 < cx.ENGINE_VERSION
    assert codex.space.encoder.image == source.space.encoder.image
    assert codex.space_id == source.space_id, "every vector of the source stays admissible"
    assert codex.collection_id != source.collection_id
    assert codex.relation == "root"


def test_a_version_3_space_copied_is_written_at_version_4_in_the_same_space(
        tmp_path, monkeypatch):
    """09 §9.3 (operator, 2026-10-03): "a version-3 `space` is a valid
    version-4 one, and `space_from` copies it into a version-4 Codex
    unchanged" — the engine carries no `options` there, so not a byte of it
    moves, and its space is the source's."""
    from tests.hold.conftest import pubmed
    source, codex = _create(tmp_path, monkeypatch, pubmed(version=3))
    # version 4, not CODEX_VERSION: at 5 the layers would carry `input` and
    # the copy would be another space (09 §9.3, amended 2026-10-08)
    assert source.version == 3 and codex.version == 4 == cx.INPUT_VERSION - 1
    assert codex.space.to_dict()["encoder"] == source.space.to_dict()["encoder"]
    assert codex.space_id == source.space_id


def test_a_recipe_cannot_state_a_version_2_space_itself():
    """Only a copy: an image-shaped space written into a recipe is still
    refused, so no NEW version-2 space is ever minted."""
    v2 = pubmed_v2().space.to_dict()
    v2.pop("envelope")
    v2["calibration"].pop("vectors")
    v2["calibration"].pop("query_vectors", None)
    with pytest.raises(template.TemplateError, match="engine|ENGINE"):
        template.from_recipe("pubmed", a_recipe(space=v2), platform_image=PLATFORM,
                             calibration=("sha256:" + "a" * 64, "sha256:" + "b" * 64,
                                          "sha256:" + "c" * 64))


def test_a_version_4_space_copied_stays_version_4_in_the_same_space(tmp_path, monkeypatch):
    """The input-forms amendment (09 §9.3, 2026-10-08): a version-4 source's
    layers carry no `input`, so a version-5 copy would add one and be
    another space. The copy is written at 4, and is the source's space."""
    from tests.hold.conftest import pubmed
    source, codex = _create(tmp_path, monkeypatch, pubmed())
    assert source.version == 4 and codex.version == 4
    assert "input" not in codex.to_dict()["layers"][0]
    assert codex.space_id == source.space_id


def test_a_version_5_space_copied_is_version_5_with_its_form(tmp_path, monkeypatch):
    """A pair space is calibrated on PAIRS (09 §9.12, amended 2026-10-08), so
    the source names a pair set, and the copy — whose source holds no member
    here — is given its file."""
    from dataclasses import replace
    from app.hold.codex import Chunker, Layer
    from tests.hold.conftest import pubmed
    from tests.hold.test_pair_calibration import PAIR_BYTES, PAIR_SET, pair_embed
    pair = {"form": "pair", "split": "first-section"}
    base = pubmed()
    space = replace(base.space, calibration=replace(base.space.calibration, set=PAIR_SET))
    pairs_file = tmp_path / "pairs.jsonl"
    pairs_file.write_bytes(PAIR_BYTES)
    source, codex = _create(tmp_path, monkeypatch, pubmed(
        version=5, space=space, layers=(Layer(Chunker("abstract", 1, {}), "default", pair),)),
        embed=pair_embed, calibration_set=str(pairs_file))
    assert codex.version == 5 and codex.layers[0].input == pair
    assert codex.space_id == source.space_id
    with pytest.raises(cx.CodexError, match="--calibration-set"):
        _create(tmp_path / "again", monkeypatch, source, embed=pair_embed)

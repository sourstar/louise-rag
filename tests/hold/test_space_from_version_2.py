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


def _create(tmp_path, monkeypatch, source):
    root = tmp_path / "collections"
    held = col.create(root, source, name="pubmed-dev")
    monkeypatch.setattr(node_keys, "load_signing_key", lambda: KEY)
    monkeypatch.setattr(louise, "_embed_for_recipe",
                        lambda space, url: fake_query_embed if "query" in url else fake_embed)
    r = a_recipe(cleaning_version=source.cleaning_version)
    codex, _ = louise.create_from(root, "gutenberg", r, platform_image=PLATFORM,
                                  space_from=held.collection_id,
                                  encoder="http://enc.invalid/article",
                                  query_encoder="http://enc.invalid/query")
    return held, codex


def test_a_version_2_space_copied_is_the_same_space(tmp_path, monkeypatch):
    source, codex = _create(tmp_path, monkeypatch, pubmed_v2())
    assert codex.version == 2 < cx.ENGINE_VERSION
    assert codex.space.encoder.image == source.space.encoder.image
    assert codex.space_id == source.space_id, "every vector of the source stays admissible"
    assert codex.collection_id != source.collection_id
    assert codex.relation == "root"


def test_a_version_3_space_copied_stays_version_3(tmp_path, monkeypatch):
    from tests.hold.conftest import pubmed
    source, codex = _create(tmp_path, monkeypatch, pubmed())
    assert codex.version == cx.ENGINE_VERSION
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

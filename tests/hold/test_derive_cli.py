"""`hold collection derive` end to end (07 §7.3; docs/notes/v5-migration.md G4).

A sister in a NEW space embeds her own reference at the endpoint named, signs
it with the node's key at the one clock, and gets her collection file; one in
her mother's space inherits the member and her mother's [encoder]. Before
2026-10-09 the command reached `col.derive` with no encoder at all, so a
sister in a new space always failed.
"""
import json
import tomllib

import pytest

from app import louise
from app.bundles import keys as node_keys
from app.hold import calibration as cal, collection as col, drag
from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed, fake_query_embed
from tests.hold.test_template import recipe

MOTHER_URL = "http://enc.invalid/upstream/mother"
SISTER_URL = "http://enc.invalid/upstream/sister"
QUERY_URL = "http://enc.invalid/upstream/query"


def sister_embed(texts):
    """Another build: another function over the same texts."""
    return fake_embed(texts, tag=b"sister")


@pytest.fixture
def node(tmp_path, monkeypatch):
    root = tmp_path / "collections"
    mother = col.create_from_recipe(root, "gutenberg", recipe(), platform_image=PLATFORM,
                                    embed=fake_embed, embed_query=fake_query_embed,
                                    name="mother", key=KEY, now=NOW)
    louise._write_held_file(root, mother, MOTHER_URL)
    monkeypatch.setattr(node_keys, "load_signing_key", lambda *a, **k: KEY)
    calls = []

    def embed_for(space, url, model=None, **kw):
        side = (url, kw.get("query", False))
        fn = fake_query_embed if side[1] else sister_embed

        def embed(texts):
            calls.append(side)
            return fn(texts)
        return embed

    monkeypatch.setattr(louise, "_embed_for_recipe", embed_for)
    return root, mother, calls


def _space_file(tmp_path, mother, **over):
    block = mother.space.to_dict()
    block["encoder"] = {**block["encoder"], **over}
    p = tmp_path / "space.json"
    p.write_text(json.dumps(block), encoding="utf-8")
    return p


def test_a_sister_in_a_new_space_embeds_her_own_reference_and_is_held(node, tmp_path, capsys):
    root, mother, calls = node
    f = _space_file(tmp_path, mother, weights="sha256:" + "5e" * 32)
    assert louise.main(["hold", "collection", "derive", "mother", "--root", str(root),
                        "--space", str(f), "--encoder", SISTER_URL,
                        "--query-encoder", QUERY_URL, "--name", "sister", "--accept"]) == 0
    cid = capsys.readouterr().out.strip().splitlines()[-1]
    name, sister = col.resolve(root, cid)
    assert name == "sister" and sister.derived_from == mother.collection_id
    assert sister.space_id != mother.space_id
    assert sorted(calls) == [(QUERY_URL, True), (SISTER_URL, False)], \
        "each side embedded the set once (RULE 1: the member is written from those vectors)"
    member = col.drag_of(root, "sister") / drag.calibration_paths(sister.space.calibration.set)[0]
    cal.read_member(member, expect_vectors=sister.space.calibration.vectors,
                    expect_envelope=sister.space.envelope)
    assert sister.space.calibration.vectors != mother.space.calibration.vectors
    held = tomllib.loads((root / "sister.toml").read_text(encoding="utf-8"))
    assert held["codex"] == sister.collection_id
    assert held["encoder"]["url"] == SISTER_URL and held["encoder"]["query_url"] == QUERY_URL


def test_a_sister_in_her_mothers_space_inherits_the_member_and_the_encoder(node, tmp_path, capsys):
    root, mother, calls = node
    work = tmp_path / "work.json"
    work.write_text(json.dumps({"tokens": 1234, "per": "P1M"}), encoding="utf-8")
    assert louise.main(["hold", "collection", "derive", "mother", "--root", str(root),
                        "--work", str(work), "--name", "weekly", "--accept"]) == 0
    cid = capsys.readouterr().out.strip().splitlines()[-1]
    sister = col.read(root, cid)
    assert sister.space_id == mother.space_id and calls == [], "nothing embedded"
    a = col.drag_of(root, "mother") / drag.calibration_paths(mother.space.calibration.set)[0]
    b = col.drag_of(root, "weekly") / drag.calibration_paths(sister.space.calibration.set)[0]
    assert a.read_bytes() == b.read_bytes()
    held = tomllib.loads((root / "weekly.toml").read_text(encoding="utf-8"))
    assert held["encoder"]["url"] == MOTHER_URL, "her mother's endpoint serves her space"


def test_a_sister_in_a_new_space_without_an_encoder_is_refused_and_nothing_is_written(
        node, tmp_path, capsys):
    root, mother, _calls = node
    f = _space_file(tmp_path, mother, weights="sha256:" + "5e" * 32)
    assert louise.main(["hold", "collection", "derive", "mother", "--root", str(root),
                        "--space", str(f), "--name", "lost", "--accept"]) == 2
    assert not (root / "lost.toml").exists()


def test_the_signing_key_is_needed_before_anything_is_embedded(node, tmp_path, monkeypatch):
    root, mother, calls = node
    monkeypatch.setattr(node_keys, "load_signing_key", lambda *a, **k: None)
    f = _space_file(tmp_path, mother, weights="sha256:" + "5e" * 32)
    assert louise.main(["hold", "collection", "derive", "mother", "--root", str(root),
                        "--space", str(f), "--encoder", SISTER_URL,
                        "--query-encoder", QUERY_URL, "--name", "unsigned", "--accept"]) == 2
    assert not (root / "unsigned").exists(), "no Codex without its member"

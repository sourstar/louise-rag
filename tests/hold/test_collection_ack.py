"""tests/hold/test_collection_ack.py — 09 §9.13's acknowledgement.

Invariant 9: a yellow- or red-class collection's data is not USED until a
peer records that it read the terms. `records._ack_of` refuses to build
one without `state/ack.json` and names `hold collection ack` as the way
to make it — and that verb did not exist until step 8 needed it for
`pubmed`, which is yellow.

AN ACKNOWLEDGEMENT IS OF A TEXT, not of a collection: "a collection whose
Codex names a hash this peer has acknowledged under any collection is
acknowledged" (§9.13). So what is recorded is the terms hash and the time,
with the collection only as provenance.
"""
import json

import pytest

from app import louise
from app.hold import codex as cx, collection as col
from tests.hold.conftest import fake_query_embed  # noqa: E402,F811
from tests.hold.conftest import KEY, NOW, PLATFORM, TERMS, fake_embed, pubmed


@pytest.fixture
def yellow(isolated_config, tmp_path):
    """A red-class collection with its terms member — `pubmed()`'s Codex
    is red and names `TERMS`, which is what the fixture hashes to."""
    root = tmp_path / "collections"
    codex = pubmed()
    col.create(root, codex)
    terms = root / "pubmed" / "drag" / "LICENSE-TERMS"
    terms.parent.mkdir(parents=True, exist_ok=True)
    return root, codex, terms




# ---- the verb ------------------------------------------------------------

def test_the_verb_is_in_the_tree():
    a = louise.build_parser().parse_args(
        ["hold", "collection", "ack", "pubmed", "--yes"])
    assert a.fn is not None and a.yes is True


def test_a_green_collection_has_nothing_to_acknowledge(isolated_config,
                                                       tmp_path, capsys):
    """"a green collection has `terms_sha256: null` and no member" — the
    command says so and exits 0 rather than inventing an ack."""
    root = tmp_path / "collections"
    from tests.hold.test_template import recipe
    col.create_from_recipe(root, "gutenberg", recipe(), platform_image=PLATFORM,
                           embed=fake_embed, embed_query=fake_query_embed, name="gutenberg", key=KEY, now=NOW)
    assert louise.main(["hold", "collection", "ack", "gutenberg",
                        "--root", str(root), "--yes"]) == 0
    assert "nothing to acknowledge" in capsys.readouterr().out


def test_without_the_terms_member_it_refuses(yellow, capsys):
    """LICENSE-TERMS is one of the three members fetchable BEFORE an
    acknowledgement (04 §4.4), so a copy without it cannot be
    acknowledged."""
    root, codex, terms = yellow
    assert louise.main(["hold", "collection", "ack", "pubmed",
                        "--root", str(root), "--yes"]) == 2
    assert "no drag/LICENSE-TERMS" in capsys.readouterr().err


def test_terms_that_do_not_hash_to_the_codexs_are_refused(yellow, capsys):
    """"Terms text is byte-exact and never converted (a CRLF checkout
    would silently invalidate every recorded ack)." Nothing is written."""
    root, codex, terms = yellow
    terms.write_bytes(b"not the terms this Codex pins\n")
    assert louise.main(["hold", "collection", "ack", "pubmed",
                        "--root", str(root), "--yes"]) == 2
    err = capsys.readouterr().err
    assert "byte-exact" in err
    assert not (root / "pubmed" / "state" / "ack.json").exists()


def test_it_records_9_13s_object(isolated_config, tmp_path):
    """The five fields, exactly."""
    import hashlib
    from dataclasses import replace
    root = tmp_path / "collections"
    body = b"THE ORIGIN'S TERMS, byte for byte.\n"
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    codex = replace(pubmed(), license=cx.License("red", digest))
    col.create(root, codex)
    terms = root / "pubmed" / "drag" / "LICENSE-TERMS"
    terms.parent.mkdir(parents=True, exist_ok=True)
    terms.write_bytes(body)

    assert louise.main(["hold", "collection", "ack", "pubmed",
                        "--root", str(root), "--yes"]) == 0
    doc = json.loads((root / "pubmed" / "state" / "ack.json").read_bytes())
    assert set(doc) == {"kind", "version", "terms_sha256", "time",
                        "collection_id"}
    assert doc["kind"] == "louise-ack" and doc["version"] == 1
    assert doc["terms_sha256"] == digest
    assert doc["collection_id"] == codex.collection_id
    assert doc["time"].endswith("Z")


def test_the_ack_is_what_records_build_looks_for(isolated_config, tmp_path):
    """The whole point: `records._ack_of` refuses without it and reads it
    when it is there. A verb that wrote a file nothing looked at would be
    a checkbox."""
    import hashlib
    from dataclasses import replace
    from app.hold import records as rec
    root = tmp_path / "collections"
    body = b"terms\n"
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    codex = replace(pubmed(), license=cx.License("red", digest))
    col.create(root, codex)
    state = root / "pubmed" / "state"
    terms = root / "pubmed" / "drag" / "LICENSE-TERMS"
    terms.parent.mkdir(parents=True, exist_ok=True)
    terms.write_bytes(body)

    with pytest.raises(rec.RecordsError, match="no acknowledgement recorded"):
        rec._ack_of(codex, state)

    louise.main(["hold", "collection", "ack", "pubmed", "--root", str(root),
                 "--yes"])
    ack = rec._ack_of(codex, state)
    assert ack and "/" in ack
    assert ack.endswith(digest.replace("sha256:", ""))


# ---- the member `ack` needs, which `create` never wrote ------------------

def test_create_writes_LICENSE_TERMS_for_a_non_green_collection(isolated_config,
                                                                tmp_path):
    """09 §9.13 puts the terms text at the drag root and 02 §2.6 lists it
    as a member. Nothing wrote it until 2026-09-10, so `create` produced a
    yellow collection that could never be acknowledged and therefore never
    built — found taking `pubmed` through step 8.
    """
    import hashlib
    from app.acquire.specs import ACQUIRE_REGISTRY
    from tests.hold.test_template import ORIGIN, SPACE, VALIDATION

    body = ACQUIRE_REGISTRY["pubmed"].terms_text_path.read_bytes()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    root = tmp_path / "collections"
    recipe = {
        "descriptive": {"title": "PubMed", "summary": "Abstracts."},
        "space": dict(SPACE), "work": {"tokens": 40_000_000},
        "origin": dict(ORIGIN), "validation": dict(VALIDATION),
        "license": {"class": "red", "terms_sha256": digest},
    }
    col.create_from_recipe(root, "pubmed", recipe, platform_image=PLATFORM,
                           embed=fake_embed, embed_query=fake_query_embed, name="pubmed", key=KEY, now=NOW)
    terms = root / "pubmed" / "drag" / "LICENSE-TERMS"
    assert terms.is_file(), "no LICENSE-TERMS: the collection cannot be acked"
    assert terms.read_bytes() == body, "byte-exact, never converted"

    # and it is immediately usable: ack records against it without help
    assert louise.main(["hold", "collection", "ack", "pubmed",
                        "--root", str(root), "--yes"]) == 0
    doc = json.loads((root / "pubmed" / "state" / "ack.json").read_bytes())
    assert doc["terms_sha256"] == digest


def test_a_green_collection_gets_no_terms_member(isolated_config, tmp_path):
    """"a green collection has `terms_sha256: null` and no member"."""
    from tests.hold.test_template import recipe
    root = tmp_path / "collections"
    col.create_from_recipe(root, "gutenberg", recipe(), platform_image=PLATFORM,
                           embed=fake_embed, embed_query=fake_query_embed, name="gutenberg", key=KEY, now=NOW)
    assert not (root / "gutenberg" / "drag" / "LICENSE-TERMS").exists()


def test_a_recipe_pinning_the_wrong_terms_hash_is_refused(isolated_config,
                                                          tmp_path):
    """The check that makes the member meaningful: the bytes written must
    be the bytes the Codex pins, or nothing is written."""
    from app.hold.codex import CodexError
    from tests.hold.test_template import ORIGIN, SPACE, VALIDATION
    root = tmp_path / "collections"
    recipe = {
        "descriptive": {"title": "PubMed", "summary": "Abstracts."},
        "space": dict(SPACE), "work": {"tokens": 40_000_000},
        "origin": dict(ORIGIN), "validation": dict(VALIDATION),
        "license": {"class": "red", "terms_sha256": "sha256:" + "ab" * 32},
    }
    with pytest.raises(CodexError, match="byte-exact"):
        col.create_from_recipe(root, "pubmed", recipe, platform_image=PLATFORM,
                               embed=fake_embed, embed_query=fake_query_embed, name="pubmed", key=KEY, now=NOW)

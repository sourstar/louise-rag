"""tests/hold/test_codex.py — the Codex is the identity.

docs/09-formats.md §9.3 fixes the bytes; docs/02-collections.md §2.1, §2.4,
§2.7, §2.8 say why. The first test in this file writes the documented
object out by hand and asserts the module produces exactly it: everything
downstream — snapshots, bundles, DHT keys, the whole network — hashes
through these bytes, so a drift here is a drift everywhere.
"""
import hashlib
import json
from dataclasses import replace

import pytest

from app.hold import codex as cx
from app.hold.codex import (Calibration, Chunker, CodexError, Certifier,
                            Defect, Descriptive, Encoder, Facet, Layer,
                            License, Parser, Replaces, Toolchain, Update, Work)
from tests.hold.conftest import (CAL_QVECS, CAL_SET, CAL_VECS, ENCODER, ENVELOPE,
                                 PLATFORM, PUBMED_CHECKS, PUBMED_SAMPLE,
                                 PUBMED_SLICING, PUBMED_SPEC, Q_ENCODER,
                                 Q_WEIGHTS, TERMS, WEIGHTS, a_credit, a_facet,
                                 a_community_credit, a_corpus_credit,
                                 pubmed_v2, replaced, with_credits)


def _h(obj) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()


# ---- the worked example, written out by hand ------------------------------

#: 09 §9.3's engine, hand-written: the run the reference vectors came from.
ENGINE_OBJECT = {
    "name": "llama.cpp", "build": "b10630", "variant": "sycl", "image": ENCODER,
    "args": ["llama-server", "-m", "${WEIGHTS}", "--embedding", "--pooling", "cls",
             "-c", "512", "-ngl", "99", "--port", "${PORT}"],
    "env": {}, "device": "Intel(R) Arc(TM) Pro B70 Graphics"}

WORKED_EXAMPLE = {
    "kind": "louise-codex", "version": 4,
    "descriptive": {
        "title": "PubMed",
        "summary": "Every MEDLINE abstract, sliced by year, for biomedical retrieval."},
    "plugin": "pubmed",
    "origin": {
        "spec": PUBMED_SPEC,
        "form": "the XML baseline and update files, because the API paginates "
                "and the FTP files are checksummed",
        "unit": "container-member", "clock": "America/New_York",
        "cutoff_rule": "update-files", "jurisdiction": None},
    "toolchain": {"platform_image": PLATFORM, "mother_platform_image": None},
    "parser": {"id": "pubmed", "version": 4},
    "cleaning_version": 1,
    "protocol_version": 1,
    # 09 §9.3: "a figureless collection created under a later format writes
    # `version` 2 with `record_line` 1". The document's own worked example
    # says so; this file said `version` 1 and omitted the key until
    # 2026-09-10, so the test that "pins the format" pinned a variant.
    "record_line": 1,
    "layers": [{"chunker": {"id": "abstract", "version": 1, "params": {}},
                "rule": "default"}],
    "slicing": PUBMED_SLICING,
    "space": {
        # the ENGINE the reference vectors were computed with, written out
        # whole (the engine amendment, 0d85f7f) — not its image alone
        "encoder": {"weights": WEIGHTS, "engine": ENGINE_OBJECT,
                    "label": "MedCPT-Article-Encoder"},
        "dim": 768, "dtype": "f16", "ctx": 512, "pooling": "cls",
        "normalise": True,
        # "the same, its own weights"
        "query": {"encoder": {"weights": Q_WEIGHTS, "engine": ENGINE_OBJECT},
                  "instruction": None},
        "tolerance": "0.95",
        "calibration": {"set": CAL_SET, "vectors": CAL_VECS,
                        "query_vectors": CAL_QVECS,
                        "quorum": {"n": 16, "min_pass": 12}},
        "envelope": ENVELOPE, "epoch": "0", "evaluation": None},
    "work": {"tokens": 40000000, "per": "P1M"},
    "modality": "text", "erasure": "retractable",
    "validation": {"checks": list(PUBMED_CHECKS), "sample": PUBMED_SAMPLE,
                   "required": []},
    "license": {"class": "red", "terms_sha256": TERMS},
    "privacy": "public",
    "update": {"cadence": "monthly", "day": 1, "grace_days": 7},
    "publication": "open", "certifier": None, "embedding": "open",
    "succession": "frozen", "content": None,
    "continues": None, "continues_through": None, "origin_reachable_until": None,
    # `derivation` is 09 §9.3's, required and null where there is none. It
    # was MISSING from the module entirely until 2026-09-12, so every Codex
    # this platform had written carried 34 keys where the spec lists 35 —
    # and since every key but `descriptive` is inside the hash, every
    # collection_id it computed was over the wrong bytes.
    "derived_from": None, "space_from": None, "derivation": None,
    "replaces": None,
    "facets": [],
    # 02 §2.7's three kinds. Two of them are on every collection: the
    # corpus provider, because the text came from someone, and the
    # community, because `embedding` is "open". Sorted by (kind, archive,
    # producer), which puts "community" before "corpus".
    "credits": [
        {"kind": "community", "archive": None,
         "producer": "the Project Louise community", "license": None,
         "attribution": "Vectors contributed by the Project Louise community",
         "coverage": "all"},
        {"kind": "corpus", "archive": None,
         "producer": "the U.S. National Library of Medicine",
         "license": "nlm-terms",
         "attribution": ("PubMed/MEDLINE, U.S. National Library of Medicine, "
                         "National Institutes of Health"),
         "coverage": "all"},
    ],
    "profile": None,
}


def test_the_codex_object_is_the_documented_object(codex):
    """Field for field, 09 §9.3. This is the test that pins the format."""
    assert codex.to_dict() == WORKED_EXAMPLE


def test_the_two_hashes_are_taken_over_different_bytes(codex):
    inner = {k: v for k, v in WORKED_EXAMPLE.items() if k != "descriptive"}
    assert codex.collection_id == _h(inner)
    assert codex.codex_hash == _h(WORKED_EXAMPLE)
    assert codex.collection_id != codex.codex_hash


def test_space_id_is_the_documented_preimage(codex):
    """09 §9.3's own words: "the engine nowhere in it"."""
    assert codex.space_id == _h({
        "cleaning_version": 1,
        "encoder": {"weights": WEIGHTS},
        "layers": [{"chunker": {"id": "abstract", "version": 1, "params": {}},
                    "rule": "default"}],
        "space": {"ctx": 512, "dim": 768, "dtype": "f16", "epoch": "0",
                  "normalise": True, "pooling": "cls",
                  "query": {"encoder": {"weights": Q_WEIGHTS}, "instruction": None}},
    })
    assert codex.space_segment == codex.space_id.split(":")[1][:16]
    assert len(codex.space_segment) == 16


# ---- the engine amendment (0d85f7f): inside the identity, outside the space ----

def _on(codex, engine):
    return replace(codex, space=replace(
        codex.space, encoder=replace(codex.space.encoder, engine=engine),
        query=replace(codex.space.query,
                      encoder=replace(codex.space.query.encoder, engine=engine))))


def test_two_creators_on_different_engines_land_in_one_space(codex):
    """A newer build, another device, another image: another COLLECTION —
    its reference was computed another way — in the SAME space, because a
    space is the model and its settings, and whether an engine computes it
    faithfully is measured by the gate, never declared (02 §2.4)."""
    engine = codex.space.encoder.engine
    newer = replace(engine, build="b10700", image="sha256:" + "9d" * 32,
                    device="cpu", args=engine.args + ("--threads", "8"))
    other = _on(codex, newer)
    cx.validate(other)
    assert other.space_id == codex.space_id
    assert other.collection_id != codex.collection_id
    assert newer.engine_id != engine.engine_id


def test_engine_id_is_the_hash_of_the_engine_object(codex):
    engine = codex.space.encoder.engine
    assert engine.to_dict() == ENGINE_OBJECT
    assert engine.engine_id == _h(ENGINE_OBJECT)


def test_other_weights_are_another_space(codex):
    other = replace(codex, space=replace(
        codex.space, encoder=replace(codex.space.encoder, weights="sha256:" + "f0" * 32)))
    assert other.space_id != codex.space_id


@pytest.mark.parametrize("change, rule", [
    ({"image": "rage:latest"}, "a tag is not an image"),
    ({"args": ()}, "argument vector"),
    ({"args": ("s", "${PORT}", "--alt", "${PORT}")}, "more than once"),
    ({"name": ""}, "non-empty"),
    ({"variant": ""}, "non-empty string or null"),
    ({"env": {"A": 1}}, "names to strings"),
])
def test_an_engine_that_cannot_be_run_again_is_refused(codex, change, rule):
    with pytest.raises(CodexError, match=rule):
        cx.validate(_on(codex, replace(codex.space.encoder.engine, **change)))


def test_an_asymmetric_space_pins_its_query_reference_and_a_symmetric_one_has_none(codex):
    lost = replace(codex, space=replace(codex.space, calibration=replace(
        codex.space.calibration, query_vectors=None)))
    with pytest.raises(CodexError, match="query_vectors"):
        cx.validate(lost)
    symmetric = replace(codex, space=replace(
        codex.space, query=cx.Query(None, None),
        calibration=replace(codex.space.calibration, query_vectors=None)))
    cx.validate(symmetric)
    assert symmetric.to_dict()["space"]["calibration"]["query_vectors"] is None, \
        "present and null: every field is present (09 §9.3)"
    with pytest.raises(CodexError, match="null in a symmetric space"):
        cx.validate(replace(symmetric, space=replace(symmetric.space, calibration=replace(
            symmetric.space.calibration, query_vectors=CAL_QVECS))))


# ---- version 2 is read for ever, and never becomes version 3 ---------------------

def test_a_version_2_codex_is_still_exactly_what_it_was():
    """Its encoder is an image alone, that image is INSIDE its space_id, and
    it has no `query_vectors` key — the bytes version-2 collections were
    identified by. `version` is inside `collection_id`: a Codex already
    written cannot be moved."""
    old = pubmed_v2()
    cx.validate(old)
    d = old.to_dict()
    assert d["version"] == 2
    assert d["space"]["encoder"] == {"image": ENCODER, "weights": WEIGHTS,
                                     "label": "MedCPT-Article-Encoder"}
    assert d["space"]["query"]["encoder"] == {"image": Q_ENCODER, "weights": Q_WEIGHTS}
    assert "query_vectors" not in d["space"]["calibration"]
    assert old.space_id == _h({
        "cleaning_version": 1,
        "encoder": {"image": ENCODER, "weights": WEIGHTS},
        "layers": [{"chunker": {"id": "abstract", "version": 1, "params": {}},
                    "rule": "default"}],
        "space": {"ctx": 512, "dim": 768, "dtype": "f16", "epoch": "0",
                  "normalise": True, "pooling": "cls",
                  "query": {"encoder": {"image": Q_ENCODER, "weights": Q_WEIGHTS},
                            "instruction": None}},
    })
    again = cx.loads(cx.dumps(old))
    assert again == old and again.collection_id == old.collection_id


def test_the_same_recipe_at_two_versions_is_two_collections_in_two_spaces(codex):
    old = pubmed_v2()
    assert old.collection_id != codex.collection_id
    assert old.space_id != codex.space_id, \
        "version 2 hashed the image into the space; version 3 took it out"


def test_the_encoders_shape_follows_the_version_both_ways(codex):
    old = pubmed_v2()
    with pytest.raises(CodexError, match="names the ENGINE"):
        cx.validate(replace(codex, space=old.space))          # an image alone at 3
    with pytest.raises(CodexError, match="cannot be moved"):
        cx.validate(replace(old, space=codex.space))          # an engine at 2
    mixed = dict(codex.to_dict())
    mixed["space"] = dict(mixed["space"], encoder=dict(mixed["space"]["encoder"],
                                                       image=ENCODER))
    with pytest.raises(CodexError, match="image"):
        cx.from_dict(mixed)                                   # both, in one block


def test_the_file_round_trips_through_its_own_bytes(codex):
    again = cx.loads(cx.dumps(codex))
    assert again == codex
    assert again.collection_id == codex.collection_id
    assert again.codex_hash == codex.codex_hash


# ---- descriptive is outside the identity and inside the file --------------

def test_two_titles_are_one_collection_and_two_files(codex):
    other = replace(codex, descriptive=Descriptive("PubMed (mirror)", "A copy."))
    assert other.collection_id == codex.collection_id
    assert other.codex_hash != codex.codex_hash


def test_descriptive_is_removed_not_nulled(codex):
    """A null value is a different object (§9.3), so nulling would give a
    different id from removing — and every peer must agree on which."""
    nulled = dict(codex.to_dict(), descriptive=None)
    assert codex.collection_id != _h(nulled)


def test_a_title_may_not_be_empty_or_multi_line(codex):
    with pytest.raises(CodexError, match="title may not be empty"):
        cx.validate(replace(codex, descriptive=Descriptive("  ", "x")))
    with pytest.raises(CodexError, match="title is one line"):
        cx.validate(replace(codex, descriptive=Descriptive("a\nb", "x")))


# ---- canonical form -------------------------------------------------------

def test_canonical_json_is_sorted_compact_utf8():
    b = cx.canonical_json({"b": 1, "a": {"z": None, "y": [True, "é"]}})
    assert b == '{"a":{"y":[true,"é"],"z":null},"b":1}'.encode("utf-8")


def test_canonical_json_refuses_floats_anywhere():
    with pytest.raises(CodexError, match=r"\$\.space\.tol.*float"):
        cx.canonical_json({"space": {"tol": 0.95}})
    with pytest.raises(CodexError, match=r"\$\[1\]"):
        cx.canonical_json([1, 2.0])


def test_key_order_does_not_change_identity(codex):
    scrambled = dict(reversed(list(codex.to_dict().items())))
    assert cx.from_dict(scrambled).collection_id == codex.collection_id


def test_non_canonical_bytes_are_refused(codex):
    pretty = json.dumps(codex.to_dict(), indent=2).encode("utf-8")
    with pytest.raises(CodexError, match="not canonical"):
        cx.loads(pretty)


def test_any_hashed_field_changes_the_identity(codex):
    base = codex.collection_id
    for changed in (
        replace(codex, parser=Parser("pubmed", 5)),
        replace(codex, cleaning_version=2),
        replace(codex, protocol_version=2),
        replace(codex, update=Update("monthly", 2, 7)),
        replace(codex, update=Update("monthly", 1, 8)),
        replace(codex, work=Work(40_000_001, "P1M")),
        replace(codex, license=License("red", "sha256:" + "aa" * 32)),
        replace(codex, privacy="restricted"),
        replace(codex, facets=(a_facet(),)),
        replace(codex, credits=(a_credit(),)),
        replace(codex, profile={"eli": {"jurisdiction": "IT"}}),
    ):
        assert changed.collection_id != base, changed


# ---- §9.18: unknown and missing fields ------------------------------------

def test_an_unknown_field_is_refused_not_ignored(codex):
    d = dict(codex.to_dict(), encoder_image="sha256:" + "0" * 64)
    with pytest.raises(CodexError, match="unknown field.*encoder_image"):
        cx.from_dict(d)


def test_a_third_toolchain_key_is_refused(codex):
    """The rule that made `toolchain.encoder_image` unsatisfiable: the
    encoder image is named in space.encoder.image and nowhere else."""
    d = codex.to_dict()
    d["toolchain"] = dict(d["toolchain"], encoder_image=ENCODER)
    with pytest.raises(CodexError, match="toolchain: unknown field"):
        cx.from_dict(d)


def test_a_missing_field_is_refused(codex):
    d = codex.to_dict()
    del d["privacy"]
    with pytest.raises(CodexError, match="missing field.*privacy"):
        cx.from_dict(d)


def test_an_unknown_version_is_refused(codex):
    # 7 is the annotations Codex since 2026-10-09 (02 §2.10); 8 is unknown
    d = dict(codex.to_dict(), version=8)
    with pytest.raises(CodexError, match="unsupported Codex version 8"):
        cx.from_dict(d)


def test_version_1_is_read_and_is_the_object_WITHOUT_record_line(codex):
    """09 §9.3: "Version 1 is this object without `record_line`; a Codex
    already written stays 1 and cannot be moved, because `version` is
    inside the canonical bytes and so inside `collection_id` — changing it
    does not upgrade a collection, it names a different one\"."""
    d = dict(pubmed_v2().to_dict(), version=1)   # version 1 names an image, as 2 does
    del d["record_line"]
    old = cx.from_dict(d)
    assert old.version == 1 and old.record_line is None
    assert "record_line" not in old.to_dict()
    assert old.collection_id != codex.collection_id, \
        "a version bump names a different collection, which is the point"


def test_a_version_1_codex_carrying_record_line_is_refused(codex):
    """Absent at 1, present at 2, both enforced by the schema — a Codex
    that carried the key at 1 would be a third shape nobody else computes
    the same id for."""
    d = dict(pubmed_v2().to_dict(), version=1)
    with pytest.raises(CodexError, match="WITHOUT `record_line`"):
        cx.from_dict(d)


def test_a_version_2_codex_without_record_line_is_refused(codex):
    d = codex.to_dict()
    del d["record_line"]
    with pytest.raises(CodexError, match="missing field.*record_line"):
        cx.from_dict(d)


def test_record_line_is_inside_the_identity(codex):
    """It is what makes the format enforce the rule rather than a
    reviewer: "a collection never moves between them — one whose records
    gain plates is a NEW collection (02 §2.7)"."""
    from dataclasses import replace
    assert replace(codex, record_line=2).collection_id != codex.collection_id


def test_record_line_2_is_now_writable_and_1_is_still_the_default(codex):
    """`record_line` 2 was refused at create until 2026-09-11 because the
    figures containers, the member kind and the plugin side were unbuilt.
    They are built now (app/hold/figures.py, drag.MEMBER_KINDS, base.Figure,
    pack/build._record_of, plugins/gutenberg/epub_source.py), so a recipe
    may ASK for plates.

    The DEFAULT stays 1, which is the separate question: "a figureless
    collection created under a later format writes `version` 2 with
    `record_line` 1" (09 §9.3). A collection gets plates by asking, never
    by being created after the amendment landed."""
    from app.hold import template as tpl
    from tests.hold.test_template import recipe
    assert tpl._record_line(recipe(record_line=2)) == 2
    assert tpl._record_line(recipe()) == 1
    with pytest.raises(tpl.TemplateError, match="record_line is 1 or 2"):
        tpl._record_line(recipe(record_line=3))


# ---- the space block ------------------------------------------------------

def test_tolerance_is_a_decimal_string_never_a_float(codex):
    with pytest.raises(CodexError, match="tolerance must be a decimal string"):
        cx.validate(replace(codex, space=replace(codex.space, tolerance="ninety")))


def test_a_quorum_may_not_ask_more_than_it_draws(codex):
    bad = replace(codex.space, calibration=replace(codex.space.calibration, min_pass=17))
    with pytest.raises(CodexError, match="min_pass <= n"):
        cx.validate(replace(codex, space=bad))


def test_the_last_layer_is_the_default_rule(codex):
    two = (Layer(Chunker("verbatim", 1, {}), "default"),
           Layer(Chunker("abstract", 1, {}), "field:type=review"))
    with pytest.raises(CodexError, match='last layer.*"default"'):
        cx.validate(replace(codex, layers=two))


def test_one_chunker_per_layer(codex):
    two = (Layer(Chunker("abstract", 1, {}), "field:type=review"),
           Layer(Chunker("abstract", 1, {}), "default"))
    with pytest.raises(CodexError, match="one chunker per layer"):
        cx.validate(replace(codex, layers=two))


# ---- the plan -------------------------------------------------------------

def test_the_plan_sets_shards_and_never_chunks(codex):
    """09 §9.3: a plan-set chunk count needed a scaling rule a creator could
    get backwards — raising the sample to strengthen the check, weakening it."""
    bad = replace(codex.validation,
                  sample=dict(PUBMED_SAMPLE,
                              **{"vectors-sample": {"shards": "0.10", "chunks": 32}}))
    with pytest.raises(CodexError, match="never sets how many.*chunks"):
        cx.validate(replace(codex, validation=bad))


def test_a_plan_may_not_name_a_check_a_peer_only_finds(codex):
    bad = replace(codex.validation, checks=PUBMED_CHECKS + ("forgery",))
    with pytest.raises(CodexError, match="not a plan check"):
        cx.validate(replace(codex, validation=bad))


def test_a_planned_sampled_check_needs_a_size(codex):
    bad = replace(codex.validation, sample={k: v for k, v in PUBMED_SAMPLE.items()
                                            if k != "chunks"})
    with pytest.raises(CodexError, match="no size for planned check 'chunks'"):
        cx.validate(replace(codex, validation=bad))


def test_required_is_empty_on_an_open_collection(codex):
    bad = replace(codex.validation, required=("roots",))
    with pytest.raises(CodexError, match=r"required is \[\] on an open"):
        cx.validate(replace(codex, validation=bad))


# ---- update, work, licence, privacy ---------------------------------------

def test_a_weekly_cadence_reads_day_as_an_iso_weekday(codex):
    cx.validate(replace(codex, update=Update("weekly", 7, 0)))
    with pytest.raises(CodexError, match="update.day is 1-7 on a weekly"):
        cx.validate(replace(codex, update=Update("weekly", 8, 0)))
    with pytest.raises(CodexError, match="update.day is 1-28 on a monthly"):
        cx.validate(replace(codex, update=Update("monthly", 29, 0)))


def test_only_the_two_cadences_exist(codex):
    with pytest.raises(CodexError, match="cadence must be monthly|weekly"):
        cx.validate(replace(codex, update=Update("daily", 1, 0)))


def test_work_per_defaults_to_the_cadence():
    assert cx.default_per("monthly") == "P1M"
    assert cx.default_per("weekly") == "P1W"


def test_terms_are_null_on_green_and_only_on_green(codex):
    with pytest.raises(CodexError, match="null on green and only on green"):
        cx.validate(replace(codex, license=License("green", TERMS)))
    with pytest.raises(CodexError, match="names its terms by hash"):
        cx.validate(replace(codex, license=License("yellow", None)))
    cx.validate(replace(codex, license=License("green", None)))


def test_erasable_content_may_never_be_shared(codex):
    with pytest.raises(CodexError, match='"erasable" requires privacy "never-share"'):
        cx.validate(replace(codex, erasure="erasable", privacy="public"))
    cx.validate(replace(codex, erasure="erasable", privacy="never-share"))


# ---- facets and credits ---------------------------------------------------

def test_a_tree_facet_carries_a_separator_and_a_flat_one_does_not(codex):
    with pytest.raises(CodexError, match="separator is non-null exactly"):
        cx.validate(replace(codex, facets=(Facet("subjects", "tree", None, None),)))
    with pytest.raises(CodexError, match="separator is non-null exactly"):
        cx.validate(replace(codex, facets=(Facet("lang", "flat", "/", None),)))
    cx.validate(replace(codex, facets=(a_facet(),)))


def test_an_authority_carries_no_version(codex):
    bad = Facet("subjects", "tree", "/", {"name": "MeSH", "version": "2026"})
    with pytest.raises(CodexError, match="carries no version"):
        cx.validate(replace(codex, facets=(bad,)))


def test_facets_and_credits_are_sorted(codex):
    with pytest.raises(CodexError, match="facets are sorted"):
        cx.validate(replace(codex, facets=(a_facet("subjects"), a_facet("lang"))))
    with pytest.raises(CodexError, match="credits are sorted"):
        cx.validate(replace(codex, credits=(a_credit("zenodo"), a_credit("cohere"))))


def test_a_credit_carries_its_licence_and_attribution_and_no_key(codex):
    """The permanent half of an archive's entry is hashed; the signing keys
    live in the registry, because a key is lost and reissued and an archive
    that published nothing signed may begin (02 §2.7, 09 §9.3)."""
    assert set(a_credit().to_dict()) == {
        "kind", "archive", "producer", "license", "attribution", "coverage"}
    assert "key" not in a_credit().to_dict()
    for missing, msg in (("license", "credited under"),
                         ("attribution", "names a producer")):
        bad = replace(a_credit(), **{missing: ""})
        with pytest.raises(CodexError, match=msg):
            cx.validate(replace(codex, credits=with_credits(bad)))


# ---- publication ----------------------------------------------------------

CERT = Certifier("ed25519:" + "5c" * 32, "https://gazzetta.example/.well-known/louise", True)


def test_a_certified_collection_is_manifest_only(codex):
    certified = replaced(codex, publication="certified", certifier=CERT,
                         content="manifest-only", embedding="certifier-only")
    cx.validate(certified)
    with pytest.raises(CodexError, match='content is "manifest-only"'):
        cx.validate(replace(certified, content=None))
    with pytest.raises(CodexError, match="only a certified collection"):
        cx.validate(replace(codex, certifier=CERT))
    with pytest.raises(CodexError, match="names its certifier"):
        cx.validate(replace(codex, publication="certified", content="manifest-only"))


def test_an_open_collection_is_open_and_frozen(codex):
    with pytest.raises(CodexError, match="embedding is open"):
        cx.validate(replace(codex, embedding="certifier-only"))
    with pytest.raises(CodexError, match="succession is frozen"):
        cx.validate(replace(codex, succession={"open-after": "P2Y"}))


# ---- lineage --------------------------------------------------------------

A_DEFECT = Defect("records-vs-origin", PLATFORM, ("year=1975-1979/00003",),
                  "the origin's bytes", "a truncated abstract",
                  "the parser dropped everything after the first <AbstractText>")


def test_a_root_has_epoch_zero(codex):
    assert codex.space.epoch == "0"
    with pytest.raises(CodexError, match='root\'s space.epoch is "0"'):
        cx.validate(replace(codex, space=replace(codex.space, epoch="")))


def test_a_daughter_names_the_mothers_last_snapshot(codex):
    mother_id = codex.collection_id
    d = replace(codex, continues=mother_id, continues_through="2026-08",
                toolchain=Toolchain("sha256:" + "2a" * 32, PLATFORM))
    cx.validate(d)
    assert d.relation == "daughter"
    with pytest.raises(CodexError, match="continues_through"):
        cx.validate(replace(d, continues_through=None))


def test_continues_through_belongs_to_a_daughter(codex):
    with pytest.raises(CodexError, match="belong.*to a daughter"):
        cx.validate(replace(codex, continues_through="2026-08"))


def test_a_codex_names_at_most_one_relation(codex):
    with pytest.raises(CodexError, match="at most one"):
        cx.validate(replace(codex, continues="sha256:" + "1" * 64,
                            continues_through="2026-08",
                            derived_from="sha256:" + "2" * 64))


def test_a_corrective_sisters_epoch_is_salted(codex):
    mother_id = codex.collection_id
    corrective = replace(
        codex, replaces=Replaces(mother_id, A_DEFECT),
        space=replace(codex.space, epoch=cx.corrective_epoch(mother_id)))
    cx.validate(corrective)
    assert corrective.relation == "corrective-sister"
    assert corrective.space_id != codex.space_id
    with pytest.raises(CodexError, match="salted with the"):
        cx.validate(replace(corrective, space=replace(codex.space, epoch="0")))


def test_a_corrective_sister_carries_witnesses(codex):
    mother_id = codex.collection_id
    bare = replace(A_DEFECT, witnesses=())
    with pytest.raises(CodexError, match="names at least one shard"):
        cx.validate(replace(
            codex, replaces=Replaces(mother_id, bare),
            space=replace(codex.space, epoch=cx.corrective_epoch(mother_id))))


def test_the_sister_of_a_corrective_sister_keeps_the_salted_epoch(codex):
    """09 §9.3, corrected: forcing "0" on every sister would give the weekly
    sister of a corrected collection a different space_id from her mother
    although encoder, weights, layers and cleaning are identical."""
    mother_id = codex.collection_id
    salted = cx.corrective_epoch(mother_id)
    corrective = replace(codex, replaces=Replaces(mother_id, A_DEFECT),
                         space=replace(codex.space, epoch=salted))
    her_sister = replace(corrective, replaces=None,
                         derived_from=corrective.collection_id,
                         update=Update("weekly", 1, 0), work=Work(9_000_000, "P1W"))
    cx.validate(her_sister)
    assert her_sister.space.epoch == salted
    assert her_sister.space_id == corrective.space_id


def test_space_from_belongs_to_a_sister(codex):
    with pytest.raises(CodexError, match="space_from belongs to a sister"):
        cx.validate(replace(codex, space_from="sha256:" + "3" * 64))


# ---- lineage walks --------------------------------------------------------

def test_lineage_id_walks_continues_to_the_root(codex):
    root = codex
    d1 = replace(root, continues=root.collection_id, continues_through="2026-08",
                 toolchain=Toolchain("sha256:" + "2a" * 32, PLATFORM))
    d2 = replace(d1, continues=d1.collection_id, continues_through="2027-01",
                 toolchain=Toolchain("sha256:" + "3a" * 32, d1.toolchain.platform_image))
    held = {c.collection_id: c for c in (root, d1, d2)}
    assert cx.lineage_id(d2, held.get) == root.collection_id
    assert [c.collection_id for c in cx.walk_lineage(d2, held.get)] == \
           [d2.collection_id, d1.collection_id, root.collection_id]


def test_an_unresolvable_mother_stops_the_walk_honestly(codex):
    d1 = replace(codex, continues=codex.collection_id, continues_through="2026-08",
                 toolchain=Toolchain("sha256:" + "2a" * 32, PLATFORM))
    assert cx.lineage_id(d1, lambda _cid: None) == codex.collection_id
    assert [c.collection_id for c in cx.walk_lineage(d1, lambda _cid: None)] == \
           [d1.collection_id]


# ---- the seeded sample (09 §9.1) ------------------------------------------

def test_the_sample_is_hmac_over_the_ids_lowest_first():
    import hmac as _hmac
    seed = bytes(range(32))
    universe = [str(i) for i in range(20)]
    got = cx.sample(seed, 5, universe)
    scored = sorted(universe, key=lambda i: (
        int.from_bytes(_hmac.new(seed, i.encode(), hashlib.sha256).digest()[:8], "big"),
        i.encode()))
    assert got == scored[:5]


def test_the_same_seed_draws_the_same_sample_anywhere():
    """Reproducible by anyone once the seed is published, predictable by
    nobody before (09 §9.1)."""
    seed = b"\x07" * 32
    u = [str(i) for i in range(512)]
    assert cx.sample(seed, 16, u) == cx.sample(seed, 16, list(reversed(u)))


def test_a_different_seed_draws_a_different_sample():
    u = [str(i) for i in range(512)]
    assert cx.sample(b"\x01" * 32, 16, u) != cx.sample(b"\x02" * 32, 16, u)


def test_the_universe_sorts_by_utf8_bytes_not_numerically():
    """§9.12's universe is {"0"…"511"} — decimal indices as STRINGS, so
    "10" sorts before "9". The order only breaks ties, but it is written
    into the rule and two peers must break them the same way."""
    seed = b"\x03" * 32
    u = ["9", "10", "100"]
    assert cx.sample(seed, 3, u) == cx.sample(seed, 3, ["100", "9", "10"])


def test_a_seed_is_thirty_two_bytes():
    with pytest.raises(CodexError, match="32 bytes"):
        cx.sample(b"short", 1, ["a"])


def test_a_fraction_of_a_universe_is_at_least_one():
    """`n = max(1, ⌈f × N⌉)` (09 §9.1)."""
    assert cx.sample_size("0.05", 100) == 5
    assert cx.sample_size("0.05", 101) == 6
    assert cx.sample_size("0.001", 10) == 1, "never zero"
    assert cx.sample_size("1", 40) == 40, "\"1\" is exhaustive"


def test_a_fraction_is_a_decimal_string_because_the_plan_forbids_floats():
    with pytest.raises(CodexError, match="decimal string"):
        cx.sample_size("five percent", 100)
    for bad in ("0", "1.5", "-0.1"):
        with pytest.raises(CodexError, match=r"in \(0, 1\]|decimal string"):
            cx.sample_size(bad, 100)


def test_the_initial_sort_is_redundant():
    """09 §9.1 opens with "sort the universe of ids by their UTF-8 bytes",
    and `sample` does not perform that sort.

    It is redundant because the draw's own order is `(score, id bytes)`, a
    total order on distinct ids, so the input order cannot reach the output.
    This pins the two formulations against each other over a universe big
    enough for a difference to show — "obviously equivalent" is exactly how
    a sampling function stops being reproducible without anyone noticing.
    """
    import hmac as _hmac
    seed = b"\x05" * 32
    u = [f"pubmed:{i}:0:1" for i in range(4000)]

    def score(i):
        tag = _hmac.new(seed, i.encode("utf-8"), hashlib.sha256).digest()
        return int.from_bytes(tag[:8], "big"), i.encode("utf-8")

    with_sort = sorted(sorted(u, key=lambda s: s.encode("utf-8")), key=score)
    assert cx.sample(seed, 16, u) == with_sort[:16]
    assert cx.sample(seed, 16, u) == cx.sample(seed, 16, list(reversed(u)))


def test_the_universe_may_be_a_generator_and_is_never_materialised():
    """The centres rule draws over every chunk id in the collection — 36M
    for PubMed. Sorting peaks at 232 MB per 1M ids (measured), i.e. about
    8 GB at that size; drawing with a heap of `n` peaks at 5 KB. So the
    universe must be allowed to stream, and a caller that hands over a
    generator must not have it silently turned into a list."""
    seed = b"\x06" * 32
    n_seen = 0

    def universe():
        nonlocal n_seen
        for i in range(5000):
            n_seen += 1
            yield f"pubmed:{i}:0:1"

    got = cx.sample(seed, 16, universe())
    assert len(got) == 16
    assert n_seen == 5000, "streamed once, in full"
    assert got == cx.sample(seed, 16, [f"pubmed:{i}:0:1" for i in range(5000)])


def test_a_bad_seed_is_refused_before_the_universe_is_touched():
    """At 36M ids the checks used to run after the sort, so a mistyped seed
    cost a full corpus sort before saying so."""
    touched = False

    def universe():
        nonlocal touched
        touched = True
        yield "a"

    with pytest.raises(CodexError, match="32 bytes"):
        cx.sample(b"\x01" * 31, 4, universe())
    with pytest.raises(CodexError, match="not negative"):
        cx.sample(b"\x01" * 32, -1, universe())
    assert not touched


def test_derivation_is_a_required_field_and_null_where_there_is_none():
    """09 §9.3 lists `derivation` as required ("object or null | yes"), and
    the module did not know the field at all until 2026-09-12 — so every
    Codex written before then has 34 of the spec's 35 keys, and `_only`
    refuses such a file now: "every field is present, a field not in use
    is null". That refusal is deliberate; it is what makes this a flag day
    rather than a silent divergence."""
    assert "derivation" in cx._TOP
    assert "derivation" in cx._TOP_V1, "§9.3: version 1 is this object without record_line"
    d = dict(WORKED_EXAMPLE)
    del d["derivation"]
    with pytest.raises(CodexError, match="missing field"):
        cx.loads(cx.canonical_json(d))


def test_a_derivation_names_the_other_spaces_space_id(codex):
    """`from` is a `space_id` and NEVER a `collection_id` (§9.3). The two
    are the same shape, so what code can settle is that it is not this
    object's own id: a space that is a pure function of itself is not a
    derivation."""
    base = codex

    ok = replace(base, derivation=cx.Derivation(
        "sha256:" + "a" * 64, cx.TRUNCATE, {"dim": 256}))
    cx.validate(ok)
    assert ok.to_dict()["derivation"] == {
        "from": "sha256:" + "a" * 64, "kind": "truncate", "params": {"dim": 256}}, \
        "`from` on the wire, not the Python-keyword-dodging `from_`"

    for bad, match in (
            (cx.Derivation("not-a-digest", cx.TRUNCATE, {}), "space_id"),
            (cx.Derivation(base.space_id, cx.TRUNCATE, {}), "own space_id")):
        with pytest.raises(CodexError, match=match):
            cx.validate(replace(base, derivation=bad))

    # NOT checkable against `collection_id`, and the attempt is a trap:
    # `derivation` is inside the identity, so an object's own
    # collection_id already accounts for the derivation it holds and can
    # never equal the `from` inside it. `space_id` excludes `derivation`
    # (02 §2.4), which is what makes it stable enough to compare against.
    naming_mothers_collection = replace(
        base, derivation=cx.Derivation(base.collection_id, cx.QUANTISE, {}))
    cx.validate(naming_mothers_collection)
    assert naming_mothers_collection.derivation.from_ != \
        naming_mothers_collection.collection_id

    with pytest.raises(CodexError, match="derivation.kind"):
        cx.loads(cx.canonical_json(dict(WORKED_EXAMPLE, derivation={
            "from": "sha256:" + "a" * 64, "kind": "reroll", "params": {}})))


def test_derivation_is_inside_collection_id_but_not_space_id(codex):
    """02 §2.4 calls this load-bearing: two creators applying the same
    function to the same source produce the SAME NUMBERS, so if the
    derivation entered `space_id` they would land in different spaces and
    could not share one bundle — which is the whole point of allowing it.
    It is still inside `collection_id`, like every key but `descriptive`."""
    base = codex
    derived = replace(base, derivation=cx.Derivation(
        "sha256:" + "b" * 64, cx.QUANTISE, {"bits": 8}))
    assert derived.space_id == base.space_id, "NOT part of space_id"
    assert derived.collection_id != base.collection_id, "but inside the identity"

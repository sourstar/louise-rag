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
from tests.hold.conftest import (CAL_SET, CAL_VECS, ENCODER, ENVELOPE,
                                 PLATFORM, PUBMED_CHECKS, PUBMED_SAMPLE,
                                 PUBMED_SLICING, PUBMED_SPEC, Q_ENCODER,
                                 Q_WEIGHTS, TERMS, WEIGHTS, a_credit, a_facet)


def _h(obj) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()


# ---- the worked example, written out by hand ------------------------------

WORKED_EXAMPLE = {
    "kind": "louise-codex", "version": 1,
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
    "layers": [{"chunker": {"id": "abstract", "version": 1, "params": {}},
                "rule": "default"}],
    "slicing": PUBMED_SLICING,
    "space": {
        "encoder": {"image": ENCODER, "weights": WEIGHTS,
                    "label": "MedCPT-Article-Encoder"},
        "dim": 768, "dtype": "f16", "ctx": 512, "pooling": "cls",
        "normalise": True,
        "query": {"encoder": {"image": Q_ENCODER, "weights": Q_WEIGHTS},
                  "instruction": None},
        "tolerance": "0.95",
        "calibration": {"set": CAL_SET, "vectors": CAL_VECS,
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
    "derived_from": None, "space_from": None, "replaces": None,
    "facets": [], "credits": [], "profile": None,
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
    assert codex.space_id == _h({
        "cleaning_version": 1,
        "encoder": {"image": ENCODER, "weights": WEIGHTS},
        "layers": [{"chunker": {"id": "abstract", "version": 1, "params": {}},
                    "rule": "default"}],
        "space": {"ctx": 512, "dim": 768, "dtype": "f16", "epoch": "0",
                  "normalise": True, "pooling": "cls",
                  "query": {"encoder": {"image": Q_ENCODER, "weights": Q_WEIGHTS},
                            "instruction": None}},
    })
    assert codex.space_segment == codex.space_id.split(":")[1][:16]
    assert len(codex.space_segment) == 16


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
    d = dict(codex.to_dict(), version=2)
    with pytest.raises(CodexError, match="unsupported Codex version 2"):
        cx.from_dict(d)


# ---- the space block ------------------------------------------------------

def test_tolerance_is_a_decimal_string_never_a_float(codex):
    with pytest.raises(CodexError, match="tolerance must be a decimal string"):
        cx.validate(replace(codex, space=replace(codex.space, tolerance="ninety")))


def test_a_quorum_may_not_ask_more_than_it_draws(codex):
    bad = replace(codex.space, calibration=Calibration(CAL_SET, CAL_VECS, 16, 17))
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
        "archive", "producer", "license", "attribution", "coverage"}
    for missing in ("license", "attribution"):
        bad = replace(a_credit(), **{missing: ""})
        with pytest.raises(CodexError, match="credited under"):
            cx.validate(replace(codex, credits=(bad,)))


# ---- publication ----------------------------------------------------------

CERT = Certifier("ed25519:" + "5c" * 32, "https://gazzetta.example/.well-known/louise", True)


def test_a_certified_collection_is_manifest_only(codex):
    certified = replace(codex, publication="certified", certifier=CERT,
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

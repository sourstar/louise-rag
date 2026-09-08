"""tests/hold/test_collection.py — a collection on disk is a directory named
by the 64 hexadecimal digits of its `collection_id` (docs/09 §9.3, 07 §7.3),
and the four ways to make one are the four relations of docs/02 §2.7.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from app.hold import collection as col
from app.hold.codex import (CodexError, Defect, Descriptive, Encoder, Space,
                            Update, Work)
from tests.hold.conftest import PLATFORM, a_credit, pubmed

PLATFORM2 = "sha256:" + "2a" * 32
ENCODER2 = "sha256:" + "9d" * 32
WEIGHTS2 = "sha256:" + "af" * 32

A_DEFECT = Defect("records-vs-origin", PLATFORM2, ("year=1975-1979/00017",),
                  "the origin's bytes", "a truncated abstract",
                  "the parser dropped everything after the first <AbstractText>")


def other_space(codex) -> Space:
    return replace(codex.space, encoder=Encoder(ENCODER2, WEIGHTS2, "bge-m3"))


# ---- on disk --------------------------------------------------------------

def test_create_writes_canonical_bytes_under_its_local_name(tmp_path: Path, codex):
    """07 §7.8: the directory is the local name — the plugin's, by default."""
    c = col.create(tmp_path, codex)
    d = tmp_path / "pubmed" / "drag"
    assert (d / "codex.json").read_bytes() == c.canonical()
    assert col.name_of(tmp_path, c.collection_id) == "pubmed"
    col.create(tmp_path, pubmed())                       # idempotent
    assert [x.collection_id for x in col.list_all(tmp_path)] == [c.collection_id]


def test_a_collection_resolves_by_all_four_forms(tmp_path: Path, codex):
    """07 §7.7: a full id with or without its prefix, a unique prefix of at
    least 12 hex characters, a local name, or a lineage id."""
    c = col.create(tmp_path, codex)
    bare = c.collection_id.replace("sha256:", "")
    assert col.read(tmp_path, c.collection_id) == c
    assert col.read(tmp_path, bare) == c
    assert col.read(tmp_path, bare[:12]) == c
    assert col.read(tmp_path, "pubmed") == c
    with pytest.raises(CodexError, match="no collection"):
        col.read(tmp_path, "nope")


def test_a_prefix_shorter_than_twelve_is_not_a_reference(tmp_path: Path, codex):
    c = col.create(tmp_path, codex)
    with pytest.raises(CodexError, match="no collection"):
        col.read(tmp_path, c.collection_id.replace("sha256:", "")[:11])


def test_a_second_collection_takes_a_name_with_its_id_prefix(tmp_path, codex):
    """07 §7.8: the plugin's name, or the plugin's name and the first 12 hex
    characters of the id when that is taken."""
    first = col.create(tmp_path, codex)
    second = col.create(tmp_path, pubmed(cleaning_version=2))
    names = sorted(n for n, _ in col.list_named(tmp_path))
    assert names[0] == "pubmed"
    assert names[1] == "pubmed-" + second.collection_id.split(":")[1][:12]
    assert first.collection_id != second.collection_id


def test_a_name_holds_one_collection_for_its_life(tmp_path: Path, codex):
    col.create(tmp_path, codex, name="mine")
    with pytest.raises(CodexError, match="already holds"):
        col.create(tmp_path, pubmed(cleaning_version=2), name="mine")


def test_a_name_that_now_holds_another_collection_no_longer_answers_the_old_id(
        tmp_path: Path, codex):
    """The directory claims no id, so the refusal that used to hang on the
    directory's name is gone (09 §9.3, corrected). What replaces it: the id
    simply does not resolve, because nothing under the root holds it."""
    c = col.create(tmp_path, codex)
    (tmp_path / "pubmed" / "drag" / "codex.json").write_bytes(
        pubmed(cleaning_version=99).canonical())
    with pytest.raises(CodexError, match="no collection"):
        col.read(tmp_path, c.collection_id)
    assert col.read(tmp_path, "pubmed").cleaning_version == 99


def test_a_retitled_copy_keeps_its_directory(tmp_path: Path, codex):
    """Two peers may hold one collection_id under two titles (09 §9.3), so
    the directory name still resolves and only `codex_hash` differs."""
    c = col.create(tmp_path, codex)
    retitled = replace(codex, descriptive=Descriptive("PubMed (mirror)", "A copy."))
    (tmp_path / "pubmed" / "drag" / "codex.json").write_bytes(retitled.canonical())
    back = col.read(tmp_path, c.collection_id)
    assert back.collection_id == c.collection_id
    assert back.codex_hash != c.codex_hash


def test_create_refuses_lineage_fields(tmp_path: Path, codex):
    with pytest.raises(CodexError, match="create makes a root"):
        col.create(tmp_path, replace(codex, derived_from="sha256:" + "1" * 64))


# ---- derive: a sister -----------------------------------------------------

def test_derive_makes_a_sister_in_a_new_space(tmp_path: Path, codex):
    mother = col.create(tmp_path, codex)
    sister = col.derive(tmp_path, mother.collection_id, space=other_space(codex))
    assert sister.relation == "sister"
    assert sister.derived_from == mother.collection_id
    assert sister.space_id != mother.space_id
    assert sister.origin == mother.origin
    assert sister.cleaning_version == mother.cleaning_version
    shown = col.show(tmp_path, sister.collection_id)
    assert shown["relation"] == "sister"
    assert shown["lineage_id"] == sister.collection_id   # a sister starts a lineage


def test_a_sister_may_change_only_space_layers_update_and_work(tmp_path, codex):
    mother = col.create(tmp_path, codex)
    weekly = col.derive(tmp_path, mother.collection_id,
                        update=Update("weekly", 1, 0), work=Work(9_000_000, "P1W"))
    assert weekly.space_id == mother.space_id, "same encoder, same space"
    assert weekly.update.cadence == "weekly"
    with pytest.raises(CodexError, match="a sister changes something"):
        col.derive(tmp_path, mother.collection_id)


def test_a_sister_who_changes_nothing_but_the_cadence_shares_every_vector(tmp_path, codex):
    """02 §2.7's motivating case, and the one 09 §9.3's epoch rule broke."""
    mother = col.create(tmp_path, codex)
    weekly = col.derive(tmp_path, mother.collection_id, update=Update("weekly", 1, 0))
    assert weekly.space_id == mother.space_id
    assert weekly.credits == mother.credits


def test_a_sisters_credits_follow_the_space(tmp_path: Path):
    credited = pubmed(credits=(a_credit(),))
    mother = col.create(tmp_path, credited)

    same_space = col.derive(tmp_path, mother.collection_id, update=Update("weekly", 1, 0))
    assert same_space.credits == mother.credits, "in her mother's space she owes"

    elsewhere = col.derive(tmp_path, mother.collection_id, space=other_space(credited))
    assert elsewhere.credits == (), "in another space nothing of her mother's is admissible"


def test_a_sister_in_a_new_space_may_declare_her_own_archives(tmp_path, codex):
    mother = col.create(tmp_path, codex)
    hers = (a_credit("zenodo-medcpt"),)
    sister = col.derive(tmp_path, mother.collection_id, space=other_space(codex),
                        credits=hers)
    assert sister.credits == hers


def test_archives_are_refused_where_the_space_brings_its_own_credits(tmp_path, codex):
    mother = col.create(tmp_path, codex)
    with pytest.raises(CodexError, match="carries her mother's credits"):
        col.derive(tmp_path, mother.collection_id, update=Update("weekly", 1, 0),
                   credits=(a_credit(),))


def test_space_from_copies_the_space_and_its_credits(tmp_path: Path, codex):
    source = col.create(tmp_path, pubmed(
        descriptive=Descriptive("PubMed on BGE", "Another space."),
        space=other_space(codex), credits=(a_credit(),)))
    mother = col.create(tmp_path, codex)
    sister = col.derive(tmp_path, mother.collection_id, space_from=source)
    assert sister.space_id == source.space_id
    assert sister.space_from == source.collection_id
    assert sister.credits == source.credits


def test_space_from_refuses_a_different_cleaning_version(tmp_path: Path, codex):
    """Copying it would re-derive the corpus the sister is defined to
    inherit; not copying it makes her space_id differ from the source's,
    which is the whole of what the flag promises (07 §7.3, 09 §9.3)."""
    source = col.create(tmp_path, pubmed(
        descriptive=Descriptive("PubMed cleaned again", "A different cleaner."),
        cleaning_version=2, space=other_space(codex)))
    mother = col.create(tmp_path, codex)
    with pytest.raises(CodexError, match="cleaning_version is 2 and the mother's is 1"):
        col.derive(tmp_path, mother.collection_id, space_from=source)


def test_a_sister_keeps_her_mothers_epoch(tmp_path: Path, codex):
    mother = col.create(tmp_path, codex)
    corrective = col.replace(tmp_path, mother.collection_id, defect=A_DEFECT,
                             platform_image=PLATFORM2)
    her_sister = col.derive(tmp_path, corrective.collection_id,
                            update=Update("weekly", 1, 0))
    assert her_sister.space.epoch == corrective.space.epoch
    assert her_sister.space_id == corrective.space_id


# ---- continue: a daughter -------------------------------------------------

def test_continue_makes_a_daughter_in_the_same_space_with_a_lineage(tmp_path, codex):
    mother = col.create(tmp_path, codex)
    d1 = col.continue_(tmp_path, mother.collection_id, platform_image=PLATFORM2,
                       continues_through="2026-10",
                       origin_reachable_until="2026-09")
    assert d1.relation == "daughter"
    assert d1.space_id == mother.space_id, "a daughter keeps her mother's space"
    assert d1.continues_through == "2026-10"
    assert d1.toolchain.mother_platform_image == mother.toolchain.platform_image
    d2 = col.continue_(tmp_path, d1.collection_id,
                       platform_image="sha256:" + "7c" * 32,
                       continues_through="2027-02")
    assert col.show(tmp_path, d2.collection_id)["lineage_id"] == mother.collection_id
    assert col.show(tmp_path, mother.collection_id)["lineage_id"] == mother.collection_id


def test_a_daughter_carries_her_mothers_platform_image(tmp_path: Path, codex):
    mother = col.create(tmp_path, codex)
    d = col.continue_(tmp_path, mother.collection_id, platform_image=PLATFORM2,
                      continues_through="2026-10")
    assert d.toolchain.mother_platform_image == PLATFORM
    assert d.toolchain.platform_image == PLATFORM2


# ---- replace: a corrective sister -----------------------------------------

def test_replace_makes_a_corrective_sister_with_a_fresh_space(tmp_path, codex):
    mother = col.create(tmp_path, codex)
    fixed = col.replace(tmp_path, mother.collection_id, defect=A_DEFECT,
                        platform_image=PLATFORM2)
    assert fixed.relation == "corrective-sister"
    assert fixed.replaces.codex == mother.collection_id
    assert fixed.replaces.defect == A_DEFECT
    assert fixed.layers == mother.layers
    assert fixed.cleaning_version == mother.cleaning_version
    assert fixed.space.encoder == mother.space.encoder
    assert fixed.space_id != mother.space_id, \
        "identical encoder and layers, still a new space by construction"
    assert col.read(tmp_path, fixed.collection_id).replaces.defect == A_DEFECT


def test_a_corrective_sister_owes_nothing(tmp_path: Path):
    """Her space is fresh, so no archive's bundles are admissible to her."""
    mother = col.create(tmp_path, pubmed(credits=(a_credit(),)))
    fixed = col.replace(tmp_path, mother.collection_id, defect=A_DEFECT)
    assert fixed.credits == ()


# ---- show -----------------------------------------------------------------

def test_show_names_both_hashes_and_the_space_segment(tmp_path: Path, codex):
    c = col.create(tmp_path, codex)
    shown = col.show(tmp_path, c.collection_id)
    assert shown["collection_id"] == c.collection_id
    assert shown["codex_hash"] == c.codex_hash
    assert shown["space"] == c.space_segment
    assert shown["license"] == {"class": "red"}
    assert shown["refused"] is None

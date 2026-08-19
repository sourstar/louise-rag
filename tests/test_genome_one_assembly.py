"""§9: one collection per assembly — enforced, not trusted.

GenomeSource's native_id is the record ordinal WITHIN a FASTA, so two
assemblies in one corpus mint the same document ids and overwrite each
other. Observed live: growing the collection to the old `full` profile
downloaded dm6 and hg38 beside sacCer3, and the manifest ended up with
yeast titles on human locators while yeast vectors stayed attached to
those ids.
"""
import gzip

import pytest

from app.acquire.specs import ACQUIRE_REGISTRY, GENOME_SPEC
from app.plugins.base import DiscoverState
from app.plugins.genome.source import GenomeSource, _assembly_of


def _fasta(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f">{name}\n{seq}\n" for name, seq in records)
    if path.name.endswith(".gz"):
        path.write_bytes(gzip.compress(body.encode()))
    else:
        path.write_text(body)


def test_a_second_assembly_is_refused_not_merged(tmp_path):
    root = tmp_path / "corpus"
    _fasta(root / "dm6" / "bigZips" / "dm6.fa.gz",
           [("chr2L", "ACGT" * 10), ("chr3R", "TTGA" * 10)])
    _fasta(root / "sacCer3" / "bigZips" / "sacCer3.fa.gz",
           [("chrI", "GGCC" * 10)])

    refs = list(GenomeSource().discover(
        DiscoverState(corpus_root=root, state_dir=None)))
    docs = [r for r in refs if not r.error]
    refusals = [r for r in refs if r.error]

    # exactly one assembly ingested, the other refused as a CONTAINER
    # report — never as a document that could collide
    assert {r.extra["assembly"] for r in docs} == {"dm6.fa.gz"}
    assert len(refusals) == 1
    assert refusals[0].scope == "container"
    assert "own collection" in refusals[0].error
    # and the refused file contributed no document ids at all
    assert all("sacCer3" not in r.locator for r in docs)


def test_one_assembly_alone_is_unaffected(tmp_path):
    root = tmp_path / "corpus"
    _fasta(root / "sacCer3" / "bigZips" / "sacCer3.fa.gz",
           [("chrI", "GGCC" * 10), ("chrII", "AATT" * 10)])
    refs = list(GenomeSource().discover(
        DiscoverState(corpus_root=root, state_dir=None)))
    assert [r.error for r in refs] == [None, None]
    assert [r.native_id for r in refs] == ["0", "1"]


def test_assembly_of_reads_the_stem():
    from pathlib import Path
    assert _assembly_of(Path("/x/hg38/bigZips/hg38.fa.gz")) == "hg38"
    assert _assembly_of(Path("/x/sacCer3.fasta")) == "sacCer3"


def test_genome_profiles_are_alternatives_never_increments():
    """Each profile is ONE assembly, and the spec says so, so the UI's
    grow flow cannot offer a second corpus into the same collection."""
    assert GENOME_SPEC.profile_mode == "exclusive"
    for name, f in GENOME_SPEC.profiles.items():
        assert len(f.include) == 1, f"profile {name!r} stacks assemblies"
    assert set(GENOME_SPEC.profiles) == {"demo", "sacCer3", "dm6", "hg38"}
    # genome is the ONLY source that must never combine profiles
    for plugin, spec in ACQUIRE_REGISTRY.items():
        if plugin != "genome":
            assert spec.profile_mode != "exclusive"


def test_grow_offers_nothing_for_exclusive_profiles(isolated_config, tmp_path):
    from app import collections as collections_mod, web
    c = collections_mod.Collection(
        name="g", plugin="genome", profile="sacCer3", embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")
    assert web._grow_options(c) == [], \
        "growing a genome collection would drop a second assembly into it"

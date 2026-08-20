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

from app.acquire import specs
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
    # the catalogue grows; these are the ones the validation ladder names
    assert {"demo", "sacCer3", "dm6", "hg38"} <= set(GENOME_SPEC.profiles)
    # every profile is one UCSC assembly plus the "demo" alias for the
    # smallest — nothing else may appear here, since a profile name IS the
    # assembly a collection pins itself to
    assert set(GENOME_SPEC.profiles) == {"demo"} | {
        db for db, _, _ in specs._UCSC_ASSEMBLIES}
    assert GENOME_SPEC.profiles["demo"] == GENOME_SPEC.profiles["sacCer3"]
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


def test_every_assembly_declares_its_size():
    """A declared size is what makes the plan honest and adoption possible.

    HTTPS has no directory-listing protocol, so the engine knows exactly and
    only what the spec declares: an undeclared size reports a 983 MB assembly
    as "0 bytes total" in the budget planner, and makes is_adoptable() refuse
    every already-present file, because without it there is no way to tell a
    complete download from a truncated one. Found live — three genome
    corpora sat on disk unrecognised for exactly this reason.
    """
    sizes = {db: size for db, _, size in specs._UCSC_ASSEMBLIES}
    declared = {f.rel_path: f.size
                for e in GENOME_SPEC.endpoints for f in e.static_files}
    assert declared, "the spec must declare its files: HTTPS cannot list them"
    for db, size in sizes.items():
        rel = f"{db}/bigZips/{db}.fa.gz"
        assert declared.get(rel) == size, f"{db}: static_file size must match the table"
    assert all(s and s > 0 for s in declared.values()), "no zero or None sizes"


def test_the_size_table_and_the_estimates_agree():
    """estimated_bytes drives the disk preflight, static_files drives the
    plan. Two numbers for one file is one number too many."""
    for db, _, size in specs._UCSC_ASSEMBLIES:
        assert GENOME_SPEC.estimated_bytes[db] == size, db
    assert GENOME_SPEC.estimated_bytes["demo"] == \
        GENOME_SPEC.estimated_bytes["sacCer3"], "demo is the sacCer3 alias"


def test_a_preseeded_assembly_is_adoptable(tmp_path):
    """The live case: a FASTA placed on disk by hand (or by zfs send) must
    be recognised and recorded, not re-downloaded."""
    from app import collections as collections_mod
    from app.acquire import engine

    corpus = tmp_path / "corpus"
    fasta = corpus / "dm6" / "bigZips" / "dm6.fa.gz"
    fasta.parent.mkdir(parents=True)
    size = dict((db, s) for db, _, s in specs._UCSC_ASSEMBLIES)["dm6"]
    fasta.write_bytes(b"\0" * size)

    col = collections_mod.Collection(
        name="genome-dm6", plugin="genome", profile="dm6",
        embed_mode_default=None, corpus_dir=corpus,
        manifest_path=tmp_path / "m.db", fts_dir=tmp_path / "fts",
        lance_dir=tmp_path / "lance")
    remote = next(f for e in GENOME_SPEC.endpoints for f in e.static_files
                  if f.rel_path == "dm6/bigZips/dm6.fa.gz")
    assert engine.is_adoptable(col, remote)

    fasta.write_bytes(b"\0" * (size - 1))       # truncated
    assert not engine.is_adoptable(col, remote)


def test_a_relocated_corpus_updates_locators_without_reindexing(isolated_config,
                                                                 tmp_path):
    """Moving a dataset changes every stored locator while leaving mtime and
    size untouched — so the change-detection branch, which is keyed on
    content identity, cannot see it.

    Observed live: renaming the genome collection's ZFS dataset left all 17
    documents pointing at the old mount and every one failed the embed stage
    with FileNotFoundError. Rescanning did not help; the file really was
    unchanged. The bytes being identical is exactly why the vectors must
    survive the fix.
    """
    import shutil
    from app import collections as collections_mod, db, discovery

    def collection_at(corpus):
        return collections_mod.Collection(
            name="g", plugin="genome", profile="sacCer3", embed_mode_default=None,
            corpus_dir=corpus, manifest_path=tmp_path / "m.db",
            fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")

    before = tmp_path / "corpora" / "genome"
    _fasta(before / "sacCer3.fa", [("chrI", "ACGT" * 40), ("chrII", "TTGC" * 40)])
    col = collection_at(before)
    con = db.connect(col)
    discovery.sync_collection(con, col)

    rows = con.execute("SELECT uid,path FROM documents ORDER BY uid").fetchall()
    assert rows and all("corpora/genome/" in r["path"] for r in rows)
    # pretend the pipeline already ran: this is what must NOT be thrown away
    con.execute("UPDATE documents SET rdf_done=1,fts_done=1,embed_done=1,"
                "error='embed: FileNotFoundError',retries=2")

    after = tmp_path / "corpora" / "genome-sacCer3"
    after.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(before), str(after))

    stats = discovery.sync_collection(con, collection_at(after))
    assert stats.get("relocated") == 2, stats
    assert stats["changed"] == 0, "the bytes did not change, so nothing was re-read"

    for r in con.execute("SELECT * FROM documents ORDER BY uid"):
        assert str(after) in r["path"], "locator must follow the corpus"
        assert r["error"] is None, "a path-only failure must clear itself"
        assert r["retries"] == 0
        # the expensive work survives: same bytes, same parse, same vectors
        assert (r["rdf_done"], r["fts_done"], r["embed_done"]) == (1, 1, 1)


def test_an_unmoved_corpus_reports_no_relocation(isolated_config, tmp_path):
    """The new branch must stay silent on the ordinary path, or every scan
    would rewrite every row for nothing."""
    from app import collections as collections_mod, db, discovery

    corpus = tmp_path / "corpus"
    _fasta(corpus / "sacCer3.fa", [("chrI", "ACGT" * 40)])
    col = collections_mod.Collection(
        name="g", plugin="genome", profile="sacCer3", embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")
    con = db.connect(col)
    discovery.sync_collection(con, col)
    stats = discovery.sync_collection(con, col)
    assert "relocated" not in stats, stats

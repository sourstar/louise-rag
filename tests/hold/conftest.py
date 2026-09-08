"""A whole, valid Codex to build the hold tests on.

It is 09 §9.3's worked example with the elided digests filled in: the same
fields, the same shapes, the same values wherever the document gives one.
Nothing here is convenience — a fixture that omitted a field would let a
test pass against a Codex the specification refuses.
"""
import pytest

from app.hold.codex import (Calibration, Chunker, Codex, Credit, Descriptive,
                            Encoder, Facet, Layer, License, Origin, Parser,
                            Query, Space, Toolchain, Update, Validation, Work)

PLATFORM = "sha256:" + "1f" * 32
ENCODER = "sha256:" + "8c" * 32
WEIGHTS = "sha256:" + "e2" * 32
Q_ENCODER = "sha256:" + "0a" * 32
Q_WEIGHTS = "sha256:" + "b7" * 32
CAL_SET = "sha256:" + "6b" * 32
CAL_VECS = "sha256:" + "d4" * 32
ENVELOPE = "sha256:" + "77" * 32
TERMS = "sha256:" + "b9" * 32
ARCHIVE_KEY = "ed25519:" + "3e" * 32

PUBMED_SPEC = {
    "source_id": "pubmed", "transport": "ftp",
    "url": "ftp://ftp.ncbi.nlm.nih.gov/pubmed/",
    "layout": "container-files", "listing": "directory", "files": None,
    "filters": ["baseline/*.xml.gz", "updatefiles/*.xml.gz"],
    "checksums": {"kind": "sidecar", "suffix": ".md5", "algo": "md5"},
    "delta": {"mode": "updatefiles-dir", "subdir": "updatefiles"},
}

PUBMED_SLICING = {
    "keys": ["year"],
    "year": {"field": "date", "bins": ["<1950", "5y<2000", "1y"]},
    "unknown": "year=unknown", "shard_records": 30000, "order": "numeric",
}

PUBMED_CHECKS = ("roots", "records", "chunks", "records-vs-origin",
                 "vectors-sample", "calibration", "centres", "labels",
                 "license", "signature")

PUBMED_SAMPLE = {
    "records": {"shards": "0.05"},
    "chunks": {"shards": "0.05"},
    "records-vs-origin": {"shards": "0.05"},
    "vectors-sample": {"shards": "0.10"},
}


def pubmed(**over) -> Codex:
    """09 §9.3's worked example, whole."""
    base = dict(
        descriptive=Descriptive(
            "PubMed",
            "Every MEDLINE abstract, sliced by year, for biomedical retrieval."),
        plugin="pubmed",
        origin=Origin(
            spec=PUBMED_SPEC,
            form="the XML baseline and update files, because the API paginates "
                 "and the FTP files are checksummed",
            unit="container-member", clock="America/New_York",
            cutoff_rule="update-files", jurisdiction=None),
        toolchain=Toolchain(PLATFORM, None),
        parser=Parser("pubmed", 4),
        cleaning_version=1,
        protocol_version=1,
        layers=(Layer(Chunker("abstract", 1, {}), "default"),),
        slicing=PUBMED_SLICING,
        space=Space(
            encoder=Encoder(ENCODER, WEIGHTS, "MedCPT-Article-Encoder"),
            dim=768, dtype="f16", ctx=512, pooling="cls", normalise=True,
            query=Query(Encoder(Q_ENCODER, Q_WEIGHTS, ""), None),
            tolerance="0.95",
            calibration=Calibration(CAL_SET, CAL_VECS, 16, 12),
            envelope=ENVELOPE, epoch="0", evaluation=None),
        work=Work(40_000_000, "P1M"),
        modality="text",
        erasure="retractable",
        validation=Validation(PUBMED_CHECKS, PUBMED_SAMPLE, ()),
        license=License("red", TERMS),
        privacy="public",
        update=Update("monthly", 1, 7),
        publication="open", certifier=None, embedding="open",
        succession="frozen", content=None,
        continues=None, continues_through=None, origin_reachable_until=None,
        derived_from=None, space_from=None, replaces=None,
        facets=(), credits=(), profile=None,
    )
    base.update(over)
    return Codex(**base)


def a_credit(archive: str = "wikipedia-bge-m3") -> Credit:
    return Credit(archive=archive, producer="Cohere", key=ARCHIVE_KEY,
                  license="CC-BY-SA-4.0",
                  attribution="Wikipedia embeddings, BGE-M3, 2025-11",
                  coverage="all")


def a_facet(field_: str = "subjects") -> Facet:
    return Facet(field_, "tree", separator="/", authority={"name": "MeSH"})


@pytest.fixture
def codex() -> Codex:
    return pubmed()

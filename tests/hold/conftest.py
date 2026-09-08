"""A whole, valid Codex to build the hold tests on.

It is 09 §9.3's worked example with the elided digests filled in: the same
fields, the same shapes, the same values wherever the document gives one.
Nothing here is convenience — a fixture that omitted a field would let a
test pass against a Codex the specification refuses.
"""
import pytest
from nacl import signing

from app.hold import calibration as _cal
from app.hold.codex import (Calibration, Chunker, Codex, Credit, Descriptive,
                            Encoder, Facet, Layer, License, Origin, Parser,
                            Query, Space, Toolchain, Update, Validation, Work)

PLATFORM = "sha256:" + "1f" * 32
ENCODER = "sha256:" + "8c" * 32
WEIGHTS = "sha256:" + "e2" * 32
Q_ENCODER = "sha256:" + "0a" * 32
Q_WEIGHTS = "sha256:" + "b7" * 32
# The REAL set this platform ships: a Codex naming a set the platform does
# not carry cannot be calibrated, so the fixture names the real one and
# stays honest if the set is ever re-frozen.
CAL_SET, CAL_TEXTS = _cal.load("text")
DIM = 768


def fake_embed(texts, *, dim: int = DIM, tag: bytes = b""):
    """A deterministic stand-in for the encoder `hold collection create`
    calls (06 §6.6). Every vector is a function of the text, so the same
    text embeds to the same place and two different texts do not — which is
    all the fixtures need, and all an offline test can honestly have.

    It exists because `create` genuinely needs an encoder: the reference
    vectors' hashes are Codex fields, so there is no offline path that
    produces a valid Codex. A test that hard-coded those hashes would be
    asserting numbers no computation ever produced.
    """
    import hashlib
    import numpy as np
    out = np.empty((len(texts), dim), dtype="float32")
    for i, t in enumerate(texts):
        seed = hashlib.blake2b(tag + t.encode("utf-8"), digest_size=8).digest()
        v = np.random.default_rng(int.from_bytes(seed, "big")) \
              .standard_normal(dim).astype("float32")
        out[i] = v / np.linalg.norm(v)
    return out.tolist()


# Computed, never invented: these two ARE the hashes of what `fake_embed`
# produces over the real 512 texts, so a fixture Codex is one `create`
# could have written.
CAL_VECS, ENVELOPE = _cal.build_member(
    CAL_TEXTS, fake_embed, normalise=True, dim=DIM)[2:]
TERMS = "sha256:" + "b9" * 32

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
    return Credit(archive=archive, producer="Cohere",
                  license="CC-BY-SA-4.0",
                  attribution="Wikipedia embeddings, BGE-M3, 2025-11",
                  coverage="all")


def a_facet(field_: str = "subjects") -> Facet:
    return Facet(field_, "tree", separator="/", authority={"name": "MeSH"})


@pytest.fixture
def codex() -> Codex:
    return pubmed()


def a_root(root, codex, **kw):
    """`create`, plus the calibration member `create_from_recipe` writes.

    The low-level `create` takes a finished Codex and so has no vectors to
    write, but every derivation INHERITS the member (09 §9.12, and it is in
    every snapshot's `files`, §9.4) — so a mother without one is a
    collection that could not exist, and a test built on one would prove
    nothing about `derive`.
    """
    from app.hold import calibration as _cal, collection as _col
    out = _col.create(root, codex, **kw)
    v, e, _, _ = _cal.build_member(CAL_TEXTS, fake_embed, normalise=True,
                                   dim=codex.space.dim)
    d = _col.drag_of(root, _col.name_of(root, out.collection_id))
    _col.write_calibration(d, codex, v, e, key=KEY, now=NOW)
    return out


#: The creator's key and clock. 09 §9.12 makes the member's `key` header
#: required and §9.4 lists its signature without allowing null, so there is
#: no such thing as an unsigned collection; and nothing under `hold` reads
#: the clock, so a caller always supplies the time.
KEY = signing.SigningKey(b"\x05" * 32)
NOW = "2026-09-08T00:00:00Z"

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
                            Encoder, Engine, Facet, Layer, License, Origin,
                            Parser, Query, Space, Toolchain, Update, Validation,
                            Work)

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
#: An asymmetric space pins its QUERY encoder's reference vectors too (09
#: §9.12, from Codex version 3): the same texts through another function.
CAL_QVECS = _cal.build_member(
    CAL_TEXTS, lambda texts: fake_embed(texts, tag=b"query"), normalise=True, dim=DIM)[2]

#: 09 §9.3's worked example, the engine: the run the reference vectors came
#: from, written out whole. `ENCODER` is its image; the query side is "the
#: same, its own weights".
ENGINE = Engine(
    name="llama.cpp", build="b10630", variant="sycl", image=ENCODER,
    args=("llama-server", "-m", "${WEIGHTS}", "--embedding", "--pooling", "cls",
          "-c", "512", "-ngl", "99", "--port", "${PORT}"),
    env={}, device="Intel(R) Arc(TM) Pro B70 Graphics")
#: The REAL hash of the terms text the pubmed plugin ships, not a made-up
#: digest. `collection.write_terms` copies that file into the drag as
#: `LICENSE-TERMS` and refuses when its bytes do not hash to what the
#: Codex pins (09 §9.13, "byte-exact and never converted") — so a fixture
#: with an invented digest describes a collection that cannot be created,
#: and every red-class fixture below would be testing an impossibility.
TERMS = "sha256:" + __import__("hashlib").sha256(
    (__import__("pathlib").Path(__file__).resolve().parents[2]
     / "app" / "plugins" / "pubmed" / "TERMS.txt").read_bytes()).hexdigest()

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
            encoder=Encoder(None, WEIGHTS, "MedCPT-Article-Encoder", ENGINE),
            dim=768, dtype="f16", ctx=512, pooling="cls", normalise=True,
            query=Query(Encoder(None, Q_WEIGHTS, "", ENGINE), None),
            tolerance="0.95",
            calibration=Calibration(CAL_SET, CAL_VECS, 16, 12, CAL_QVECS),
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
        facets=(), credits=BASE_CREDITS, profile=None,
    )
    base.update(over)
    # The community credit is DERIVED from `embedding` (02 §2.7), so a test
    # that overrides `embedding` gets credits that agree with it without
    # having to know that the two are connected — which is the whole claim
    # the rule makes. A test that passes `credits` explicitly is taken at
    # its word, because some of them are testing exactly that disagreement.
    if "credits" not in over:
        base["credits"] = derive_credits(base["credits"], base["embedding"])
    return Codex(**base)


def replaced(codex: Codex, **over) -> Codex:
    """`dataclasses.replace`, keeping `credits` consistent with `embedding`.

    A test that changes `embedding` is almost never testing the credits
    rule — it is testing certification, or the open-collection rule — but
    the two fields ARE connected (02 §2.7), so a bare `replace` produces a
    Codex the specification refuses for a reason the test does not care
    about. Pass `credits` explicitly to opt out and test the disagreement.
    """
    from dataclasses import replace as _r
    out = _r(codex, **over)
    if "embedding" in over and "credits" not in over:
        out = _r(out, credits=derive_credits(out.credits, out.embedding))
    return out


def derive_credits(credits, embedding: str) -> tuple[Credit, ...]:
    """`credits` with its community entry present or absent as `embedding`
    says, in 09 §9.3's order."""
    from app.hold.codex import credit_order
    out = [c for c in credits if c.kind != "community"]
    if embedding in ("open", "endorsed"):
        out.append(a_community_credit())
    return tuple(sorted(out, key=credit_order))


def a_corpus_credit() -> Credit:
    """02 §2.7's corpus entry. Every collection has exactly one, so a
    fixture without it is a Codex the specification refuses — which is the
    whole reason this file builds a WHOLE Codex rather than a convenient
    one."""
    return Credit(kind="corpus", archive=None,
                  producer="the U.S. National Library of Medicine",
                  license="nlm-terms",
                  attribution=("PubMed/MEDLINE, U.S. National Library of "
                               "Medicine, National Institutes of Health"),
                  coverage="all")


def a_community_credit() -> Credit:
    """02 §2.7's community entry: present exactly when `embedding` is
    `"open"` or `"endorsed"`, which the fixture's is."""
    return Credit(kind="community", archive=None,
                  producer="the Project Louise community", license=None,
                  attribution=("Vectors contributed by the Project Louise "
                               "community"),
                  coverage="all")


#: What every fixture Codex carries before a test adds an archive: the two
#: kinds that are not a creator's to choose. Sorted as 09 §9.3 requires —
#: "community" before "corpus".
BASE_CREDITS = (a_community_credit(), a_corpus_credit())


def with_credits(*archives: Credit) -> tuple[Credit, ...]:
    """`BASE_CREDITS` plus archive entries, in 09 §9.3's order."""
    from app.hold.codex import credit_order
    return tuple(sorted(BASE_CREDITS + archives, key=credit_order))


def a_credit(archive: str = "wikipedia-bge-m3") -> Credit:
    return Credit(kind="archive", archive=archive, producer="Cohere",
                  license="CC-BY-SA-4.0",
                  attribution="Wikipedia embeddings, BGE-M3, 2025-11",
                  coverage="all")


def a_facet(field_: str = "subjects") -> Facet:
    return Facet(field_, "tree", separator="/", authority={"name": "MeSH"})


def pubmed_v2(**over) -> Codex:
    """The same collection as it was written BEFORE the engine amendment:
    Codex version 2, the encoder named by its image alone, that image inside
    `space_id`, no `query_vectors`. Such a Codex is read for ever and never
    rewritten — `version` is inside `collection_id` (09 §9.3)."""
    v3 = pubmed()
    space = Space(
        encoder=Encoder(ENCODER, WEIGHTS, "MedCPT-Article-Encoder"),
        dim=v3.space.dim, dtype=v3.space.dtype, ctx=v3.space.ctx,
        pooling=v3.space.pooling, normalise=v3.space.normalise,
        query=Query(Encoder(Q_ENCODER, Q_WEIGHTS, ""), None),
        tolerance=v3.space.tolerance,
        calibration=Calibration(CAL_SET, CAL_VECS, 16, 12),
        envelope=ENVELOPE, epoch="0", evaluation=None)
    return pubmed(**{"space": space, "version": 2, **over})


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
    q = None
    if codex.space.calibration.query_vectors is not None:
        # an asymmetric space's member carries the query side's reference too
        q, _ = _cal.build_query_member(CAL_TEXTS, fake_query_embed,
                                       instruction=codex.space.query.instruction,
                                       normalise=True, dim=codex.space.dim)
    _col.write_calibration(d, codex, v, e, key=KEY, now=NOW, query_vectors=q)
    return out


#: The creator's key and clock. 09 §9.12 makes the member's `key` header
#: required and §9.4 lists its signature without allowing null, so there is
#: no such thing as an unsigned collection; and nothing under `hold` reads
#: the clock, so a caller always supplies the time.
KEY = signing.SigningKey(b"\x05" * 32)
NOW = "2026-09-08T00:00:00Z"


def fake_query_embed(texts, *, dim: int = DIM):
    """The stand-in for a bi-encoder's QUERY side (MedCPT's query encoder):
    another function over the same texts. `CAL_QVECS` above is its hash over
    the real 512, so a fixture Codex pins what `create` would have computed."""
    return fake_embed(texts, dim=dim, tag=b"query")


def a_stated_run(codex=None):
    """The run RAGE would state for the engine of 09 §9.3's worked example
    (09 §9.12): its id, from `Louise-Run`, and what it stands for, from
    `GET /louise/runs/<id>`. A version-2 calibration statement names it;
    without one a pass cannot be kept (03 §3.3)."""
    from app.hold.codex import canonical_json, digest
    from app.hold.embedder import StatedRun
    engine = (codex.space.encoder.engine if codex is not None
              and codex.space.encoder.engine is not None else ENGINE).to_dict()
    weights = codex.space.encoder.weights if codex is not None else WEIGHTS
    driver = "kernel 6.18.0-production; xe 9F2C1A; libze-intel-gpu1 26.05.1"
    run = {"driver": driver, "engine": engine, "weights": weights}
    return StatedRun(run_id=digest(canonical_json(run)), engine=engine,
                     weights=weights, driver=driver)

"""Per-source AcquisitionSpecs. Real endpoints match architecture-v2.md
§5.1's "Per-source current best forms" table; demo profiles are small
enough to complete in well under 2 minutes even against a real, reachable
mirror. Tests point an equivalent HTTPS spec at a local mock server instead
(see tests/acquire/) — never at these real endpoints (non-goal: real origin
traffic in tests).
"""
from pathlib import Path

from ..plugins.base import LicenseClass
from .spec import (AcquisitionSpec, ChecksumSource, DeltaSpec, Endpoint, Filter,
                   RemoteFile, Transport)

GUTENBERG_SPEC = AcquisitionSpec(
    source_id="gutenberg",
    transport=Transport.RSYNC,
    endpoints=(
        Endpoint(transport=Transport.RSYNC,
                 url="rsync.mirrorservice.org::gutenberg.org/cache/epub"),
        Endpoint(transport=Transport.RSYNC, url="aleph.gutenberg.org::gutenberg-epub"),
    ),
    layout="one-dir-per-doc",
    filters=("*.txt*", "*.rdf", "!*mbt-*", "!*.mobi", "!*.kf8*"),
    checksums=ChecksumSource(kind="none"),   # PG publishes no sidecar; blake2 always applies
    delta=DeltaSpec(mode="full-resync"),
    profiles={
        "demo": Filter(include=("1/**", "2/**", "3/**"), max_files=3, max_bytes=5_000_000),
        "starter": Filter(max_files=500, max_bytes=2_000_000_000),
        "recent": Filter(max_files=5000),
        "full": Filter(),
    },
    estimated_bytes={
        "demo": 2_000_000, "starter": 2_000_000_000,
        "recent": 20_000_000_000, "full": 80_000_000_000,
    },
    license_class=LicenseClass.GREEN,
)

PUBMED_SPEC = AcquisitionSpec(
    source_id="pubmed",
    transport=Transport.FTP,
    endpoints=(
        Endpoint(transport=Transport.FTP, url="ftp.ncbi.nlm.nih.gov/pubmed/baseline"),
    ),
    layout="container-files",
    filters=("*.xml.gz", "*.xml.gz.md5"),
    checksums=ChecksumSource(kind="sidecar", sidecar_suffix=".md5", sidecar_algo="md5"),
    delta=DeltaSpec(mode="updatefiles-dir", updatefiles_subdir="updatefiles"),
    profiles={
        # one baseline container + its .md5 sidecar; real baseline files run
        # 9-30 MB (the original 10 MB cap couldn't admit a single one)
        "demo": Filter(max_files=2, max_bytes=40_000_000),
        "starter": Filter(max_files=50),
        "recent": Filter(max_files=200),
        "full": Filter(),
    },
    estimated_bytes={
        "demo": 40_000_000, "starter": 500_000_000,
        "recent": 5_000_000_000, "full": 150_000_000_000,
    },
    license_class=LicenseClass.YELLOW,   # publisher copyright on text; raw is pointer-only (§8)
    terms_text_path=Path(__file__).parent.parent / "plugins" / "pubmed" / "TERMS.txt",
)

def _wiki_files(wiki: str) -> tuple[RemoteFile, ...]:
    """Stable -latest- names per wiki: the multistream dump + its index are
    the only two files the plugin needs. rel_path is nested (it is also the
    corpus-relative destination), so multiple wikis coexist in one corpus."""
    base = f"{wiki}/latest/{wiki}-latest-pages-articles-multistream"
    return (RemoteFile(rel_path=f"{base}.xml.bz2"),
            RemoteFile(rel_path=f"{base}-index.txt.bz2"))


WIKIPEDIA_SPEC = AcquisitionSpec(
    source_id="wikipedia",
    transport=Transport.HTTPS,
    endpoints=(
        Endpoint(transport=Transport.HTTPS, url="https://dumps.wikimedia.org",
                 max_conns=2,   # dumps.wikimedia.org etiquette: max 2 connections
                 static_files=_wiki_files("simplewiki") + _wiki_files("enwiki")),
    ),
    layout="container-files",
    filters=("*multistream.xml.bz2", "*multistream-index.txt.bz2"),
    checksums=ChecksumSource(kind="none"),
    delta=DeltaSpec(mode="full-resync"),
    profiles={
        # profiles select WHICH wiki: Simple English is a real, useful,
        # self-contained corpus at 1/70th of enwiki's size
        "demo": Filter(include=("simplewiki/*",)),
        "starter": Filter(include=("simplewiki/*",)),
        "full": Filter(),                       # simplewiki + enwiki
    },
    estimated_bytes={
        "demo": 350_000_000, "starter": 350_000_000, "full": 23_000_000_000,
    },
    license_class=LicenseClass.YELLOW,          # CC BY-SA (§8)
    terms_text_path=Path(__file__).parent.parent / "plugins" / "wikipedia" / "TERMS.txt",
)


# UCSC goldenPath reference assemblies, smallest first: (db, organism,
# bytes of <db>/bigZips/<db>.fa.gz).
#
# ONE table feeding three structures (static_files, profiles,
# estimated_bytes) because three hand-maintained parallel lists is three
# chances to add an assembly to two of them. The sizes are the real
# Content-Length of each file, probed against hgdownload on 2026-08-19 —
# the budget planner shows them to an operator BEFORE a fetch starts, and
# a guessed number there is a lie someone plans a night's download around.
#
# Adding one: probe the URL first (a 404 here becomes a fetch error much
# later, after the collection exists). dp6 was a candidate and is NOT
# here — UCSC serves no dp6/bigZips/dp6.fa.gz.
_UCSC_ASSEMBLIES: tuple[tuple[str, str, int], ...] = (
    ("sacCer3",  "yeast (S. cerevisiae)",      3_820_548),
    ("ce11",     "roundworm (C. elegans)",    31_816_111),
    ("dm6",      "fruit fly (D. melanogaster)", 45_153_922),
    ("anoGam3",  "mosquito (A. gambiae)",     81_983_405),
    ("galGal6",  "chicken",                  347_959_049),
    ("xenTro10", "frog (X. tropicalis)",     449_164_532),
    ("danRer11", "zebrafish",                543_241_465),
    ("canFam6",  "dog",                      759_784_137),
    ("susScr11", "pig",                      811_422_721),
    ("felCat9",  "cat",                      812_014_328),
    ("equCab3",  "horse",                    829_063_609),
    ("oryCun2",  "rabbit",                   854_412_134),
    ("rn7",      "rat (mRatBN7.2)",          859_160_243),
    ("mm10",     "mouse (GRCm38)",           870_141_360),
    ("mm39",     "mouse (GRCm39)",           870_543_764),
    ("bosTau9",  "cow",                      882_512_073),
    ("rn6",      "rat (Rnor_6.0)",           896_422_133),
    ("calJac4",  "marmoset",                 931_125_471),
    ("rheMac10", "rhesus macaque",           941_070_922),
    ("gorGor6",  "gorilla",                  947_696_407),
    ("hg19",     "human (GRCh37)",           948_731_419),
    ("panTro6",  "chimpanzee",               963_190_563),
    ("ponAbe3",  "orangutan",                973_229_526),
    ("hg38",     "human (GRCh38)",           983_659_424),
    ("monDom5",  "opossum",                1_152_791_307),
)

GENOME_SPEC = AcquisitionSpec(
    source_id="genome",
    transport=Transport.HTTPS,
    endpoints=(
        Endpoint(transport=Transport.HTTPS,
                 url="https://hgdownload.soe.ucsc.edu/goldenPath",
                 max_conns=2,
                 # Declaring every assembly costs nothing per collection:
                 # _apply_filters() intersects this list with the profile's
                 # include patterns, so a dm6 collection still lists exactly
                 # one file.
                 static_files=tuple(
                     RemoteFile(rel_path=f"{db}/bigZips/{db}.fa.gz")
                     for db, _, _ in _UCSC_ASSEMBLIES)),
    ),
    layout="container-files",
    filters=("*.fa.gz",),
    checksums=ChecksumSource(kind="none"),
    delta=DeltaSpec(mode="full-resync"),     # assemblies are immutable; a new
                                              # assembly is a NEW collection (§9)
    profiles={
        # ONE ASSEMBLY PER PROFILE, and profiles never stack — §9's "one
        # collection per assembly" is not a style preference: native_id is
        # the record ordinal WITHIN a FASTA, so a second assembly in the
        # same corpus mints the same document ids and overwrites the first.
        # §9's validation ladder is still here, as separate collections:
        # yeast then fly prove the plugin before anyone spends human-scale
        # hours on GRCh38.
        "demo": Filter(include=("sacCer3/*",)),      # alias for sacCer3
        **{db: Filter(include=(f"{db}/*",)) for db, _, _ in _UCSC_ASSEMBLIES},
    },
    profile_mode="exclusive",                # never combine two assemblies
    estimated_bytes={
        "demo": 3_820_548,
        **{db: size for db, _, size in _UCSC_ASSEMBLIES},
    },
    profile_labels={
        "demo": "yeast (S. cerevisiae) — smallest, proves the pipeline",
        **{db: organism for db, organism, _ in _UCSC_ASSEMBLIES},
    },
    license_class=LicenseClass.GREEN,        # consortium reference data
)


# ~4.4 KB per row (633 GB / 144M rows, dataset card); per-language row
# counts from the card. Profiles select a LANGUAGE — each is a complete,
# self-contained corpus, and the pre-embedded vectors make even "en" a
# disk-bound import rather than a GPU-month.
_EMBEDDED_LANG_BYTES = {
    "tr": 9_200_000_000, "fa": 11_500_000_000, "zh": 13_000_000_000,
    "pt": 26_000_000_000, "ja": 35_000_000_000, "it": 44_000_000_000,
    "es": 57_000_000_000, "ru": 62_000_000_000, "fr": 79_000_000_000,
    "de": 88_000_000_000, "en": 207_000_000_000,
}

EMBEDDED_SPEC = AcquisitionSpec(
    source_id="embedded",
    transport=Transport.HF_HUB,
    endpoints=(
        Endpoint(transport=Transport.HF_HUB,
                 url="datasets/Upstash/wikipedia-2024-06-bge-m3",
                 max_conns=2),   # no static_files: the transport lists the
                                 # repo tree dynamically (~1,450 shards)
    ),
    layout="container-files",
    filters=("data/*/*.parquet",),
    checksums=ChecksumSource(kind="none"),   # frozen dataset; blake2 applies
    delta=DeltaSpec(mode="full-resync"),
    profiles={
        # demo = the smallest complete language (Turkish, ~2M chunks)
        "demo": Filter(include=("data/tr/*",)),
        **{lang: Filter(include=(f"data/{lang}/*",))
           for lang in _EMBEDDED_LANG_BYTES},
        "all": Filter(),
    },
    # languages COMBINE: each is an independent slice of the same repo, and
    # the plugin encodes the language into native_id, so "tr,it,en" is a
    # perfectly coherent corpus. Growing adds a language rather than
    # replacing one.
    profile_mode="additive",
    estimated_bytes={
        "demo": _EMBEDDED_LANG_BYTES["tr"],
        **_EMBEDDED_LANG_BYTES,
        "all": 633_000_000_000,
    },
    license_class=LicenseClass.YELLOW,       # content is Wikipedia CC BY-SA
    terms_text_path=Path(__file__).parent.parent / "plugins" / "embedded" / "TERMS.txt",
)


ACQUIRE_REGISTRY: dict[str, AcquisitionSpec] = {
    "embedded": EMBEDDED_SPEC,
    "genome": GENOME_SPEC,
    "gutenberg": GUTENBERG_SPEC,
    "pubmed": PUBMED_SPEC,
    "wikipedia": WIKIPEDIA_SPEC,
}

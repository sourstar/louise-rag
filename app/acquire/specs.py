"""Per-source AcquisitionSpecs. Real endpoints match architecture-v2.md
§5.1's "Per-source current best forms" table; demo profiles are small
enough to complete in well under 2 minutes even against a real, reachable
mirror. Tests point an equivalent HTTPS spec at a local mock server instead
(see tests/acquire/) — never at these real endpoints (non-goal: real origin
traffic in tests).
"""
import re
from pathlib import Path

from ..plugins.base import LicenseClass
from .spec import (AcquisitionSpec, ChecksumSource, DeltaSpec, Endpoint, Filter,
                   RemoteFile, Transport)

GUTENBERG_SPEC = AcquisitionSpec(
    source_id="gutenberg",
    transport=Transport.RSYNC,
    # ADMISSION RULE: a mirror joins this list only if it serves EXACTLY the
    # official cache/epub layout (probe_file enforces it per benchmark, and
    # every candidate below was probed live from the reference node,
    # 2026-08-24):
    #   ftp.ibiblio.org        — structure OK, distinct host (152.19.134.42),
    #                            fastest measured (1.74 s vs 2.2 s on the probe);
    #                            mirrors-first etiquette spares the master
    #   aleph.gutenberg.org    — the PG master (69.55.231.8);
    #                            gutenberg.pglaf.org / aleph.pglaf.org are the
    #                            SAME host under other names — fake diversity,
    #                            not listed
    #   rsync.mirrorservice.org — REMOVED: its gutenberg.org module no longer
    #                            carries cache/epub (legacy tree only, rc 23) —
    #                            it was this spec's first endpoint, dead weight
    #   mirrors.xmission.com / csclub.uwaterloo / dotsrc — no gutenberg-epub
    #                            module (legacy tree layouts): excluded
    endpoints=(
        Endpoint(transport=Transport.RSYNC, url="ftp.ibiblio.org::gutenberg-epub"),
        Endpoint(transport=Transport.RSYNC, url="aleph.gutenberg.org::gutenberg-epub"),
    ),
    layout="one-dir-per-doc",
    filters=("*.txt*", "*.rdf", "!*mbt-*", "!*.mobi", "!*.kf8*"),
    checksums=ChecksumSource(kind="none"),   # PG publishes no sidecar; blake2 always applies
    delta=DeltaSpec(mode="full-resync"),
    # small, stable, and part of the official layout — the structure probe
    # and benchmark unit (app/acquire/mirrors.py)
    probe_file="1/pg1.rdf",
    # optional extra formats, LOCAL-ONLY (spec.extras policy): someone may
    # want the epubs beside the text — they never enter the p2p exchange
    extras={"epub": Filter(include=("*.epub",), exclude=("*images*",))},
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

WIKIS = ("simplewiki", "enwiki")
_SNAPSHOT_RE = re.compile(r"^\d{8}$")


def _wiki_files(wiki: str, snapshot: str = "latest") -> tuple[RemoteFile, ...]:
    """The multistream dump + its index, the only two files the plugin needs.

    `snapshot` is a dump date ("20260801") or the literal "latest".

    A DATED path is immutable: the same URL serves the same bytes forever,
    which is what makes the corpus torrentable, cacheable, adoptable, and
    comparable with a peer's. "latest" is the opposite — the same URL serves
    different bytes every few weeks, so a collection on it cannot say which
    dump it holds and every refresh is a full re-download. It remains the
    default only because changing it under an existing collection would
    orphan a 27 GB corpus; see Collection.snapshot.

    rel_path is nested (it is also the corpus-relative destination), so
    multiple wikis — and multiple snapshots — coexist in one corpus.
    """
    base = f"{wiki}/{snapshot}/{wiki}-{snapshot}-pages-articles-multistream"
    return (RemoteFile(rel_path=f"{base}.xml.bz2"),
            RemoteFile(rel_path=f"{base}-index.txt.bz2"))


def _wikipedia_snapshot_files(snapshot: str) -> tuple[RemoteFile, ...]:
    files: tuple[RemoteFile, ...] = ()
    for wiki in WIKIS:
        files += _wiki_files(wiki, snapshot)
    return files


def wikipedia_snapshots(wiki: str = "enwiki", timeout: float = 30.0) -> tuple[str, ...]:
    """Dump dates the origin currently offers, NEWEST FIRST.

    Wikimedia keeps only the last few dumps online, so this is also the
    honest answer to "can I still get the snapshot I am pinned to?" — a date
    that has fallen off the end is gone, and a collection pinned to it can
    no longer be rebuilt from the origin (which is exactly the case a peer
    swarm is for).

    Parsed from the directory index because there is no listing API. Only
    8-digit names are accepted, so no amount of unexpected HTML can turn
    into a fetch URL.
    """
    import httpx

    url = f"https://dumps.wikimedia.org/{wiki}/"
    headers = {"User-Agent": _WIKI_USER_AGENT}
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
    found = {m for m in re.findall(r'href="(\d{8})/"', response.text)
             if _SNAPSHOT_RE.match(m)}
    return tuple(sorted(found, reverse=True))


# Wikimedia returns 403 to generic library user agents (learned the hard
# way against the dumps host); the listing endpoint is under the same
# policy as the files.
_WIKI_USER_AGENT = ("louise-rag/0.1 (+https://github.com/sourstar/louise-rag) "
                    "httpx")


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
    # Dated dumps, once a collection pins one: each is a complete, immutable
    # snapshot, which is what a delta chain and a torrent both need.
    delta=DeltaSpec(mode="dated-dumps"),
    snapshot_files=_wikipedia_snapshot_files,
    snapshot_index=wikipedia_snapshots,
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
                 #
                 # `size` is declared, not left None, and it is load-bearing
                 # twice over. It is what makes `bytes_total` honest in the
                 # plan — an undeclared size reports a 983 MB assembly as
                 # "0 bytes total" — and it is the whole basis on which
                 # is_adoptable() can tell a complete local file from a
                 # truncated one. HTTPS lists exactly what the spec declares
                 # (there is no directory protocol to ask), so a size absent
                 # here is a size absent everywhere downstream.
                 static_files=tuple(
                     RemoteFile(rel_path=f"{db}/bigZips/{db}.fa.gz", size=size)
                     for db, _, size in _UCSC_ASSEMBLIES)),
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

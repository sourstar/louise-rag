"""Boxed from app/acquire/specs.py with the `genome` plugin (operator,
2026-09-14): kept for reference until converted to the final format or
dropped. Nothing imports it."""
from app.acquire.spec import (AcquisitionSpec, ChecksumSource, DeltaSpec, Endpoint,
                              Filter, RemoteFile, Transport)
from app.plugins.base import LicenseClass


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

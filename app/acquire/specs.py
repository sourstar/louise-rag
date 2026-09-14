"""Per-source AcquisitionSpecs. Real endpoints match architecture-v2.md
§5.1's "Per-source current best forms" table; demo profiles are small
enough to complete in well under 2 minutes even against a real, reachable
mirror. Tests point an equivalent HTTPS spec at a local mock server instead
(see tests/acquire/) — never at these real endpoints (non-goal: real origin
traffic in tests).
"""
import re
from pathlib import Path
from urllib.parse import quote, urlencode

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
    # THE EPUB IS NOW PART OF THE CORPUS, not an optional extra beside it
    # (operator, 2026-09-11). The plates are only in the epub, and the
    # collection reads the epub where it exists — so a sync that fetched
    # only `*.txt*` left 38% of the books, measured on the mirror, with no
    # illustrated edition to read. The origin publishes one: all four
    # txt-only books sampled had `pg<id>-images.epub` upstream.
    #
    # `-images` is the edition that carries plates, and the plain `.epub`
    # is kept as the fallback for the handful published without one. The
    # `.txt` and `.rdf` stay: the reading rule is epub, then txt, then rdf,
    # so the later forms are not leftovers — they are what a book without
    # an epub is read from.
    #
    # `.mobi`/`.kf8` remain excluded: they are re-renderings of the same
    # text in formats nothing here reads.
    filters=("*.txt*", "*.rdf", "*.epub", "!*mbt-*", "!*.mobi", "!*.kf8*"),
    checksums=ChecksumSource(kind="none"),   # PG publishes no sidecar; blake2 always applies
    delta=DeltaSpec(mode="full-resync"),
    # small, stable, and part of the official layout — the structure probe
    # and benchmark unit (app/acquire/mirrors.py)
    probe_file="1/pg1.rdf",
    # No extras. `epub` was one — `include=("*.epub",),
    # exclude=("*images*",)` — which excluded precisely the illustrated
    # editions this collection now exists to read. That was right while the
    # epub was a local convenience beside the text and wrong the moment it
    # became the form the corpus is parsed from, so it moves into `filters`
    # above rather than staying an opt-in that contradicts them.
    extras={},
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

#: wikivec's languages — the collection holds them all (operator,
#: 2026-09-14) — one dump each. Bytes: the latest multistream dump plus its
#: index, as dumps.wikimedia.org answered a HEAD on 2026-09-14.
_WIKI_DUMP_BYTES: dict[str, int] = {
    "en": 27_081_871_176, "de": 8_313_373_480, "fr": 7_313_571_295,
    "ru": 6_251_105_573, "es": 5_430_508_793, "it": 4_480_650_780,
    "ja": 4_884_253_000, "pt": 2_879_266_237, "zh": 3_625_051_601,
    "fa": 1_590_148_993, "tr": 1_145_175_844,
}
WIKIS = tuple(f"{lang}wiki" for lang in _WIKI_DUMP_BYTES)
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
                 static_files=sum((_wiki_files(w) for w in WIKIS), ())),
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
        # A LANGUAGE per profile, and they combine: the native id carries
        # the language (wikipedia parser 4), so any set of wikis coexists —
        # the pre-embedded corpus's own shape. simplewiki is not among them:
        # it is not one of wikivec's languages, and it used to pass as "en"
        # and share enwiki's page ids (the old collection held 19.4M
        # documents mixed that way).
        "demo": Filter(include=("trwiki/*",)),      # the smallest dump, 1.1 GB
        **{lang: Filter(include=(f"{lang}wiki/*",)) for lang in _WIKI_DUMP_BYTES},
        "all": Filter(),
    },
    profile_mode="additive",
    estimated_bytes={"demo": _WIKI_DUMP_BYTES["tr"], **_WIKI_DUMP_BYTES,
                     "all": sum(_WIKI_DUMP_BYTES.values())},
    license_class=LicenseClass.YELLOW,          # CC BY-SA (§8)
    terms_text_path=Path(__file__).parent.parent / "plugins" / "wikipedia" / "TERMS.txt",
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


# Normattiva's precompiled collections (dati.normattiva.it), as
# `collections/collection-predefinite` listed them on 2026-09-14: 60 rows,
# each collection in ORIGINALE, VIGENTE and MULTIVIGENTE. ONE form each:
# VIGENTE where offered, ORIGINALE for the four published in no other form
# — the 124,043 repealed acts among them (the plugin's docstring has the
# reasons). (name, form, acts, bytes): the bytes are the Content-Range total
# of a one-byte GET of each AKN zip that day, and seed `estimated_bytes`.
#
# The files declare NO size, deliberately. The origin rebuilds a zip in
# place (most `dataCreazione`s moved within the month), and a stale declared
# size would let a segmented download cut the rebuilt, longer zip at the old
# length and record it complete. An unknown size is honest; a wrong one is
# a corrupt corpus that passes. (Rebuilds are therefore not re-fetched by
# themselves: the listing carries no size, date or ETag to compare.)
_NORMATTIVA: tuple[tuple[str, str, int, int], ...] = (
    ("Codici", "V", 40, 10_531_559),
    ("Leggi costituzionali", "V", 50, 313_481),
    ("Testi Unici", "V", 257, 9_449_203),
    ("Atti di attuazione Regolamenti UE", "V", 39, 452_689),
    ("Atti di recepimento direttive UE", "V", 1_093, 24_165_741),
    ("DL decaduti", "V", 1_735, 3_822_367),
    ("DL e leggi di conversione", "V", 7_439, 59_689_174),
    ("DL proroghe", "V", 75, 1_393_173),
    ("DPCM", "V", 363, 5_636_141),
    ("DPR", "V", 47_761, 183_268_338),
    ("Decreti legislativi luogotenenziali", "V", 1_215, 3_608_774),
    ("Leggi contenenti deleghe", "V", 79, 2_461_235),
    ("Leggi di delegazione europea", "V", 32, 1_913_246),
    ("Leggi di ratifica", "V", 2_334, 31_242_548),
    ("Regi decreti", "V", 91_346, 355_397_370),
    ("Regi decreti legislativi", "V", 120, 492_939),
    ("Regolamenti di delegificazione", "V", 376, 6_957_845),
    ("Regolamenti governativi", "V", 1_943, 28_944_006),
    ("Regolamenti ministeriali", "V", 2_078, 32_557_719),
    ("Atti normativi abrogati (in originale)", "O", 124_043, 476_590_208),
    ("Decreti Legislativi", "O", 2_922, 72_637_059),
    ("Leggi delega e relativi provvedimenti delegati", "O", 1_153, 38_701_237),
    ("Leggi finanziarie e di bilancio", "O", 58, 8_616_271),
)


def _normattiva_rel(name: str, form: str) -> str:
    """"Atti normativi abrogati (in originale)", "O" →
    "akn/O/atti-normativi-abrogati-in-originale.zip"."""
    return f"akn/{form}/{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}.zip"


_NORMATTIVA_STARTER = ("Codici", "Leggi costituzionali", "Testi Unici")

NORMATTIVA_SPEC = AcquisitionSpec(
    source_id="normattiva",
    transport=Transport.HTTPS,
    endpoints=(
        # no auth and no rate limit documented (API PDF rev 10, probed
        # 2026-09-14); the download answers 302 to a tokenised file that
        # serves Range
        Endpoint(transport=Transport.HTTPS,
                 url="https://api.normattiva.it/t/normattiva.api/bff-opendata/"
                     "v1/api/v1/collections/download",
                 max_conns=2,          # a public service: etiquette
                 static_files=tuple(
                     RemoteFile(rel_path=_normattiva_rel(name, form),
                                remote="collection-preconfezionata?" + urlencode(
                                    {"nome": name, "formato": "AKN",
                                     "formatoRichiesta": form}, quote_via=quote))
                     for name, form, _, _ in _NORMATTIVA)),
    ),
    layout="container-files",
    filters=("*.zip",),
    checksums=ChecksumSource(kind="none"),     # none published (API PDF §6)
    delta=DeltaSpec(mode="full-resync"),
    profiles={
        "demo": Filter(include=(_normattiva_rel("Codici", "V"),)),
        "starter": Filter(include=tuple(_normattiva_rel(n, "V")
                                        for n in _NORMATTIVA_STARTER)),
        "full": Filter(),
    },
    estimated_bytes={
        "demo": 10_531_559,
        "starter": sum(b for n, f, _, b in _NORMATTIVA if n in _NORMATTIVA_STARTER),
        "full": sum(b for _, _, _, b in _NORMATTIVA),
    },
    profile_labels={
        "demo": "the 40 codes, in force (Codici)",
        "starter": "codes, constitutional laws and consolidated texts, in force",
        "full": "every precompiled collection: in force where offered, else original",
    },
    license_class=LicenseClass.YELLOW,          # CC BY 4.0, after an NC window (TERMS.txt)
    terms_text_path=Path(__file__).parent.parent / "plugins" / "normattiva" / "TERMS.txt",
)


ACQUIRE_REGISTRY: dict[str, AcquisitionSpec] = {
    "embedded": EMBEDDED_SPEC,
    "gutenberg": GUTENBERG_SPEC,
    "normattiva": NORMATTIVA_SPEC,
    "pubmed": PUBMED_SPEC,
    "wikipedia": WIKIPEDIA_SPEC,
}

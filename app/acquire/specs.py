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
#: each run's `page_props` table (wikipedia parser 5: the QIDs, from the
#: source), as dumps.wikimedia.org answered a HEAD for 20260801 on 2026-10-18
_WIKI_PROPS_BYTES: dict[str, int] = {
    "en": 458_205_292, "de": 102_496_830, "fr": 103_768_704, "ru": 62_653_053,
    "es": 60_733_218, "it": 61_981_816, "ja": 59_626_394, "pt": 41_806_943,
    "zh": 53_185_451, "fa": 38_020_402, "tr": 22_010_094,
}
_SNAPSHOT_RE = re.compile(r"^\d{8}$")


def _wiki_files(wiki: str, snapshot: str = "latest") -> tuple[RemoteFile, ...]:
    """The multistream dump + its index, and the run's `page_props` table:
    the three files the plugin reads (parser 5 takes each page's Wikidata item
    and properties from the same run as its text).

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
            RemoteFile(rel_path=f"{base}-index.txt.bz2"),
            RemoteFile(rel_path=f"{wiki}/{snapshot}/{wiki}-{snapshot}-page_props.sql.gz"))


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

    from . import budget
    url = f"https://dumps.wikimedia.org/{wiki}/"
    headers = {"User-Agent": _WIKI_USER_AGENT}
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(url, headers=headers)
        budget.acquire_bytes(len(response.content))      # invariant 5: a listing moves bytes
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
    filters=("*multistream.xml.bz2", "*multistream-index.txt.bz2", "*-page_props.sql.gz"),
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
    # a language's dump holds every `lang=<code>/pages=…` slice of it: the
    # slices the origin delivers whole (07 §7.3 `records fetch --slice`)
    slice_profiles={f"lang={lang}": lang for lang in _WIKI_DUMP_BYTES},
    estimated_bytes={"demo": _WIKI_DUMP_BYTES["tr"] + _WIKI_PROPS_BYTES["tr"],
                     **{lang: n + _WIKI_PROPS_BYTES[lang] for lang, n in _WIKI_DUMP_BYTES.items()},
                     "all": sum(_WIKI_DUMP_BYTES.values()) + sum(_WIKI_PROPS_BYTES.values())},
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
    slice_profiles={f"lang={lang}": lang for lang in _EMBEDDED_LANG_BYTES},
    estimated_bytes={
        "demo": _EMBEDDED_LANG_BYTES["tr"],
        **_EMBEDDED_LANG_BYTES,
        "all": 633_000_000_000,
    },
    license_class=LicenseClass.YELLOW,       # content is Wikipedia CC BY-SA
    terms_text_path=Path(__file__).parent.parent / "plugins" / "embedded" / "TERMS.txt",
)


# Normattiva's precompiled collections (dati.normattiva.it): the MULTIVIGENTE
# zip of each of the 22 collections that serve one, and the ORIGINALE zip of
# the repealed acts, which the origin publishes in no other form (a
# repealed act has no consolidation to produce). One record per Expression
# (docs/notes/normattiva-multivigenza-review.md §3, operator 2026-10-12):
# the M zip holds every act's ORIGINALE and every VIGENZA, so no V or O
# duplicate is fetched beside it. (name, form, members, bytes), measured on
# 2026-10-12 from each zip's central directory and its Content-Range total;
# the listing (`collection-predefinite`) advertises M for only 15 of the 22
# and is not a statement of availability — availability is probed. The
# bytes seed `estimated_bytes` only.
#
# The files declare NO size, deliberately. The origin rebuilds every zip
# daily (its `dataCreazione` is today on nearly every row, so it is no
# change signal either), and a stale declared size would let a segmented
# download cut the rebuilt, longer zip at the old length and record it
# complete. An unknown size is honest; a wrong one is a corrupt corpus that
# passes. The engine re-fetches a zip whose size moved and adopts one whose
# size did not; the central-directory delta of the review's §3.10 replaces
# both with a per-member comparison.
_NORMATTIVA: tuple[tuple[str, str, int, int], ...] = (
    ("Leggi costituzionali", "M", 95, 910_258),
    ("Codici", "M", 3_321, 1_205_370_292),
    ("Testi Unici", "M", 5_344, 528_440_671),
    ("Atti di attuazione Regolamenti UE", "M", 68, 1_607_938),
    ("Atti di recepimento direttive UE", "M", 4_136, 234_775_427),
    ("DL decaduti", "M", 5_779, 28_771_802),
    ("DL e leggi di conversione", "M", 14_459, 53_845_923),
    ("DL proroghe", "M", 633, 30_132_098),
    ("DPCM", "M", 603, 12_907_463),
    ("DPR", "M", 57_640, 762_252_068),
    ("Decreti Legislativi", "M", 13_813, 1_274_638_021),
    ("Decreti legislativi luogotenenziali", "M", 3_276, 14_333_101),
    ("Leggi contenenti deleghe", "M", 277, 11_708_478),
    ("Leggi delega e relativi provvedimenti delegati", "M", 5_906, 619_041_562),
    ("Leggi di delegazione europea", "M", 160, 10_918_324),
    ("Leggi di ratifica", "M", 2_879, 41_806_800),
    ("Leggi finanziarie e di bilancio", "M", 3_027, 562_583_354),
    ("Regi decreti", "M", 201_929, 1_695_365_828),
    ("Regi decreti legislativi", "M", 295, 1_449_876),
    ("Regolamenti di delegificazione", "M", 911, 51_066_114),
    ("Regolamenti governativi", "M", 4_062, 133_899_158),
    ("Regolamenti ministeriali", "M", 2_836, 51_314_559),
    ("Atti normativi abrogati (in originale)", "O", 123_868, 476_602_139),
)


def _normattiva_rel(name: str, form: str) -> str:
    """"Atti normativi abrogati (in originale)", "O" →
    "akn/O/atti-normativi-abrogati-in-originale.zip"."""
    return f"akn/{form}/{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}.zip"


_NORMATTIVA_STARTER = ("Leggi costituzionali", "Codici", "Testi Unici")

#: THE ACTS OUTSIDE THE 23 COLLECTIONS (docs/notes/normattiva-full-corpus.md,
#: 2026-10-13): the pre-packaged collections are thematic and hold 85 % of
#: the state's acts since 1948 — no collection of ordinary laws exists. The
#: origin's asynchronous export (app/acquire/export_job.py) builds a zip
#: per request, ONE version per job: `O`, the originals, and `V`, the
#: versions in force on the day of the pass. One job per year and form,
#: under `akn/E/<form>/<year>.zip` — the directory letter is what the
#: plugin ranks a copy by (M before V before O), so a version the M zips
#: hold is kept from them. The `full` profile keeps to the 23 collections,
#: which is the live collection's scope; `archive` adds the exports.
EXPORT_FIRST_YEAR = 1861


def _export_years() -> range:
    import time as _time
    return range(EXPORT_FIRST_YEAR, _time.gmtime().tm_year + 1)


def _normattiva_exports() -> tuple[RemoteFile, ...]:
    from . import export_job
    return tuple(
        RemoteFile(rel_path=f"akn/E/{form}/{year}.zip",
                   remote=export_job.encode({"year": year, "form": form}))
        for year in _export_years() for form in ("O", "V"))

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
                     for name, form, _, _ in _NORMATTIVA) + _normattiva_exports()),
    ),
    layout="container-files",
    filters=("*.zip",),
    checksums=ChecksumSource(kind="none"),     # none published (API PDF §6)
    # the zips are rebuilt daily and their listing names no size or date:
    # each pass reads each zip's central directory by Range and fetches the
    # changed members (app/acquire/zipdelta.py; review §3.10, decision 5)
    delta=DeltaSpec(mode="zip-directory"),
    profiles={
        "demo": Filter(include=(_normattiva_rel("Leggi costituzionali", "M"),)),
        "starter": Filter(include=tuple(_normattiva_rel(n, "M")
                                        for n in _NORMATTIVA_STARTER)),
        # the 23 pre-packaged collections: the live collection's scope
        # (STATUS 2026-10-13, operator) — named explicitly now that the
        # exports exist, so its passes keep fetching exactly what they did
        "full": Filter(include=("akn/M/*", "akn/O/*")),
        # the 23 collections and the yearly exports of every act: the full
        # body of state law, for a new collection
        "archive": Filter(),
    },
    all_profile="full",
    estimated_bytes={
        "demo": 910_258,
        "starter": sum(b for n, f, _, b in _NORMATTIVA if n in _NORMATTIVA_STARTER),
        "full": sum(b for _, _, _, b in _NORMATTIVA),
    },
    profile_labels={
        "demo": "the constitutional laws, every version (Leggi costituzionali)",
        "starter": "constitutional laws, codes and consolidated texts, every version",
        "full": "every precompiled collection, every version; the repealed acts in original",
    },
    license_class=LicenseClass.YELLOW,          # CC BY 4.0, after an NC window (TERMS.txt)
    terms_text_path=Path(__file__).parent.parent / "plugins" / "normattiva" / "TERMS.txt",
)


# ---- Wikidata (docs/notes/wikidata-build-plan.md D1; app/plugins/wikidata) ----

#: the full entity dump of 20261012 as dumps.wikimedia.org listed it on
#: 2026-10-19 (München's probe): 103.4 GB bz2; the checksum files beside it
_WIKIDATA_DUMP_BYTES = 103_409_622_204
_WIKIDATA_CHECKSUM_BYTES = 401 + 449
_WIKIDATA_DIR = "wikidatawiki/entities"


def _wikidata_files(snapshot: str) -> tuple[RemoteFile, ...]:
    """The dated directory's full JSON dump and its two checksum files
    (`wikidata-<date>-md5sums.txt`, `-sha1sums.txt`: every file of the run
    with its digest — the acquisition verifies the dump against them). A
    dated path is immutable: the same URL serves the same bytes, which is
    what pins a snapshot (D1); `latest-all.json.bz2` is the opposite and is
    never fetched."""
    base = f"{_WIKIDATA_DIR}/{snapshot}/wikidata-{snapshot}"
    return (RemoteFile(rel_path=f"{base}-all.json.bz2"),
            RemoteFile(rel_path=f"{base}-md5sums.txt"),
            RemoteFile(rel_path=f"{base}-sha1sums.txt"))


def wikidata_snapshots(timeout: float = 30.0, *, probe: int = 6) -> tuple[str, ...]:
    """The dump dates that hold a FULL JSON dump, newest first. Not every
    dated directory does: 20261014 held lexemes and the truthy RDF, 20261016
    lexemes only (München, 2026-10-19), so the newest directory is not the
    newest dump — each of the newest `probe` directories is listed and kept
    only where `wikidata-<date>-all.json.bz2` is among its files."""
    import httpx

    from . import budget
    headers = {"User-Agent": _WIKI_USER_AGENT}
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(f"https://dumps.wikimedia.org/{_WIKIDATA_DIR}/", headers=headers)
        budget.acquire_bytes(len(r.content))             # invariant 5: a listing moves bytes
        r.raise_for_status()
        dates = sorted({m for m in re.findall(r'href="(\d{8})/"', r.text) if _SNAPSHOT_RE.match(m)}, reverse=True)
        out = []
        for date in dates[:probe]:
            listing = client.get(f"https://dumps.wikimedia.org/{_WIKIDATA_DIR}/{date}/", headers=headers)
            budget.acquire_bytes(len(listing.content))
            if listing.status_code != 200:
                continue
            if f"wikidata-{date}-all.json.bz2" in listing.text:
                out.append(date)
    return tuple(out)


WIKIDATA_SPEC = AcquisitionSpec(
    source_id="wikidata",
    transport=Transport.HTTPS,
    endpoints=(
        Endpoint(transport=Transport.HTTPS, url="https://dumps.wikimedia.org",
                 max_conns=2,   # dumps.wikimedia.org etiquette: max 2 connections
                 static_files=()),  # dated only: a collection pins a snapshot (D1)
    ),
    # one file holds every entity; documents are counted as the pack is built
    layout="single-file",
    filters=("*-all.json.bz2", "*-md5sums.txt", "*-sha1sums.txt"),
    # the run's own checksum files travel with the dump (`-md5sums.txt`); the
    # engine's sidecar check wants `<file>.md5`, which this origin does not
    # publish — the pack build verifies the dump against the listed md5 instead
    checksums=ChecksumSource(kind="none"),
    delta=DeltaSpec(mode="dated-dumps"),
    snapshot_files=_wikidata_files,
    snapshot_index=wikidata_snapshots,
    profiles={"all": Filter()},
    all_profile="all",
    estimated_bytes={"all": _WIKIDATA_DUMP_BYTES + _WIKIDATA_CHECKSUM_BYTES},
    license_class=LicenseClass.GREEN,            # CC0: no terms to acknowledge
)


ACQUIRE_REGISTRY: dict[str, AcquisitionSpec] = {
    "embedded": EMBEDDED_SPEC,
    "gutenberg": GUTENBERG_SPEC,
    "normattiva": NORMATTIVA_SPEC,
    "pubmed": PUBMED_SPEC,
    "wikidata": WIKIDATA_SPEC,
    "wikipedia": WIKIPEDIA_SPEC,
}

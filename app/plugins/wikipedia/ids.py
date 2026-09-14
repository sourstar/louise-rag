"""Wikipedia's native ids: the language, the page, and which source the
document is read from (docs/notes/wikipedia-two-layers.md).

ONE collection holds wikivec's languages in two layers — the pre-embedded
archive's articles and the dumps' — and the id is what keeps them apart
and lined up: an archive-read article keeps the very id the archive's rows
are keyed by (`ordinal × 10^10 + page id`, the frozen table below), and a
dump-read page is numbered from 10^12, above the archive's whole range, so
which layer a document falls in is a pure function of its id. 06 §6.3's
`archive:` rule reads the archive's coverage `{ids_per_shard: 10^10,
shards: 11}` from the Codex's credits (09 §9.3), and a dump-read page is
never inside it.
"""

#: wikivec's language numbering, FROZEN and identical to the archive's
#: (tests/test_wikipedia_ids.py holds it equal to the legacy `embedded`
#: plugin's): a new language APPENDS, and a number never changes, or every
#: uid minted under it orphans.
LANG_ORDINAL = {"en": 0, "de": 1, "fr": 2, "ru": 3, "es": 4, "it": 5,
                "ja": 6, "pt": 7, "zh": 8, "fa": 9, "tr": 10}
_ORDINAL_LANG = {v: k for k, v in LANG_ORDINAL.items()}
LANG_BASE = 10 ** 10
DUMP_BASE = 10 ** 12


def native_id_of(lang: str, page_id: int, *, archive: bool = False) -> int:
    """The document id: the language's block, the page id inside it, and
    the dump's range unless the page is read from the archive."""
    return (0 if archive else DUMP_BASE) + LANG_ORDINAL[lang] * LANG_BASE + int(page_id)


def decode_native_id(native_id: int) -> tuple[str, int, bool]:
    """`(language, page id, read from the archive?)`."""
    archive = native_id < DUMP_BASE
    rest = native_id if archive else native_id - DUMP_BASE
    return _ORDINAL_LANG[rest // LANG_BASE], rest % LANG_BASE, archive

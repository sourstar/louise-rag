"""EmbeddedSource — CorpusSource over pre-embedded parquet corpora
(architecture-v2.md §17 amendment 11: format v1). LEGACY: it is `wikivec`'s
plugin, and wikivec becomes layer 1 of the `wikipedia` collection
(docs/notes/wikipedia-two-layers.md). Its reading machinery moved to
`app/plugins/wikipedia/archive.py`, which both use; this module stays only
until wikivec is migrated.

The corpus IS the published embedding dataset: parquet shards whose rows
carry (id, url, title, text, embedding), with `id = "{article}_{seq}"` and
the rows of one article consecutive. Text travels WITH the vectors, so
possession, FTS and citations work exactly as for any other corpus — and
the co-located `embedding` column is what the genesis "corpus-embedded"
adapter imports, after the same-function proof, instead of re-embedding.

chunking_profile = "verbatim": one parquet row = one section = one chunk.
The producer already chunked; preserving their boundaries 1:1 is what makes
their vectors attach at all.

Layout (the Upstash convention, adopted as format v1's on-disk shape):
    data/{lang}/{NNN}.parquet

Locator (plugin-opaque, survives the manifest): one or more segments
    "<shard path>::<start_row>::<end_row>"     joined by "|"
end_row exclusive; a second segment covers an article whose rows straddle a
shard boundary.

THE ORIGIN FORM, and why — invariant 11 wants it recorded beside the
plugin (06 §6.4). The origin is the PRODUCER's published dataset
(`EMBEDDED_SPEC`: Upstash's `wikipedia-2024-06-bge-m3` on the Hugging Face
Hub), taken as its parquet shards exactly as published: their rows are the
only form its vectors attach to — the producer's own text, cut at the
producer's own boundaries — so a re-derivation from Wikipedia's dumps
would be another corpus, not this one. What the dumps give is the
`wikipedia` plugin's dump layer; this archive is that collection's layer 1
(docs/notes/wikipedia-two-layers.md).
"""
import hashlib
import json

from .. import cursor
from ..base import (CanonicalDoc, DiscoverState, DocRef, Erasure, LicenseClass,
                    Modality, Mutability, PROTOCOL_VERSION, PrivacyClass, RawDoc)
from ..wikipedia import archive as _archive
from ..wikipedia import ids as _ids
# the moved machinery, under the names this module's callers and tests use
from ..wikipedia.archive import (  # noqa: F401 — re-exported
    _group_table, _groups, _head_continuation, _layout, _layouts, _tail_carry,
    embed_input, join_segments as _join_segments, lang_of_shard as _lang_of_shard,
    read_rows as _read_rows, split_locator, split_row_id)

# native_id must be int-castable and unique across languages, so it encodes
# (language, article id) as ordinal * 10^10 + article_id. FROZEN — and the
# table is the wikipedia plugin's (app/plugins/wikipedia/ids.py): its
# archive-read articles keep these very ids.
LANG_ORDINAL = _ids.LANG_ORDINAL
_LANG_BASE = _ids.LANG_BASE


def encode_native_id(lang: str, article_id: int) -> int:
    return _ids.native_id_of(lang, article_id, archive=True)


def decode_native_id(native_id: int) -> tuple[str, int]:
    lang, article, _archive_read = _ids.decode_native_id(native_id)
    return lang, article


class EmbeddedSource:
    source_id = "embedded"
    # The dataset repo's metadata license does not relicense the CONTENT:
    # Wikipedia text inside stays CC BY-SA (amendment 11).
    license_class = LicenseClass.YELLOW
    # 06 §6.1's corpus credit. The one pre-embedded dataset this plugin is
    # used for is Upstash's Wikipedia (`DEFAULT_ARCHIVE_BY_PLUGIN`), whose
    # TEXT is Wikipedia's — the embedder is credited separately, as the
    # `kind: "archive"` entry. A different pre-embedded corpus under this
    # plugin needs its own provider here; the plugin is generic and this
    # line is not, which is why it says which dataset it means.
    corpus_provider = "Wikipedia contributors"
    corpus_license = "CC-BY-SA-4.0"
    corpus_attribution = "Wikipedia contributors, CC BY-SA 4.0"
    privacy_class = PrivacyClass.PUBLIC
    chunking_profile = "verbatim"
    # A frozen published dataset — and its boundaries are the
    # PRODUCER'S, which is the whole basis for reusing their vectors.
    # Re-cutting them for any reason would orphan every one.
    mutability = Mutability.IMMUTABLE
    protocol_version = PROTOCOL_VERSION
    PARSER_VERSION = 1
    modality = Modality.TEXT
    # A published dataset release is frozen; a corrected release is a new
    # dataset, not an erasure of this one.
    erasure = Erasure.APPEND_ONLY
    native_id_order = "numeric"          # the producer's article ids
    default_facets = [
        {"field": "lang", "kind": "flat", "separator": None, "authority": None},
    ]

    def discover(self, state: DiscoverState):
        if not state.corpus_root.is_dir():
            return
        yield from _archive.discover_articles(
            state, self.source_id, cursor.Progress(state, self.source_id), self._ref)

    def _ref(self, pending: dict, lang: str) -> DocRef:
        try:
            native = encode_native_id(lang, int(pending["article"]))
        except (KeyError, ValueError):
            return DocRef(source_id=self.source_id, native_id=pending["article"],
                          locator=_join_segments(pending["segments"]),
                          error=f"malformed article id {pending['article']!r}")
        # size = the article's ROW COUNT, not its shard's byte size: the
        # change gate is (mtime, size), and a shard's size says nothing
        # about one article in it. A straddler that gains a segment (its
        # continuation shard arriving later) changes row count and is
        # re-hashed and re-processed; with the shard size it could read as
        # a mere relocation — same mtime, same size, new path — and keep
        # stale stage flags over changed content.
        return DocRef(
            source_id=self.source_id, native_id=str(native),
            locator=_join_segments(pending["segments"]),
            mtime=pending["mtime"], size=pending["rows"],
            extra={"title": pending["title"], "lang": lang,
                   "article": pending["article"]})

    def fetch(self, ref: DocRef) -> RawDoc:
        rows: list[dict] = []
        for path, start, end in split_locator(ref.locator):
            rows.extend(_read_rows(path, start, end))
        data = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.blake2b(data, digest_size=16).hexdigest()
        return RawDoc(ref=ref, raw_hash=digest, data=data)

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        rows = json.loads(raw.data.decode("utf-8"))
        rows.sort(key=lambda r: split_row_id(r["id"])[1])
        title = rows[0]["title"] if rows else ""
        lang = raw.ref.extra.get("lang") or ""
        article = raw.ref.extra.get("article") or ""
        if not lang or not article:
            # ref rebuilt from a manifest row: recover both from native_id
            lang, aid = decode_native_id(int(raw.ref.native_id))
            article = str(aid)
        sections = [("", embed_input(r["title"], r["text"])) for r in rows]
        return CanonicalDoc(
            uid=f"{self.source_id}:{raw.ref.native_id}",
            title=title,
            creators=[],
            language=lang,
            date=None,
            subjects=[],
            keys=[f"wikipedia:{lang}:{article}"],
            extra={"url": rows[0]["url"] if rows else ""},
            sections=sections,
            raw_hash=raw.raw_hash,
        )

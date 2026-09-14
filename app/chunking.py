"""Chunking with deterministic ids: {uid}:{seq}:{version}.

Two families, chosen by whether the source revises what it publishes
(plugins/base.py::Mutability): fixed-width windows for immutable
sources, content-defined boundaries for mutable ones.
"""
import hashlib
import re

from . import config

def _est_tokens(s: str) -> int:
    return max(1, len(s) // 4)

def chunk_book(uid: str, native_id, chapters: list[tuple[str, str]],
                version=None, params=None):
    """Yield dicts: id, book_id, seq, chapter, text, tokens.

    `uid` (e.g. "gutenberg:1342") namespaces the chunk id; `native_id` is the
    physical `book_id` stored on each chunk row for Tantivy/Lance filtering
    and delete-then-insert, unchanged from pre-refactor behavior.

    Reads its window from the ENVIRONMENT, which 06 §6.3 forbids
    ("a chunker reads no node configuration") — and it stays that way. This
    is the grandfathered chunker: the vectors already on this node were cut
    by it under whatever CHUNK_TOKENS was set then, and changing where it
    reads from would re-cut them. `fixed` below is the same windows with the
    parameters carried in the layer, which is what a new collection uses.
    `params` is accepted and ignored so that every chunker has one shape.
    """
    max_chars = config.CHUNK_TOKENS * 4
    # Clamped to guarantee the hard-split loop below terminates. That loop
    # advances with `p = p[max_chars - overlap_chars:]`, so once
    # overlap_chars >= max_chars the slice index turns negative, `p` stops
    # shrinking, and the generator yields chunks forever — an unbounded-memory
    # hang reachable from configuration alone (CHUNK_OVERLAP >= CHUNK_TOKENS).
    #
    # NOT a CHUNK_VERSION bump: a configuration terminates if and only if
    # overlap_chars < max_chars, and for every such configuration this clamp is
    # a no-op. Only the configurations that could never produce output at all
    # are affected, so no existing chunking changes (invariant 3).
    overlap_chars = min(config.CHUNK_OVERLAP * 4, max_chars - 1)
    yield from _fixed_windows(uid, native_id, chapters, version,
                              max_chars, overlap_chars)


def chunk_fixed(uid: str, native_id, chapters: list[tuple[str, str]],
                 version=None, params=None):
    """06 §6.3's `fixed`: windows of `tokens` with `overlap` between
    neighbours, paragraph-aligned, a paragraph larger than the window split
    hard. Byte for byte what `chunk_book` does — the ONE difference is where
    the two numbers come from.

    They come from the LAYER, which is the whole point: 06 §6.3 says "a
    chunker reads no node configuration", and `space_id` covers the layer's
    params (09 §9.3), so a chunker that read the environment would let two
    peers agree on a space id and disagree on chunks. That is invariant 1's
    failure mode and it is undetectable — both peers computed the id the
    document prescribes, and both are honest.

    So there is no default. A layer that does not say is refused.
    """
    if not params or "tokens" not in params or "overlap" not in params:
        raise ChunkerError(
            "the `fixed` chunker takes `tokens` and `overlap` from its "
            "layer's params (06 §6.3, 09 §9.3); it has no defaults and does "
            "not read the environment, because two peers that agreed on a "
            "space_id and disagreed on chunks would never find out")
    tokens, overlap = int(params["tokens"]), int(params["overlap"])
    if tokens < 1:
        raise ChunkerError(f"fixed: tokens is at least 1, got {tokens}")
    if not 0 <= overlap < tokens:
        raise ChunkerError(
            f"fixed: overlap is at least 0 and less than tokens, got "
            f"{overlap} against {tokens}; at or above it the hard-split loop "
            f"stops shrinking and yields for ever")
    yield from _fixed_windows(uid, native_id, chapters, version,
                              tokens * 4, overlap * 4)


class ChunkerError(ValueError):
    """A layer does not give a chunker what it needs."""


def _fixed_windows(uid: str, native_id, chapters, version,
                   max_chars: int, overlap_chars: int):
    """The windows themselves, shared by `book` and `fixed` so that the two
    cannot drift. `book` passes the environment's numbers and `fixed` the
    layer's; everything below this line is identical for both, which is what
    makes `fixed{tokens: CHUNK_TOKENS, overlap: CHUNK_OVERLAP}` produce the
    same chunk ids as `book` — and therefore what lets a Codex collection
    adopt the vectors a legacy pack already holds."""
    seq = 0
    for chapter_title, body in chapters:
        paras = [p for p in body.split("\n\n") if p.strip()]
        buf: list[str] = []
        size = 0
        for para in paras:
            p = para.strip()
            # a single paragraph larger than the window gets hard-split
            while len(p) > max_chars:
                head, p = p[:max_chars], p[max_chars - overlap_chars:]
                yield _mk(uid, native_id, seq, chapter_title, head, version)
                seq += 1
            if size + len(p) > max_chars and buf:
                text = "\n\n".join(buf)
                yield _mk(uid, native_id, seq, chapter_title, text, version)
                seq += 1
                tail = text[-overlap_chars:]
                buf, size = [tail, p], len(tail) + len(p)
            else:
                buf.append(p)
                size += len(p)
        if buf:
            yield _mk(uid, native_id, seq, chapter_title, "\n\n".join(buf), version)
            seq += 1

def chunk_abstract(uid: str, native_id, sections: list[tuple[str, str]],
                    version=None, params=None):
    """One chunk per record: chunking_profile='abstract' (PubMed) — the
    whole title+abstract becomes a single chunk, no windowing/overlap.

    This is how MedCPT was fed (title + abstract, one vector per article,
    the encoder truncating at its 512-token window — the embed client's
    char budget plus the server's own truncation do the same here), so
    the genesis shape gate (one archive vector per chunk) accepts every
    article. Live 2026-08-25 with chunk_book: title and abstract became
    two chunks, so 42–70 % of shard documents were shape mismatches and
    the archive covered only title-only records. Measured against the
    archive on 24 abstract-bearing articles: cosine median 0.988, min
    0.960 for "title\\n\\nabstract" (the [SEP] join NCBI used scored
    0.991 — not worth a marker in the displayed text).

    No chapter label: the chunk IS the article. Versioned by
    ABSTRACT_CHUNK_VERSION, its own number (invariant 3)."""
    text = "\n\n".join(body for _, body in sections if body).strip()
    if not text:
        return
    yield _mk(uid, native_id, 0, "", text, version)

def chunk_verbatim(uid: str, native_id, sections: list[tuple[str, str]],
                    version=None, params=None):
    """One chunk per SECTION, exactly as the plugin emitted it — no windowing,
    no merge, no hard-split (amendment 11). This is the pre-embedded corpus
    profile: the producer already chunked, their embedding input IS the
    section text, and preserving it 1:1 is what makes the chunk-shape gate a
    row-count check and the sample re-encode a like-for-like comparison. A
    section longer than CHUNK_TOKENS stays whole — splitting it would orphan
    the producer's vector for the tail."""
    for seq, (label, body) in enumerate(sections):
        if body:
            yield _mk(uid, native_id, seq, label or "", body, version)


# ---------------- content-defined chunking (mutable sources) ----------------
#
# Fixed-width chunking counts characters from the start of the text, so every
# boundary is a function of everything before it. Insert one paragraph near
# the top of an article and every later boundary moves: each chunk now holds
# different text, gets a different embedding, and must be recomputed. A typo
# fix re-embeds the whole document.
#
# Content-defined chunking decides boundaries from the CONTENT instead. A
# rolling hash runs over a small sliding window and a cut is made wherever
# the hash matches a pattern, so a boundary depends only on the bytes around
# it. An edit disturbs its own neighbourhood and leaves every other chunk
# byte-identical — which is what makes a monthly delta cost the edits rather
# than the corpus. Same trick as rsync, borg and restic.
#
# THE TRADE, stated plainly: there is no overlap between CDC chunks. Overlap
# would make each chunk depend on its neighbour's tail, which is precisely
# the coupling this exists to remove, and a passage straddling a boundary is
# therefore split. The lexical leg and RRF absorb some of that; it is a real
# cost, accepted deliberately for a source that would otherwise be
# unmaintainable.
#
# DETERMINISM (invariant 3): the gear table is derived from a fixed constant
# by blake2b, so it is identical on every machine and every Python version —
# never random.*, never a literal nobody can regenerate. Changing any
# parameter below changes every boundary, so it changes CDC_CHUNK_VERSION.

# Gear hash: one 64-bit value per byte value, mixed in as the window slides.
_GEAR = tuple(
    int.from_bytes(
        hashlib.blake2b(bytes([i]), digest_size=8, person=b"louise-cdc").digest(),
        "big")
    for i in range(256))
_MASK64 = (1 << 64) - 1

# Sizes in CHARACTERS. The maximum is the binding constraint: a chunk must
# fit the encoder's context, and wikipedia's recipe is ctx=2048 tokens at the
# ~4 chars/token this codebase estimates with.
CDC_MIN_CHARS = 1_000
CDC_TARGET_CHARS = 4_000
CDC_MAX_CHARS = 8_000

# Normalized chunking (FastCDC): a STRICTER mask before the target size and a
# looser one after it. Cuts therefore become unlikely while a chunk is small
# and likely once it is big enough, which concentrates sizes near the target
# instead of producing the long tail a single mask gives.
_MASK_STRICT = (1 << 14) - 1
_MASK_LOOSE = (1 << 10) - 1


def _cdc_boundaries(text: str):
    """Yield chunk end offsets for `text`, honouring min/target/max."""
    length = len(text)
    start = 0
    while start < length:
        remaining = length - start
        if remaining <= CDC_MIN_CHARS:
            yield length
            return
        limit = min(remaining, CDC_MAX_CHARS)
        target = min(remaining, CDC_TARGET_CHARS)
        h = 0
        cut = None
        # The minimum is skipped without hashing: no cut may be taken there,
        # so the hash over it would be discarded anyway.
        for offset in range(CDC_MIN_CHARS, limit):
            h = ((h << 1) + _GEAR[ord(text[start + offset]) & 0xFF]) & _MASK64
            mask = _MASK_STRICT if offset < target else _MASK_LOOSE
            if not (h & mask):
                cut = offset + 1
                break
        end = start + (cut if cut is not None else limit)
        yield end
        start = end


def cdc_split(text: str) -> list[str]:
    """`text` cut into content-defined pieces. Pure function of the text."""
    pieces = []
    previous = 0
    for end in _cdc_boundaries(text):
        piece = text[previous:end]
        if piece:
            pieces.append(piece)
        previous = end
    return pieces


def chunk_cdc(uid: str, native_id, sections: list[tuple[str, str]],
               version=None, params=None):
    """Content-defined chunks, per section.

    Per SECTION rather than over the whole document, for the same reason CDC
    exists at all: a section is a natural locality boundary, so an edit in
    one section cannot move boundaries in another even in principle.
    """
    seq = 0
    for label, body in sections:
        text = (body or "").strip()
        if not text:
            continue
        for piece in cdc_split(text):
            yield _mk(uid, native_id, seq, label or "", piece, version)
            seq += 1


# ---------------- akn_article: one chunk per article ----------------
#
# docs/notes/gazzetta-plugin.md §5, approved by the operator 2026-09-14.
# Legal text carries its own unit — the article, which an amendment
# replaces and a citation names — so it is cut there and not by content
# hashing, which would land mid-comma. The plugin's sections ARE its
# articles and annexes (plugins/normattiva/rendition.py). An article past
# `max_chars` is split between its commi, and a comma past it by CDC
# within itself; all three levels are a pure function of (section text,
# version, params) — invariant 3.

#: The default until the pilot measures it (§5: "whole article" against
#: "split at N" is an empirical question). A recipe that measured sets
#: `params.max_chars`, which 09 §9.3 hashes into the space id.
AKN_MAX_CHARS = 8_000
#: a comma's number at the margin: "1. ", "2-bis. ", "10-quinquies."
_AKN_COMMA = re.compile(r"^\d+(?:-[a-z]+)?\.(?:\s|$)")


def _akn_pieces(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    units: list[str] = []
    cur: list[str] = []
    for line in text.split("\n"):
        if cur and _AKN_COMMA.match(line):
            units.append("\n".join(cur))
            cur = []
        cur.append(line)
    if cur:
        units.append("\n".join(cur))
    pieces: list[str] = []
    buf = ""
    for unit in units:
        if len(unit) > limit:
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.extend(cdc_split(unit))
        elif not buf:
            buf = unit
        elif len(buf) + 1 + len(unit) <= limit:
            buf = f"{buf}\n{unit}"
        else:
            pieces.append(buf)
            buf = unit
    if buf:
        pieces.append(buf)
    return pieces


def chunk_akn_article(uid: str, native_id, sections: list[tuple[str, str]],
                      version=None, params=None):
    """One chunk per article; an empty body (an article repealed in place,
    an annex the XML holds no text for) yields none."""
    limit = int((params or {}).get("max_chars") or AKN_MAX_CHARS)
    seq = 0
    for label, body in sections:
        text = (body or "").strip()
        if not text:
            continue
        for piece in _akn_pieces(text, limit):
            yield _mk(uid, native_id, seq, label or "", piece, version)
            seq += 1


# ---------------- which chunker, and at which version ----------------

BOOK = "book"
FIXED = "fixed"
VERBATIM = "verbatim"
ABSTRACT = "abstract"
CDC = "cdc"
AKN_ARTICLE = "akn_article"

_CHUNKERS = {BOOK: chunk_book, FIXED: chunk_fixed, VERBATIM: chunk_verbatim,
             ABSTRACT: chunk_abstract, CDC: chunk_cdc,
             AKN_ARTICLE: chunk_akn_article}

# CDC is versioned SEPARATELY from the fixed-width chunkers, and that
# separation is the point: CHUNK_VERSION used to be one global number that
# entered every collection's space_id, so changing how TEXT is chunked would
# have re-pinned collections whose chunker never changed — including the
# pre-embedded ones, whose vectors were imported against a producer's
# boundaries and cannot be recomputed by re-chunking at all.
#
# Existing chunkers keep config.CHUNK_VERSION, so every existing collection's
# space_id is unchanged to the byte.
CDC_CHUNK_VERSION = 1
# The abstract chunker (PubMed, 2026-08-26) has its own number for the same
# reason: it enters ONLY PubMed's space id. Bump it — and only it — when
# how an article becomes its one chunk changes.
ABSTRACT_CHUNK_VERSION = 1
# The article chunker (Normattiva, 2026-09-14) enters only the spaces of
# collections that declare it, like the two above.
AKN_CHUNK_VERSION = 1

# `fixed` is `book` with its window carried in the layer instead of read
# from the environment, and it is version 1 BECAUSE THAT IS WHAT `book`
# IS: config.CHUNK_VERSION defaults to 1, a chunk id is
# `{uid}:{seq}:{version}`, and the two chunkers cut identically at the
# same window. So a Codex collection declaring
# `fixed{tokens: 1000, overlap: 120}` produces the chunk ids a legacy
# gutenberg pack already holds, and its vectors carry over rather than
# being re-embedded. Bump this only when the WINDOWS change.
FIXED_CHUNK_VERSION = 1


def chunker_name(profile: str | None, mutability=None) -> str:
    """Which chunker a collection uses.

    verbatim wins over everything: those boundaries belong to the producer
    of the vectors, and re-cutting them would orphan every one, mutable
    source or not. Otherwise a MUTABLE source gets content-defined chunking
    and everything else keeps the fixed-width chunker it has always had.
    """
    from .plugins.base import Mutability

    if profile == VERBATIM:
        return VERBATIM
    if profile == ABSTRACT:
        # one chunk per article (PubMed) — wired 2026-08-26; until then the
        # profile was declared but fell through to BOOK, which cut title
        # and abstract into separate chunks
        return ABSTRACT
    if profile == AKN_ARTICLE:
        # one chunk per article (Normattiva): its source is MUTABLE, and
        # would otherwise get CDC, which cuts mid-comma
        return AKN_ARTICLE
    if mutability == Mutability.MUTABLE:
        return CDC
    return BOOK


def chunk_version_for(name: str) -> int:
    if name == CDC:
        return CDC_CHUNK_VERSION
    if name == ABSTRACT:
        return ABSTRACT_CHUNK_VERSION
    if name == AKN_ARTICLE:
        return AKN_CHUNK_VERSION
    if name == FIXED:
        return FIXED_CHUNK_VERSION
    return config.CHUNK_VERSION


def chunker_version_for_collection(collection) -> tuple[str, int]:
    """(chunker name, chunk version) for a collection — the pair that decides
    both how its text is cut and what its space_id records."""
    plugin = collection.plugin_instance()
    name = chunker_name(getattr(plugin, "chunking_profile", None),
                         getattr(plugin, "mutability", None))
    return name, chunk_version_for(name)

def chunker_for(profile: str | None, mutability=None):
    """The chunker for a collection, already bound to its chunk version.

    Returns a callable with the original (uid, native_id, sections)
    signature, so no caller has to know that the version is now per-chunker
    rather than one global number.
    """
    name = chunker_name(profile, mutability)
    fn = _CHUNKERS[name]
    version = chunk_version_for(name)

    def _chunk(uid, native_id, sections):
        return fn(uid, native_id, sections, version)

    return _chunk

def _mk(uid, native_id, seq, chapter, text, version=None):
    # version=None keeps every direct caller and test working against the
    # global, which is still correct for every fixed-width chunker.
    if version is None:
        version = config.CHUNK_VERSION
    return {
        "id": f"{uid}:{seq}:{version}",
        "book_id": native_id,
        "seq": seq,
        "chapter": chapter,
        "text": text,
        "tokens": _est_tokens(text),
    }

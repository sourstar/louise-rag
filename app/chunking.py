"""Chunking with deterministic ids: {uid}:{seq}:{version}.

Two families, chosen by whether the source revises what it publishes
(plugins/base.py::Mutability): fixed-width windows for immutable
sources, content-defined boundaries for mutable ones.
"""
import hashlib

from . import config

def _est_tokens(s: str) -> int:
    return max(1, len(s) // 4)

def chunk_book(uid: str, native_id, chapters: list[tuple[str, str]],
                version=None):
    """Yield dicts: id, book_id, seq, chapter, text, tokens.

    `uid` (e.g. "gutenberg:1342") namespaces the chunk id; `native_id` is the
    physical `book_id` stored on each chunk row for Tantivy/Lance filtering
    and delete-then-insert, unchanged from pre-refactor behavior.
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
                    version=None):
    """One chunk per record: chunking_profile='abstract' (e.g. PubMed) — the
    whole title+abstract becomes a single chunk, no windowing/overlap."""
    text = "\n\n".join(body for _, body in sections if body).strip()
    if not text:
        return
    yield _mk(uid, native_id, 0, sections[0][0] if sections else "", text,
               version)

def chunk_verbatim(uid: str, native_id, sections: list[tuple[str, str]],
                    version=None):
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


def chunk_cdc(uid: str, native_id, sections: list[tuple[str, str]], version=None):
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


# ---------------- which chunker, and at which version ----------------

BOOK = "book"
VERBATIM = "verbatim"
ABSTRACT = "abstract"
CDC = "cdc"

_CHUNKERS = {BOOK: chunk_book, VERBATIM: chunk_verbatim,
             ABSTRACT: chunk_abstract, CDC: chunk_cdc}

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
    if mutability == Mutability.MUTABLE:
        return CDC
    return BOOK


def chunk_version_for(name: str) -> int:
    return CDC_CHUNK_VERSION if name == CDC else config.CHUNK_VERSION


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

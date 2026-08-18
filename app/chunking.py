"""Chapter-aware chunking with deterministic ids: {uid}:{seq}:{version}."""
from . import config

def _est_tokens(s: str) -> int:
    return max(1, len(s) // 4)

def chunk_book(uid: str, native_id, chapters: list[tuple[str, str]]):
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
                yield _mk(uid, native_id, seq, chapter_title, head)
                seq += 1
            if size + len(p) > max_chars and buf:
                text = "\n\n".join(buf)
                yield _mk(uid, native_id, seq, chapter_title, text)
                seq += 1
                tail = text[-overlap_chars:]
                buf, size = [tail, p], len(tail) + len(p)
            else:
                buf.append(p)
                size += len(p)
        if buf:
            yield _mk(uid, native_id, seq, chapter_title, "\n\n".join(buf))
            seq += 1

def chunk_abstract(uid: str, native_id, sections: list[tuple[str, str]]):
    """One chunk per record: chunking_profile='abstract' (e.g. PubMed) — the
    whole title+abstract becomes a single chunk, no windowing/overlap."""
    text = "\n\n".join(body for _, body in sections if body).strip()
    if not text:
        return
    yield _mk(uid, native_id, 0, sections[0][0] if sections else "", text)

def chunk_verbatim(uid: str, native_id, sections: list[tuple[str, str]]):
    """One chunk per SECTION, exactly as the plugin emitted it — no windowing,
    no merge, no hard-split (amendment 11). This is the pre-embedded corpus
    profile: the producer already chunked, their embedding input IS the
    section text, and preserving it 1:1 is what makes the chunk-shape gate a
    row-count check and the sample re-encode a like-for-like comparison. A
    section longer than CHUNK_TOKENS stays whole — splitting it would orphan
    the producer's vector for the tail."""
    for seq, (label, body) in enumerate(sections):
        if body:
            yield _mk(uid, native_id, seq, label or "", body)

def chunker_for(profile: str | None):
    """Chunking dispatch by the plugin's chunking_profile. Every profile that
    predates the dispatch keeps chunk_book — byte-identical behavior for all
    existing collections (invariant 3: no silent chunking change). Only
    'verbatim' (introduced WITH this dispatch, amendment 11) maps elsewhere."""
    return chunk_verbatim if profile == "verbatim" else chunk_book

def _mk(uid, native_id, seq, chapter, text):
    return {
        "id": f"{uid}:{seq}:{config.CHUNK_VERSION}",
        "book_id": native_id,
        "seq": seq,
        "chapter": chapter,
        "text": text,
        "tokens": _est_tokens(text),
    }

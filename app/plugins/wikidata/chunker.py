"""The `wikidata-text` chunker (06 §6.3; docs/notes/wikidata-build-plan.md
§2 "The chunker"): per section — one per language of the entity's text —
windows of at most `max_tokens` tokens of a pinned tokenizer, split at the
textifier's own statement boundaries as Wikimedia's `chunk_item_text`
splits them (WikidataTextEmbedding `src/utils.py`, commit c9dc819, MIT,
Copyright (c) 2024 Jonathan Fraine — the rule carried here, see
VENDORED.md), so a chunk of ours is a chunk of theirs for the same text:

- the whole text, when it fits;
- else every chunk = the HEAD (label, description, aliases) + the STICKY
  line (the entity's `instance of` statement, which textify.py renders
  first) + the longest run of further statement lines that fits;
- a single statement too long for a window is cut at the token boundary
  that fits (upstream `_trim_to_length`), never split across windows;
- no overlap beyond the head and the sticky line, which every chunk
  repeats so that it says what it is about.

A chunker reads no node configuration (06 §6.3): the tokenizer is named
in the layer's params by the sha256 of its `tokenizer.json`, which enters
`space_id`; the file is found under `config.TOKENIZERS_DIR` (the platform
image fetches it at build), checked against the sha256, and loaded once
(Hugging Face `tokenizers`, D6). Chunk ids `<uid>:<seq>:<version>`,
`seq` running across the sections in their order; `chapter` is the
section's language.
"""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from app import config

CHUNK_VERSION = 1
DEFAULT_MAX_TOKENS = 1024
#: what separates the head from the statements in textify.py's text, in
#: every language (the connective varies; the line shape does not)
_ATTR_MARK = ":\n- "
_LINE = "\n- "


class ChunkerError(ValueError):
    """The layer's params or the tokenizer they name are not usable."""


# ---- the tokenizer, by sha256 ---------------------------------------------------------------

_tokenizers: dict[str, Any] = {}
_lock = threading.Lock()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def register(sha256: str, tokenizer: Any) -> None:
    """A tokenizer object under its sha256 — what a test gives, or what the
    image's loader gives once it has checked the file."""
    with _lock:
        _tokenizers[sha256] = tokenizer


def tokenizer_for(sha256: str) -> Any:
    """The `tokenizers.Tokenizer` the layer names: from the registry, else
    `<TOKENIZERS_DIR>/<hex>.json` whose sha256 must be the one named."""
    with _lock:
        got = _tokenizers.get(sha256)
    if got is not None:
        return got
    if not isinstance(sha256, str) or not sha256.startswith("sha256:") or len(sha256) != 71:
        raise ChunkerError(f"the layer's `tokenizer` is the sha256 of a tokenizer.json, not {sha256!r}")
    path = Path(config.TOKENIZERS_DIR) / f"{sha256[7:]}.json"
    if not path.is_file():
        raise ChunkerError(f"tokenizer {sha256} is not in {config.TOKENIZERS_DIR} (the platform image "
                           f"fetches it at build; set TOKENIZERS_DIR where it is)")
    found = sha256_of(path)
    if found != sha256:
        raise ChunkerError(f"{path} is {found}, the layer names {sha256}: not the pinned tokenizer")
    try:
        from tokenizers import Tokenizer
    except ImportError as e:   # pragma: no cover — the image carries it (D6)
        raise ChunkerError(f"the `tokenizers` package is not installed: {e}") from e
    tok = Tokenizer.from_file(str(path))
    register(sha256, tok)
    return tok


def _encode(tok: Any, text: str) -> tuple[int, list[tuple[int, int]]]:
    enc = tok.encode(text, add_special_tokens=False)
    return len(enc.ids), list(enc.offsets)


# ---- the cut -------------------------------------------------------------------------------

def _trim(text: str, offsets: Sequence[tuple[int, int]], max_tokens: int) -> str:
    """Upstream `_trim_to_length`: the text up to the end of the last token
    that fits."""
    if not offsets:
        return text
    if len(offsets) < max_tokens:
        return text[: offsets[-1][1]]
    return text[: offsets[max_tokens - 1][1]]


def split_text(text: str) -> tuple[str, list[str]]:
    """textify.py's text → (head, statement lines)."""
    i = text.find(_ATTR_MARK)
    if i < 0:
        return text, []
    head = text[: i + 1]                       # keeps the connective's colon
    body = text[i + len(_ATTR_MARK):]
    return head, body.split(_LINE)


def _join(head: str, lines: Sequence[str]) -> str:
    return head + _LINE + _LINE.join(lines) if lines else head


def windows(text: str, tok: Any, max_tokens: int, *, sticky_first: bool = True) -> list[str]:
    """One section's text cut as `chunk_item_text` cuts it."""
    n, offsets = _encode(tok, text)
    if n < max_tokens:
        return [text]
    head, lines = split_text(text)
    if not lines:
        return [_trim(text, offsets, max_tokens)]
    sticky = lines[:1] if sticky_first else []
    rest = lines[1:] if sticky_first else lines
    base = _join(head, sticky)
    n, offsets = _encode(tok, base)
    if n >= max_tokens:
        return [_trim(base, offsets, max_tokens)]
    if not rest:
        return [base]
    out: list[str] = []
    start = 0
    while start < len(rest):
        # the longest run from `start` that still fits: a prefix's token count
        # grows with the prefix, so the binary search upstream runs is this search
        lo, hi, best = start + 1, len(rest) + 1, None
        while lo < hi:
            mid = (lo + hi) // 2
            cand = _join(head, [*sticky, *rest[start:mid]])
            n, _ = _encode(tok, cand)
            if n < max_tokens:
                best = (mid, cand)
                lo = mid + 1
            else:
                hi = mid
        if best is None:
            # one statement too long for a window with its head: cut at the token that fits
            cand = _join(head, [*sticky, rest[start]])
            _, offs = _encode(tok, cand)
            out.append(_trim(cand, offs, max_tokens))
            start += 1
            continue
        out.append(best[1])
        start = best[0]
    return out


def chunk_wikidata_text(uid: str, native_id, sections: list[tuple[str, str]],
                        version=None, params=None) -> Iterator[dict]:
    """The chunker as `chunking._CHUNKERS` calls it."""
    from app.chunking import _mk
    params = params or {}
    max_tokens = int(params.get("max_tokens") or DEFAULT_MAX_TOKENS)
    if max_tokens < 16:
        raise ChunkerError(f"max_tokens {max_tokens}: a window shorter than a label")
    sha = params.get("tokenizer")
    if not sha:
        raise ChunkerError("the wikidata-text layer names its tokenizer by sha256 (`tokenizer`); "
                           "a chunker reads no node configuration (06 §6.3)")
    tok = tokenizer_for(str(sha))
    seq = 0
    for label, body in sections:
        text = (body or "").strip()
        if not text:
            continue
        for piece in windows(text, tok, max_tokens):
            yield _mk(uid, native_id, seq, label or "", piece, version if version is not None else CHUNK_VERSION)
            seq += 1

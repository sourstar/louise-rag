"""Snowball stemming in Python, via PyStemmer (libstemmer's own bindings).

WHY NOT tantivy's built-in stemmer filter: it compiles rust-stemmers 1.2.0,
whose last release was November 2019 and whose Greek algorithm PANICS on
ordinary words — the panic kills the whole document's indexing, and an
unindexed document cannot be found at all. See
docs/notes/upstream-rust-stemmers-greek.md for the repro and the mechanism.

WHY PyStemmer: it wraps the Snowball project's own libstemmer, was last
released 2024-10-10, ships prebuilt manylinux cp312 wheels (no toolchain in
the image), and covers 29 languages against rust-stemmers' shorter list.
Verified before adoption, on the real corpus: it produces stems IDENTICAL to
rust-stemmers for every word rust-stemmers manages ("ανθρωπ", "βιβλι",
"καλημερ", "πηγαινετ"), stems the word that crashes it, and raised nothing
across all 6,243 distinct tokens of the book that used to fail.

The trade this makes: stemming moves from the index's analyzer chain into
our code, so an indexed document and a query MUST pass through the same
function here. A stemmed index searched with unstemmed terms returns
nothing, silently — which is why fts.py routes both sides through
stem_text() and the tests assert an inflected query still matches.
"""
import re
import threading

import Stemmer

# Matches tantivy's `simple` tokenizer closely enough for our purposes:
# both split on non-alphanumerics. Exactness is not required — what IS
# required is that indexing and querying tokenize identically, which they
# do by both calling stem_text().
_TOKEN = re.compile(r"\w+", re.UNICODE)

_local = threading.local()


def _stemmer(algorithm: str) -> "Stemmer.Stemmer":
    """One Stemmer per (thread, algorithm).

    PyStemmer objects hold C state and are documented as not thread-safe.
    Four threads sharing one raised no error in testing, but absence of a
    raised error is not proof against C-level corruption — and the worker
    runs a pipeline thread per collection plus a parallel embed pool, so a
    race here would surface as quietly wrong stems, the worst kind of bug
    to find later.
    """
    cache = getattr(_local, "cache", None)
    if cache is None:
        cache = _local.cache = {}
    stemmer = cache.get(algorithm)
    if stemmer is None:
        stemmer = cache[algorithm] = Stemmer.Stemmer(algorithm)
    return stemmer


def stem_text(algorithm: str, text: str) -> str:
    """Lowercased, tokenized, stemmed, whitespace-joined."""
    tokens = _TOKEN.findall(text.lower())
    if not tokens:
        return ""
    return " ".join(_stemmer(algorithm).stemWords(tokens))


def stem_word(algorithm: str, word: str) -> str:
    return _stemmer(algorithm).stemWord(word.lower())

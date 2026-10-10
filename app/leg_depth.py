"""How deep a leg's list goes: to its depth in RECORDS, not in chunks
(05 §5.1, amended 2026-10-10).

A leg ranks chunks and contributes its top `depth` records. Asking the
index for `depth` CHUNKS returned fewer records whenever a book matched
with several chunks: measured live on gutenberg (2026-10-10, 118 queries,
docs/notes/one-language-per-work.md), 50 vector chunks held a median of 43
records (p10 34), and the text leg's top 50 records changed on 51 of 118
queries once each variant went deep enough.

So a list is fetched `first` chunks deep and, while it still holds fewer
than `depth` records and the index had more to give, again at twice the
depth — never past `depth * CHUNK_CAP` chunks, so a list whose records
are all one long book stays bounded. Measured: the vector leg needs a
median 59 chunks for 50 records (p90 72, max 82), so it starts at twice
the depth and almost never asks twice (+55 ms median at 100 chunks);
the text variants start at the depth and double (all variants together
+110 ms median at 300 chunks).
"""
from __future__ import annotations

from typing import Callable

#: a list goes at most this many times its depth in chunks
CHUNK_CAP = 8


def record_of_chunk(chunk_id: str) -> str:
    """A chunk id is `<uid>:<seq>:<version>`; a native id may hold a colon,
    so the split is from the right (app/chunking.py)."""
    return str(chunk_id).rsplit(":", 2)[0]


def records_in(hits: list[dict]) -> int:
    return len({record_of_chunk(h["chunk_id"]) for h in hits})


def cut_at_records(hits: list[dict], depth: int) -> list[dict]:
    """The prefix of `hits` that holds its first `depth` records: the list
    ends at the chunk that brought the `depth`-th."""
    seen: set[str] = set()
    for i, h in enumerate(hits):
        seen.add(record_of_chunk(h["chunk_id"]))
        if len(seen) >= depth:
            return hits[:i + 1]
    return hits


def to_records(fetch: Callable[[int], list[dict]], depth: int, first: int | None = None,
               cap: int | None = None) -> list[dict]:
    """`fetch(n)` returns a leg's first `n` chunk hits in rank order. Returns
    the hits holding the leg's top `depth` records (fewer where the index
    holds fewer, or `cap` chunks hold fewer)."""
    depth = max(1, int(depth))
    cap = cap if cap is not None else depth * CHUNK_CAP
    n = max(1, min(first if first is not None else depth, cap))
    while True:
        hits = fetch(n)
        if len(hits) < n or n >= cap or records_in(hits) >= depth:
            return cut_at_records(hits, depth)
        n = min(n * 2, cap)

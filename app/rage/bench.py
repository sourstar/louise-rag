"""RAGE's bench: one collection's row for a window of the card record
(docs/notes/gpu-saturation-plan.md, the testbench; operator, 2026-11-01:
"This is a testbench for all the collections and models to come").

The BEFORE and AFTER rows of every lever were read by hand from three
places — the card's busy share, the worker log's chunks, the gaps. They are
all in the per-second card record now (app/rage/card_record.py: busy,
gates, backlogs, queries, and the documents and chunks done), so a row is
one call:

    louise dev schedule-report --bench wikipedia --since 22:32 --until 22:47

and it says the same things the hand-made rows said: the card's busy share
on its compute engine, chunks and documents a second, the card time lost
and the gaps (a minute's seconds with work waiting and the card short of
busy), the seconds this collection held the encoder gates and whether
another held them too — a row is only a collection's when it ran ALONE.
When it did, the row is also the measurement a RAGE setting is recorded
with (`hold rage-settings record`, 09 §9.19), and the command is printed.

Read-only: it reads the record and nothing else.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import card_record

#: another collection holding the gates less than this share of the
#: collection's own seconds still leaves the row "alone" (a stray request)
ALONE_BELOW = 0.02
#: a window shorter than this is not a measurement to record a setting by
RECORD_MIN_S = 600


@dataclass(frozen=True)
class Row:
    collection: str
    since: float
    until: float
    seconds: int                      # seconds recorded in the window
    card_seconds: int                 # of those, with the card's figure
    busy: float | None                # mean busy share of the compute engine
    docs: int
    chunks: int
    waiting_s: int                    # seconds this collection had work waiting
    held_s: float                     # request-seconds it held the encoder gates
    query_s: int                      # seconds a person's query held the claim
    lost_per_min: float               # card time unused with work waiting, s/min
    gaps_per_min: float               # seconds idle with work waiting, s/min
    others: dict[str, float] = field(default_factory=dict)  # other collections' held s

    @property
    def chunks_per_s(self) -> float:
        return self.chunks / self.seconds if self.seconds else 0.0

    @property
    def docs_per_s(self) -> float:
        return self.docs / self.seconds if self.seconds else 0.0

    @property
    def alone(self) -> bool:
        return sum(self.others.values()) <= ALONE_BELOW * max(self.held_s, 1.0)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.update(chunks_per_s=round(self.chunks_per_s, 3), docs_per_s=round(self.docs_per_s, 3),
                   alone=self.alone)
        return out


def row(collection: str, since: float, until: float, *, out_dir: Path | None = None,
        card: str | None = None) -> Row:
    """The window's row for `collection`, from the record."""
    rows = list(card_record.read(since, until, out_dir))
    w = card_record.summarize(rows, minute=False, card=card)
    if not w:
        return Row(collection, since, until, 0, 0, None, 0, 0, 0, 0.0, 0, 0.0, 0.0, {})
    w = w[0]
    per_min = 60.0 / w.seconds if w.seconds else 0.0
    return Row(collection=collection, since=since, until=until, seconds=w.seconds,
               card_seconds=w.card_seconds, busy=w.busy,
               docs=w.docs.get(collection, 0), chunks=w.chunks.get(collection, 0),
               waiting_s=w.backlog.get(collection, 0),
               held_s=round(w.served.get(collection, 0.0), 1), query_s=w.query,
               lost_per_min=round(w.lost * per_min, 1), gaps_per_min=round(w.gaps * per_min, 1),
               others={p: round(s, 1) for p, s in w.served.items() if p != collection and s > 0})


def text(r: Row) -> str:
    """The row as the scheduling log writes it."""
    if not r.seconds:
        return f"{r.collection}: nothing recorded in the window"
    busy = "—" if r.busy is None else f"{r.busy:.1%}"
    who = "alone" if r.alone else "NOT alone: " + ", ".join(
        f"{p} held {s:.0f} s" for p, s in sorted(r.others.items(), key=lambda x: -x[1]))
    return (f"{r.collection}: {busy} busy (compute engine) at {r.chunks_per_s:.1f} chunks/s "
            f"({r.docs_per_s:.2f} docs/s); lost {r.lost_per_min:.1f} s/min, gaps "
            f"{r.gaps_per_min:.1f} s/min; work waiting {r.waiting_s} of {r.seconds} s, held "
            f"{r.held_s:.0f} request-s, queries {r.query_s} s; {who}; "
            f"{r.chunks:,} chunks, {r.docs:,} documents in {r.seconds} s")


def record_command(r: Row, *, batch: int, concurrency: int) -> str | None:
    """`hold rage-settings record` for this row, or None and why not."""
    if not r.alone or r.seconds < RECORD_MIN_S or r.busy is None or not r.chunks:
        return None
    return (f"louise hold rage-settings record {r.collection} --batch {batch} "
            f"--concurrency {concurrency} --busy {r.busy:.3f} "
            f"--chunks-per-s {r.chunks_per_s:.1f} --window-s {r.seconds}")


def why_not_recorded(r: Row) -> str:
    if r.busy is None:
        return "the card's figure is missing (the GPU probe did not answer)"
    if not r.alone:
        return "another collection held the card in the window"
    if r.seconds < RECORD_MIN_S:
        return f"the window is {r.seconds} s; a setting is recorded from {RECORD_MIN_S} s"
    return "no chunk was embedded in the window"

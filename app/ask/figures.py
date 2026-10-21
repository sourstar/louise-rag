"""Figures on results (05 §5.3) and their bytes (07 §7.4's `GET
/api/ask/figure`). Migration step 11 — until 2026-10-07 a result carried
`figures: []` for every chunk of a collection with plates.

A figure is PLACED, never ranked: nothing embeds a picture, so it is a
fact about the chunk that was returned. 09 §9.5 makes the Nth
`[Illustration…]` marker of a record its `figures[N]`, so a chunk's plates
are the entries whose markers fall inside it. The record's chunks are
recomputed from its sections by its layer's chunker (invariant 3: the
same chunks the index holds) and walked in order, each marker given the
next index — except the markers of an OVERLAP: a chunker that repeats the
tail of one window at the head of the next (06 §6.3's `fixed`) repeats
that tail's markers too, and they keep the indices the previous chunk
gave them.

Each entry: `{hash, media_type, caption, state}` — never the bytes. `held`:
this peer's container for the record's shard holds them; `not-held`: the
collection carries them and this peer has not fetched the member yet;
`not-carried`: the origin has an image here the collection does not admit
(`carried: false`) — asking again never works.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from app.hold.codex import CodexError

#: a marker's start: `[Illustration]` or `[Illustration: caption]`, as the
#: plugins write it (app/plugins/gutenberg/pg_epub.py)
MARKER = re.compile(r"\[Illustration(?=[:\]])")
MAX_OVERLAP = 16384


class FigureNotServed(CodexError):
    """`status` is the HTTP answer: 404 not held or not carried, 403 never served."""
    exit_code = 2

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def record_of(shard_tree: Path, snapshot, uid: str, order: str = "bytes") -> tuple[dict[str, Any], Any] | None:
    """The record line and its shard: the snapshot's ranges give one
    candidate shard per slice (05 §5.5), and the one held that holds the
    uid is it. None when no shard held holds it."""
    from app.hold import shard as shard_mod
    from app.pack import records as pack_records
    if snapshot is None or ":" not in uid:
        return None
    for shard in snapshot.shards_of(uid.split(":", 1)[1], order):
        path = Path(shard_tree) / shard_mod.shard_path(shard.slice, shard.n)
        if not path.is_file():
            continue
        try:
            return pack_records.read_record(path, uid), shard
        except (KeyError, OSError, ValueError):
            continue
    return None


def _order(codex) -> str:
    return (getattr(codex, "slicing", None) or {}).get("order", "bytes")


def _overlap(prev: str, cur: str) -> int:
    """How many leading characters of `cur` repeat the tail of `prev`: the
    longest suffix of `prev` that `cur` starts with."""
    if not prev or not cur:
        return 0
    i = max(0, len(prev) - MAX_OVERLAP)
    while True:
        i = prev.find(cur[0], i)
        if i < 0:
            return 0
        if cur.startswith(prev[i:]):
            return len(prev) - i
        i += 1


def placed(texts: list[str]) -> list[list[int]]:
    """For each chunk text, in order, the indices of the record's markers
    it carries. A marker whose start lies wholly inside the overlap with
    the previous chunk was counted there and keeps that index."""
    out: list[list[int]] = []
    nxt = 0
    prev: str | None = None
    for text in texts:
        ends = [m.end() for m in MARKER.finditer(text)]
        skip = 0
        if prev is not None and ends:
            overlap = _overlap(prev, text)
            skip = sum(1 for e in ends if e <= overlap)
            skip = min(skip, len(out[-1]))
        carried_over = out[-1][len(out[-1]) - skip:] if skip else []
        fresh = list(range(nxt, nxt + len(ends) - skip))
        nxt += len(fresh)
        out.append(carried_over + fresh)
        prev = text
    return out


def _captions(sections: Iterable) -> list[str]:
    out = []
    for _, body in sections:
        body = str(body or "")
        for m in MARKER.finditer(body):
            out.append(body[m.end():].split("]", 1)[0].lstrip(":").strip())
    return out


def held_hashes(shard_tree: Path, shard) -> set[str]:
    """The figure hashes (bare hex) this peer holds for the record's shard."""
    from app.hold import figures as fig
    path = fig.path_for(Path(shard_tree), shard.slice, shard.n)
    if not path.is_file():
        return set()
    try:
        return set(fig.read_index(path))
    except Exception:   # noqa: BLE001 — a container that does not read holds nothing usable
        return set()


def for_record(collection, record: dict[str, Any], held: set[str], *, red: bool = False) -> dict[int, list]:
    """`seq -> figures` for every chunk of the record."""
    from app import layers
    from app.hold import figures as fig
    entries = record.get("figures")
    if entries is None:
        return {}
    sections = [(str(n or ""), str(b or "")) for n, b in (record.get("sections") or [])]
    uid = str(record["uid"])
    row = {"uid": uid, "native_id": str(record.get("native_id") or uid.split(":", 1)[1]),
           "lang": record.get("lang")}
    chunks = sorted(layers.chunks_for_document(collection, row, sections), key=lambda c: c["seq"])
    captions = _captions(sections)
    out: dict[int, list] = {}
    for c, indices in zip(chunks, placed([c["text"] for c in chunks])):
        figs = []
        for i in indices:
            if i >= len(entries):
                break                     # the writer's alignment broken: place nothing past it
            e = entries[i]
            state = "not-carried" if not e.get("carried", True) else \
                ("held" if fig.bare(str(e["hash"])) in held else "not-held")
            figs.append({"hash": e["hash"], "media_type": e.get("media_type"),
                         "caption": None if red else (captions[i] if i < len(captions) else ""),
                         "state": state})
        out[int(c["seq"])] = figs
    return out


def place(collection, codex, shard_tree: Path, snapshot, hits: list[dict[str, Any]], *,
          red: bool = False) -> None:
    """Fill `figures` on each result of a collection with plates, one
    record read and re-chunked per uid."""
    by_uid: dict[str, list[dict[str, Any]]] = {}
    for h in hits:
        by_uid.setdefault(h["uid"], []).append(h)
    for uid, hs in by_uid.items():
        got = record_of(shard_tree, snapshot, uid, _order(codex))
        if got is None:
            continue                          # not held as a record here: `[]`, as before
        record, shard = got
        for h in hs:
            # how many plates the whole document carries: a passage that
            # talks about one placed elsewhere is linked to them (`mentions`)
            h["record_plates"] = len(record.get("figures") or [])
        try:
            per_seq = for_record(collection, record, held_hashes(shard_tree, shard), red=red)
        except Exception:   # noqa: BLE001 — a record that will not re-chunk places nothing
            continue
        for h in hs:
            try:
                seq = int(str(h["chunk_id"]).rsplit(":", 2)[1])
            except (KeyError, IndexError, ValueError):
                continue
            h["figures"] = per_seq.get(seq, [])


def figure_bytes(collection: str, uid: str, hash_: str, *, root: Path | None = None) -> tuple[bytes, str]:
    """07 §7.4: one plate's bytes and its media type, checked against the
    hash the record names. 403 for a red-class collection (its figures
    never travel, as its text never does, 04 §4.4); 404 for a plate not
    held, not named by the record, or not carried."""
    from app import collections as collections_mod
    from app.hold import collection as col, figures as fig
    from app.share import seeder
    root = Path(root) if root is not None else collections_mod.louise_root() / "collections"
    name, codex = col.resolve(root, collection)
    if codex.license.cls == "red":
        raise FigureNotServed(403, f"{name}: a red-class collection's figures are never served (04 §4.4)")
    from app import licensing
    held = collections_mod.by_name(name)
    if held is not None and licensing.state_for(held) == licensing.MISSING:
        raise FigureNotServed(403, f"{name}: its terms are not acknowledged; nothing of it is used (04 §4.4)")
    shard_tree = col.shard_dir_of(root, name)
    got = record_of(shard_tree, seeder.current_snapshot(shard_tree), uid, _order(codex))
    if got is None:
        raise FigureNotServed(404, f"{uid}: no record held")
    record, shard = got
    want = fig.bare(hash_)
    entry = next((e for e in record.get("figures") or [] if fig.bare(str(e["hash"])) == want), None)
    if entry is None:
        raise FigureNotServed(404, f"{uid} names no figure {want[:12]}…")
    if not entry.get("carried", True):
        raise FigureNotServed(404, f"{want[:12]}… is not carried by this collection: asking again never works")
    path = fig.path_for(shard_tree, shard.slice, shard.n)
    try:
        data = fig.read_figure(path, want)
    except (fig.FigureError, OSError):
        raise FigureNotServed(404, f"{want[:12]}… is not held here yet") from None
    media = str(entry.get("media_type") or "")
    if sniff(data) != media:
        raise FigureNotServed(404, f"{want[:12]}…: the bytes are not the {media} the record names")
    return data, media


def sniff(data: bytes) -> str | None:
    """08 §8.7's admitted types, by what the bytes are — never by what a
    record says they are."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


#: why a mentioned picture is not drawn, by its state
_NOT_DRAWN = {"not-held": "this node has not fetched it yet",
              "not-carried": "this collection does not carry it",
              None: "this collection's records name no plates"}


#: a picture named in prose, with no marker: "the frontispiece", "see
#: plate IV", "Fig. 3", a list of illustrations — the picture is elsewhere
#: in the document (München, 2026-10-21: 55261:6 "Portrait of Author |
#: Frontispiece", 46415:234 "His portrait forms the frontispiece").
#: Conservative on purpose: a word that is also ordinary prose ("portrait",
#: "map", "picture") is not enough on its own.
PROSE = re.compile(r"\b(?:illustrations?|illustrated|frontispiece|plates?\s+(?:[IVXLC]+|\d+)\b"
                   r"|fig(?:ure)?s?\.?\s*\d+|illustrazion[ei]|tavol[ae]\s+(?:[IVXLC]+|\d+)"
                   r"|planches?\s+(?:[IVXLC]+|\d+)|abbildungs?|tafel\s+(?:[IVXLC]+|\d+))",
                   re.IGNORECASE)


def mentions(text: str, figures: list | None, *, link: str | None,
             record_plates: int | None = None) -> list[dict[str, Any]]:
    """Every picture the passage MENTIONS, in order — each `[Illustration…]`
    marker in its text — with the picture or a link to it (operator,
    2026-10-21: "either the link or the picture shall be present"). The
    marker's figure (`figures[i]`, placed for this chunk, 09 §9.5's
    alignment) is drawn when this node holds it; otherwise the entry points
    at `link`, the origin's page that shows it, and says why it is not
    drawn here. `kind`: "img" (`hash`), "link" (`href`), or "none" when
    no page is known either — said, never silent."""
    out: list[dict[str, Any]] = []
    text = str(text or "")
    for i, m in enumerate(MARKER.finditer(text)):
        caption = text[m.end():].split("]", 1)[0].lstrip(":").strip()
        f = figures[i] if figures and i < len(figures) else None
        if f is not None and f.get("state") == "held":
            out.append({"kind": "img", "hash": f["hash"], "caption": f.get("caption") or caption})
            continue
        why = _NOT_DRAWN.get(f.get("state") if f is not None else None, _NOT_DRAWN[None])
        out.append({"kind": "link" if link else "none", "href": link, "caption": caption, "why": why})
    if not out and link and record_plates != 0 and PROSE.search(text):
        # named in prose, placed elsewhere: the document's pictures, one link
        # (`record_plates` 0: the document has none, and nothing is offered)
        n = f"{record_plates} " if record_plates else ""
        out.append({"kind": "link", "href": link, "caption": f"the document's {n}illustrations",
                    "why": "the passage mentions a picture that is elsewhere in the document"})
    return out

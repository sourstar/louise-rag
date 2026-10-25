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

Each entry: `{hash, media_type, bytes, caption, state}` — never the bytes
themselves. `held`: this peer's container for the record's shard holds
them; `not-held`: the collection carries them and this peer has not fetched
the member yet; `not-carried`: a record written before 08 §8.7's amendment
of 2026-10-25 named an image it did not carry (`carried: false`) — asking
again never works.

DISPLAY IS THE GATE (08 §8.7 amended 2026-10-25, operator: "Every plate").
Every plate the origin has is carried and served as opaque bytes; only
display decodes, and display admits the closed list — `image/jpeg`,
`image/png` — within the caps: 16 MiB of bytes, 64 megapixels DECLARED in
the header, read before any pixel is. `not_displayable` is that rule over
the bytes; `entry_not_displayable` the part of it a record entry answers
without them (its type and size), so a page never points an `<img>` at
what it already knows it may not show. A plate outside the rule is offered
only as a download: `application/octet-stream`, `attachment`, never
inline (app/ask/api.py `figure`).
"""
from __future__ import annotations

import re
import struct
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

from app.ask.part_cache import PartCache
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


#: figures container -> (its stat, the hashes it holds)
_HELD: "OrderedDict[str, tuple[tuple[int, int], frozenset[str]]]" = OrderedDict()
_held_lock = threading.Lock()


def held_hashes(shard_tree: Path, shard) -> set[str] | frozenset[str]:
    """The figure hashes (bare hex) this peer holds for the record's shard,
    read once per container while it stays the same file."""
    from app.hold import figures as fig
    path = fig.path_for(Path(shard_tree), shard.slice, shard.n)
    try:
        st = path.stat()
    except OSError:
        return set()
    stamp, key = (st.st_size, st.st_mtime_ns), str(path)
    with _held_lock:
        hit = _HELD.get(key)
        if hit is not None and hit[0] == stamp:
            _HELD.move_to_end(key)
            return hit[1]
    try:
        got = frozenset(fig.read_index(path))
    except Exception:   # noqa: BLE001 — a container that does not read holds nothing usable
        return set()
    with _held_lock:
        _HELD[key] = (stamp, got)
        while len(_HELD) > 256:
            _HELD.popitem(last=False)
    return got


def _sections(record: dict[str, Any]) -> list[tuple[str, str]]:
    return [(str(n or ""), str(b or "")) for n, b in (record.get("sections") or [])]


def _seq_indices(collection, record: dict[str, Any], sections) -> dict[int, list[int]]:
    """`seq -> indices into record["figures"]` for every chunk of the record."""
    from app import layers
    entries = record.get("figures") or []
    uid = str(record["uid"])
    row = {"uid": uid, "native_id": str(record.get("native_id") or uid.split(":", 1)[1]),
           "lang": record.get("lang")}
    chunks = sorted(layers.chunks_for_document(collection, row, sections), key=lambda c: c["seq"])
    out: dict[int, list[int]] = {}
    for c, indices in zip(chunks, placed([c["text"] for c in chunks])):
        keep = []
        for i in indices:
            if i >= len(entries):
                break                     # the writer's alignment broken: place nothing past it
            keep.append(i)
        out[int(c["seq"])] = keep
    return out


def _figures(entries: list, captions: list[str], indices: list[int], held, red: bool) -> list[dict]:
    from app.hold import figures as fig
    out = []
    for i in indices:
        e = entries[i]
        state = "not-carried" if not e.get("carried", True) else \
            ("held" if fig.bare(str(e["hash"])) in held else "not-held")
        out.append({"hash": e["hash"], "media_type": e.get("media_type"),
                    # the size the record names, so a page can say "not
                    # displayable: image/svg+xml, 12 KB" without the bytes
                    "bytes": e.get("bytes"),
                    "caption": None if red else (captions[i] if i < len(captions) else ""),
                    "state": state})
    return out


def for_record(collection, record: dict[str, Any], held: set[str], *, red: bool = False) -> dict[int, list]:
    """`seq -> figures` for every chunk of the record."""
    entries = record.get("figures")
    if entries is None:
        return {}
    sections = _sections(record)
    captions = _captions(sections)
    return {seq: _figures(entries, captions, idx, held, red)
            for seq, idx in _seq_indices(collection, record, sections).items()}


#: what `place` needs of a record, per part (app/ask/part_cache.py): the
#: placement is a re-chunk of the whole record (invariant 3), and reading
#: ten hits' records cost 0.82 s of frames, the containers' indexes 0.49 s
#: more (München, 2026-10-22); a change to the placement rule bumps this.
#: 2 (2026-10-25): an entry keeps the record's `bytes`, for display.
PLACEMENT_VERSION = 2


def _decode(v: dict) -> dict:
    if "s" in v:
        v["s"] = {int(k): idx for k, idx in v["s"].items()}
    return v


CACHE = PartCache("figure-placement", PLACEMENT_VERSION, decode=_decode)


def _placement(collection):
    """The part's records -> `{uid: {n, e?, c?, s?, x?}}`: `n` plates in the
    record, `e` its entries (null when it names none), `c` its captions,
    `s` the non-empty `seq -> indices`, `x` set when it will not re-chunk."""
    def make(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for r in records:
            entries = r.get("figures")
            v: dict[str, Any] = {"n": len(entries or [])}
            if entries is not None:
                sections = _sections(r)
                v["e"] = [{"hash": e["hash"], "media_type": e.get("media_type"),
                           "bytes": e.get("bytes"),
                           "carried": e.get("carried", True)} for e in entries]
                v["c"] = _captions(sections)
                try:
                    v["s"] = {str(k): idx for k, idx in _seq_indices(collection, r, sections).items() if idx}
                except Exception:   # noqa: BLE001 — as `place`: a record that will not re-chunk places nothing
                    v["x"] = 1
            out[str(r["uid"])] = v
        return out
    return make


def _placed_of(collection, shard_tree: Path, snapshot, uid: str, order: str):
    """`(placement, shard)` from the part's file, `(None, shard)` while the
    part has none (the record is read as before), None when no shard held
    holds the uid."""
    from app.hold import shard as shard_mod
    if snapshot is None or ":" not in uid:
        return None
    store = CACHE.store(shard_tree)
    for shard in snapshot.shards_of(uid.split(":", 1)[1], order):
        if not (Path(shard_tree) / shard_mod.shard_path(shard.slice, shard.n)).is_file():
            continue
        part = CACHE.get(shard_tree, store, shard, _placement(collection))
        if part is None:
            return None, shard
        if uid in part:
            return part[uid], shard
    return None


def place(collection, codex, shard_tree: Path, snapshot, hits: list[dict[str, Any]], *,
          red: bool = False) -> None:
    """Fill `figures` on each result of a collection with plates: from the
    part's placement file, or one record read and re-chunked per uid while
    the part has none."""
    by_uid: dict[str, list[dict[str, Any]]] = {}
    for h in hits:
        by_uid.setdefault(h["uid"], []).append(h)
    order = _order(codex)
    for uid, hs in by_uid.items():
        got = _placed_of(collection, shard_tree, snapshot, uid, order)
        if got is None:
            continue                          # not held as a record here: `[]`, as before
        v, shard = got
        if v is None:
            per_seq = _read_and_place(collection, shard_tree, snapshot, uid, order, hs, red)
        else:
            for h in hs:
                h["record_plates"] = v["n"]
            if "e" not in v or v.get("x"):
                continue
            held = held_hashes(shard_tree, shard)
            per_seq = {seq: _figures(v["e"], v["c"], idx, held, red) for seq, idx in v.get("s", {}).items()}
        if per_seq is None:
            continue
        for h in hs:
            try:
                seq = int(str(h["chunk_id"]).rsplit(":", 2)[1])
            except (KeyError, IndexError, ValueError):
                continue
            h["figures"] = per_seq.get(seq, [])


def _read_and_place(collection, shard_tree: Path, snapshot, uid: str, order: str, hs: list,
                    red: bool) -> dict[int, list] | None:
    got = record_of(shard_tree, snapshot, uid, order)
    if got is None:
        return None
    record, shard = got
    for h in hs:
        # how many plates the whole document carries: a passage that
        # talks about one placed elsewhere is linked to them (`mentions`)
        h["record_plates"] = len(record.get("figures") or [])
    if record.get("figures") is None:
        return None
    try:
        return for_record(collection, record, held_hashes(shard_tree, shard), red=red)
    except Exception:   # noqa: BLE001 — a record that will not re-chunk places nothing
        return None


def figure_bytes(collection: str, uid: str, hash_: str, *, root: Path | None = None) -> tuple[bytes, str]:
    """07 §7.4: one plate's bytes and the media type its record names,
    checked against the hash the record names. 403 for a red-class
    collection (its figures never travel, as its text never does, 04 §4.4);
    404 for a plate not held, not named by the record, or not carried.

    OPAQUE BYTES: nothing here decodes or trusts them beyond their hash.
    Whether they may be SHOWN is `not_displayable`, which the route asks
    before it answers inline (08 §8.7, amended 2026-10-25)."""
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
    got = record_of(shard_tree, seeder.cached_snapshot(shard_tree), uid, _order(codex))
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
    return data, str(entry.get("media_type") or "")


def sniff(data: bytes) -> str | None:
    """08 §8.7's displayable types, by what the bytes are — never by what a
    record says they are."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


#: 08 §8.7's closed list — what DISPLAY admits (amended 2026-10-25: storage
#: carries every type)
DISPLAYABLE = ("image/jpeg", "image/png")
#: 08 §8.7's caps, applied before decoding: bytes, and pixels DECLARED in
#: the header. Measured 2026-09-09 over 786,699 Gutenberg images: the
#: largest is 1.8 MB, so neither refuses an honest plate.
MAX_DISPLAY_BYTES = 16 << 20
MAX_DISPLAY_PIXELS = 64_000_000

#: JPEG start-of-frame markers, the ones whose segment carries the frame's
#: dimensions (ITU-T T.81 Table B.1): C0–CF except DHT (C4), JPG (C8), DAC (CC)
_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def declared_size(data: bytes, media: str) -> tuple[int, int] | None:
    """`(width, height)` as the image's HEADER declares them, or None when
    the header does not say — read before any pixel, which is the point:
    the megapixel cap stops a decompression bomb only if it is applied to
    the declared dimensions, before a decoder is handed the bytes.

    PNG: the IHDR chunk, which the format requires first (RFC 2083 §4.1.1).
    JPEG: the first start-of-frame segment, walked to by segment length.
    Hand-written because it is two header reads and the standard library
    has neither; the standard alternative is Pillow's `Image.open`, which
    reads the header lazily and enforces `MAX_IMAGE_PIXELS` — worth
    adopting the day display admits a third type, at the cost of a new
    dependency (Pillow is not in requirements.txt today)."""
    if media == "image/png":
        if len(data) >= 24 and data[12:16] == b"IHDR":
            w, h = struct.unpack(">II", data[16:24])
            return (w, h) if w and h else None
        return None
    if media == "image/jpeg":
        i = 2
        while i + 4 <= len(data):
            if data[i] != 0xFF:
                return None
            m = data[i + 1]
            if m == 0xFF:                       # fill byte
                i += 1
                continue
            if m in (0x01, 0xD8) or 0xD0 <= m <= 0xD7:
                i += 2                          # a marker with no segment
                continue
            if m in (0xD9, 0xDA):
                return None                     # end of image / scan before a frame header
            length = int.from_bytes(data[i + 2:i + 4], "big")
            if m in _SOF:
                if i + 9 > len(data):
                    return None
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return (w, h) if w and h else None
            if length < 2:
                return None
            i += 2 + length
        return None
    return None


def _size(n: int | None) -> str:
    if n is None:
        return "size unknown"
    for unit, scale in (("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} bytes"


def entry_not_displayable(entry: dict) -> str | None:
    """Why a record's figure entry may not be shown, from what the entry
    itself says — its type and its size — or None when nothing in it
    forbids display. Not the whole rule: the bytes' real type and their
    declared dimensions are `not_displayable`'s, at the route."""
    media = entry.get("media_type")
    size = entry.get("bytes")
    if media is not None and media not in DISPLAYABLE:
        return f"{media}, {_size(size if isinstance(size, int) else None)}: not a type this page shows"
    if isinstance(size, int) and size > MAX_DISPLAY_BYTES:
        return f"{media or 'image'}, {_size(size)}: over the 16 MiB display cap"
    return None


def not_displayable(data: bytes, media: str) -> str | None:
    """08 §8.7's display rule over the bytes, or None when they may be
    shown inline: a type on the closed list, which the bytes ACTUALLY ARE
    (a disagreement refuses display, never the reverse), within 16 MiB, and
    whose header declares at most 64 megapixels."""
    size = _size(len(data))
    if media not in DISPLAYABLE:
        return f"{media or 'an unnamed type'}, {size}: not a type this page shows"
    if sniff(data) != media:
        return f"{size}: the bytes are not the {media} the record names"
    if len(data) > MAX_DISPLAY_BYTES:
        return f"{media}, {size}: over the 16 MiB display cap"
    dims = declared_size(data, media)
    if dims is None:
        return f"{media}, {size}: its header declares no dimensions"
    if dims[0] * dims[1] > MAX_DISPLAY_PIXELS:
        return f"{media}, {size}: {dims[0]}×{dims[1]} is over the 64-megapixel display cap"
    return None


def download_name(hash_: str, media: str) -> str:
    """The file name a download is offered under: the plate's hash and the
    extension of the type its record names — `.bin` for one we do not know."""
    from app.hold import figures as fig
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/svg+xml": "svg", "image/gif": "gif",
           "image/webp": "webp", "image/tiff": "tif"}.get(media, "bin")
    return f"{fig.bare(hash_)[:16]}.{ext}"


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
    drawn here. `kind`: "img" (`hash`), "link" (`href`), "download"
    (`hash`, `media_type`, `bytes`, `size`, `why`: held, but not displayable under
    08 §8.7 — an SVG, a GIF, a plate over a cap — so offered as a file and
    never drawn), or "none" when no page is known either — said, never
    silent."""
    out: list[dict[str, Any]] = []
    text = str(text or "")
    for i, m in enumerate(MARKER.finditer(text)):
        caption = text[m.end():].split("]", 1)[0].lstrip(":").strip()
        f = figures[i] if figures and i < len(figures) else None
        if f is not None and f.get("state") == "held":
            why = entry_not_displayable(f)
            if why is not None:
                out.append({"kind": "download", "hash": f["hash"], "caption": f.get("caption") or caption,
                            "media_type": f.get("media_type"), "bytes": f.get("bytes"),
                            "size": _size(f["bytes"] if isinstance(f.get("bytes"), int) else None),
                            "why": f"not displayable: {why}"})
                continue
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

"""tests/ask/test_figures.py — figures on results (05 §5.3) and their bytes
(07 §7.4 `GET /api/ask/figure`): a chunk's plates are the entries whose
`[Illustration…]` markers fall inside it — the Nth marker is `figures[N]`,
an overlap's markers keep the index the previous chunk gave them — each
`held` · `not-held` · `not-carried`, never the bytes; the route serves a
held, carried plate checked against its hash and its real type, 404 for
anything else and 403 for a red collection. And the snapshot's uid → shard
lookup the two stand on, in the collection's own id order and per slice."""
import hashlib
import struct
import zlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import collections as collections_mod
from app.ask import api, figures as F
from app.hold import collection as col, figures as fig, snapshot as sn
from app.hold.codex import Chunker, Layer, License
from app.pack import records as pack_records
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.hold.conftest import TERMS, a_root, pubmed

def png(width: int, height: int, tail: bytes) -> bytes:
    """A PNG's signature and IHDR — all a display check reads (08 §8.7:
    the caps are applied to the DECLARED dimensions, before any pixel)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
            + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr)) + tail)


PLATE_A = png(640, 480, b"plate A")
PLATE_C = png(640, 480, b"plate C")
H_A = "sha256:" + hashlib.sha256(PLATE_A).hexdigest()
H_B = "sha256:" + "bb" * 32
H_C = "sha256:" + hashlib.sha256(PLATE_C).hexdigest()
TEXT = ("Opening words here.\n\n[Illustration: The frontispiece]\n\nMore words follow in the first chapter."
        "\n\n[Illustration: A map]\n\nClosing words.\n\n[Illustration: The frontispiece]\n\nThe end, almost."
        "\n\n[Illustration]\n\nThe end.")
FIGURES = [{"hash": H_A, "media_type": "image/png", "bytes": len(PLATE_A), "seq": 0, "carried": True},
           {"hash": H_B, "media_type": "image/gif", "bytes": 10, "seq": 1, "carried": False},
           {"hash": H_A, "media_type": "image/png", "bytes": len(PLATE_A), "seq": 2, "carried": True},
           {"hash": H_C, "media_type": "image/png", "bytes": len(PLATE_C), "seq": 3, "carried": True}]
FIXED = (Layer(Chunker("fixed", 1, {"tokens": 12, "overlap": 4}), "default"),)


def _record(native: str, figures=FIGURES, text=TEXT) -> dict:
    return {"uid": f"pubmed:{native}", "native_id": native, "lang": "en", "title": "A Book", "creators": [],
            "date": None, "subjects": [], "keys": [], "sections": [["I", text]], "figures": figures,
            "extra": {}, "origin": f"{native}.xml", "content_hash": "ab" * 16, "origin_hash": "cd" * 16}


def _shard(slice_: str, n: int, first: str, last: str) -> sn.Shard:
    return sn.Shard(slice=slice_, n=n, root="sha256:" + "11" * 32, records_root="sha256:" + "22" * 32,
                    first_id=first, last_id=last, records=1, chunks=1, chunks_root="sha256:" + "33" * 32,
                    tokens=1, bytes=1)


def an_illustrated_copy(node, *, cls="green"):  # noqa: F811
    codex = pubmed(record_line=2, layers=FIXED, license=License(cls, None if cls == "green" else TERMS))
    a_root(node, codex)
    name = col.name_of(node, codex.collection_id)
    d = col.shard_dir_of(node, name)
    (d / "records/year=2023").mkdir(parents=True, exist_ok=True)
    # numeric order: "10" is in the SECOND shard, though as text it sorts before "9"
    pack_records.write_shard(d / "records/year=2023/00001.jsonl.zst", [_record("2", [], "Nothing here.")],
                             record_line=2)
    pack_records.write_shard(d / "records/year=2023/00002.jsonl.zst", [_record("10")], record_line=2)
    fig.write_container(fig.path_for(d, "year=2023", 2), {H_A: PLATE_A})
    snap = sn.Snapshot(collection_id=codex.collection_id, snapshot_id="2026-08", previous=None,
                       shards=(_shard("year=2023", 1, "1", "9"), _shard("year=2023", 2, "10", "20")),
                       delta=sn.Delta((), (), ()), files={})
    (d / "snapshots").mkdir(exist_ok=True)
    (d / "snapshots/2026-08.json").write_bytes(snap.canonical())
    return codex, name, d, snap


def test_the_snapshot_finds_a_uid_s_shard_in_the_collection_s_order_and_slice():
    s = sn.Snapshot(collection_id="sha256:" + "0" * 64, snapshot_id="2026-08", previous=None,
                    shards=(_shard("lang=en", 1, "1", "9"), _shard("lang=en", 2, "10", "20"),
                            _shard("lang=fr", 1, "5", "15")), delta=sn.Delta((), (), ()), files={})
    assert [x.n for x in s.shards_of("10", "numeric")] == [2, 1] and s.shard_of("10", "numeric") is None, \
        "two slices can hold 10: only the record knows which"
    assert s.shard_of("3", "numeric").n == 1 and s.shard_of("18", "numeric").n == 2
    assert [(x.slice, x.n) for x in s.shards_of("10", "bytes")] == [("lang=en", 1), ("lang=en", 2)], \
        "as text, 10 sorts before 9: the range 1..9 claims it too, which is what the order fixes"
    assert s.shards_of("x", "numeric") == [], "an id the order cannot place is in no shard"


def test_markers_in_an_overlap_keep_the_index_the_previous_chunk_gave_them():
    texts = ["a [Illustration: one] b [Illustration: two]", "[Illustration: two] c [Illustration: three]",
             "d e f", "tail [Illus", "[Illustration: four] g"]
    assert F.placed(texts) == [[0, 1], [1, 2], [], [], [3]], \
        "the overlap's marker repeats index 1; a marker cut at the window's edge is counted where it is whole"
    assert F._overlap("abc[Illustration: two]", "[Illustration: two] c") == len("[Illustration: two]")
    assert F._overlap("abc", "xyz") == 0


def test_a_chunk_carries_the_plates_whose_markers_fall_inside_it(node):  # noqa: F811
    codex, name, d, snap = an_illustrated_copy(node)
    record, shard = F.record_of(d, snap, "pubmed:10", "numeric")
    assert shard.n == 2, "numeric order: the second shard"
    collection = SimpleNamespace(name=name, layers=codex.layers)
    per_seq = F.for_record(collection, record, F.held_hashes(d, shard))
    assert len(per_seq) > 2, "small windows: several chunks"
    from app import layers
    chunks = layers.chunks_for_document(collection, {"uid": "pubmed:10", "native_id": "10"},
                                        [tuple(s) for s in record["sections"]])
    seen: dict[int, dict] = {}
    for c in chunks:
        figs = per_seq[c["seq"]]
        in_text = [c["text"][m.end():].split("]", 1)[0].lstrip(":").strip() for m in F.MARKER.finditer(c["text"])]
        assert [f["caption"] for f in figs] == in_text, (c["text"], figs)
        for f in figs:
            assert set(f) == {"hash", "media_type", "bytes", "caption", "state"}, "never the bytes themselves"
    placed = [f for c in chunks for f in per_seq[c["seq"]]]
    states = {(f["hash"], f["caption"]): f["state"] for f in placed}
    assert states == {(H_A, "The frontispiece"): "held", (H_B, "A map"): "not-carried", (H_C, ""): "not-held"}
    red = F.for_record(collection, record, set(), red=True)
    assert all(f["caption"] is None for figs in red.values() for f in figs), "a red record's caption is text"
    # place() on results: by chunk id
    hits = [{"uid": "pubmed:10", "chunk_id": f"pubmed:10:{c['seq']}:1", "figures": []} for c in chunks]
    F.place(collection, codex, d, snap, hits)
    assert [h["figures"] for h in hits] == [per_seq[c["seq"]] for c in chunks]
    plain = [{"uid": "pubmed:2", "chunk_id": "pubmed:2:0:1", "figures": []}]
    F.place(collection, codex, d, snap, plain)
    assert plain[0]["figures"] == [], "a chunk with no markers: []"


def test_the_route_serves_a_held_carried_plate_and_nothing_else(node, monkeypatch):  # noqa: F811
    codex, name, d, snap = an_illustrated_copy(node)
    monkeypatch.setattr(collections_mod, "louise_root", lambda: node.parent)
    data, media = F.figure_bytes(codex.collection_id, "pubmed:10", H_A)
    assert (data, media) == (PLATE_A, "image/png")
    for h, why in ((H_B, "not carried"), (H_C, "not held"), ("sha256:" + "ee" * 32, "names no figure")):
        with pytest.raises(F.FigureNotServed, match=why) as e:
            F.figure_bytes(name, "pubmed:10", h)
        assert e.value.status == 404
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    r = client.get("/api/ask/figure", params={"collection": name, "uid": "pubmed:10", "hash": H_A})
    assert r.status_code == 200 and r.content == PLATE_A and r.headers["content-type"] == "image/png"
    r = client.get("/api/ask/figure", params={"collection": name, "uid": "pubmed:10", "hash": H_B})
    assert r.status_code == 404 and "not carried" in r.json()["error"]
    # bytes that are not the type the record names are not SHOWN as it (08
    # §8.7 amended 2026-10-25: the disagreement refuses display, and the
    # plate is still the collection's, offered as a file)
    fig.write_container(fig.path_for(d, "year=2023", 2), {H_A: PLATE_A, H_C: PLATE_C})
    record = _record("10", [dict(FIGURES[3], media_type="image/jpeg")])
    pack_records.write_shard(d / "records/year=2023/00002.jsonl.zst", [record], record_line=2)
    pack_records.reset_caches()
    assert F.figure_bytes(name, "pubmed:10", H_C) == (PLATE_C, "image/jpeg")
    r = client.get("/api/ask/figure", params={"collection": name, "uid": "pubmed:10", "hash": H_C})
    assert r.status_code == 415 and "not the image/jpeg" in r.json()["error"] and r.content != PLATE_C


def test_a_red_collection_s_figures_are_never_served(node, monkeypatch):  # noqa: F811
    codex, name, d, snap = an_illustrated_copy(node, cls="red")
    monkeypatch.setattr(collections_mod, "louise_root", lambda: node.parent)
    with pytest.raises(F.FigureNotServed, match="never served") as e:
        F.figure_bytes(name, "pubmed:10", H_A)
    assert e.value.status == 403


def test_every_mention_is_drawn_or_linked_in_order():
    """Operator, 2026-10-21: a picture the text mentions is shown, or a link
    to it is. The Nth marker is the chunk's Nth placed figure (09 §9.5)."""
    from app.ask.figures import mentions
    text = "a [Illustration: One] b [Illustration] c [Illustration: Three] d"
    figs = [{"hash": "h1", "caption": "One", "state": "held"},
            {"hash": "h2", "caption": "", "state": "not-carried"}]
    got = mentions(text, figs, link="https://origin/x")
    assert [g["kind"] for g in got] == ["img", "link", "link"]
    assert got[0]["hash"] == "h1" and got[1]["why"] == "this collection does not carry it"
    assert got[2]["caption"] == "Three" and got[2]["why"] == "this collection's records name no plates"
    assert all(g["href"] == "https://origin/x" for g in got[1:])
    assert [g["kind"] for g in mentions(text, None, link=None)] == ["none"] * 3, "said, never silent"
    assert mentions("no pictures here", figs, link="x") == []


def test_a_picture_named_in_prose_links_the_documents_pictures():
    """München, 2026-10-21: every marker was placed and held; what showed no
    picture was prose about one elsewhere in the book."""
    from app.ask.figures import mentions
    got = mentions("His portrait forms the frontispiece of this book.", [], link="https://o/x", record_plates=12)
    assert got == [{"kind": "link", "href": "https://o/x", "caption": "the document's 12 illustrations",
                    "why": "the passage mentions a picture that is elsewhere in the document"}]
    assert mentions("Portrait of Author | Frontispiece", None, link="https://o/x")[0]["caption"] == \
        "the document's illustrations", "plates unknown (record not held here): still linked"
    assert mentions("see Plate IV and Fig. 3", [], link="u", record_plates=4)[0]["kind"] == "link"
    assert mentions("the frontispiece", [], link="u", record_plates=0) == [], "a document with no pictures"
    assert mentions("a portrait of the sea, a map of the town", [], link="u", record_plates=5) == [], \
        "ordinary words alone are not a mention"
    assert mentions("the frontispiece", [], link=None, record_plates=3) == [], "no page to send to"


def test_a_parts_placement_is_built_once_then_looked_up(node, monkeypatch):  # noqa: F811
    """München, 2026-10-22: the plates of ten hits read their records' frames
    and the containers' indexes on every search (1.4 s of 10 s). The first
    search that meets a part places as before and queues it; the file built
    off its thread then gives the same plates without reading a record."""
    codex, name, d, snap = an_illustrated_copy(node)
    collection = SimpleNamespace(name=name, layers=codex.layers)
    record, shard = F.record_of(d, snap, "pubmed:10", "numeric")
    seqs = sorted(F.for_record(collection, record, set()))

    def hits():
        return [{"uid": u, "chunk_id": f"{u}:{s}:1", "figures": []}
                for u, s in [("pubmed:10", s) for s in seqs] + [("pubmed:2", 0)]]
    first = hits()
    F.place(collection, codex, d, snap, first)
    F.CACHE.queue.join()
    files = sorted(p.name for p in F.CACHE.store(d).glob("v*.json"))
    assert [f.split("-1111")[0] for f in files] == [f"v{F.PLACEMENT_VERSION}-year=2023-00001",
                                                     f"v{F.PLACEMENT_VERSION}-year=2023-00002"], files
    F.CACHE.clear()

    def no_read(*a, **k):
        raise AssertionError("a record was read")
    monkeypatch.setattr(pack_records, "read_record", no_read)
    monkeypatch.setattr(pack_records, "iter_records", no_read)
    again = hits()
    F.place(collection, codex, d, snap, again)
    assert again == first
    assert [h["record_plates"] for h in again] == [4] * len(seqs) + [0]
    assert any(h["figures"] for h in again), "the fixture places plates"


# ---- 08 §8.7 amended 2026-10-25 ("Every plate"): display is the gate ------

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
BOMB = png(10_000, 10_000, b"a few hundred kilobytes that decode to gigabytes")
H_SVG = "sha256:" + hashlib.sha256(SVG).hexdigest()
H_BOMB = "sha256:" + hashlib.sha256(BOMB).hexdigest()


def test_the_display_rule_is_the_closed_list_within_the_caps():
    assert F.not_displayable(PLATE_A, "image/png") is None
    jpeg = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xc0\x00\x11\x08\x01\xe0\x02\x80\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01")
    assert F.declared_size(jpeg, "image/jpeg") == (640, 480)
    assert F.not_displayable(jpeg, "image/jpeg") is None
    assert "not a type this page shows" in F.not_displayable(SVG, "image/svg+xml")
    assert "not the image/png" in F.not_displayable(SVG, "image/png"), "the bytes decide, never the record"
    assert "64-megapixel" in F.not_displayable(BOMB, "image/png"), "the DECLARED dimensions, before any pixel"
    big = png(100, 100, b"\0" * (F.MAX_DISPLAY_BYTES + 1))
    assert "16 MiB" in F.not_displayable(big, "image/png")
    assert "declares no dimensions" in F.not_displayable(b"\x89PNG\r\n\x1a\n" + b"short", "image/png")
    # what an entry alone answers: its type and its size
    assert F.entry_not_displayable({"media_type": "image/svg+xml", "bytes": 12_000}).startswith(
        "image/svg+xml, 11.7 KB")
    assert "16 MiB" in F.entry_not_displayable({"media_type": "image/png", "bytes": F.MAX_DISPLAY_BYTES + 1})
    assert F.entry_not_displayable({"media_type": "image/png", "bytes": 10}) is None


def test_a_plate_display_refuses_is_carried_and_served_only_as_a_download(node, monkeypatch):  # noqa: F811
    """Stored and shared like any plate; never inline, never as
    image/svg+xml: without `download` the route answers 415 and no bytes, so
    an <img> pointed at it gets nothing to decode; with it, octet-stream and
    `attachment`, so a browser saves it and never renders it."""
    codex, name, d, snap = an_illustrated_copy(node)
    monkeypatch.setattr(collections_mod, "louise_root", lambda: node.parent)
    fig.write_container(fig.path_for(d, "year=2023", 2), {H_A: PLATE_A, H_SVG: SVG, H_BOMB: BOMB})
    text = "Words.\n\n[Illustration: A plan]\n\nMore.\n\n[Illustration: A bomb]\n\nEnd."
    entries = [{"hash": H_SVG, "media_type": "image/svg+xml", "bytes": len(SVG), "seq": 0, "carried": True},
               {"hash": H_BOMB, "media_type": "image/png", "bytes": len(BOMB), "seq": 1, "carried": True}]
    pack_records.write_shard(d / "records/year=2023/00002.jsonl.zst", [_record("10", entries, text)],
                             record_line=2)
    pack_records.reset_caches()
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    for h, raw, why, ext in ((H_SVG, SVG, "image/svg+xml", "svg"), (H_BOMB, BOMB, "64-megapixel", "png")):
        assert F.figure_bytes(name, "pubmed:10", h)[0] == raw, "carried and held: the bytes are there"
        r = client.get("/api/ask/figure", params={"collection": name, "uid": "pubmed:10", "hash": h})
        assert r.status_code == 415 and why in r.json()["error"] and raw not in r.content
        assert not r.headers["content-type"].startswith("image/")
        r = client.get("/api/ask/figure", params={"collection": name, "uid": "pubmed:10", "hash": h,
                                                   "download": 1})
        assert r.status_code == 200 and r.content == raw, "byte for byte"
        assert r.headers["content-type"] == "application/octet-stream"
        assert r.headers["content-disposition"] == f'attachment; filename="{fig.bare(h)[:16]}.{ext}"'
        assert r.headers["x-content-type-options"] == "nosniff"
    # the display path: held, and the SVG said not displayable with its type and size
    collection = SimpleNamespace(name=name, layers=codex.layers)
    record, shard = F.record_of(d, snap, "pubmed:10", "numeric")
    placed = [f for figs in F.for_record(collection, record, F.held_hashes(d, shard)).values() for f in figs]
    assert [f["state"] for f in placed] == ["held", "held"]
    shown = F.mentions("[Illustration: A plan] and [Illustration: A bomb]", placed, link="https://o/x")
    assert shown[0]["kind"] == "download" and shown[0]["media_type"] == "image/svg+xml"
    assert shown[0]["bytes"] == len(SVG) and shown[0]["why"].startswith("not displayable: image/svg+xml")
    assert shown[1]["kind"] == "img", "its header is read at the route: the <img> gets a 415, never the bytes"

"""tests/ask/test_figures.py — figures on results (05 §5.3) and their bytes
(07 §7.4 `GET /api/ask/figure`): a chunk's plates are the entries whose
`[Illustration…]` markers fall inside it — the Nth marker is `figures[N]`,
an overlap's markers keep the index the previous chunk gave them — each
`held` · `not-held` · `not-carried`, never the bytes; the route serves a
held, carried plate checked against its hash and its real type, 404 for
anything else and 403 for a red collection. And the snapshot's uid → shard
lookup the two stand on, in the collection's own id order and per slice."""
import hashlib
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

PLATE_A = b"\x89PNG\r\n\x1a\n" + b"plate A"
PLATE_C = b"\x89PNG\r\n\x1a\n" + b"plate C"
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
            assert set(f) == {"hash", "media_type", "caption", "state"}, "never the bytes"
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
    # bytes that are not the type the record names are not served as it
    fig.write_container(fig.path_for(d, "year=2023", 2), {H_A: PLATE_A, H_C: PLATE_C})
    record = _record("10", [dict(FIGURES[3], media_type="image/jpeg")])
    pack_records.write_shard(d / "records/year=2023/00002.jsonl.zst", [record], record_line=2)
    pack_records.reset_caches()
    with pytest.raises(F.FigureNotServed, match="not the image/jpeg"):
        F.figure_bytes(name, "pubmed:10", H_C)


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

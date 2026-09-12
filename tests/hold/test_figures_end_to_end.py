"""A plates collection, built and checked the way an operator builds one.

Every artefact here comes from the real writers over a real epub corpus:
`pick_form` chooses the epub, `pg_epub.figures` lists the appearances,
`pack build` writes the shard AND its container, `snapshot create` carries
the container's root, and `hold check`'s `figures` row re-hashes the
plates. A hand-built fixture would prove the fixture agrees with the
checker and nothing about the collection.
"""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from app.hold import check as chk, codex as cx, collection as col, \
    drag as drag_mod, records as rec, snapshot as snap
from app.hold import figures as fig
from app.pack import records as pack_records
from tests.hold.conftest import KEY, NOW, PLATFORM
from tests.hold.test_admit import made, space_embed  # noqa: F401
from tests.hold.test_vectors_export import fake_embed, recipe
from tests.test_plugins_gutenberg_epub import build as build_epub, page

SEED = b"\x07" * 32

# Distinguishable JPEG-shaped bytes. `_media_type` reads the NAME; 08 §8.7's
# check against the actual bytes belongs to admission, not to this pass.
PLATES = {f"plate{i}.jpg": bytes([0xFF, 0xD8, 0xFF, 0xE0]) + bytes([i]) * 512
          for i in range(1, 4)}


def _book(root: Path, book_id: int, plate_names) -> None:
    d = root / str(book_id)
    d.mkdir(parents=True, exist_ok=True)
    body = "<p>Before the plates.</p>" + "".join(
        f'<p><img src="{n}" alt="Plate {i}"/></p>'
        for i, n in enumerate(plate_names, 1)) + "<p>After them.</p>"
    build_epub(d / f"pg{book_id}-images.epub",
               [("c1.xhtml", page(body))],
               images=[(n, PLATES[n]) for n in dict.fromkeys(plate_names)])
    (d / f"pg{book_id}.rdf").write_text(
        '<?xml version="1.0"?><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:dcterms="http://purl.org/dc/terms/">'
        f'<pgterms:ebook rdf:about="ebooks/{book_id}" '
        'xmlns:pgterms="http://www.gutenberg.org/2009/pgterms/">'
        f'<dcterms:title>Book {book_id}</dcterms:title>'
        '<dcterms:language><rdf:Description><rdf:value>en</rdf:value>'
        '</rdf:Description></dcterms:language>'
        '</pgterms:ebook></rdf:RDF>', encoding="utf-8")


@pytest.fixture
def plates_corpus(tmp_path) -> Path:
    root = tmp_path / "plates-corpus"
    _book(root, 20001, ["plate1.jpg", "plate2.jpg"])
    # the repeat: one plate, three appearances — `figures[N]` alignment
    _book(root, 20002, ["plate3.jpg", "plate3.jpg", "plate3.jpg"])
    return root


def _recipe_2() -> dict:
    """The fixture recipe, moved to `record_line` 2 — which also means the
    plan has to name `figures`, because `_validate_validation` refuses a
    plates collection that never checks its plates."""
    r = json.loads(json.dumps(recipe()))
    r["record_line"] = 2
    r["cleaning_version"] = 2
    v = r.setdefault("validation", {})
    v["checks"] = sorted(set(v.get("checks") or []) | {"roots", "records", "figures"})
    v.setdefault("sample", {})["figures"] = {"shards": "1"}
    v["sample"].setdefault("records", {"shards": "1"})
    return r


@pytest.fixture
def plates_drag(isolated_config, tmp_path, plates_corpus, monkeypatch):
    monkeypatch.delenv("EMBED_URL", raising=False)
    root = tmp_path / "collections"
    col.create_from_recipe(root, "gutenberg", _recipe_2(),
                           platform_image=PLATFORM, embed=fake_embed,
                           name="plates", key=KEY, now=NOW)
    rec.build(root, "plates", corpus_dir=plates_corpus, log=lambda *_: None,
              records_per_shard=10)
    rec.write_validation(root, "plates")
    snapshot = snap.create(root, "plates", log=lambda *_: None)
    return root, snapshot, col.read(root, "plates")


# ---- what the build produced ---------------------------------------------

def test_the_records_carry_figures_and_the_container_holds_them(plates_drag):
    root, snapshot, codex = plates_drag
    assert codex.record_line == 2
    drag = root / "plates" / "drag"

    named, by_uid = set(), {}
    for s in snapshot.shards:
        for r in pack_records.iter_records(drag / s.path):
            assert "figures" in r, "shape 2 writes the key on every line"
            by_uid[r["uid"]] = r["figures"]
            named |= {fig.bare(f["hash"]) for f in r["figures"]}

    # 20001 has two distinct plates; 20002 shows ONE plate three times
    assert len(by_uid["gutenberg:20001"]) == 2
    assert len(by_uid["gutenberg:20002"]) == 3
    assert len({f["hash"] for f in by_uid["gutenberg:20002"]}) == 1
    assert [f["seq"] for f in by_uid["gutenberg:20002"]] == [0, 1, 2]

    held = set()
    for s in snapshot.shards:
        p = drag / drag_mod.figures_path(s.slice, s.n)
        assert p.is_file(), "a container is written with its shard"
        held |= set(fig.read_index(p))
    assert held == named, "stored once each, and every named plate is there"
    assert len(held) == 3


def test_a_plate_reads_back_as_the_origin_shipped_it(plates_drag):
    root, snapshot, _codex = plates_drag
    drag = root / "plates" / "drag"
    want = {hashlib.sha256(b).hexdigest(): b for b in PLATES.values()}
    got = {}
    for s in snapshot.shards:
        for h, data in fig.walk(drag / drag_mod.figures_path(s.slice, s.n)):
            got[h] = data
    assert got == want


def test_content_hash_never_covers_a_plate(plates_drag):
    """09 §9.5's rule, and the reason a collection that gains plates keeps
    every text vector it had: `content_hash` is over title and sections,
    and the caption is in sections like any other text."""
    root, snapshot, _codex = plates_drag
    drag = root / "plates" / "drag"
    for s in snapshot.shards:
        for r in pack_records.iter_records(drag / s.path):
            sections = [tuple(x) for x in r["sections"]]
            assert rec.content_hash(r["title"], sections) == r["content_hash"]
            assert "Plate 1" in json.dumps(r["sections"]) or \
                "Plate 3" in json.dumps(r["sections"]), "the caption is indexed"


# ---- the snapshot ---------------------------------------------------------

def test_the_snapshot_prices_the_plates_before_a_byte_moves(plates_drag):
    root, snapshot, _codex = plates_drag
    assert snapshot.figures, "a plates collection's snapshot carries them"
    by_shard = {(f.slice, f.n): f for f in snapshot.figures}
    assert set(by_shard) == {(s.slice, s.n) for s in snapshot.shards}
    drag = root / "plates" / "drag"
    for (slice_, n), entry in by_shard.items():
        p = drag / drag_mod.figures_path(slice_, n)
        assert entry.bytes == p.stat().st_size
        assert entry.root == drag_mod.pieces_root(p.read_bytes())
        assert entry.figures == len(fig.read_index(p))

    doc = json.loads((drag / drag_mod.snapshot_path(snapshot.snapshot_id)).read_bytes())
    assert doc["version"] == 2
    assert doc["figures"] == [f.to_dict() for f in
                              sorted(snapshot.figures,
                                     key=lambda f: (f.slice.encode(), f.n))]


def test_a_version_1_snapshot_still_loads(plates_drag):
    """"snapshots already written stay 1, and a chain links by `previous`
    hash rather than by version, so nothing is re-issued" (09 §9.4)."""
    root, snapshot, _codex = plates_drag
    doc = json.loads(snapshot.canonical())
    doc["version"] = 1
    del doc["figures"]
    old = snap._load(json.dumps(doc).encode())
    assert old.figures == (), "absent IS version 1, never a guess"


# ---- the check ------------------------------------------------------------

def _ctx(root, codex, snapshot):
    return chk.Ctx(root=root, name="plates", codex=codex, snapshot=snapshot,
                   seed=SEED)


def test_the_figures_check_passes_on_what_the_build_wrote(plates_drag):
    root, snapshot, codex = plates_drag
    out = chk.check_figures(_ctx(root, codex, snapshot))
    assert out.result == chk.PASS, out.why
    assert out.detail["named"] == out.detail["held"] == 3
    assert out.detail["failed"] == []


def test_not_holding_the_plates_is_not_a_failure(plates_drag):
    """The containers are a member like any other: 04 §4.4 lets a peer ask
    to be spared 51 GB of images and still hold the records."""
    root, snapshot, codex = plates_drag
    for s in snapshot.shards:
        (root / "plates" / "drag" / drag_mod.figures_path(s.slice, s.n)).unlink()
    out = chk.check_figures(_ctx(root, codex, snapshot))
    assert out.result == chk.PASS, out.why
    assert out.detail["named"] == 3 and out.detail["held"] == 0
    assert out.detail["failed"] == []


def test_a_plate_whose_bytes_moved_FAILS(plates_drag):
    root, snapshot, codex = plates_drag
    s = snapshot.shards[0]
    p = root / "plates" / "drag" / drag_mod.figures_path(s.slice, s.n)
    entry = next(iter(fig.read_index(p).values()))
    raw = bytearray(p.read_bytes())
    raw[entry.offset + entry.frame_bytes - 1] ^= 0xFF
    p.write_bytes(bytes(raw))

    out = chk.check_figures(_ctx(root, codex, snapshot))
    assert out.result == chk.FAIL, out.why
    assert out.detail["failed"]


def test_a_record_line_1_collection_has_nothing_to_check(plates_drag):
    from dataclasses import replace
    root, snapshot, codex = plates_drag
    out = chk.check_figures(_ctx(root, replace(codex, record_line=1), snapshot))
    assert out.result == chk.INCONCLUSIVE
    assert "record_line" in out.why


# ---- what create refuses --------------------------------------------------

def test_a_plates_collection_must_plan_the_figures_check(isolated_config, tmp_path):
    """`validation` is inside `collection_id`, so the moment to add the
    check is before the id exists — and there is no other moment."""
    bad = _recipe_2()
    bad["validation"]["checks"] = [c for c in bad["validation"]["checks"]
                                   if c != "figures"]
    del bad["validation"]["sample"]["figures"]
    with pytest.raises(cx.CodexError, match="must name"):
        col.create_from_recipe(tmp_path / "c", "gutenberg", bad,
                               platform_image=PLATFORM, embed=fake_embed,
                               name="nope", key=KEY, now=NOW)

"""The cut amendment (operator, 2026-10-18; 09 §9.3 `ctx` and `cut`, §9.18;
02 §2.4 A1e; docs/notes/char-cut-truncation.md).

How an input longer than `ctx` is cut is part of the function its vector is
of, so a space states it: `cut: "tokens"`, from Codex version 8, written for a
space that declares it ALONE and inside `space_id`. Absent below, meaning the
platform's cut to about `ctx` x 4 characters every space before it was
calibrated through — so no Codex already written moves (tests/hold/
test_backend_engine.py pins them). A table crosses from a character space
into its token twin by `dev carry --recut` alone (A1e): the rows the
character cut left whole, and an archive's, stay; the rest are embedded again.
"""
import pytest

from app import collections as collections_mod, db, embeddings
from app.hold import carry, codex as cx, records as rec, seed, snapshot as sn
from app.hold.codex import CodexError, canonical_json

from .conftest import PLATFORM
from .test_backend_engine import _backend_recipe
from .test_carry import ARCHIVE, _create, _local, pair, quiet  # noqa: F401 — the fixture
from .test_template import from_recipe, recipe


def _token_recipe(**over):
    r = _backend_recipe(**over)
    r["space"] = dict(r["space"], cut=cx.TOKEN_CUT)
    return r


def _token_codex():
    return from_recipe("gutenberg", _token_recipe(), platform_image=PLATFORM)


# ---- the Codex ------------------------------------------------------------------------------

def test_a_space_that_cuts_by_tokens_is_written_at_8_in_a_space_of_its_own():
    c = _token_codex()
    assert c.version == cx.CUT_VERSION == 8 and c.space.cut == "tokens"
    raw = cx.dumps(c)
    again = cx.loads(raw)
    assert again.space.cut == "tokens" and cx.dumps(again) == raw
    chars = from_recipe("gutenberg", _backend_recipe(), platform_image=PLATFORM)
    assert chars.version == 6 and chars.space.cut is None
    assert "cut" not in chars.to_dict()["space"], "absent, never null, where none is declared"
    assert c.space_id != chars.space_id, "the cut is the function's: another space"
    assert "annotates" not in c.to_dict(), "8 is not an annotations Codex"


def test_version_8_declares_the_token_cut_and_nothing_else():
    d = _token_codex().to_dict()
    no_cut = dict(d, space={k: v for k, v in d["space"].items() if k != "cut"})
    with pytest.raises(CodexError, match="missing field.*cut"):
        cx.loads(canonical_json(no_cut))
    with pytest.raises(CodexError, match="declares its cut"):
        cx.loads(canonical_json(dict(d, space=dict(d["space"], cut="characters"))))
    with pytest.raises(CodexError, match="unknown field.*annotates"):
        cx.loads(canonical_json(dict(d, annotates=None)))


def test_below_version_8_the_cut_is_an_unknown_field():
    d = from_recipe("gutenberg", _backend_recipe(), platform_image=PLATFORM).to_dict()
    with pytest.raises(CodexError, match="unknown field.*cut"):
        cx.loads(canonical_json(dict(d, space=dict(d["space"], cut="tokens"))))


def test_the_token_cut_needs_an_encoder_behind_vega():
    """The platform holds no tokenizer (invariant 7): the encoder cuts, VeGa's
    wrapper in front of a backend engine."""
    r = recipe()
    r["space"] = dict(r["space"], cut="tokens")
    with pytest.raises(CodexError, match="names no backend engine"):
        cx.dumps(from_recipe("gutenberg", r, platform_image=PLATFORM))


def test_a_token_codex_sends_its_inputs_whole_with_the_window():
    c = _token_codex()
    r = collections_mod._codex_encoder({"url": "http://localai:30286", "api": "vega",
                                        "embed_model": "m", "query_model": "m"}, c, "x.toml")
    assert r.cut == embeddings.TOKEN_CUT == cx.TOKEN_CUT
    chars = from_recipe("gutenberg", _backend_recipe(), platform_image=PLATFORM)
    assert collections_mod._codex_encoder({"url": "http://localai:30286", "api": "vega",
                                           "embed_model": "m", "query_model": "m"},
                                          chars, "x.toml").cut == "characters"


def test_create_makes_the_reference_through_the_recipes_cut(monkeypatch):
    """`hold collection create` embeds the calibration set through the cut the
    recipe declares, or the reference vectors would name other inputs."""
    from app import louise
    seen = []
    monkeypatch.setattr(embeddings, "embed_calibration",
                        lambda recipe, texts, **kw: seen.append(recipe.cut) or [[0.0]])
    monkeypatch.setattr(louise, "check_reference_runs", lambda *a, **k: None)
    for space, want in ((_token_recipe()["space"], "tokens"), (recipe()["space"], "characters")):
        louise._embed_for_recipe(space, "http://localai:30286", "m", api="vega")(["a"])
        assert seen[-1] == want


def test_a_token_space_through_an_endpoint_not_behind_vega_sends_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(embeddings, "_request_vectors", lambda *a, **k: sent.append(a) or [])
    r = collections_mod.EmbedRecipe(url="http://enc:8080", api="openai", model="m",
                                    dtype="f16", ctx=512, dim=4, cut=embeddings.TOKEN_CUT)
    with pytest.raises(embeddings.EndpointMisconfigured, match="cuts by tokens"):
        embeddings._embed_batch(["x" * 5000], 10, r)
    assert not sent, "never the character cut in its place"


def test_the_recuts_dense_ranges_are_the_cuts():
    edges = [c for lo, hi in embeddings.DENSE_RANGES for c in (lo - 1, lo, hi, hi + 1)]
    for o in edges + [0x41, 0x3B1, 0x3042, 0x4E2D, 0xD55C]:
        assert embeddings._dense_script(chr(o)) == any(
            lo <= o <= hi for lo, hi in embeddings.DENSE_RANGES), hex(o)


# ---- A1e: the carry -------------------------------------------------------------------------

def _tokens_target(root):
    tok = _create(root, "tok", over={"space": _token_recipe()["space"]})
    assert tok.version == 8
    rec.write_validation(root, "tok")
    out = seed.seed_shards(root, "tok", "old", log=quiet)
    sn.create(root, "tok", snapshot_id=out["snapshot_id"], log=quiet)
    return tok


LONG = "a " * 1100                                     # 2,200 characters: over 512 x 4


def _rows(root, old, texts):
    """`texts[(doc index, seq)]` as rows of `old`'s table, its space pinned."""
    import numpy as np
    coll = _local(root, "old")
    con = db.connect(coll)
    try:
        docs = con.execute("SELECT uid, native_id, lang FROM documents ORDER BY rowid").fetchall()
        con.execute("INSERT INTO space(id, space_id, dim, pinned_at) VALUES(1, ?, ?, 0)",
                    (old.space_id, old.space.dim))
    finally:
        con.close()
    rng = np.random.default_rng(3)
    rows = [{"id": f"{docs[d]['uid']}:{seq}:1", "book_id": int(docs[d]["native_id"]),
             "lang": "en", "seq": seq, "chapter": "", "text": t,
             "vector": rng.standard_normal(old.space.dim).astype("float32")}
            for (d, seq), t in texts.items()]
    embeddings.upsert_chunks(rows, collection=coll, dtype=old.space.dtype)
    return [d["uid"] for d in docs]


def _marks(root, name):
    con = db.connect(_local(root, name))
    try:
        return dict(con.execute("SELECT uid, embed_done FROM documents").fetchall())
    finally:
        con.close()


def test_a_plain_carry_never_crosses_the_cut(pair):
    root, old, _new = pair
    _tokens_target(root)
    with pytest.raises(CodexError, match="Only `--recut`"):
        carry.carry(root, "old", "tok", log=quiet)


def test_recut_drops_what_the_character_cut_shortened_and_keeps_the_rest(pair):
    root, old, _new = pair
    _tokens_target(root)
    uids = _rows(root, old, {(0, 0): LONG, (0, 1): "short", (1, 0): LONG, (2, 0): "short"})
    carry.carry(root, "old", "tok", apply=True, recut=True, log=quiet)
    coll = _local(root, "tok")
    con = db.connect(coll)
    try:
        con.execute("UPDATE documents SET embed_done=1")
        con.execute("INSERT INTO chunk_verification(chunk_id, uid, state, producer) "
                    "VALUES(?,?,?,?)", (f"{uids[1]}:0:1", uids[1], "imported", ARCHIVE))
    finally:
        con.close()

    plan = carry.recut(root, "tok", budget=old.space.ctx * 4, log=quiet)
    assert (plan["to_drop"], plan["kept_archive"], plan["apply"]) == (1, 1, False)
    assert embeddings.stats(coll)["vectors"] == 4, "a dry run writes nothing"

    out = carry.recut(root, "tok", budget=2048, apply=True, window=1, log=quiet)
    assert (out["dropped"], out["documents_reset"], out["vectors_now"], out["verified"]) == \
        (1, 1, 3, True)
    books = [int(u.split(":")[1]) for u in uids]
    assert sorted(embeddings.chunk_ids_of_books(books, collection=coll)) == \
        sorted([f"{uids[0]}:1:1", f"{uids[1]}:0:1", f"{uids[2]}:0:1"])
    marks = _marks(root, "tok")
    assert marks[uids[0]] == 0, "the shortened row's document is embedded again"
    assert marks[uids[1]] == 1 and marks[uids[2]] == 1
    again = carry.recut(root, "tok", budget=2048, apply=True, log=quiet)
    assert again["dropped"] == 0 and again["verified"], "a second run: nothing left"


def test_a_dense_script_row_refuses_the_recut_with_nothing_written(pair):
    root, old, _new = pair
    _tokens_target(root)
    _rows(root, old, {(0, 0): LONG, (1, 0): "漢字の要旨"})
    carry.carry(root, "old", "tok", apply=True, recut=True, log=quiet)
    with pytest.raises(CodexError, match="dense-script"):
        carry.recut(root, "tok", budget=2048, apply=True, log=quiet)
    assert embeddings.stats(_local(root, "tok"))["vectors"] == 2


def test_recut_goes_one_way(pair):
    root, old, _new = pair
    _tokens_target(root)
    with pytest.raises(CodexError, match="never the other way"):
        carry.carry(root, "tok", "old", recut=True, log=quiet)
    with pytest.raises(CodexError, match="nothing to recut"):
        carry.carry(root, "old", "new", recut=True, log=quiet)


def test_a_pair_layer_embedded_every_input_alone():
    """A pair layer is admitted only on a chunker that makes one chunk per
    record (09 §9.3), and the worker embeds one document at a time."""
    from dataclasses import replace
    c = _token_codex()
    assert not carry.embedded_alone(c), "a text layer's chunks share requests"
    pairs = replace(c, layers=tuple(replace(l, input=dict(cx.INPUT_PAIR)) for l in c.layers))
    assert carry.embedded_alone(pairs)


def test_where_every_input_was_alone_a_dense_row_is_dropped_not_refused(pair, monkeypatch):
    """pubmed, 2026-10-18: 102 of its 4.58 M local rows hold CJK characters;
    each was its own request, so no neighbour's budget moved and they are
    dropped with the rest, embedded again by the book."""
    root, old, _new = pair
    _tokens_target(root)
    uids = _rows(root, old, {(0, 0): LONG, (1, 0): "漢字の要旨", (2, 0): "short"})
    carry.carry(root, "old", "tok", apply=True, recut=True, log=quiet)
    con = db.connect(_local(root, "tok"))
    try:
        con.execute("UPDATE documents SET embed_done=1")
    finally:
        con.close()
    monkeypatch.setattr(carry, "embedded_alone", lambda codex: True)
    out = carry.recut(root, "tok", budget=2048, apply=True, log=quiet)
    assert (out["dense_not_archive"], out["dropped"], out["vectors_now"]) == (1, 2, 1)
    marks = _marks(root, "tok")
    assert (marks[uids[0]], marks[uids[1]], marks[uids[2]]) == (0, 0, 1),         "the two dropped rows' documents are embedded again, the kept one is not"
    books = [int(u.split(":")[1]) for u in uids]
    assert embeddings.chunk_ids_of_books(books, collection=_local(root, "tok")) == \
        [f"{uids[2]}:0:1"], "only the short Latin row stays"


def test_recut_with_keep_crosses_the_cut_keeping_the_archives_rows_alone(pair, capsys):
    """Wikipedia's parser-5 switch (2026-10-21): the cut and the weights move at
    once, so only the archive's rows may stay — `--keep` drops every other row,
    the shortened ones among them, and the recut pass has nothing left."""
    import json
    from app import louise
    root, old, _new = pair
    _tokens_target(root)
    uids = _rows(root, old, {(0, 0): LONG, (1, 0): LONG, (2, 0): "short"})
    con = db.connect(_local(root, "old"))
    try:
        con.execute("UPDATE documents SET embed_done=1")
        con.execute("INSERT INTO chunk_verification(chunk_id, uid, state, producer) VALUES(?,?,?,?)",
                    (f"{uids[1]}:0:1", uids[1], "imported", ARCHIVE))
    finally:
        con.close()
    assert louise.main(["dev", "carry", "old", "tok", "--root", str(root), "--yes", "--recut",
                        "--keep", "producer:medcpt-pubmed"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert "recut" not in printed and printed["keep"]["kept"] == 1
    coll = _local(root, "tok")
    books = [int(u.split(":")[1]) for u in uids]
    assert embeddings.chunk_ids_of_books(books, collection=coll) == [f"{uids[1]}:0:1"], \
        "the archive's row alone, though it was long"
    marks = _marks(root, "tok")
    assert marks[uids[0]] == 0 and marks[uids[2]] == 0 and marks[uids[1]] == 1


def test_recut_with_rebuilt_needs_keep(pair, capsys):
    from app import louise
    root, _old, _new = pair
    _tokens_target(root)
    assert louise.main(["dev", "carry", "old", "tok", "--root", str(root), "--recut", "--rebuilt"]) != 0
    assert "needs --keep" in capsys.readouterr().err

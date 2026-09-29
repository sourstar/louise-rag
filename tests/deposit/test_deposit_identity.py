"""tests/deposit/test_deposit_identity.py — a deposit collection, created and written
by a client (docs/02 §2.9, 06 §6.7, 07 §7.3; migration step 15a).

Two collections from one set of defaults are two collections; a re-added
key replaces; deposit numbers ascend node-wide; a deposit is searchable by
text when `add` returns; nothing refused is ever stored.
"""
import json

import pytest

from app import collections as collections_mod, fts
from app.hold import collection as col, deposit
from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed
from tests.hold.test_template import SPACE

ENCODER_URL = "http://rage:30450/upstream/granite-embedding-107m-multilingual"

#: A symmetric space: a deposit collection's encoder embeds queries too.
TEXT_SPACE = {**SPACE, "query": {"encoder": None, "instruction": None}}


@pytest.fixture
def node(tmp_path, monkeypatch):
    root = tmp_path / "louise"
    (root / "collections").mkdir(parents=True)
    (tmp_path / "config" / "collections").mkdir(parents=True)
    monkeypatch.setenv("LOUISE_ROOT", str(root))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(collections_mod.config, "DATA_DIR", root)
    return root / "collections"


def make(node, name, *, embed=fake_embed, space=TEXT_SPACE, **kw):
    codex = deposit.create(node, name, platform_image=PLATFORM,
                           encoder_url=ENCODER_URL, space=space, embed=embed,
                           key=KEY, now=NOW, **kw)
    deposit.write_held_file(node, codex, ENCODER_URL)
    return codex


# ---- the collection ------------------------------------------------------

def test_two_collections_from_one_set_of_defaults_have_two_ids(node):
    a, b = make(node, "alice"), make(node, "bob")
    assert a.collection_id != b.collection_id
    assert a.space_id == b.space_id          # one encoder, one space (02 §2.9)


def test_the_origin_spec_is_exactly_source_id_and_nonce(node):
    spec = make(node, "alice").origin.spec
    assert set(spec) == {"source_id", "nonce"}
    assert spec["source_id"] == "deposit"
    assert len(spec["nonce"]) == 32 and int(spec["nonce"], 16) >= 0


def test_it_is_never_share_and_erasable_with_one_fixed_layer(node):
    c = make(node, "alice")
    assert (c.plugin, c.privacy, c.erasure) == ("deposit", "never-share", "erasable")
    assert c.license.cls == "green"
    [layer] = c.layers
    assert layer.chunker.id == "fixed"
    assert layer.chunker.params == {"tokens": 448, "overlap": 56}   # ctx 512


def test_the_second_collection_on_an_encoder_copies_the_calibration(node):
    """CLAUDE.md RULE 1: the first pays the calibration once; the second
    copies the member byte for byte instead of embedding the set again."""
    first = make(node, "alice")
    calls = []
    second = make(node, "bob", embed=lambda t: calls.append(1) or fake_embed(t))
    assert calls == []
    assert second.space.calibration.vectors == first.space.calibration.vectors
    member = "calibration"
    a = sorted(p.relative_to(col.drag_of(node, "alice")) for p in
               col.drag_of(node, "alice").rglob("*") if member in str(p))
    b = sorted(p.relative_to(col.drag_of(node, "bob")) for p in
               col.drag_of(node, "bob").rglob("*") if member in str(p))
    assert a == b and a


def test_a_held_name_is_refused(node):
    make(node, "alice")
    with pytest.raises(deposit.DepositError, match="held already"):
        make(node, "alice")


def test_with_no_space_anywhere_it_asks_for_one(node):
    with pytest.raises(deposit.DepositError, match="--space-from"):
        deposit.create(node, "alice", platform_image=PLATFORM,
                       encoder_url=ENCODER_URL, key=KEY, now=NOW)


def test_the_collection_file_embeds_everything_and_shares_nothing(node):
    make(node, "alice")
    c = collections_mod.by_name("alice")
    assert c is not None and c.codex is not None and c.share is False
    assert c.embed is not None and c.embed.url == ENCODER_URL


# ---- deposits ------------------------------------------------------------

def test_a_deposit_is_listed_shown_and_searchable_by_text_at_once(node):
    make(node, "alice")
    got = deposit.add("alice", "2026-09-29-10-00-00-abc.txt",
                      b"The quick brown fox jumps over the lazy dog.\n")
    assert got["chunks"] >= 1
    assert deposit.keys("alice") == ["2026-09-29-10-00-00-abc.txt"]
    shown = deposit.show("alice", "2026-09-29-10-00-00-abc.txt")
    assert shown["content"] == "The quick brown fox jumps over the lazy dog.\n"
    assert shown["chunk_count"] == got["chunks"]
    c = collections_mod.by_name("alice")
    hits = fts.search("other", "fox", collection=c)
    assert any(int(h["book_id"]) == got["native_id"] for h in hits)


def test_the_bytes_are_kept_exactly_as_deposited(node):
    make(node, "alice")
    data = "# Titolo\n\nÈ un testo in italiano.\r\n".encode("utf-8")
    got = deposit.add("alice", "note.md", data)
    c = collections_mod.by_name("alice")
    d = c.corpus_dir / str(got["native_id"])
    assert (d / "note.md").read_bytes() == data
    meta = json.loads((d / ".deposit.json").read_text())
    assert meta["key"] == "note.md"


def test_re_adding_a_key_replaces_the_deposit_whole(node):
    make(node, "alice")
    first = deposit.add("alice", "a.txt", b"first version")
    second = deposit.add("alice", "a.txt", b"second version")
    assert second["native_id"] > first["native_id"]
    assert deposit.keys("alice") == ["a.txt"]
    assert deposit.show("alice", "a.txt")["content"] == "second version"
    c = collections_mod.by_name("alice")
    assert not (c.corpus_dir / str(first["native_id"])).exists()


def test_deposit_numbers_ascend_across_collections(node):
    make(node, "alice")
    make(node, "bob")
    ids = [deposit.add("alice", "1.txt", b"one")["native_id"],
           deposit.add("bob", "1.txt", b"two")["native_id"],
           deposit.add("alice", "2.txt", b"three")["native_id"]]
    assert ids == sorted(ids) and len(set(ids)) == 3


def test_erase_removes_the_deposit(node):
    make(node, "alice")
    got = deposit.add("alice", "a.txt", b"a unique zebra sentence")
    deposit.add("alice", "b.txt", b"another text")
    assert deposit.erase("alice", "a.txt") == ["b.txt"]
    c = collections_mod.by_name("alice")
    assert not (c.corpus_dir / str(got["native_id"])).exists()
    assert not any(int(h["book_id"]) == got["native_id"]
                   for h in fts.search("other", "zebra", collection=c))
    with pytest.raises(deposit.DepositError, match="no deposit"):
        deposit.show("alice", "a.txt")


@pytest.mark.parametrize("key,data,why", [
    ("", b"x", "1–255"),
    (".hidden.txt", b"x", "leading dot"),
    ("a/b.txt", b"x", "no `/`"),
    ("x" * 252 + ".txt", b"x", "at most 255"),
    ("tool.exe", b"MZ", ".txt, .md or .pdf"),
    ("latin1.txt", "caffè".encode("latin-1"), "not valid UTF-8"),
])
def test_what_is_refused_is_never_stored(node, key, data, why):
    make(node, "alice")
    with pytest.raises(deposit.DepositError, match=why):
        deposit.add("alice", key, data)
    c = collections_mod.by_name("alice")
    assert deposit.keys("alice") == []
    assert not c.corpus_dir.exists() or not any(c.corpus_dir.iterdir())


def test_only_a_deposit_collection_takes_deposits(node, tmp_path):
    from tests.hold.conftest import fake_query_embed
    from tests.hold.test_template import recipe
    codex = col.create_from_recipe(node, "gutenberg", recipe(), platform_image=PLATFORM,
                                   embed=fake_embed, embed_query=fake_query_embed,
                                   name="gutenberg", key=KEY, now=NOW)
    (tmp_path / "config" / "collections" / "gutenberg.toml").write_text(
        f'codex = "{codex.collection_id}"\n', encoding="utf-8")
    with pytest.raises(deposit.DepositError, match="not a deposit collection"):
        deposit.add("gutenberg", "a.txt", b"x")


# ---- the command line ------------------------------------------------------

def test_the_command_line_creates_adds_lists_shows_and_erases(node, tmp_path, capsys):
    from app import louise
    first = make(node, "alice")
    root = str(node)
    assert louise.main(["hold", "collection", "create", "deposit", "--name", "bob",
                        "--root", root, "--space-from", first.collection_id,
                        "--encoder", ENCODER_URL, "--platform-image", PLATFORM]) == 0
    bob_id = capsys.readouterr().out.strip().splitlines()[-1]
    assert bob_id.startswith("sha256:") and bob_id != first.collection_id

    f = tmp_path / "memo.md"
    f.write_text("# Memo\n\nRemember the milk.\n", encoding="utf-8")
    assert louise.main(["hold", "deposit", "add", "bob", str(f)]) == 0
    capsys.readouterr()
    assert louise.main(["hold", "deposit", "list", "bob", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == ["memo.md"]
    assert louise.main(["hold", "deposit", "show", "bob", "memo.md"]) == 0
    assert json.loads(capsys.readouterr().out)["content"].startswith("# Memo")
    assert louise.main(["hold", "deposit", "erase", "bob", "memo.md"]) == 2
    assert louise.main(["hold", "deposit", "erase", "bob", "memo.md", "--yes"]) == 0
    capsys.readouterr()
    assert louise.main(["hold", "deposit", "list", "bob", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_create_deposit_without_a_name_is_refused(node, capsys):
    from app import louise
    assert louise.main(["hold", "collection", "create", "deposit", "--root", str(node),
                        "--platform-image", PLATFORM]) == 2
    assert "--name" in capsys.readouterr().err

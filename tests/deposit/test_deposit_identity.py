"""tests/deposit/test_deposit_identity.py — a deposit collection, created and written
by a client (docs/02 §2.9, 06 §6.7, 07 §7.3; migration step 16a).

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
    a = sorted(p.relative_to(col.shard_dir_of(node, "alice")) for p in
               col.shard_dir_of(node, "alice").rglob("*") if member in str(p))
    b = sorted(p.relative_to(col.shard_dir_of(node, "bob")) for p in
               col.shard_dir_of(node, "bob").rglob("*") if member in str(p))
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
    assert louise.main(["hold", "deposit", "add", "bob", str(f), "--here"]) == 0
    capsys.readouterr()
    assert louise.main(["hold", "deposit", "list", "bob", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == ["memo.md"]
    assert louise.main(["hold", "deposit", "show", "bob", "memo.md"]) == 0
    assert json.loads(capsys.readouterr().out)["content"].startswith("# Memo")
    assert louise.main(["hold", "deposit", "erase", "bob", "memo.md"]) == 2
    assert louise.main(["hold", "deposit", "erase", "bob", "memo.md", "--yes", "--here"]) == 0
    capsys.readouterr()
    assert louise.main(["hold", "deposit", "list", "bob", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_create_deposit_without_a_name_is_refused(node, capsys):
    from app import louise
    assert louise.main(["hold", "collection", "create", "deposit", "--root", str(node),
                        "--platform-image", PLATFORM]) == 2
    assert "--name" in capsys.readouterr().err


def test_the_command_line_queues_for_the_worker_by_default(node, tmp_path, capsys):
    """On a running node only the worker writes a collection's indexes
    (tantivy: one writer per index), so the CLI queues like the web does."""
    import threading
    from app import jobs, louise
    make(node, "alice")
    stop = threading.Event()
    t = threading.Thread(target=jobs.serve_deposits, args=(stop,),
                         kwargs={"poll_s": 0.02, "log": lambda m: None}, daemon=True)
    t.start()
    try:
        f = tmp_path / "queued.txt"
        f.write_text("through the worker", encoding="utf-8")
        assert louise.main(["hold", "deposit", "add", "alice", str(f)]) == 0
        assert json.loads(capsys.readouterr().out)["key"] == "queued.txt"
        assert deposit.keys("alice") == ["queued.txt"]
        assert louise.main(["hold", "deposit", "erase", "alice", "queued.txt", "--yes"]) == 0
        assert deposit.keys("alice") == []
    finally:
        stop.set()
        t.join(5)


def test_create_writes_the_empty_origin(node):
    """07 §7.8: an empty deposit collection has its corpus directory, so the
    worker's discovery does not report it missing every round."""
    make(node, "alice")
    make(node, "bob")                      # the copied-space path too
    for name in ("alice", "bob"):
        c = collections_mod.by_name(name)
        assert c.corpus_dir.is_dir() and not any(c.corpus_dir.iterdir())


# ---- the manifest exists before anyone can find the collection -----------

def test_create_leaves_the_manifest_in_wal_mode_before_the_held_file(node):
    """A fresh manifest's first open converts it to WAL under a lock SQLite
    takes without the busy handler, so two threads opening one at once —
    the deposit thread's `finish_pending`, the sources thread — got
    "database is locked" out of `db.connect` (2026-10-05). `create` opens
    it once, in the creating thread, before the held file exists."""
    import sqlite3
    deposit.create(node, "alice", platform_image=PLATFORM, encoder_url=ENCODER_URL,
                   space=TEXT_SPACE, embed=fake_embed, key=KEY, now=NOW)
    manifest = node / "alice" / "state" / "manifest.sqlite"
    assert manifest.exists() and not deposit.held_file(node, "alice").exists()
    con = sqlite3.connect(manifest)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        con.close()


# ---- the space file the deposit runbook writes (deploy/16f/deposit-16f.sh) --

def test_a_space_whose_engine_is_a_localai_run_makes_a_version_4_codex_the_others_copy(node):
    """deploy/16f: the first deposit collection's space file is written from
    the run RAGE over LocalAI states (09 §9.12: name localai/<backend>,
    variant null, `options` the load call). Such an engine needs Codex 4
    (09 §9.3); the next collection on the encoder copies the space whole,
    options and all (02 §2.9). Granite's shape: 384 wide, 512 tokens, CLS."""
    import numpy as np
    from app.hold import calibration as cal, codex as cx

    def embed384(texts):
        v = np.asarray(fake_embed(texts), dtype=np.float32)[:, :384]
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    engine = {
        "name": "localai/llama-cpp", "build": "58830f7+patches.17b54353635e", "variant": None,
        "image": "sha256:" + "b" * 64,
        "args": ["/backends/llama-cpp/llama-cpp-grpc", "--addr", "127.0.0.1:${PORT}"],
        "env": {"NEO_CACHE_DIR": "/state/neo-cache"}, "device": "Intel(R) Arc(TM) Pro B70 Graphics",
        "options": {"Model": "${WEIGHTS}", "ModelFile": "${WEIGHTS}", "ContextSize": 512,
                    "NBatch": 512, "Seed": 0, "Options": ["parallel:1", "pooling:cls"],
                    "ModelPath": "${MODELS}"},
    }
    space = {
        "encoder": {"weights": "sha256:" + "3" * 64, "label": "granite f16", "engine": engine},
        "dim": 384, "dtype": "f16", "ctx": 512, "pooling": "cls", "normalise": True,
        "query": {"encoder": None, "instruction": None}, "tolerance": "0.995",
        "calibration": {"set": cal.load("text")[0], "quorum": {"n": 16, "min_pass": 12}},
        "epoch": "0", "evaluation": None,
    }
    first = make(node, "deposit-granite", embed=embed384, space=space)
    cx.validate(first)
    # options need version 4 at least (09 §9.3); a new Codex is written at 5
    assert first.version == cx.CODEX_VERSION >= cx.OPTIONS_VERSION
    assert first.space.encoder.engine.options["ContextSize"] == 512
    assert first.space.dim == 384 and first.space.ctx == 512 and first.space.query.encoder is None
    second = make(node, "alice")                   # no space given: copies the first's
    assert second.space_id == first.space_id and second.collection_id != first.collection_id
    assert second.space.encoder.engine.options == engine["options"]
    assert second.version == first.version


# ---- the copy state follows the plan (03 §3.6 on a never-share collection) --

def test_a_deposit_collection_waits_for_its_gate_and_for_no_shard_or_origin_check(node):
    """03 §3.6's tests 1 and 5 have nothing to run on for a never-share
    collection (02 §2.9): a deposit collection plans calibration and
    vectors-sample and nothing else, holds no shard for roots and chunks,
    and its origin is the client — so neither keeps it in quarantine.
    Found live on 2026-10-06: the first deposit collection was held for
    `hold check --checks roots,chunks` and `records-vs-origin`, which it
    can never run."""
    from app import copy_state
    codex = make(node, "alice")
    assert "records-vs-origin" not in codex.validation.checks
    s = copy_state.evaluate(node, "alice", codex, licence="not_required", key=None,
                            answering={})
    assert s.test("roots-and-chunks").status == copy_state.PASSED
    assert s.test("origin").status == copy_state.PASSED
    assert s.origin_check == copy_state.ORIGIN_NOT_PLANNED == "not-planned"
    assert s.state == "quarantine" and len(s.reasons) == 1, s.reasons    # the gate alone
    assert s.test("gate").status != copy_state.PASSED

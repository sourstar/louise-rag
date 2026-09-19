"""tests/test_codex_collection.py — the worker runs a Codex collection.

THE SEAM between the `hold` tree and the running node, and the one piece
that stood between "migrated" and "in use". Step 8 produced complete,
verified Codex collections under `<LOUISE_ROOT>/collections/<name>/` that
nothing in the worker could see: `Collection` was built entirely from a
legacy TOML's `[paths]` block, and `app/collections.py` had never heard
of a Codex.

07 §7.8 specifies the bridge — a `codex = "sha256:…"` key in
`collections/<name>.toml`, and the collection's tree beside its drag —
and nothing implemented it.
"""
import json

import pytest

from app import collections as collections_mod, db, spaces
from app.hold import collection as col
from tests.hold.conftest import fake_query_embed  # noqa: E402,F811
from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed
from tests.hold.test_template import recipe


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A LOUISE_ROOT with one migrated collection and its TOML."""
    root = tmp_path / "louise"
    (root / "collections").mkdir(parents=True)
    codex = col.create_from_recipe(
        root / "collections", "gutenberg", recipe(), platform_image=PLATFORM,
        embed=fake_embed, embed_query=fake_query_embed, name="gutenberg", key=KEY, now=NOW)

    cfg = tmp_path / "config" / "collections"
    cfg.mkdir(parents=True)
    (cfg / "gutenberg.toml").write_text(
        f'codex = "{codex.collection_id}"\n'
        f'embed = "on-demand"\n', encoding="utf-8")

    monkeypatch.setenv("LOUISE_ROOT", str(root))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(collections_mod.config, "DATA_DIR", root)
    return root, cfg, codex


# ---- the bridge ----------------------------------------------------------

def test_a_toml_naming_a_codex_is_loaded_from_it(node):
    root, cfg, codex = node
    got = [c for c in collections_mod.load_all() if c.name == "gutenberg"]
    assert got, "the collection did not load at all"
    c = got[0]
    assert c.codex is not None
    assert c.codex.collection_id == codex.collection_id


def test_it_takes_7_8s_tree_and_not_a_paths_block(node):
    """`state/manifest.sqlite`, `index/`, `lance/` beside the drag — the
    same three `records.local_collection` builds, so a stage cannot write
    to a directory another stage never reads."""
    root, cfg, codex = node
    c = next(x for x in collections_mod.load_all() if x.name == "gutenberg")
    d = root / "collections" / "gutenberg"
    assert c.manifest_path == d / "state" / "manifest.sqlite"
    assert c.fts_dir == d / "index"
    assert c.lance_dir == d / "lance"


def test_plugin_and_layers_come_from_the_codex_not_the_file(node):
    """Both are inside `collection_id`. A TOML that set them could name a
    collection and then chunk it differently — invariant 1's failure mode,
    and undetectable, since both peers computed the id honestly."""
    root, cfg, codex = node
    c = next(x for x in collections_mod.load_all() if x.name == "gutenberg")
    assert c.plugin == codex.plugin
    assert c.layers is not None and len(c.layers) == len(codex.layers)
    assert [l.chunker for l in c.layers] == [l.chunker.id for l in codex.layers]


def test_a_codex_whose_hash_is_not_the_files_is_refused(node):
    """07 §7.8: such a collection "is not served, not shared and not
    searched". A name holds one collection for its life."""
    root, cfg, codex = node
    (cfg / "gutenberg.toml").write_text(
        f'codex = "sha256:{"ab" * 32}"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="not served, shared or searched"):
        collections_mod.load_all()


def test_a_missing_drag_says_so(node):
    root, cfg, codex = node
    (root / "collections" / "gutenberg" / "drag" / "codex.json").unlink()
    with pytest.raises(ValueError, match="no .*codex.json"):
        collections_mod.load_all()


def test_a_legacy_toml_is_untouched(tmp_path, monkeypatch):
    """Everything that does not name a Codex behaves exactly as before —
    which is every collection that predates step 8."""
    cfg = tmp_path / "config" / "collections"
    cfg.mkdir(parents=True)
    (cfg / "legacy.toml").write_text(
        'plugin = "gutenberg"\nembed_mode = "off"\n'
        f'[paths]\nmanifest = "{tmp_path / "m.db"}"\n', encoding="utf-8")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    c = next(x for x in collections_mod.load_all() if x.name == "legacy")
    assert c.codex is None
    assert c.manifest_path == tmp_path / "m.db"


# ---- invariant 1, against the Codex's own space id -----------------------

def test_the_boot_pin_is_the_codexs_space_id(node):
    """NOT `compute_space_id`'s BLAKE2b preimage: the two are different
    functions naming different things (step6-preflight §4), so a Codex
    collection validated against the legacy one would refuse to boot for
    a difference that means nothing."""
    root, cfg, codex = node
    c = next(x for x in collections_mod.load_all() if x.name == "gutenberg")
    con = db.connect(c)
    try:
        assert spaces._validate_one(con, c) is True
        row = con.execute("SELECT space_id, dim FROM space WHERE id=1").fetchone()
        assert row["space_id"] == codex.space_id
        assert row["dim"] == codex.space.dim
    finally:
        con.close()


def test_a_table_pinned_to_another_space_refuses_to_start(node):
    """"A vector table whose dim/space mismatches the Codex is a
    REFUSE-TO-START error FOR THAT COLLECTION" (invariant 1)."""
    root, cfg, codex = node
    c = next(x for x in collections_mod.load_all() if x.name == "gutenberg")
    con = db.connect(c)
    try:
        spaces._validate_one(con, c)
        con.execute("UPDATE space SET space_id=? WHERE id=1",
                    ("sha256:" + "cd" * 32,))
        with pytest.raises(spaces.BootValidationError,
                           match="No vector crosses a space boundary"):
            spaces._validate_one(con, c)
    finally:
        con.close()


def test_reporting_never_pins(node):
    """The System page must not mutate space state — an operator reset a
    pin, opened the UI, and the report re-pinned a half-edited recipe
    behind their back."""
    root, cfg, codex = node
    c = next(x for x in collections_mod.load_all() if x.name == "gutenberg")
    con = db.connect(c)
    try:
        assert spaces._validate_one(con, c, pin_if_missing=False) is False
        assert con.execute("SELECT COUNT(*) FROM space").fetchone()[0] == 0
    finally:
        con.close()

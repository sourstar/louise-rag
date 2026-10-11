"""tests/test_codex_only_node.py — a node serves only Codex collections.

The operator's rule, 2026-09-11: Louise shows the converted collections
and nothing else; an unconverted one is kept aside and appears again only
once it has been migrated. Until 2026-10-08 it was a policy a node opted
into (`[node] codex_only`); since the pre-Codex shape left it is the only
behaviour, and the key is obsolete.

It lives on the READ side, where nothing can walk around it: a `mv`, a
restored backup, or the Library's own create flow writing a codex-less
file all land on the same refusal.

A refusal is NOT an error, and the difference is load-bearing. A
malformed file still raises out of `load_all()` — "a typo must not
silently become the default" (tests/test_config_tiers_embed_mode.py), and
an operator who mistyped `embed` must be told rather than quietly served a
node with one collection missing. A codex-less file is the opposite:
nothing is malformed, and the collection is set aside with a reason
recorded.
"""
import pytest

from app import collections as collections_mod
from tests.conftest import codex_collection


def _toml(cfg, name, body):
    (cfg / "collections").mkdir(parents=True, exist_ok=True)
    p = cfg / "collections" / f"{name}.toml"
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A config dir with one unconverted collection; the Codex one is added
    by whichever test needs it (building a real SHARD is not free)."""
    cfg = tmp_path / "node-config"
    _toml(cfg, "wikivec",
          'plugin = "embedded"\nembed_mode = "off"\n'
          f'[paths]\nmanifest = "{(tmp_path / "wv.db").as_posix()}"\n')
    monkeypatch.setenv("CONFIG_DIR", str(cfg))
    return cfg, tmp_path


# ---- a refusal is not an error -------------------------------------------

def test_a_malformed_file_still_raises(isolated_config, data_dir, config_dir):
    """The line refusal must not cross. A refusal is a decision; a parse
    error is a mistake, and swallowing it would hide the operator's own
    typo behind the same silence as an unconverted file."""
    _toml(config_dir, "wikivec", 'plugin = "embedded"\n')
    codex_collection("oops", louise_root=data_dir, config_dir=config_dir,
                     toml_extra='embed = "always"\n')
    with pytest.raises(ValueError, match="embed must be one of"):
        collections_mod.load_all()


def test_a_refusal_is_recorded_with_its_reason(node):
    """Refusing has to be visible, or a collection the operator can see on
    disk simply vanishes from the UI with no explanation."""
    collections_mod.load_all()
    refused = {r.name: r for r in collections_mod.refusals()}
    assert "wikivec" in refused
    assert refused["wikivec"].reason
    assert refused["wikivec"].path.name == "wikivec.toml"


def test_refusals_are_recomputed_not_accumulated(node):
    cfg, tmp = node
    collections_mod.load_all()
    assert len(collections_mod.refusals()) == 1
    (cfg / "collections" / "wikivec.toml").unlink()
    collections_mod.load_all()
    assert collections_mod.refusals() == ()


# ---- the rule ------------------------------------------------------------

def test_an_unconverted_collection_is_not_served(node):
    assert collections_mod.load_all() == []
    with pytest.raises(collections_mod.NotConverted):
        collections_mod._load_one(node[0] / "collections" / "wikivec.toml")


def test_the_refusal_says_why_and_names_the_way_back(node):
    """'Where did my collection go' must be answerable from the message."""
    collections_mod.load_all()
    (refusal,) = collections_mod.refusals()
    assert refusal.name == "wikivec"
    assert "codex" in refusal.reason.lower()
    assert "migrated" in refusal.reason or "converted" in refusal.reason


def test_an_unconverted_toml_created_later_is_still_not_served(node):
    """THE durable half: a file written after the migration by any route is
    refused the same way."""
    cfg, tmp = node
    _toml(cfg, "pubmed",
          'plugin = "pubmed"\nembed_mode = "off"\n'
          f'[paths]\nmanifest = "{(tmp / "pm.db").as_posix()}"\n')
    assert collections_mod.load_all() == []
    assert {r.name for r in collections_mod.refusals()} == {"wikivec", "pubmed"}


def test_the_obsolete_policy_key_changes_nothing(node):
    """`[node] codex_only = false` is still accepted and read by no one:
    it does not bring an unconverted collection back."""
    cfg, tmp = node
    (cfg / "platform.toml").write_text("[node]\ncodex_only = false\n", encoding="utf-8")
    assert collections_mod.load_all() == []
    assert [r.name for r in collections_mod.refusals()] == ["wikivec"]


# ---- the fallback that must not fire ------------------------------------

def test_an_all_refused_node_never_falls_back_to_the_legacy_tree(node):
    """`default_collection()` falls back to a control collection whose
    paths are DATA_DIR/manifest.db, DATA_DIR/fts, DATA_DIR/lancedb — on
    the live node the LEGACY gutenberg tree that is queued for deletion.
    Returning it because every file was refused would put the node's
    control state into a tree nothing serves and someone is about to
    remove."""
    with pytest.raises(ValueError, match="every collection file was refused"):
        collections_mod.default_collection()


def test_a_node_with_no_collections_dir_has_none_and_is_not_refused(isolated_config, config_dir):
    """No collections/ at all is a node with no collections — not a
    refusal, so the control fallback stands, and never a synthesized
    default."""
    import shutil
    collections_mod.load_all()      # this test's own last load, not another's
    shutil.rmtree(config_dir / "collections")
    assert collections_mod.load_all() == []
    assert collections_mod.refusals() == ()
    assert collections_mod.default_collection().name == "control"


def test_refusals_are_recomputed_when_the_directory_goes(node):
    """`refusals()` is "as fresh as the listing beside it": a node whose
    collections/ disappeared has refused nothing, and `default_collection`
    must not raise "every collection file was refused" over a stale list."""
    import shutil
    cfg, tmp = node
    collections_mod.load_all()
    assert len(collections_mod.refusals()) == 1
    shutil.rmtree(cfg / "collections")
    assert collections_mod.load_all() == []
    assert collections_mod.refusals() == ()

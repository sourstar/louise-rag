"""tests/test_codex_only_node.py — a node that serves only Codex collections.

The operator's rule, 2026-09-11: Louise shows the converted collections
and nothing else; an unconverted one is kept aside and appears again only
once it has been migrated.

Two halves, and the second is the one that bites. Moving the files aside
is arrangement — a `mv` undoes it, a restored backup undoes it, and the
Library's own create flow writes a legacy TOML by construction
(`_write_collection_toml(..., codex=None)`), pointing at corpora the move
has just freed. So the policy has to live on the READ side, where nothing
can walk around it.

A policy refusal is NOT an error, and the difference is load-bearing. A
malformed file still raises out of `load_all()` — "a typo must not
silently become the default" (tests/test_config_tiers_embed_mode.py), and
an operator who mistyped `embed_mode` must be told rather than quietly
served a node with one collection missing. A legacy file on a Codex-only
node is the opposite: nothing is wrong, the operator asked for exactly
this, and the collection is set aside with a reason recorded.
"""
import pytest

from app import collections as collections_mod


def _toml(cfg, name, body):
    (cfg / "collections").mkdir(parents=True, exist_ok=True)
    p = cfg / "collections" / f"{name}.toml"
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A config dir with one legacy collection; the Codex one is added by
    whichever test needs it (building a real drag is not free)."""
    cfg = tmp_path / "config"
    _toml(cfg, "wikivec",
          'plugin = "embedded"\nembed_mode = "off"\n'
          f'[paths]\nmanifest = "{(tmp_path / "wv.db").as_posix()}"\n')
    monkeypatch.setenv("CONFIG_DIR", str(cfg))
    return cfg, tmp_path


def _set_policy(cfg, on: bool):
    (cfg / "platform.toml").write_text(
        f"[node]\ncodex_only = {'true' if on else 'false'}\n", encoding="utf-8")


# ---- a policy refusal is not an error -----------------------------------

def test_a_malformed_file_still_raises(node):
    """The line this feature must not cross. A refusal is a decision; a
    parse error is a mistake, and swallowing it would hide the operator's
    own typo behind the same silence as a deliberate retirement."""
    cfg, tmp = node
    _set_policy(cfg, True)
    _toml(cfg, "oops", 'plugin = "gutenberg"\nembed_mode = "always"\n')
    with pytest.raises(ValueError, match="embed_mode must be one of"):
        collections_mod.load_all()


def test_a_refusal_is_recorded_with_its_reason(node):
    """Refusing has to be visible, or a collection the operator can see on
    disk simply vanishes from the UI with no explanation."""
    cfg, tmp = node
    _set_policy(cfg, True)
    collections_mod.load_all()
    refused = {r.name: r for r in collections_mod.refusals()}
    assert "wikivec" in refused
    assert refused["wikivec"].reason
    assert refused["wikivec"].path.name == "wikivec.toml"


def test_refusals_are_recomputed_not_accumulated(node):
    cfg, tmp = node
    _set_policy(cfg, True)
    collections_mod.load_all()
    assert len(collections_mod.refusals()) == 1
    (cfg / "collections" / "wikivec.toml").unlink()
    collections_mod.load_all()
    assert collections_mod.refusals() == ()


# ---- the policy ----------------------------------------------------------

def test_off_by_default_a_legacy_collection_is_served(node):
    """Every node that predates the migration must be unaffected."""
    cfg, tmp = node
    assert [c.name for c in collections_mod.load_all()] == ["wikivec"]
    assert collections_mod.codex_only() is False


def test_on_a_legacy_collection_is_not_served(node):
    cfg, tmp = node
    _set_policy(cfg, True)
    assert collections_mod.codex_only() is True
    assert collections_mod.load_all() == []


def test_the_refusal_says_why_and_names_the_way_back(node):
    """'Where did my collection go' must be answerable from the message."""
    cfg, tmp = node
    _set_policy(cfg, True)
    collections_mod.load_all()
    (refusal,) = collections_mod.refusals()
    assert refusal.name == "wikivec"
    assert "codex" in refusal.reason.lower()
    assert "migrated" in refusal.reason or "converted" in refusal.reason


def test_a_legacy_toml_created_later_is_still_not_served(node):
    """THE durable half. The Library's create flow hardcodes `codex=None`,
    and retiring the files frees their corpora for it to point at — so
    arrangement alone is not a policy."""
    cfg, tmp = node
    _set_policy(cfg, True)
    _toml(cfg, "pubmed",
          'plugin = "pubmed"\nembed_mode = "off"\n'
          f'[paths]\nmanifest = "{(tmp / "pm.db").as_posix()}"\n')
    assert collections_mod.load_all() == []
    assert {r.name for r in collections_mod.refusals()} == {"wikivec", "pubmed"}


def test_a_malformed_policy_does_not_hide_everything(node):
    """`codex_only = "false"` read as truthy would empty the Library and
    look exactly like data loss."""
    cfg, tmp = node
    (cfg / "platform.toml").write_text(
        '[node]\ncodex_only = "false"\n', encoding="utf-8")
    assert collections_mod.codex_only() is False
    assert [c.name for c in collections_mod.load_all()] == ["wikivec"]


# ---- the fallback that must not fire ------------------------------------

def test_an_all_refused_node_never_falls_back_to_the_legacy_tree(node):
    """`default_collection()` falls back to a synthesized collection whose
    paths are DATA_DIR/manifest.db, DATA_DIR/fts, DATA_DIR/lancedb — on
    the live node the LEGACY gutenberg tree that is queued for deletion.
    Returning it because every file was refused would put the node's
    control state into a tree nothing serves and someone is about to
    remove."""
    cfg, tmp = node
    _set_policy(cfg, True)
    with pytest.raises(ValueError, match="every collection file was refused"):
        collections_mod.default_collection()


def test_a_node_with_no_collections_dir_still_synthesizes(tmp_path, monkeypatch):
    """The genuine legacy deployment is untouched: no collections/ at all
    still means the one synthesized default, not a refusal."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "empty"))
    got = collections_mod.load_all()
    assert len(got) == 1 and got[0].name == "gutenberg"
    assert collections_mod.default_collection().name == "gutenberg"

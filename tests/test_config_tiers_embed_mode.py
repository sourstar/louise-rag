"""Invariant 10 for `embed_mode`: TOML = desired state, DB = runtime
override that does NOT survive restart.

Before this, the TOML's `embed_mode` was parsed into
Collection.embed_mode_default and consumed by exactly one place — the
System page, which DISPLAYED it as Tier 2 — while the effective mode was
whatever the Library's control last wrote to the manifest, forever. Tier 2
was decorative and Tier 3 acted as desired state: the inversion the
invariant forbids. A node restored from `git clone + compose up` came up
with every collection on `lazy` regardless of its TOMLs.

Now the worker applies the TOML on restart, on add, and on a TOML edit; the
Library override keeps working between those, and the event log says when
and why the file won.
"""
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from app import collections as collections_mod, db, discovery, worker
from tests.conftest import codex_collection

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"


def _collection(tmp_path, name="alpha", embed_mode=None):
    corpus = tmp_path / f"corpus_{name}"
    if not corpus.exists():
        shutil.copytree(FIXTURE_CORPUS, corpus)
    return collections_mod.Collection(
        name=name, plugin="gutenberg", profile=None, embed_mode_default=embed_mode,
        corpus_dir=corpus, manifest_path=tmp_path / name / "manifest.db",
        fts_dir=tmp_path / name / "fts", lance_dir=tmp_path / name / "lancedb")


def _events(con, stage="control"):
    return [r["message"] for r in con.execute(
        "SELECT message FROM events WHERE stage=? ORDER BY id", (stage,))]


def test_toml_value_is_applied_on_restart_over_a_ui_override(tmp_path):
    c = _collection(tmp_path, embed_mode="all")
    con = db.connect(c)
    # the Library's control wrote a Tier-3 override...
    scope = db.get_scope(con)
    scope["embed_mode"] = "off"
    db.set_scope(con, scope)
    assert db.get_scope(con)["embed_mode"] == "off"

    # ...and the next restart makes the file win, audibly
    worker._apply_tier2_embed_mode(con, c, "restart")
    assert db.get_scope(con)["embed_mode"] == "all"
    assert any("'off' -> 'all' on restart" in m and "collections/alpha.toml" in m
               for m in _events(con)), _events(con)


def test_a_silent_toml_means_the_default_not_the_last_click(tmp_path):
    c = _collection(tmp_path, embed_mode=None)
    con = db.connect(c)
    scope = db.get_scope(con)
    scope["embed_mode"] = "all"
    db.set_scope(con, scope)

    worker._apply_tier2_embed_mode(con, c, "restart")
    assert db.get_scope(con)["embed_mode"] == collections_mod.DEFAULT_EMBED_MODE
    assert any("the TOML sets none" in m for m in _events(con)), _events(con)


def test_applying_an_already_effective_mode_is_silent(tmp_path):
    """No spurious log line on every boot for the common case."""
    c = _collection(tmp_path, embed_mode="lazy")
    con = db.connect(c)
    assert db.apply_desired_embed_mode(con, c) is None
    worker._apply_tier2_embed_mode(con, c, "restart")
    assert _events(con) == []


def test_the_override_still_works_between_restarts(tmp_path):
    """Tier 3 is an override, not a no-op: after boot applied the TOML the
    Library can still flip the mode, and the worker's selection follows it
    until the next restart."""
    c = _collection(tmp_path, embed_mode="off")
    con = db.connect(c)
    worker._apply_tier2_embed_mode(con, c, "restart")
    assert db.get_scope(con)["embed_mode"] == "off"
    scope = db.get_scope(con)
    scope["embed_mode"] = "all"
    db.set_scope(con, scope)
    assert db.get_scope(con)["embed_mode"] == "all"


def test_a_toml_edit_applies_within_a_round(tmp_path, monkeypatch):
    """Hot reload: _sync_collections sees embed_mode_default change and the
    file wins again, tagged as a TOML edit."""
    before = _collection(tmp_path, embed_mode="lazy")
    con = db.connect(before)
    discovery.sync_collection(con, before)
    cons = {before.name: con}
    after = replace(before, embed_mode_default="all")
    monkeypatch.setattr(collections_mod, "load_all", lambda: [after])
    monkeypatch.setattr(worker, "_discover", lambda *a, **k: None)

    worker._sync_collections([before], cons, {}, pipeline=False)
    assert db.get_scope(con)["embed_mode"] == "all"
    assert any("on TOML edit" in m for m in _events(con)), _events(con)


def test_an_invalid_toml_value_is_refused_at_load(isolated_config, data_dir, config_dir):
    """A typo must not silently become the default."""
    codex_collection("bad", louise_root=data_dir, config_dir=config_dir,
                     toml_extra='embed = "always"\n')
    with pytest.raises(ValueError, match="embed must be one of"):
        collections_mod.load_all()


def test_the_documents_word_for_a_mode_is_read(isolated_config, data_dir, config_dir):
    """07 §7.8 spells the middle mode `on-demand`; the file is written in
    the documents' word and read as `lazy`."""
    codex_collection("od", louise_root=data_dir, config_dir=config_dir,
                     toml_extra='embed = "on-demand"\n')
    assert collections_mod.by_name("od").embed_mode_default == "lazy"


def test_a_new_collection_another_writer_holds_is_not_opened(tmp_path, monkeypatch):
    """07 §7.7, 2026-10-13: `dev carry` held normattiva-1861 and had removed
    its manifest to put the carried one in place; the worker opened the
    collection anyway, and `db.connect` made a fresh empty manifest the carry
    then took for its own. A collection whose lock another writer holds is
    not opened — not even its manifest — until the lock is free."""
    from app.hold import lock as lock_mod
    added = _collection(tmp_path / "new", embed_mode="lazy")
    state = Path(added.manifest_path).parent
    monkeypatch.setattr(collections_mod, "load_all", lambda: [added])
    monkeypatch.setattr(worker, "_discover", lambda *a, **k: None)
    monkeypatch.setattr(worker.spaces, "validate_boot", lambda cs: None)
    held = lock_mod.take(state, added.name, "louise dev carry")
    try:
        cons: dict = {}
        assert worker._sync_collections([], cons, {}, pipeline=False) == []
        assert cons == {} and not Path(added.manifest_path).exists(), "no manifest made"
    finally:
        held.release()
    assert [c.name for c in worker._sync_collections([], cons, {}, pipeline=False)] == [added.name]
    assert Path(added.manifest_path).exists()
    cons[added.name].close()


def test_the_fourth_mode_p2p_is_read_with_its_display_names(isolated_config, data_dir, config_dir):
    """07 §7.8, 2026-10-19: `all` is "all local", `p2p` is "all p2p"; the
    file carries the tokens, the Library the names, and both spellings read."""
    codex_collection("pp", louise_root=data_dir, config_dir=config_dir, toml_extra='embed = "p2p"\nshare = true\n')
    c = collections_mod.by_name("pp")
    assert c.embed_mode_default == "p2p" and c.embed_mode_note is None and c.share is True
    codex_collection("pq", louise_root=data_dir, config_dir=config_dir, toml_extra='embed = "all p2p"\nshare = true\n')
    assert collections_mod.by_name("pq").embed_mode_default == "p2p"
    codex_collection("al", louise_root=data_dir, config_dir=config_dir, toml_extra='embed = "all local"\n')
    assert collections_mod.by_name("al").embed_mode_default == "all"
    assert collections_mod.EMBED_MODE_LABELS == {"off": "off", "lazy": "on-demand", "all": "all local", "p2p": "all p2p"}
    assert "p2p" in collections_mod.EMBEDDING_MODES and "p2p" in collections_mod.BACKFILL_MODES


def test_p2p_without_sharing_runs_as_all_local_and_says_so(isolated_config, data_dir, config_dir):
    """p2p publishes what it embeds; a node that does not share publishes
    nothing. The Library asks before writing the pair; a file edited by hand
    to the pair runs as all local, and the card says why."""
    codex_collection("ph", louise_root=data_dir, config_dir=config_dir, toml_extra='embed = "p2p"\nshare = false\n')
    c = collections_mod.by_name("ph")
    assert c.embed_mode_default == "all" and c.share is False
    assert c.embed_mode_note and "share = false" in c.embed_mode_note and "all local" in c.embed_mode_note
    assert collections_mod.effective_embed_mode("p2p", True, "x") == ("p2p", None)
    assert collections_mod.effective_embed_mode("all", False, "x") == ("all", None)


def test_the_embed_stage_leaves_parts_to_peers_under_p2p_only(isolated_config, data_dir, config_dir, monkeypatch):
    """The partition of 04 §4.5 is `p2p`'s; `all` embeds every part itself."""
    from app import db, worker
    from app.share import leading
    codex_collection("pw", louise_root=data_dir, config_dir=config_dir, toml_extra='embed = "p2p"\nshare = true\n')
    c = collections_mod.by_name("pw")
    asked: list[str] = []
    monkeypatch.setattr(leading, "skip_test", lambda collection, con, **kw: (asked.append(collection.name), None)[1])
    con = db.connect(c)
    for mode, expect in (("all", []), ("p2p", ["pw"]), ("lazy", [])):
        asked.clear()
        scope = db.get_scope(con)
        scope["embed_mode"] = mode
        db.set_scope(con, scope)
        worker._backfill_rows(con, c, 5)
        assert asked == expect, f"under {mode!r}"


def test_a_public_collection_whose_file_does_not_say_is_shared(isolated_config, data_dir, config_dir):
    """07 §7.8 (operator, 2026-10-20): every collection shareable, now that a
    peer chooses what it fetches from the offer (04 §4.2). A public Codex
    collection whose file is silent on `share` is shared; `share = false`
    switches it off; a restricted or never-share Codex, or no Codex at all,
    is never shared by default."""
    from types import SimpleNamespace
    codex_collection("sh", louise_root=data_dir, config_dir=config_dir)
    c = collections_mod.by_name("sh")
    assert c.codex.privacy == "public" and c.share is True
    codex_collection("sf", louise_root=data_dir, config_dir=config_dir, toml_extra="share = false\n")
    assert collections_mod.by_name("sf").share is False
    assert collections_mod.default_share(SimpleNamespace(privacy="public")) is True
    assert collections_mod.default_share(SimpleNamespace(privacy="restricted")) is False
    assert collections_mod.default_share(SimpleNamespace(privacy="never-share")) is False
    assert collections_mod.default_share(None) is False

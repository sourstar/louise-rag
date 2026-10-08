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

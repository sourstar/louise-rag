"""`collections.by_name` looks up ONE collection: another file's fault is not
its fault (found 2026-09-21 — `hold check pubmed` died on gutenberg's
missing drag before checking anything), and its own fault still raises."""
import pytest

from app import collections as collections_mod
from tests.conftest import codex_collection


def test_another_files_fault_does_not_hide_this_collection(isolated_config, data_dir, config_dir):
    codex_collection("good", louise_root=data_dir, config_dir=config_dir)
    codex_collection("bad", louise_root=data_dir, config_dir=config_dir,
                     toml_extra='embed = "always"\n')
    assert collections_mod.by_name("good").name == "good"
    assert collections_mod.by_name("nowhere") is None
    with pytest.raises(ValueError, match="embed must be one of"):
        collections_mod.by_name("bad")
    with pytest.raises(ValueError, match="embed must be one of"):
        collections_mod.load_all()      # everything else still wants the raise


def test_a_codex_less_file_is_refused_not_raised(isolated_config, config_dir):
    """A file naming no Codex is a refusal, not a fault: the lookup of it
    says "no such collection" and the others are found."""
    (config_dir / "collections" / "old.toml").write_text('plugin = "gutenberg"\n', encoding="utf-8")
    assert collections_mod.by_name("old") is None
    assert collections_mod.by_name("gutenberg").name == "gutenberg"
    assert [r.name for r in collections_mod.refusals()] == ["old"]

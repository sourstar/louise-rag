"""`collections.by_name` looks up ONE collection: another file's fault is not
its fault (found 2026-09-21 — `hold check pubmed` died on gutenberg's
missing drag before checking anything), and its own fault still raises."""
import pytest

from app import collections as collections_mod

GOOD = ('plugin = "gutenberg"\ncorpus_dir = "{corpus}"\n\n[encoder]\n'
        'url = "http://enc.invalid:8080"\napi = "openai"\nmodel = "m"\n'
        'dtype = "f16"\nctx = 512\n')


def _config(tmp_path, monkeypatch, **files):
    cfg = tmp_path / "cfg"
    (cfg / "collections").mkdir(parents=True)
    for name, body in files.items():
        (cfg / "collections" / f"{name}.toml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("CONFIG_DIR", str(cfg))


def test_another_files_fault_does_not_hide_this_collection(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _config(tmp_path, monkeypatch,
            good=GOOD.format(corpus=corpus.as_posix()),
            bad='plugin = "gutenberg"\nembed_mode = "always"\n')
    assert collections_mod.by_name("good").name == "good"
    assert collections_mod.by_name("nowhere") is None
    with pytest.raises(ValueError, match="embed_mode must be one of"):
        collections_mod.by_name("bad")
    with pytest.raises(ValueError, match="embed_mode must be one of"):
        collections_mod.load_all()      # everything else still wants the raise

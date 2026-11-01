"""`hold engine-settings show|record` (07 §7.3, 09 §9.19)."""
from __future__ import annotations

from app import louise, run_check
from app.hold import collection as col, engine_settings as es, envelope as env
from tests.hold.conftest import KEY
from tests.hold.test_embedder_cli import _collection

ENGINE = "sha256:" + "e" * 64


def test_the_verbs_are_in_the_tree():
    p = louise.build_parser()
    for argv in (["hold", "engine-settings", "show", "g"],
                 ["hold", "engine-settings", "show", "g", "--card", "xe:8086:e223"],
                 ["hold", "engine-settings", "record", "g", "--batch", "16",
                  "--concurrency", "4", "--busy", "0.82", "--chunks-per-s", "42.0",
                  "--window-s", "600"]):
        assert p.parse_args(argv).fn is not None


def test_show_lists_every_signer_and_what_this_card_takes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "no-config-here"))
    monkeypatch.setattr(run_check, "signer", lambda key=None: None)
    root = _collection(tmp_path)
    name, codex = col.resolve(root, "gutenberg")
    es.record(col.shard_dir_of(root, name), KEY, collection_id=codex.collection_id,
              space_id=codex.space_id, time="2026-11-01T20:00:00Z",
              new=es.entry(es.Card("xe", "8086", "e223"), engine_id=ENGINE, batch=16,
                           concurrency=4, busy="0.82", time="2026-11-01T20:00:00Z"))
    assert louise.main(["hold", "engine-settings", "show", "gutenberg", "--root", str(root),
                        "--card", "xe:0x8086:E223"]) == 0
    out = capsys.readouterr().out
    assert env.key_text(KEY.verify_key) in out
    assert "xe 8086:e223  batch 16, concurrency 4" in out
    assert "recommended for it by" in out and "batch 16, concurrency 4" in out


def test_show_says_so_when_a_file_is_set_aside(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "no-config-here"))
    root = _collection(tmp_path)
    d = col.shard_dir_of(root, "gutenberg") / es.DIR
    d.mkdir()
    (d / ("0" * 64 + ".json")).write_text("{}")
    assert louise.main(["hold", "engine-settings", "show", "gutenberg", "--root", str(root),
                        "--card", "xe:8086:e223"]) == 1
    assert "set aside" in capsys.readouterr().out


def test_record_names_what_it_recommends(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "no-config-here"))
    root = _collection(tmp_path)
    assert louise.main(["hold", "engine-settings", "record", "gutenberg", "--root", str(root),
                        "--card", "xe:8086:e223"]) == 2
    assert "--batch, --concurrency" in capsys.readouterr().err


def test_record_needs_a_card_type(tmp_path, monkeypatch, capsys):
    from app import config, recommended_settings as rs
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "no-config-here"))
    monkeypatch.setattr(config, "GPU_PROBE_URL", "")
    rs.reset()
    root = _collection(tmp_path)
    assert louise.main(["hold", "engine-settings", "record", "gutenberg", "--root", str(root),
                        "--batch", "16"]) == 3
    assert "card type is not known" in capsys.readouterr().err


def test_record_refuses_a_run_this_node_has_not_validated(tmp_path, monkeypatch, capsys):
    from app.hold import embedder as emb
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "no-config-here"))
    root = _collection(tmp_path)
    monkeypatch.setattr(louise, "_recipe_for", lambda codex, name: object())
    monkeypatch.setattr(run_check, "stated_run", lambda recipe, **kw: emb.StatedRun(
        run_id="sha256:" + "a" * 64, engine={"name": "x"}, weights="sha256:" + "b" * 64,
        driver="xe"))
    monkeypatch.setattr(run_check, "probe_for", lambda codex, role="document": "probe")
    monkeypatch.setattr(run_check, "signer", lambda key=None: env.key_text(KEY.verify_key))
    assert louise.main(["hold", "engine-settings", "record", "gutenberg", "--root", str(root),
                        "--batch", "16", "--card", "xe:8086:e223"]) == 2
    assert "not validated on this node" in capsys.readouterr().err
    assert not (col.shard_dir_of(root, "gutenberg") / es.DIR).exists()


def test_record_signs_under_a_validated_run(tmp_path, monkeypatch, capsys):
    from app.bundles import keys as node_keys
    from app.hold import embedder as emb, runs as runs_mod
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "no-config-here"))
    root = _collection(tmp_path)
    run = "sha256:" + "a" * 64
    monkeypatch.setattr(louise, "_recipe_for", lambda codex, name: object())
    monkeypatch.setattr(run_check, "stated_run", lambda recipe, **kw: emb.StatedRun(
        run_id=run, engine={"name": "x"}, weights="sha256:" + "b" * 64, driver="xe"))
    monkeypatch.setattr(run_check, "probe_for", lambda codex, role="document": "probe")
    monkeypatch.setattr(run_check, "signer", lambda key=None: env.key_text(KEY.verify_key))
    monkeypatch.setattr(runs_mod, "statements", lambda *a, **k: {run: {}})
    monkeypatch.setattr(node_keys, "load_signing_key", lambda path=None: KEY)
    assert louise.main(["hold", "engine-settings", "record", "gutenberg", "--root", str(root),
                        "--batch", "16", "--concurrency", "4", "--busy", "0.82",
                        "--card", "xe:8086:e223"]) == 0
    name, codex = col.resolve(root, "gutenberg")
    body = es.read(es.path_of(col.shard_dir_of(root, name), env.key_text(KEY.verify_key)))
    e, = body["entries"]
    assert e["settings"] == {"batch": 16, "concurrency": 4}
    assert e["engine_id"] == es.engine_id_of({"name": "x"})
    assert body["collection_id"] == codex.collection_id

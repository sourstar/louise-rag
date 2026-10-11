"""tests/test_louise_ported.py — the old `python -m app.cli` commands given
their homes in `louise` (migration step 12; 07 §7.3, §7.6): `node health`,
`dev genesis recheck|recount`, `hold records show`, `hold snapshot delta`,
`hold index build|show`, `hold vectors import --archive ID --retry`,
`hold config show|apply`. None of them imports app/cli.py, which goes."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from app import louise


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_no_new_command_reaches_into_app_cli():
    import ast
    import inspect
    for mod in (louise, __import__("app.effective_config", fromlist=["x"]),
                __import__("app.hold.chain", fromlist=["x"])):
        for node in ast.walk(ast.parse(inspect.getsource(mod))):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in ("app.cli", "cli"), f"{mod.__name__} imports {node.module}"
                assert "cli" not in [a.name for a in node.names], f"{mod.__name__} imports cli"
            elif isinstance(node, ast.Import):
                assert "app.cli" not in [a.name for a in node.names], f"{mod.__name__} imports app.cli"


def test_the_old_tree_is_gone():
    """07 §7.6: retired 2026-10-08. Nothing in app/ or tools/ may bring it
    back by importing it."""
    import importlib.util
    assert importlib.util.find_spec("app.cli") is None


# ---- node health ------------------------------------------------------------

def test_node_health_is_a_check_exit_1_when_blocked(isolated_config, monkeypatch, capsys):
    from app import health
    seen = {}

    def check(cols, *, errors=False):
        seen["names"], seen["errors"] = [c.name for c in cols], errors
        return [], ["gutenberg/embed: the encoder is unreachable"]
    monkeypatch.setattr(health, "check", check)
    monkeypatch.setattr(health, "render", lambda reports: "page\n")
    assert louise.main(["node", "health", "gutenberg", "--errors"]) == 1
    assert seen == {"names": ["gutenberg"], "errors": True}
    io = capsys.readouterr()
    assert io.out == "page\n" and "gutenberg/embed: the encoder is unreachable" in io.err

    monkeypatch.setattr(health, "check", lambda cols, *, errors=False: ([], []))
    assert louise.main(["node", "health"]) == 0
    assert louise.main(["node", "health", "nope"]) == 2


def test_node_health_runs_the_real_check(isolated_config, capsys):
    assert louise.main(["node", "health"]) in (0, 1)
    assert "gutenberg" in capsys.readouterr().out


# ---- dev genesis ------------------------------------------------------------

def test_dev_genesis_recheck_reopens_and_commits(isolated_config, monkeypatch, capsys):
    from app.bundles import genesis
    seen = []

    def reopen(con, collection):
        seen.append((collection.name, con.in_transaction))
        return {"00003": 5, "00007": 12}
    monkeypatch.setattr(genesis, "reopen_finals_with_unserved", reopen)
    assert louise.main(["dev", "genesis", "recheck", "gutenberg"]) == 0
    assert seen == [("gutenberg", True)], "inside BEGIN IMMEDIATE"
    out = capsys.readouterr().out
    assert out.index("shard 00007: 12 unserved") < out.index("shard 00003: 5 unserved")
    assert "2 shard(s) reopened, 17 documents" in out

    monkeypatch.setattr(genesis, "reopen_finals_with_unserved", lambda con, c: {})
    assert louise.main(["dev", "genesis", "recheck", "gutenberg"]) == 0
    assert "every final shard" in capsys.readouterr().out
    assert louise.main(["dev", "genesis", "recheck", "nope"]) == 2


def test_dev_genesis_recount_is_a_dry_run_unless_yes(isolated_config, monkeypatch, capsys):
    from app.bundles import genesis
    writes = []

    def recount(con, *, write=False):
        writes.append(write)
        return [genesis.LedgerRecount("medcpt-pubmed", "00008", 10, 20, 12, 24),
                genesis.LedgerRecount("medcpt-pubmed", "00009", 5, 5, 5, 5)]
    monkeypatch.setattr(genesis, "recount_ledger", recount)
    assert louise.main(["dev", "genesis", "recount", "gutenberg"]) == 2
    out = capsys.readouterr().out
    assert "medcpt-pubmed/00008: docs 10 -> 12, chunks 20 -> 24" in out
    assert "00009" not in out and "nothing written" in out
    assert louise.main(["dev", "genesis", "recount", "gutenberg", "--yes"]) == 0
    assert writes == [False, True]
    assert "1 row(s) set" in capsys.readouterr().out


def test_dev_genesis_recount_over_a_real_empty_ledger(isolated_config, capsys):
    assert louise.main(["dev", "genesis", "recount", "gutenberg"]) == 0
    assert "0 ledger row(s) already match" in capsys.readouterr().out


# ---- a built SHARD: records show, snapshot delta -----------------------------

@pytest.fixture
def built(isolated_config, corpus_dir):
    """gutenberg's SHARD built and frozen as 2026-08: 3 records, 2 shards."""
    from app.hold import records as rec, snapshot as sn
    root = Path(isolated_config.DATA_DIR) / "collections"
    rec.build(root, "gutenberg", corpus_dir=corpus_dir, records_per_shard=2, log=lambda *_: None)
    rec.write_validation(root, "gutenberg")
    s1 = sn.create(root, "gutenberg", snapshot_id="2026-08", log=lambda *_: None)
    return root, s1


def _uids(root, snapshot):
    from app.pack import records as pack_records
    shard_tree = root / "gutenberg" / "shard"
    return [r["uid"] for s in snapshot.shards for r in pack_records.iter_records(shard_tree / s.path)]


def test_records_show_prints_the_record_its_layer_and_its_chunks(built, capsys):
    from app.hold import collection as col
    from app import layers as layers_mod
    root, s1 = built
    uid = _uids(root, s1)[0]
    assert louise.main(["hold", "records", "show", "gutenberg", uid, "--root", str(root)]) == 0
    got = _out(capsys)
    assert got["record"]["uid"] == uid and got["snapshot"] == "2026-08"
    assert got["shard"].startswith("records/") and got["shard"].endswith(".jsonl.zst")
    _, codex = col.resolve(root, "gutenberg")
    row = {"uid": uid, "native_id": got["record"]["native_id"]}
    want = layers_mod.chunks_for_document(codex, row, [tuple(s) for s in got["record"]["sections"]])
    assert got["chunks"] == json.loads(json.dumps(want)) and want, "the chunks the stages cut"
    assert got["layer"]["index"] == 0 and got["layer"]["chunker"]["id"]

    assert louise.main(["hold", "records", "show", "gutenberg", uid, "--raw", "--root", str(root)]) == 0
    assert _out(capsys) == got["record"], "--raw: the record, no chunks"
    assert louise.main(["hold", "records", "show", "gutenberg", "gutenberg:999999999",
                        "--root", str(root)]) == 1
    assert louise.main(["hold", "records", "show", "gutenberg", "--root", str(root)]) == 2


def test_records_show_shard_streams_it_as_json_lines(built, capsysbinary):
    root, s1 = built
    s = s1.shards[0]
    assert louise.main(["hold", "records", "show", "gutenberg", "--shard", f"{s.slice}/{s.n}",
                        "--root", str(root)]) == 0
    io = capsysbinary.readouterr()
    lines = [json.loads(x) for x in io.out.decode("utf-8").splitlines()]
    assert len(lines) == s.records and f"{s.records} records".encode() in io.err
    assert louise.main(["hold", "records", "show", "gutenberg", "--shard", "lang=xx/9",
                        "--root", str(root)]) == 2


def _write(root, snapshot):
    p = root / "gutenberg" / "shard" / "snapshots" / f"{snapshot.snapshot_id}.json"
    p.write_bytes(snapshot.canonical())


def test_snapshot_delta_composes_the_chain(built, capsys):
    from app.hold import snapshot as sn
    root, s1 = built
    s2 = dataclasses.replace(s1, snapshot_id="2026-09", previous=s1.hash, delta=sn.Delta(
        added=("gutenberg:900", "gutenberg:901"), changed=("gutenberg:1",),
        tombstoned=(sn.Tombstone("gutenberg:2", "withdrawn"),)))
    s3 = dataclasses.replace(s1, snapshot_id="2026-10", previous=s2.hash, delta=sn.Delta(
        added=("gutenberg:2",), changed=("gutenberg:900",),
        tombstoned=(sn.Tombstone("gutenberg:901", "retracted"),
                    sn.Tombstone("gutenberg:3", "retracted"))))
    _write(root, s2); _write(root, s3)

    assert louise.main(["hold", "snapshot", "delta", "gutenberg", "2026-08", "2026-10",
                        "--root", str(root)]) == 0
    got = _out(capsys)
    assert got["snapshots"] == ["2026-09", "2026-10"]
    assert got["delta"] == {"added": ["gutenberg:900"],
                            "changed": ["gutenberg:1", "gutenberg:2"],
                            "tombstoned": [{"uid": "gutenberg:3", "reason": "retracted"}]}
    assert got["counts"] == {"added": 1, "changed": 2, "tombstoned": 1}

    assert louise.main(["hold", "snapshot", "delta", "gutenberg", "2026-09", "2026-09",
                        "--root", str(root)]) == 0
    assert _out(capsys)["delta"] == {"added": [], "changed": [], "tombstoned": []}
    assert louise.main(["hold", "snapshot", "delta", "gutenberg", "2026-10", "2026-08",
                        "--root", str(root)]) == 2
    assert louise.main(["hold", "snapshot", "delta", "gutenberg", "2026-08", "2026-12",
                        "--root", str(root)]) == 2


def test_snapshot_delta_refuses_a_gap_or_a_broken_link(built, capsys):
    from app.hold import snapshot as sn
    root, s1 = built
    empty = sn.Delta((), (), ())
    s2 = dataclasses.replace(s1, snapshot_id="2026-09", previous=s1.hash, delta=empty)
    s3 = dataclasses.replace(s1, snapshot_id="2026-10", previous=s2.hash, delta=empty)
    _write(root, s3)                                  # 2026-09 not held
    assert louise.main(["hold", "snapshot", "delta", "gutenberg", "2026-08", "2026-10",
                        "--root", str(root)]) == 2
    assert "gap" in capsys.readouterr().err
    _write(root, dataclasses.replace(s2, previous="sha256:" + "0" * 64))
    assert louise.main(["hold", "snapshot", "delta", "gutenberg", "2026-08", "2026-10",
                        "--root", str(root)]) == 2
    assert "broken" in capsys.readouterr().err


# ---- hold index ---------------------------------------------------------------

def test_index_show_and_build_over_the_vector_table(isolated_config, monkeypatch, capsys):
    from app import vector_index
    root = str(Path(isolated_config.DATA_DIR) / "collections")
    assert louise.main(["hold", "index", "show", "gutenberg", "--root", root]) == 0
    assert _out(capsys) == {"collection": "gutenberg", "vector_table": False}

    assert louise.main(["hold", "index", "build", "gutenberg", "--root", root]) == 2
    assert louise.main(["hold", "index", "build", "gutenberg", "--text", "--root", root]) == 2
    assert "fts stage" in capsys.readouterr().err
    assert louise.main(["hold", "index", "build", "gutenberg", "--vector", "--root", root]) == 1
    assert "no vector table" in capsys.readouterr().err

    built = []
    monkeypatch.setattr(vector_index, "build_scalar",
                        lambda c, log=None: built.append(("book_id", c.lance_dir)) or {"built": True})
    monkeypatch.setattr(vector_index, "build",
                        lambda c, index_type=None, partitions=None, log=None:
                        built.append(("vector", index_type, partitions)) or {"built": True, "type": "ivf_flat"})
    assert louise.main(["hold", "index", "build", "gutenberg", "--vector", "--type", "ivf_flat",
                        "--partitions", "4", "--root", root]) == 0
    lance = Path(root) / "gutenberg" / "lance"
    assert built == [("book_id", lance), ("vector", "ivf_flat", 4)], "the collection's own table"


# ---- vectors import --retry -------------------------------------------------------

def test_vectors_import_retry_puts_rejected_shards_back(isolated_config, monkeypatch, capsys):
    from app.bundles import genesis
    root = str(Path(isolated_config.DATA_DIR) / "collections")
    calls = []

    def retry(con, archive_id=None, shard=None):
        calls.append((archive_id, shard, con.in_transaction))
        return [(archive_id, shard or "00004")]
    monkeypatch.setattr(genesis, "retry_rejected", retry)
    assert louise.main(["hold", "vectors", "import", "gutenberg", "--archive", "medcpt-pubmed",
                        "--retry", "--root", root]) == 0
    assert calls == [("medcpt-pubmed", None, True)]
    assert "medcpt-pubmed/00004: rejected -> fetched" in capsys.readouterr().out
    calls.clear()
    assert louise.main(["hold", "vectors", "import", "gutenberg", "--archive", "medcpt-pubmed",
                        "--retry", "--shards", "00001", "00002", "--root", root]) == 0
    assert [c[1] for c in calls] == ["00001", "00002"]
    assert louise.main(["hold", "vectors", "import", "gutenberg", "--bundle", "x",
                        "--retry", "--root", root]) == 2


def test_vectors_import_retry_over_a_real_ledger(isolated_config, capsys):
    from app import db
    from app.hold import collection as col, records as rec
    root = Path(isolated_config.DATA_DIR) / "collections"
    name, codex = col.resolve(root, "gutenberg")
    con = db.connect(rec.local_collection(root, name, codex))
    con.execute("INSERT INTO genesis_imports(archive, shard, state, detail) "
                "VALUES('medcpt-pubmed', '00004', 'rejected', 'a sample of one')")
    con.close()
    assert louise.main(["hold", "vectors", "import", "gutenberg", "--archive", "medcpt-pubmed",
                        "--retry", "--root", str(root)]) == 0
    con = db.connect(rec.local_collection(root, name, codex))
    row = con.execute("SELECT state, detail FROM genesis_imports").fetchone()
    assert (row["state"], row["detail"]) == ("fetched", None)
    assert louise.main(["hold", "vectors", "import", "gutenberg", "--archive", "medcpt-pubmed",
                        "--retry", "--root", str(root)]) == 0
    assert "nothing rejected" in capsys.readouterr().out


# ---- hold config ------------------------------------------------------------

def test_config_show_labels_each_value_with_its_tier(isolated_config, capsys):
    from app import collections as collections_mod, db
    assert louise.main(["hold", "config", "show", "gutenberg"]) == 0
    got = _out(capsys)
    c = collections_mod.by_name("gutenberg")
    [g] = got["collections"]
    v = g["values"]
    assert g["collection_id"] == c.codex.collection_id
    assert v["embed_mode"]["value"] == collections_mod.DEFAULT_EMBED_MODE
    assert v["embed_mode"]["tier"] == "files"
    recipe = collections_mod.recipe_for(c)
    assert v["encoder.url"] == {"value": recipe.url, "tier": "files"}
    # 07 §7.8's keys (amended 2026-10-08), both sides, no `model`
    assert v["encoder.embed_model"] == {"value": recipe.model, "tier": "files"}
    assert v["encoder.query_model"] == {"value": recipe.query_model, "tier": "files"}
    assert "encoder.model" not in v
    assert v["encoder.dim"] == {"value": c.codex.space.dim, "tier": "codex"}
    assert v["share"] == {"value": c.share, "tier": "files"}
    assert got["node"]["mode"]["tier"] == "state database"

    con = db.connect(c)
    db.set_scope(con, {**db.get_scope(con), "embed_mode": "off"})
    con.close()
    assert louise.main(["hold", "config", "show"]) == 0
    v = _out(capsys)["collections"][0]["values"]["embed_mode"]
    assert (v["value"], v["tier"], v["files"]) == ("off", "state database",
                                                    collections_mod.DEFAULT_EMBED_MODE)
    assert louise.main(["hold", "config", "show", "nope"]) == 2


def test_config_apply_gives_the_file_back_its_value(isolated_config, config_dir, capsys):
    from app import collections as collections_mod, db
    c = collections_mod.by_name("gutenberg")
    con = db.connect(c)
    db.set_scope(con, {**db.get_scope(con), "embed_mode": "off"})
    con.close()
    assert louise.main(["hold", "config", "apply", "gutenberg"]) == 0
    got = _out(capsys)
    assert got["applied"] == [{"name": "gutenberg", "embed_mode": collections_mod.DEFAULT_EMBED_MODE,
                               "replaced": "off", "encoder": "named in its file"}]
    con = db.connect(c)
    assert db.get_scope(con)["embed_mode"] == collections_mod.DEFAULT_EMBED_MODE
    con.close()
    assert louise.main(["hold", "config", "apply"]) == 0
    assert _out(capsys)["applied"][0]["replaced"] is None, "nothing to replace twice"

    # a file the node refuses is named, exit 1 (what `embed materialize` said)
    (Path(config_dir) / "collections" / "bare.toml").write_text('plugin = "gutenberg"\n', encoding="utf-8")
    assert louise.main(["hold", "config", "apply"]) == 1
    assert [r["name"] for r in _out(capsys)["refused"]] == ["bare"]

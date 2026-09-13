"""Two CLI commands that raised NameError on their ordinary path — found
2026-09-13 by an undefined-name sweep after `pack cat` crashed in a new
test: cli.py used `json` and `config` without importing either.

`pack cat` is exercised in tests/pack/test_pack_read_ungated.py; this is
`index vector status` on a collection whose vector table has no index yet,
the state every new collection starts in.
"""
import argparse
import types

from app import cli, vector_index


def test_vector_status_of_an_unindexed_table(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_collection_by_name", lambda name: types.SimpleNamespace(name=name))
    monkeypatch.setattr(vector_index, "stats", lambda collection: {"rows": 5, "indexed": False})
    assert cli.cmd_vindex_status(argparse.Namespace(collection="c")) == 0
    out = capsys.readouterr().out
    assert "5 rows, NO vector index" in out and "built automatically at" in out

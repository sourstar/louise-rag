"""`pack verify`, `pack cat` and `pack dump` are NOT license-gated — the
operator's decision, 2026-09-13: reading a pack by path on local disk is
inspection, not use (invariant 9). They take a path, not a collection;
the data enters the node only through discovery, import or adopt, which
are gated, and `verify` is the check a pack gets before anyone
acknowledges its terms.

This pins the decision, so a later audit that finds three "ungated doors"
reads why before closing them: here, asking the gate at all fails.
"""
import argparse
import json

import pytest

from app import cli, licensing
from app.pack import manifest as manifest_mod
from tests.pack.test_verify import _pack


def _no_gate(*a, **k):
    raise AssertionError("reading a pack by path asks no license gate (operator, 2026-09-13)")


@pytest.fixture
def pack_dir(isolated_config, tmp_path, corpus_dir, monkeypatch):
    built = _pack(tmp_path, corpus_dir)         # the build IS gated: before the patch
    for name in ("state_for", "require_ack", "require_ack_or_exit", "license_class_for"):
        monkeypatch.setattr(licensing, name, _no_gate)
    return built


def test_verify_cat_and_dump_read_a_pack_without_asking_the_gate(pack_dir, capsys):
    shard = pack_dir / manifest_mod.load(pack_dir)["shards"][0]["path"]

    assert cli.cmd_pack_dump(argparse.Namespace(shard=str(shard))) == 0
    uid = json.loads(capsys.readouterr().out.splitlines()[0])["uid"]

    assert cli.cmd_pack_cat(argparse.Namespace(pack=str(pack_dir), uid=uid)) == 0
    assert json.loads(capsys.readouterr().out)["record"]["uid"] == uid

    assert cli.cmd_pack_verify(argparse.Namespace(
        pack=str(pack_dir), checks=None, sample=1.0, seed=0)) == 0

"""`louise hold records show` — was `pack cat` / `pack dump` (07 §7.6) — is
NOT license-gated: the operator's decision, 2026-09-13, that reading what
is on local disk is inspection, not use (invariant 9). The data enters the
node's use only through the doors that ARE gated (build, embed, import,
export, check: tests/hold/test_license_gate_hold.py).

This pins the decision, so a later audit that finds an "ungated door"
reads why before closing it: here, asking the gate at all fails.
(`pack verify` had the same exemption; its successor `hold check` embeds
sampled text and writes attestations made to be exchanged, so it asks.)
"""
import json

import pytest

from app import licensing, louise
from tests.test_louise_ported import _uids, built  # noqa: F401  (the fixture)


def _no_gate(*a, **k):
    raise AssertionError("reading a record on local disk asks no license gate "
                         "(operator, 2026-09-13)")


@pytest.fixture
def ungated(built, monkeypatch):                                  # noqa: F811
    for name in ("state_for", "require_ack", "require_ack_or_exit", "license_class_for"):
        monkeypatch.setattr(licensing, name, _no_gate)
    return built


def test_records_show_reads_a_record_and_a_shard_without_asking_the_gate(ungated, capsys):
    root, s1 = ungated
    uid = _uids(root, s1)[0]
    assert louise.main(["hold", "records", "show", "gutenberg", uid, "--root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["record"]["uid"] == uid

    s = s1.shards[0]
    assert louise.main(["hold", "records", "show", "gutenberg", "--shard", f"{s.slice}/{s.n}",
                        "--root", str(root)]) == 0

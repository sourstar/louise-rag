"""Phase B's review (2026-10-22): `state/predecessors.json` is appended under
a lock, so two writers never lose an entry, and the wait for it is bounded;
and every rebuild of the possession index is said with its cost, so a fetch
beat that rebuilds once per accepted bundle shows in the log."""
import fcntl
import json
import threading

import pytest

from app.share import contribute, coverage as cov, possession
from tests.share.test_contribute import ready  # noqa: F401


def test_concurrent_appends_all_land(tmp_path):
    ids = [f"sha256:{i:064x}" for i in range(16)]
    start = threading.Barrier(len(ids))

    def add(cid):
        start.wait()
        contribute.add_predecessor(tmp_path, "c", cid)

    threads = [threading.Thread(target=add, args=(cid,)) for cid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(contribute.predecessors(tmp_path, "c")) == sorted(ids)
    assert contribute.add_predecessor(tmp_path, "c", ids[0]) is False, "append-only, no duplicate"


def test_a_held_lock_is_waited_for_a_bounded_time_then_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(contribute, "PREDECESSORS_LOCK_S", 0.2)
    contribute.add_predecessor(tmp_path, "c", "sha256:" + "a" * 64)
    p = tmp_path / "c" / "state" / contribute.PREDECESSORS
    with open(p.with_suffix(".json.lock"), "a+b") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        with pytest.raises(OSError, match="lock not free"):
            contribute.add_predecessor(tmp_path, "c", "sha256:" + "b" * 64)
    assert json.loads(p.read_text()) == ["sha256:" + "a" * 64], "nothing written without the lock"


def test_each_coverage_change_rebuilds_the_index_once_and_says_so(ready, capsys):
    root, _snapshot, codex, kw = ready
    con = cov.ensure_schema(kw["con"])
    key = str(root / "gutenberg")
    possession.REBUILDS.pop(key, None)
    possession.index(root, "gutenberg", codex, con)
    possession.index(root, "gutenberg", codex, con)
    assert possession.REBUILDS[key] == 1, "unchanged table: read from the memo"
    cov.record(con, bundle_id="sha256:" + "c" * 64, shard=cov.shard_id(codex, "lang=en/00001"),
               producer="k", uids=[], chunks=0, tiers={}, source=cov.FETCHED,
               accepted_at="2026-10-22T00:00:00Z")
    possession.index(root, "gutenberg", codex, con)
    assert possession.REBUILDS[key] == 2
    out = capsys.readouterr().out
    assert out.count("[gutenberg info:share] possession index rebuilt") == 2
    assert "in this process" in out and " ms" in out

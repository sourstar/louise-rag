"""The batch line is throttled to one per collection+stage per 30 s, and it
carries every batch it stands for: on 2026-10-30 the lines of a measured fts
run summed to 604 of the 2,000 books it indexed, and the log read as a 30 s
pacing that was only its own throttle."""
from types import SimpleNamespace

from app import db, worker


def test_the_line_carries_the_batches_the_throttle_folded_into_it(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(db, "log_event", lambda con, level, stage, msg: lines.append(msg))
    monkeypatch.setattr(worker, "_batch_log_ts", {})
    monkeypatch.setattr(worker, "_batch_since", {})
    clock = iter([100.0, 110.0, 120.0, 131.0])
    monkeypatch.setattr(worker.time, "time", lambda: next(clock))
    c = SimpleNamespace(name="g")
    for docs, chunks in ((10, 1000), (12, 1500), (9, 900), (14, 1600)):
        worker._batch_log(None, "fts", c, f"indexed {docs}", docs=docs, chunks=chunks, secs=2.0)
    assert lines == ["indexed 10",
                     "indexed 14; since the last line: 3 batches, 35 documents, 4000 chunks "
                     "in 6.0s of stage time"]


def test_a_notice_without_counts_is_throttled_and_counts_nothing(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(db, "log_event", lambda con, level, stage, msg: lines.append(msg))
    monkeypatch.setattr(worker, "_batch_log_ts", {})
    monkeypatch.setattr(worker, "_batch_since", {})
    worker._batch_log(None, "embed", SimpleNamespace(name="g"), "one read failed")
    assert lines == ["one read failed"] and worker._batch_since == {}

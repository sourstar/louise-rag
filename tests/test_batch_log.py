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


def test_a_one_document_batch_is_counted_and_writes_no_line_of_its_own(monkeypatch):
    """It wrote nothing before #78, and a line it wrote went to the stdout
    of a command whose answer is JSON (`hold source list --json` after an
    in-process round: the deposit test, 2026-10-30)."""
    lines: list[str] = []
    monkeypatch.setattr(db, "log_event", lambda con, level, stage, msg: lines.append(msg))
    monkeypatch.setattr(worker, "_batch_log_ts", {})
    monkeypatch.setattr(worker, "_batch_since", {})
    clock = iter([100.0, 140.0])
    monkeypatch.setattr(worker.time, "time", lambda: next(clock))
    c = SimpleNamespace(name="g")
    worker._batch_log(None, "rdf", c, "extracted 1", docs=1, secs=0.1, emit=False)
    assert lines == []
    worker._batch_log(None, "rdf", c, "extracted 5", docs=5, secs=0.5)
    assert lines == ["extracted 5; since the last line: 2 batches, 6 documents in 0.6s of stage time"]

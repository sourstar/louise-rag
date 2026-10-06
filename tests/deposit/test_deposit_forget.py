"""tests/deposit/test_deposit_forget.py — `hold collection forget` (07 §7.3,
§7.7; 02 §2.9): the copy and its held file go; a never-share collection's
deposits are erased first and leave no trace; the worker route retires the
collection's threads before anything under its directory goes; `--kinds
vectors` takes the vector side only; forgetting what is not held is
nothing to do; the command wants `--yes` off a terminal."""
import io
import threading
from pathlib import Path

import pytest

from app import collections as collections_mod, jobs, louise
from app.hold import codex as cx, collection as col, deposit, forget as forget_mod
from tests.deposit.test_deposit_identity import ENCODER_URL, make, node  # noqa: F401

TEXT = "Il deposito segreto parla di espresso e di tram."


def held_with_deposits(node, name="alice"):  # noqa: F811
    codex = make(node, name)
    deposit.add(name, "uno.txt", TEXT.encode())
    deposit.add(name, "due.txt", "Un secondo deposito, sul vino.".encode())
    return codex


def files_with(root: Path, needle: str) -> list[str]:
    out = []
    for p in root.rglob("*"):
        if p.is_file():
            try:
                if needle.encode() in p.read_bytes():
                    out.append(str(p))
            except OSError:
                pass
    return out


# ---- the copy goes, no trace ----------------------------------------------

def test_forget_erases_every_deposit_and_removes_the_collection(node):  # noqa: F811
    held_with_deposits(node)
    held = deposit.held_file(node, "alice")
    assert held.exists() and (node / "alice").is_dir()
    assert files_with(node / "alice", "espresso"), "the deposit is stored before"
    out = forget_mod.forget(node, "alice")
    assert out["erased_deposits"] == 2 and len(out["removed"]) == 2
    assert not held.exists() and not (node / "alice").exists()
    assert files_with(node.parent.parent, "espresso") == [], "no trace anywhere (02 §2.9)"
    assert collections_mod.by_name("alice") is None


def test_forget_removes_a_curated_collection_whole(node):  # noqa: F811
    from tests.hold.conftest import a_root, pubmed
    codex = a_root(node, pubmed())
    name = col.name_of(node, codex.collection_id)
    louise._write_held_file(node, codex, ENCODER_URL)
    held = deposit.held_file(node, name)
    assert held.exists() and (node / name / "drag" / "codex.json").exists()
    out = forget_mod.forget(node, name)
    assert out["erased_deposits"] == 0 and len(out["removed"]) == 2
    assert not held.exists() and not (node / name).exists()


def test_dry_run_names_what_would_go_and_removes_nothing(node):  # noqa: F811
    held_with_deposits(node)
    out = forget_mod.forget(node, "alice", dry_run=True)
    assert out["dry_run"] and len(out["would_remove"]) == 2 and out["removed"] == []
    assert deposit.held_file(node, "alice").exists() and (node / "alice").is_dir()


def test_forgetting_what_is_not_held_is_nothing_to_do(node):  # noqa: F811
    """07 §7.7: a member is gone entire or still there — and gone is fine."""
    held_with_deposits(node)
    forget_mod.forget(node, "alice")
    assert forget_mod.forget(node, "alice") == {"collection": "alice", "gone": True,
                                                "removed": []}


# ---- only the vectors -------------------------------------------------------

def test_kinds_vectors_takes_the_vector_side_only(node):  # noqa: F811
    codex = held_with_deposits(node)
    drag = node / "alice" / "drag"
    (drag / "vectors-0123456789abcdef-all").mkdir()
    (drag / "vectors-0123456789abcdef-all" / "x.parquet").write_bytes(b"v")
    (drag / "records-all").mkdir()
    (drag / "records-all" / "r.jsonl").write_bytes(b"r")
    assert (node / "alice" / "lance").is_dir()
    out = forget_mod.forget(node, "alice", kinds=("vectors",))
    assert not (drag / "vectors-0123456789abcdef-all").exists()
    assert not (node / "alice" / "lance").exists()
    assert (drag / "records-all").exists() and (drag / "codex.json").exists()
    assert deposit.held_file(node, "alice").exists()
    assert col.read(node, "alice").collection_id == codex.collection_id
    assert sorted(Path(p).name for p in out["removed"]) == \
        ["lance", "vectors-0123456789abcdef-all"]


def test_other_kinds_are_refused(node):  # noqa: F811
    held_with_deposits(node)
    with pytest.raises(cx.CodexError, match="--kinds records"):
        forget_mod.forget(node, "alice", kinds=("records",))
    assert deposit.held_file(node, "alice").exists()


# ---- the worker route ---------------------------------------------------------

def test_the_worker_route_retires_the_threads_then_forgets(node, monkeypatch):  # noqa: F811
    held_with_deposits(node)
    from app import worker
    retired: list[str] = []
    monkeypatch.setattr(worker, "retire_collection",
                        lambda name, timeout=60.0: retired.append(name))
    job = jobs.Job(id=1, kind=jobs.DEPOSIT, args={"op": "forget", "collection": "alice"},
                   state=jobs.RUNNING, result=None, error=None, created=0.0,
                   started=0.0, finished=None)
    out = jobs.run(job)
    assert retired == ["alice"] and out["erased_deposits"] == 2
    assert not (node / "alice").exists()


def test_retire_collection_stops_the_collections_threads_and_waits(monkeypatch):
    from app import worker
    gone, stopped = threading.Event(), threading.Event()

    def run():
        gone.wait(5)
        stopped.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    monkeypatch.setitem(worker._pipeline_threads, "zed", (t, gone))
    worker.retire_collection("zed", timeout=5)
    assert stopped.is_set() and not t.is_alive()
    assert "zed" not in worker._pipeline_threads and "zed" not in worker._gone


# ---- the command ----------------------------------------------------------------

def test_the_command_needs_yes_off_a_terminal_and_then_forgets_here(node, monkeypatch):  # noqa: F811
    held_with_deposits(node)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))          # not a terminal
    assert louise.main(["hold", "collection", "forget", "alice",
                        "--root", str(node), "--here"]) == 2
    assert deposit.held_file(node, "alice").exists()
    assert louise.main(["hold", "collection", "forget", "alice",
                        "--root", str(node), "--here", "--yes"]) == 0
    assert not (node / "alice").exists()
    assert louise.main(["hold", "collection", "forget", "alice",
                        "--root", str(node), "--here", "--yes"]) == 0, "gone is fine"

"""The index maintainer yields the CPU to the pipeline (INDEX_MAINTAINER_NICE).

Live 2026-09-18: a wikipedia refresh on pylance's runtime threads took 12.2
of 12 cores at the same priority as the pipeline. The thread said
"low-priority" in its docstring and nothing made it so.
"""
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from app import worker

ROOT = Path(__file__).resolve().parent.parent
linux_only = pytest.mark.skipif(not hasattr(os, "setpriority") or sys.platform != "linux",
                                reason="per-thread nice is a Linux property")


@linux_only
def test_the_maintainer_lowers_its_own_thread_and_no_other(monkeypatch):
    main_before = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())
    seen = {}

    def first_wait(timeout):
        tid = threading.get_native_id()
        seen["maintainer"] = os.getpriority(os.PRIO_PROCESS, tid)
        return True                                  # stop at once
    monkeypatch.setattr(worker._stop, "wait", first_wait)
    t = threading.Thread(target=worker._index_maintainer)
    t.start()
    t.join(10)
    assert seen["maintainer"] == min(19, main_before + worker.INDEX_MAINTAINER_NICE)
    assert os.getpriority(os.PRIO_PROCESS, threading.get_native_id()) == main_before


@linux_only
def test_pylance_threads_inherit_it_and_lancedbs_do_not(tmp_path):
    """The mechanism, in a fresh process so no runtime exists yet: lancedb's
    is created first on the main thread (refuse_boot does that at boot), then
    the lowered thread's first pylance call creates pylance's."""
    pytest.importorskip("lance")
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent(f"""
        import os, threading, lancedb
        from app import worker
        db = lancedb.connect({str(tmp_path / "db")!r})
        tbl = db.create_table("t", data=[{{"vector": [1.0, 2.0], "id": 1}}])
        def maintainer():
            worker._lower_own_priority(worker.INDEX_MAINTAINER_NICE)
            tbl.to_lance().count_rows()
        t = threading.Thread(target=maintainer); t.start(); t.join()
        print("BASE", os.getpriority(os.PRIO_PROCESS, threading.get_native_id()))
        for tid in os.listdir("/proc/self/task"):
            try:
                stat = open(f"/proc/self/task/{{tid}}/stat").read()
            except (FileNotFoundError, ProcessLookupError):
                continue     # ended after the listing (seen on the node, 2026-09-18)
            name = stat[stat.index("(") + 1:stat.rindex(")")].rstrip("0123456789")
            nice = int(stat[stat.rindex(")") + 2:].split()[16])
            print(name, nice)
        import sys; sys.stdout.flush(); os._exit(0)   # a lancedb teardown crash must not eat the answer
    """))
    out = subprocess.run([sys.executable, str(script)], cwd=ROOT, capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=str(ROOT)), timeout=120)
    assert out.returncode == 0, out.stderr
    nice: dict[str, set[int]] = {}
    for line in out.stdout.splitlines():
        name, value = line.rsplit(" ", 1)
        nice.setdefault(name, set()).add(int(value))
    base = nice.pop("BASE").pop()
    assert nice.get("lance_backgroun") == {base + worker.INDEX_MAINTAINER_NICE}, nice
    assert nice.get("lancedb-tokio-w") == {base}, nice

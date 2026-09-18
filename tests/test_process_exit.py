"""app/process_exit: the observable half of a normal exit, without the
interpreter teardown where lancedb 0.38 crashes — exit 139 after the work
had committed, 9 of 80 short-lived processes on the node (2026-09-18)."""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app import process_exit

ROOT = Path(__file__).resolve().parent.parent


def _child(tmp_path: Path, body: str, name: str = "child.py") -> subprocess.Popen:
    script = tmp_path / name
    script.write_text(textwrap.dedent(body))
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.Popen([sys.executable, str(script)], cwd=ROOT, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@pytest.mark.parametrize("leave", ["process_exit.run(lambda: 7)", "sys.exit(7)"])
def test_threads_atexit_and_output_survive_and_only_the_teardown_is_skipped(tmp_path, leave):
    d = tmp_path / "marks"
    d.mkdir()
    proc = _child(tmp_path, f"""
        import atexit, sys, threading, time
        from concurrent.futures import ThreadPoolExecutor
        from app import process_exit
        D = {str(d)!r}
        atexit.register(lambda: open(D + "/atexit", "w").write("ran"))
        def late():
            time.sleep(0.3)
            open(D + "/thread", "w").write("joined")
        threading.Thread(target=late).start()        # non-daemon: joined first
        ThreadPoolExecutor(2).submit(lambda: None)   # idle workers must not hang it
        class Teardown:
            path = D + "/teardown"
            def __del__(self, open=open):
                open(self.path, "w").write("ran")
        keep = Teardown()                             # collected only by the teardown
        sys.stdout.write("buffered, no newline")
        {leave}
    """)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 7, err
    assert out == "buffered, no newline"
    assert (d / "atexit").read_text() == "ran"
    assert (d / "thread").read_text() == "joined"
    # the control run proves the probe sees a teardown when there is one
    assert (d / "teardown").exists() == (leave == "sys.exit(7)")


def test_the_status_is_the_one_sys_exit_would_give(tmp_path, capsys):
    assert process_exit._code(None) == 0
    assert process_exit._code(3) == 3
    assert process_exit._code("no such collection") == 1
    assert capsys.readouterr().err == "no such collection\n"
    proc = _child(tmp_path, """
        from app import process_exit
        def main():
            raise RuntimeError("the command failed")
        process_exit.run(main)
    """)
    _out, err = proc.communicate(timeout=60)
    assert proc.returncode == 1
    assert "RuntimeError: the command failed" in err and "Traceback" in err


def test_processes_that_used_lancedb_exit_with_their_own_status(tmp_path):
    """The crash itself, as far as a test reaches it: twelve processes at
    once, each having opened a table and searched it. Through plain
    interpreter exit about one in nine died SIGSEGV; through run() none
    can, because the teardown never happens."""
    lancedb = pytest.importorskip("lancedb")
    db = lancedb.connect(str(tmp_path / "db"))
    db.create_table("t", data=[{"vector": [float(i), 1.0], "id": i} for i in range(64)])
    procs = [_child(tmp_path, f"""
        import lancedb
        from app import process_exit
        t = lancedb.connect({str(tmp_path / "db")!r}).open_table("t")
        t.search([1.0, 1.0]).limit(3).to_list()
        process_exit.run(lambda: 0)
    """, name=f"child{i}.py") for i in range(12)]
    codes = [p.wait(timeout=120) for p in procs]
    for p in procs:
        p.communicate()
    assert codes == [0] * 12, codes

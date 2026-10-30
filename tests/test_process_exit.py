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
    proc = _child(tmp_path, """
        from app import process_exit
        def main():
            raise KeyboardInterrupt
        process_exit.run(main)
    """, name="ctrl_c.py")
    _out, err = proc.communicate(timeout=60)
    assert proc.returncode == 130 and "KeyboardInterrupt" in err
    proc = _child(tmp_path, """
        import sys
        from app import process_exit
        process_exit.run(lambda: sys.exit("no such collection"))
    """, name="message.py")
    _out, err = proc.communicate(timeout=60)
    assert proc.returncode == 1 and err == "no such collection\n"


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


_POOL_CHILD_KILLED = """
    import os, signal, sys, time, multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures.process import BrokenProcessPool
    from app import process_exit
    def work(payload):
        time.sleep(0.05)
        return len(payload)
    def main():
        {setup}
        ex = ProcessPoolExecutor(4, mp_context=multiprocessing.get_context("spawn"))
        payload = b"x" * 4_000_000
        futs = [ex.submit(work, payload) for _ in range(400)]
        time.sleep(1.5)
        os.kill(next(iter(ex._processes)), signal.SIGKILL)      # the kernel's OOM kill
        try:
            for f in futs:
                f.result()
        except BrokenProcessPool:
            print("the pool's parent recovers")
        return 0
    if __name__ == "__main__":
        process_exit.run(main)
"""


@pytest.mark.parametrize("setup, code", [("pass", 0),
                                         # killed by signal 13: the shell's 141
                                         ("signal.signal(signal.SIGPIPE, signal.SIG_DFL)", -13)])
def test_a_pool_child_the_kernel_kills_does_not_take_its_parent_with_it(tmp_path, setup, code):
    """München, 2026-10-30: the records build died 141 four seconds after a
    child's OOM kill — SIGPIPE, set to its default for `| head`, killed the
    parent feeding the widowed pipe before the pool could take its parts
    again. The control run (SIG_DFL) proves the probe sees it."""
    proc = _child(tmp_path, _POOL_CHILD_KILLED.format(setup=setup))
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == code, err[-2000:]
    # under SIG_DFL the parent may see the broken pool first: the feeder
    # thread's next write kills it all the same
    assert code != 0 or "the pool's parent recovers" in out


def test_no_entry_point_sets_sigpipe_back_to_its_default():
    """Python's "Note on SIGPIPE": a closed stdout is handled where it is
    written (`process_exit.run`), never by SIG_DFL for the whole process."""
    import re
    hits = [str(p.relative_to(ROOT)) for p in (ROOT / "app").rglob("*.py")
            if re.search(r"SIGPIPE\s*,\s*signal\.SIG_DFL", p.read_text(encoding="utf-8"))]
    assert hits == []


def test_a_closed_stdout_is_a_quiet_exit_and_a_pipe_of_its_own_is_an_error(tmp_path):
    script = tmp_path / "head.py"
    script.write_text(textwrap.dedent("""
        import os, sys
        from app import process_exit
        def main():
            if sys.argv[1] == "own":
                r, w = os.pipe()
                os.close(r)
                os.write(w, b"x")                    # a pipe of its own, widowed
            for i in range(1_000_000):
                print(i)
        process_exit.run(main)
    """))
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    for kind in ("stdout", "own"):
        p = subprocess.Popen([sys.executable, str(script), kind], cwd=ROOT, env=env, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if kind == "stdout":
            assert p.stdout.readline() == "0\n"
            p.stdout.close()                         # `| head -1`
            err = p.stderr.read()
        else:
            out, err = p.communicate(timeout=60)
        assert p.wait(timeout=60) == 1
        if kind == "stdout":
            assert err == "", "nothing to say: the reader had what it wanted"
        else:
            assert "BrokenPipeError" in err

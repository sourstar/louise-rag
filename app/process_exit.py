"""Leaving the interpreter without its finalization, where lancedb crashes.

lancedb 0.38 crashes at interpreter exit: a `lancedb-tokio-w` thread calls
into libpython while Py_FinalizeEx tears it down (`_lancedb.abi3.so`
+0x8dc90ab -> `libpython3.12.so` +0x273130). Measured on the node on
2026-09-18: 9 of 80 short-lived processes that had run a search died with
SIGSEGV (exit 139) AFTER their work had committed, with pylance installed
or not. The data was never at risk; the exit code was wrong. docs/07 §7.7's
"exits 130 on a signal" read 139 about one stop in nine, and any `louise`
command could report a failure it did not have.

`run(main)` does everything a normal exit does that anyone can observe,
in CPython's own order: threading's shutdown (executors stopped,
non-daemon threads joined), then the atexit callbacks (logging,
multiprocessing), then a flush of stdout and stderr. Then it leaves with
os._exit, skipping only the module teardown where the crash lives.
"""
from __future__ import annotations

import atexit
import os
import sys
import threading
import traceback
from typing import Callable, NoReturn


def _code(value: object) -> int:
    """The status `sys.exit(value)` would give."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    print(value, file=sys.stderr)
    return 1


def finish(code: int) -> NoReturn:
    """Exit with `code` after the observable half of a normal exit.
    Called from the main thread, once, as the last thing the process
    does."""
    try:
        threading._shutdown()          # Py_FinalizeEx's first step: join threads
        atexit._run_exitfuncs()        # its second
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  # noqa: BLE001 — a closed pipe has nothing left to say
                pass
        os._exit(code)


def run(main: Callable[[], object]) -> NoReturn:
    """`if __name__ == "__main__": process_exit.run(main)` — the exit
    status of `sys.exit(main())`, without the teardown."""
    try:
        code = _code(main())
    except SystemExit as e:
        code = _code(e.code)
    except KeyboardInterrupt:
        traceback.print_exc()
        code = 130                     # what the shell reports for an unhandled Ctrl-C
    except BaseException:  # noqa: BLE001 — reported exactly as the interpreter would
        traceback.print_exc()
        code = 1
    finish(code)

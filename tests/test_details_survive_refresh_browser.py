"""A drawer the reader opened stays open while its panel refreshes — in a
real browser.

The verbose text waits behind one closed line (operator, 2026-11-02: "A
crowded interface is a bad interface"). On the System tab the GPU panel is
swapped in by htmx every 5 s, so a drawer there would shut under the reader
at the next swap; each carries `hx-preserve` and an id. Whether htmx keeps
it is a question for a browser: tests/browser/details_survive_refresh.js
opens one and watches two refreshes. Skipped, saying why, where no browser
and no driver for it are installed.
"""
import os
import subprocess
from pathlib import Path

from tests.test_mobile_width_browser import CHROMIUM, NODE, pytestmark, served  # noqa: F401

SCRIPT = Path(__file__).parent / "browser" / "details_survive_refresh.js"


def test_an_open_drawer_survives_the_gpu_panels_refresh(served):
    env = dict(os.environ, ADMIN_TOKEN="sekrit", CHROMIUM=CHROMIUM or "")
    if NODE:
        env["NODE_PATH"] = os.pathsep.join(
            [env.get("NODE_PATH", ""), str(Path(NODE).resolve().parents[1] / "lib" / "node_modules")])
    run = subprocess.run([NODE, str(SCRIPT), served], capture_output=True, text=True, timeout=120, env=env)
    assert run.returncode == 0 and "PASS" in run.stdout, run.stdout + run.stderr

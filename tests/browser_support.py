"""Where the browser tests find node, a Chromium and a driver for it
(puppeteer-core in the dev image, Playwright elsewhere) — the
environment tests/browser/harness.js runs in."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

NODE = shutil.which("node")
CHROMIUM = (os.environ.get("PUPPETEER_EXECUTABLE_PATH") or shutil.which("chromium-headless-shell")
            or shutil.which("chromium") or next(
                (str(p) for p in Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
                 .glob("chromium-*/chrome-linux/chrome")), None))


def _node_paths() -> list[str]:
    paths = [p for p in os.environ.get("NODE_PATH", "").split(os.pathsep) if p]
    if NODE:
        paths.append(str(Path(NODE).resolve().parents[1] / "lib" / "node_modules"))
    return paths


def available() -> bool:
    return bool(NODE and CHROMIUM and any((Path(p) / d).is_dir() for p in _node_paths()
                                          for d in ("puppeteer-core", "playwright")))


def env() -> dict[str, str]:
    return dict(os.environ, CHROMIUM=CHROMIUM or "", NODE_PATH=os.pathsep.join(_node_paths()))


REASON = ("no browser in this image: the dev image has one "
          "(docker build --target dev -t louise-rag:dev .)")

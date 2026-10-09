"""MEASUREMENT GUARD (not product code): keep a big measured model from overcommitting the
B70 while Louise is in use (docs/notes/classical-translation.md, "Incident").

LocalAI loads a model with no check that it fits, and its reclaimer acts only after the
allocation and never on a busy model. A 19 GB translator beside the pinned encoder leaves
~4 GB, less than any of Louise's own chat (7.4 GB) or MADLAD (7.2 GB) loads. So the
translator must YIELD: this guard runs on the host beside the measurement and

  * PRE-EMPTS: the moment LocalAI starts loading any other model ("BackendLoader starting",
    read live from the container log) or the card's VRAM (debugfs vram0_mm) comes within
    1 GiB of full, it writes OUT/HOLD and shuts the measured models down
    (POST /backend/shutdown) -- before the other load uploads its weights (2.2 s after
    spawn, docs/notes/model-profile.md);
  * RELEASES: OUT/HOLD is removed only when nothing is loading (VRAM == the sum LocalAI
    lists, within 600 MiB), no unpinned model other than ours is loaded, and VRAM + the
    measured model + a 2 GiB margin fits the card -- three checks in a row, 5 s apart.
    An unpinned model of Louise's is shut down only when LocalAI has seen no request but
    the worker's embeddings for IDLE_UNLOAD_S (no idle eviction on this node; it reloads on
    its next use).
The measurement client (classical_translation.py) sends nothing while OUT/HOLD exists.

Usage (host): python3 classical_guard.py OUT MODEL_SIZE_MIB MODEL [MODEL...]
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

URL = "http://127.0.0.1:30286"
VRAM = Path("/sys/kernel/debug/dri/0000:03:00.0/vram0_mm")
CARD_MIB = 32656
MARGIN_MIB = 2048
IDLE_UNLOAD_S = 900
PINNED = {"vega-qwen3-embedding-4b", "vega-granite"}       # vega.md §13 (search models unpinned)

OUT = Path(sys.argv[1])
NEED = int(sys.argv[2])
MINE = set(sys.argv[3:])
HOLD = OUT / "HOLD"
LOG = OUT / "guard.log"
lock = threading.Lock()
last_use = [time.time()]
QUIET_PATHS = ("/v1/embeddings", "/system", "/readyz", "/healthz", "/backend/shutdown")


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    with lock, LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def vram_used() -> int:
    for ln in VRAM.read_text().splitlines():
        if "usage:" in ln:
            return int(ln.split()[1]) // (1 << 20)
    return CARD_MIB


def system() -> dict[str, int]:
    with urllib.request.urlopen(f"{URL}/system", timeout=5) as r:
        d = json.load(r)
    return {m["id"]: (m.get("size_vram") or 0) // (1 << 20) for m in d.get("loaded_models") or []}


def shutdown(model: str) -> None:
    req = urllib.request.Request(f"{URL}/backend/shutdown", data=json.dumps({"model": model}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:                       # noqa: BLE001 -- not loaded is fine
        log(f"shutdown {model}: {e}")


def preempt(reason: str) -> None:
    with lock:
        first = not HOLD.exists()
        HOLD.write_text(json.dumps({"t": time.time(), "reason": reason}))
    if first:
        log(f"PRE-EMPT ({reason})")
    for m in MINE:
        shutdown(m)


def watch_log() -> None:
    p = subprocess.Popen(["docker", "logs", "-f", "--since", "0s", "ix-localai-localai-1"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for ln in p.stdout:
        if "HTTP request" in ln and not any(p in ln for p in QUIET_PATHS):
            last_use[0] = time.time()         # a request of Louise's (ours wait under HOLD)
        if "BackendLoader starting" in ln:
            mid = ln.split('modelID\x1b[0m="', 1)[-1].split('"', 1)[0] if "modelID" in ln else "?"
            if mid not in MINE:
                preempt(f"LocalAI loading {mid}")


def watch_card() -> None:
    while True:
        try:
            if vram_used() > CARD_MIB - 1024:
                preempt(f"card at {vram_used()} MiB")
        except Exception as e:                   # noqa: BLE001
            log(f"card read: {e}")
        time.sleep(0.5)


def release_loop() -> None:
    ok = 0
    since: dict[str, float] = {}
    while True:
        time.sleep(5)
        if not HOLD.exists():
            ok = 0
            continue
        try:
            used, listed = vram_used(), system()
        except Exception as e:                   # noqa: BLE001
            log(f"release check: {e}")
            continue
        foreign = {m: s for m, s in listed.items() if m not in PINNED and m not in MINE}
        now = time.time()
        for m in list(since):
            if m not in foreign:
                since.pop(m)
        for m in foreign:
            since.setdefault(m, now)
            quiet = now - last_use[0]
            if now - since[m] > IDLE_UNLOAD_S and quiet > IDLE_UNLOAD_S:
                log(f"unloading {m}: no request but the worker's embeddings for {int(quiet)} s")
                shutdown(m)
        settled = abs(used - sum(listed.values())) < 600
        fits = used + NEED + MARGIN_MIB <= CARD_MIB
        ok = ok + 1 if (settled and not foreign and fits) else 0
        if ok >= 3:
            HOLD.unlink(missing_ok=True)
            log(f"release: card {used} MiB, listed {sorted(listed)}")
            ok = 0


def main() -> None:
    HOLD.write_text(json.dumps({"t": time.time(), "reason": "start"}))
    log(f"guard up for {sorted(MINE)} ({NEED} MiB)")
    for f in (watch_log, watch_card):
        threading.Thread(target=f, daemon=True).start()
    release_loop()


if __name__ == "__main__":
    main()

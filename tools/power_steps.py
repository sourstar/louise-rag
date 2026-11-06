"""MEASUREMENT (2026-10-26 night, the operator's noise target: fan <= ~1,200 RPM in 22:30-06:00):
the B70's power cap (hwmon power1_cap, PL1) stepped down under a steady corpus-like embedding load
(vega-qwen3-embedding-4b, 16 tagged texts of ~2.4k chars a request, two in flight, as gutenberg's
embed stage runs), logging per step: the fan (fan1_input), the card's watts (energy1_input delta),
its temperature, the clock and texts/s. Runs in a container with the hwmon dir mounted rw."""
import json
import sys
import threading
import time
from pathlib import Path

import httpx

from app.vega import client as vclient

H = Path(sys.argv[1])
F = Path(sys.argv[2])
STEPS_W = [int(x) for x in sys.argv[3].split(",")]
SECS = int(sys.argv[4])
URL = "http://192.168.178.10:30286/v1/embeddings"
TEXT = ("Call me Ishmael. Some years ago, never mind how long precisely, having little or no money in my "
        "purse, and nothing particular to interest me on shore, I thought I would sail about a little. ") * 13
done = {"n": 0}
stop = threading.Event()


def load() -> None:
    with httpx.Client(timeout=120) as c:
        while not stop.is_set():
            r = c.post(URL, json={"model": "vega-qwen3-embedding-4b", "input": list(vclient.tag_batch([TEXT] * 16).inputs)})
            if r.status_code == 200:
                done["n"] += 16
            else:
                time.sleep(1)


def rd(p: Path) -> int:
    return int(p.read_text().strip())


threads = [threading.Thread(target=load, daemon=True) for _ in range(2)]
for t in threads:
    t.start()
time.sleep(20)                              # the model loaded and warm
original = rd(H / "power1_cap")
try:
    for w in STEPS_W:
        (H / "power1_cap").write_text(str(w * 1_000_000))
        time.sleep(30)                      # the card settles at the new limit
        fans, temps, mhz = [], [], []
        e0, n0, t0 = rd(H / "energy1_input"), done["n"], time.monotonic()
        while time.monotonic() - t0 < SECS:
            fans.append(rd(H / "fan1_input")); temps.append(rd(H / "temp2_input") / 1000); mhz.append(rd(F / "act_freq"))
            time.sleep(5)
        dt = time.monotonic() - t0
        print(json.dumps({"cap_w": w, "watts": round((rd(H / "energy1_input") - e0) / 1e6 / dt, 1),
                          "fan_avg": round(sum(fans) / len(fans)), "fan_max": max(fans), "fan_last": fans[-1],
                          "temp_avg": round(sum(temps) / len(temps), 1), "temp_max": max(temps),
                          "mhz_avg": round(sum(mhz) / len(mhz)), "texts_per_s": round((done["n"] - n0) / dt, 2)}), flush=True)
finally:
    stop.set()
    (H / "power1_cap").write_text(str(original))
    print(json.dumps({"restored_cap_w": original / 1e6}), flush=True)

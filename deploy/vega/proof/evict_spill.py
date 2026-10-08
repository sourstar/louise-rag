import json
recs = [json.loads(l) for l in open("/tmp/vega-evict/snaps.jsonl")]
M = 1 << 20
prev, worst = None, []
for r in recs:
    v = r["vram0_mm"]
    used = v["total_mib"] - v["free_mib"]
    host = sum(p["host_mib"] for p in r["processes"])
    vr = sum(p["vram_mib"] for p in r["processes"])
    print(f'{r["iso"]} {r["label"]:32} used {used:6} attributed {vr:8.0f} | gtt_mm {r["gtt_mm"]["usage"] / M:8.0f} sum host {host:8.0f}')
    if prev:
        pp = {p["pid"]: p for p in prev["processes"]}
        for p in r["processes"]:
            q = pp.get(p["pid"])
            if q:
                dv, dh = p["vram_mib"] - q["vram_mib"], p["host_mib"] - q["host_mib"]
                if dv < -1 or dh > 1:
                    worst.append((r["label"], p["container"], p.get("model", ""), p["pid"], round(dv, 1), round(dh, 1)))
    prev = r
print("per-process changes between consecutive readings (vram drop or host rise > 1 MiB):")
for w in worst:
    print(" ", w)

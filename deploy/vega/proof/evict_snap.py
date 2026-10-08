"""VeGa §6.2 eviction proof: one reading of the B70, from the host.

Per process: the DRM fdinfo of every /dev/dri fd, one record per drm-client-id
(resident wins over total), as app/gpu_probe does: vram0 = on the card,
gtt + system = GPU buffers placed in host RAM. Card-wide: xe's TTM managers in
debugfs (vram0_mm usage/free, gtt_mm usage). LocalAI's own view: /system's
loaded models (and their pids, joined to host pids through NSpid and the
parent chain) and /api/resources. Appends one JSON line to the log and prints
a one-screen summary.  usage: snap.py LABEL [LOG]
"""
import json, os, re, sys, time, urllib.request

PDEV = "0000:03:00.0"
LOCALAI = "http://127.0.0.1:30286"
CONTAINERS = {"a82bfe01b8d0": "localai", "a625278e2fb7": "rage"}
UNIT = {None: 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
MEM = re.compile(r"drm-(total|resident)-([a-z0-9]+):\s*(\d+)(?:\s*([KMG])iB)?\s*$")
MIB = 1 << 20


def get(path):
    try:
        with urllib.request.urlopen(LOCALAI + path, timeout=20) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def mm(name):
    out = {}
    for line in open(f"/sys/kernel/debug/dri/0/{name}"):
        line = line.strip()
        m = re.match(r"(usage|size):\s*(\d+)$", line)
        if m:
            out[m.group(1)] = int(m.group(2))
        m = re.match(r"chunk_size:.*total:\s*(\d+)MiB, free:\s*(\d+)MiB", line)
        if m:
            out["total_mib"], out["free_mib"] = int(m.group(1)), int(m.group(2))
    return out


def status(pid):
    d = {}
    try:
        for line in open(f"/proc/{pid}/status"):
            k, _, v = line.partition(":")
            d[k] = v.strip()
    except OSError:
        pass
    return d


def container(pid):
    try:
        cg = open(f"/proc/{pid}/cgroup").read()
    except OSError:
        return "?"
    m = re.search(r"([0-9a-f]{64})", cg)
    if not m:
        return "host"
    return CONTAINERS.get(m.group(1)[:12], m.group(1)[:12])


def processes():
    clients = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            fds = os.listdir(f"/proc/{name}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                if not os.readlink(f"/proc/{name}/fd/{fd}").startswith("/dev/dri/"):
                    continue
                text = open(f"/proc/{name}/fdinfo/{fd}").read()
            except OSError:
                continue
            pdev = re.search(r"^drm-pdev:\s*(\S+)", text, re.M)
            cid = re.search(r"^drm-client-id:\s*(\S+)", text, re.M)
            if not pdev or not cid or pdev.group(1) != PDEV:
                continue
            found = {"total": {}, "resident": {}}
            for line in text.splitlines():
                m = MEM.match(line)
                if m:
                    found[m.group(1)][m.group(2)] = int(m.group(3)) * UNIT[m.group(4)]
            regions = dict(found["total"])
            regions.update(found["resident"])
            clients.setdefault(int(name), {})[cid.group(1)] = regions
    out = []
    for pid, per in clients.items():
        vram = sum(v for r in per.values() for k, v in r.items() if k.startswith("vram"))
        host = sum(v for r in per.values() for k, v in r.items() if k in ("gtt", "system"))
        if vram == 0 and host == 0:
            continue
        st = status(pid)
        out.append({"pid": pid, "nspid": int(st.get("NSpid", str(pid)).split()[-1]),
                    "ppid": int(st.get("PPid", "0")), "container": container(pid),
                    "vram_mib": round(vram / MIB, 1), "host_mib": round(host / MIB, 1)})
    return out


def label(procs, loaded):
    """LocalAI processes: the /system model whose pid is the process or an ancestor."""
    by_ns = {}
    for name in os.listdir("/proc"):
        if name.isdigit() and container(int(name)) == "localai":
            st = status(name)
            if "NSpid" in st:
                by_ns[int(st["NSpid"].split()[-1])] = (int(name), int(st.get("PPid", "0")))
    host_to_ns = {h: ns for ns, (h, _) in by_ns.items()}
    pid_model = {m.get("process", {}).get("pid"): m["id"] for m in loaded}
    for p in procs:
        if p["container"] != "localai":
            continue
        ns, hops = p["nspid"], 0
        while ns and hops < 8:
            if ns in pid_model:
                p["model"] = pid_model[ns]
                break
            host, ppid = by_ns.get(ns, (None, 0))
            ns = host_to_ns.get(ppid)
            hops += 1


def main():
    lab = sys.argv[1] if len(sys.argv) > 1 else ""
    log = sys.argv[2] if len(sys.argv) > 2 else "/tmp/vega-evict/snaps.jsonl"
    t = time.time()
    system = get("/system")
    loaded = system.get("loaded_models", []) if isinstance(system, dict) else []
    res = get("/api/resources")
    procs = processes()
    label(procs, loaded)
    rec = {"label": lab, "time": t, "iso": time.strftime("%H:%M:%S", time.localtime(t)),
           "vram0_mm": mm("vram0_mm"), "gtt_mm": mm("gtt_mm"),
           "localai_loaded": [{"id": m["id"], "pid": m.get("process", {}).get("pid"),
                               "size_vram_mib": round((m.get("size_vram") or 0) / MIB)}
                              for m in loaded],
           "localai_aggregate": res.get("aggregate"),
           "processes": sorted(procs, key=lambda p: (p["container"], -p["vram_mib"]))}
    with open(log, "a") as f:
        f.write(json.dumps(rec) + "\n")
    v, g = rec["vram0_mm"], rec["gtt_mm"]
    agg = rec["localai_aggregate"] or {}
    own = sum(p["vram_mib"] for p in procs if p["container"] == "localai")
    rage = sum(p["vram_mib"] for p in procs if p["container"] == "rage")
    print(f"[{rec['iso']}] {lab}: card used {v['total_mib'] - v['free_mib']} / {v['total_mib']} MiB "
          f"(free {v['free_mib']}), gtt_mm {g['usage'] // MIB} MiB | rage {rage:.0f} localai {own:.0f} MiB")
    if agg:
        print(f"  localai sees: total {agg.get('total_memory', 0) // MIB} used {agg.get('used_memory', 0) // MIB} "
              f"free {agg.get('free_memory', 0) // MIB} MiB, {agg.get('usage_percent', 0):.2f} %")
    print("  loaded:", ", ".join(f"{m['id']}({m['size_vram_mib']})" for m in rec["localai_loaded"]) or "-")
    for p in rec["processes"]:
        print(f"  {p['container']:8} pid {p['pid']:>7} {p.get('model', ''):28} vram {p['vram_mib']:>8} host {p['host_mib']:>7}")


if __name__ == "__main__":
    main()

"""Profile the models Louise uses in the LocalAI app: cold start, warm latency,
and what each really holds in memory (operator, 2026-10-09: "Timeouts shall
not include cold start; we must have a profiling of the cold/warm start and
of the real memory occupancy for future improvements").

For each model, one at a time:
- COLD, `--cold-reps` times: the model is shut down (LocalAI's
  `POST /backend/shutdown`), then a minimal request is timed from send to the
  first complete answer. Meanwhile a poller reads, every `--poll-s`, LocalAI's
  `/system` (when the model is listed) and the DRM fdinfo of the backend
  processes that appeared since the request (when card memory starts to
  grow, and when it reaches 95 % of what it holds at the answer). Those are
  the load phases, read from the driver, not from a log.
- WARM: the latency of representative requests, `--warm-reps` each (embeddings
  1/8/32 inputs of ~300/1,000 characters; rerank 10/50 documents of
  300/3,000; translation 1 and 10 items; chat a 1-token and a 100-token
  answer).
- MEMORY, after the load and again after the largest warm request: per
  backend process (LocalAI names the wrapper's pid; its CHILD holds the
  card, docs/notes/vega.md §10 finding 4, so the whole process tree is
  read) the fdinfo's drm-total-* and drm-resident-* per region, one record
  per drm-client-id; LocalAI's `size_vram`; the tree's host RSS; the card's
  used memory as the driver reports it.

A `vega-` model refuses untagged input, so its inputs are tagged with
app/vega/tag.py; no receipt is read — the vectors are thrown away.

Texts are Python standard-library docstrings: nothing from a collection.

At the end the models loaded before the run are loaded again and those it
loaded are shut down, so LocalAI holds what it held (pinned encoders
included). Run on the node in a throwaway container from the live Louise
image, host PID namespace (fdinfo of LocalAI's processes), host network:

    docker run --rm --pid=host --network=host --cap-add SYS_PTRACE \\
      --cap-add PERFMON --device /dev/dri -e PYTHONPATH=/srv \\
      -v /tmp/profile:/work louise-rag:local \\
      python /work/profile_models.py --out /work/profile.json

Reads no Louise data and no secret; LocalAI on the loopback needs no key.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

MIB = 1 << 20
PDEV = "0000:03:00.0"
UNIT = {None: 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
MEM = re.compile(r"drm-(total|resident)-([a-z0-9]+):\s*(\d+)(?:\s*([KMG])iB)?\s*$")

#: What Louise asks of each model, and so how it is profiled.
LOUISE_MODELS: dict[str, str] = {
    "vega-qwen3-embedding-4b": "vega-embed",
    "vega-bge-m3": "vega-embed",
    "vega-granite": "vega-embed",
    "vega-medcpt-query": "vega-embed",
    "vega-medcpt-article": "vega-embed",
    "vega-medcpt-article-ov": "vega-pairs",
    "louise-rerank-bge": "rerank",
    "louise-rerank-medcpt": "rerank",
    "louise-translate-madlad": "translate",
    "louise-chat-qwen3-8b": "chat",
}


# ---- texts ---------------------------------------------------------------------

def _texts() -> list[str]:
    """Long standard-library docstrings, whitespace flattened."""
    import collections, concurrent.futures, email, http.client, json as _j, logging
    import pathlib, subprocess, threading as _t, typing, unittest, urllib.request as _u
    out: list[str] = []
    for mod in (collections, concurrent.futures, email, http.client, _j, logging, pathlib,
                subprocess, _t, typing, unittest, _u, argparse, statistics, re):
        for _name, obj in inspect.getmembers(mod):
            doc = inspect.getdoc(obj) or ""
            doc = " ".join(doc.split())
            if len(doc) >= 400:
                out.append(doc)
    return sorted(set(out))


def _cut(text: str, n: int) -> str:
    """`text` repeated to at least `n` characters, cut back to a word boundary."""
    while len(text) < n:
        text = text + " " + text
    cut = text[:n]
    space = cut.rfind(" ")
    return cut[:space] if space > n * 0.8 else cut


class Texts:
    def __init__(self) -> None:
        self.pool = _texts()
        self.i = 0

    def take(self, count: int, chars: int) -> list[str]:
        got = []
        for _ in range(count):
            got.append(_cut(self.pool[self.i % len(self.pool)], chars))
            self.i += 1
        return got


# ---- LocalAI --------------------------------------------------------------------

class LocalAI:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def _req(self, method: str, path: str, body: Any = None, timeout: float = 600):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def post(self, path: str, body: Any, timeout: float = 600) -> tuple[int, bytes]:
        return self._req("POST", path, body, timeout)

    def system(self) -> list[dict]:
        st, raw = self._req("GET", "/system", timeout=10)
        if st != 200:
            raise RuntimeError(f"/system HTTP {st}")
        return json.loads(raw).get("loaded_models") or []

    def loaded(self) -> dict[str, dict]:
        return {m["id"]: m for m in self.system() if isinstance(m.get("id"), str)}

    def models(self) -> list[str]:
        st, raw = self._req("GET", "/v1/models", timeout=10)
        return [m["id"] for m in json.loads(raw).get("data", [])]

    def shutdown(self, model: str, wait_s: float = 300) -> float:
        """Shut `model` down and wait until /system no longer lists it."""
        t = time.monotonic()
        self.post("/backend/shutdown", {"model": model}, timeout=wait_s)
        while model in self.loaded():
            if time.monotonic() - t > wait_s:
                raise RuntimeError(f"{model}: still loaded {wait_s} s after shutdown")
            time.sleep(0.2)
        return time.monotonic() - t


# ---- requests ----------------------------------------------------------------------

def _tagged(items: list) -> list[str]:
    from app.vega import client as vclient
    return list(vclient.tag_batch(items).inputs)


def embed_body(model: str, items: list) -> dict:
    return {"model": model, "input": _tagged(items), "encoding_format": "base64"}


def call(api: LocalAI, kind: str, model: str, size: dict, texts: Texts) -> dict:
    """One request of `kind` at `size`; its latency and what it answered."""
    if kind in ("vega-embed", "vega-pairs"):
        n, chars = size["inputs"], size["chars"]
        if kind == "vega-pairs":
            items = list(zip(texts.take(n, 80), texts.take(n, chars)))
        else:
            items = texts.take(n, chars)
        path, body = "/v1/embeddings", embed_body(model, items)
    elif kind == "rerank":
        docs = texts.take(size["docs"], size["chars"])
        path, body = "/v1/rerank", {"model": model, "query": texts.take(1, 60)[0],
                                    "documents": docs, "top_n": len(docs)}
    elif kind == "translate":
        targets = ["it", "de", "fr", "es", "ja"]
        items = [{"text": t, "to": targets[i % len(targets)]}
                 for i, t in enumerate(texts.take(size["items"], size.get("chars", 200)))]
        path, body = "/v1/completions", {"model": model,
                                         "prompt": json.dumps({"items": items}, ensure_ascii=False)}
    elif kind == "chat":
        prompt = ("Say OK." if size["max_tokens"] == 1 else
                  "Write a long, detailed essay about the history of the city of Rome.")
        path, body = "/v1/chat/completions", {
            "model": model, "max_tokens": size["max_tokens"], "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
            "chat_template_kwargs": {"enable_thinking": False}}
    else:
        raise ValueError(kind)
    t = time.monotonic()
    status, raw = api.post(path, body)
    elapsed = time.monotonic() - t
    out: dict[str, Any] = {"status": status, "s": round(elapsed, 4)}
    if status != 200:
        out["error"] = raw[:300].decode("utf-8", "replace")
    elif kind == "chat":
        usage = json.loads(raw).get("usage") or {}
        out["completion_tokens"] = usage.get("completion_tokens")
        out["prompt_tokens"] = usage.get("prompt_tokens")
    return out


MINIMAL = {
    "vega-embed": {"inputs": 1, "chars": 20},
    "vega-pairs": {"inputs": 1, "chars": 20},
    "rerank": {"docs": 1, "chars": 20},
    "translate": {"items": 1, "chars": 20},
    "chat": {"max_tokens": 1},
}

WARM = {
    "vega-embed": [{"inputs": n, "chars": c} for c in (300, 1000) for n in (1, 8, 32)],
    "vega-pairs": [{"inputs": n, "chars": c} for c in (300, 1000) for n in (1, 8, 32)],
    "rerank": [{"docs": n, "chars": c} for c in (300, 3000) for n in (10, 50)],
    "translate": [{"items": 1, "chars": 200}, {"items": 10, "chars": 200}],
    "chat": [{"max_tokens": 1}, {"max_tokens": 100}],
}


# ---- processes and the card ---------------------------------------------------------

def _status(pid: int) -> dict[str, str]:
    d: dict[str, str] = {}
    try:
        for line in open(f"/proc/{pid}/status"):
            k, _, v = line.partition(":")
            d[k] = v.strip()
    except OSError:
        pass
    return d


def _cgroup(pid: int) -> str | None:
    try:
        return open(f"/proc/{pid}/cgroup").read()
    except OSError:
        return None


def localai_cgroup() -> str:
    """The cgroup of LocalAI's own process (`./local-ai`, pid 1 in its
    namespace): every backend it starts shares it."""
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            cmd = open(f"/proc/{name}/cmdline", "rb").read().split(b"\0")[0]
        except OSError:
            continue
        if cmd.endswith(b"local-ai") and _status(int(name)).get("NSpid", "").split()[-1:] == ["1"]:
            cg = _cgroup(int(name))
            if cg:
                return cg
    raise RuntimeError("LocalAI's process not found (is the host PID namespace shared?)")


def localai_procs(cgroup: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        if _cgroup(pid) != cgroup:
            continue
        st = _status(pid)
        if not st:
            continue
        out[pid] = {"pid": pid, "ppid": int(st.get("PPid", "0")),
                    "nspid": int(st.get("NSpid", name).split()[-1]),
                    "rss_kib": int(st.get("VmRSS", "0 kB").split()[0]) if "VmRSS" in st else 0}
    return out


def tree(root: int, procs: dict[int, dict]) -> list[int]:
    found, frontier = [root], [root]
    while frontier:
        nxt = [p for p, d in procs.items() if d["ppid"] in frontier and p not in found]
        found += nxt
        frontier = nxt
    return found


def fdinfo(pid: int) -> dict[str, dict[str, int]]:
    """{"total": {region: bytes}, "resident": {region: bytes}} over this
    process's DRM clients on the card, one record per drm-client-id."""
    clients: dict[str, dict[str, dict[str, int]]] = {}
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return {"total": {}, "resident": {}}
    for fd in fds:
        try:
            if not os.readlink(f"/proc/{pid}/fd/{fd}").startswith("/dev/dri/"):
                continue
            text = open(f"/proc/{pid}/fdinfo/{fd}").read()
        except OSError:
            continue
        pdev = re.search(r"^drm-pdev:\s*(\S+)", text, re.M)
        cid = re.search(r"^drm-client-id:\s*(\S+)", text, re.M)
        if not pdev or not cid or pdev.group(1) != PDEV:
            continue
        found: dict[str, dict[str, int]] = {"total": {}, "resident": {}}
        for line in text.splitlines():
            m = MEM.match(line)
            if m:
                found[m.group(1)][m.group(2)] = int(m.group(3)) * UNIT[m.group(4)]
        clients[cid.group(1)] = found
    out: dict[str, dict[str, int]] = {"total": {}, "resident": {}}
    for c in clients.values():
        for kind in ("total", "resident"):
            for region, v in c[kind].items():
                out[kind][region] = out[kind].get(region, 0) + v
    return out


def vram_of(pids: list[int]) -> int:
    total = 0
    for p in pids:
        f = fdinfo(p)
        regions = dict(f["total"])
        regions.update(f["resident"])
        total += sum(v for k, v in regions.items() if k.startswith("vram"))
    return total


def card_used_mib() -> int | None:
    try:
        from app import gpu_memory
        _total, used, _why = gpu_memory.read(PDEV)
        return None if used is None else used // MIB
    except Exception:  # noqa: BLE001
        return None


def memory(api: LocalAI, cgroup: str, model: str) -> dict:
    """What `model` holds now: per process of its tree, and summed."""
    row = api.loaded().get(model)
    if row is None:
        return {"error": "not loaded"}
    nspid = (row.get("process") or {}).get("pid")
    procs = localai_procs(cgroup)
    root = next((p for p, d in procs.items() if d["nspid"] == nspid), None)
    out: dict[str, Any] = {"size_vram_mib": round((row.get("size_vram") or 0) / MIB, 1),
                           "card_used_mib": card_used_mib(), "processes": []}
    if root is None:
        out["error"] = f"/system pid {nspid} not found among LocalAI's processes"
        return out
    sums = {"total": {}, "resident": {}}
    rss = 0
    for p in tree(root, procs):
        f = fdinfo(p)
        d = procs[p]
        rss += d["rss_kib"]
        out["processes"].append({
            "nspid": d["nspid"], "role": "listed" if p == root else "child",
            "rss_mib": round(d["rss_kib"] / 1024, 1),
            "total_mib": {k: round(v / MIB, 1) for k, v in sorted(f["total"].items())},
            "resident_mib": {k: round(v / MIB, 1) for k, v in sorted(f["resident"].items())}})
        for kind in ("total", "resident"):
            for region, v in f[kind].items():
                sums[kind][region] = sums[kind].get(region, 0) + v
    out["rss_mib"] = round(rss / 1024, 1)
    out["total_mib"] = {k: round(v / MIB, 1) for k, v in sorted(sums["total"].items())}
    out["resident_mib"] = {k: round(v / MIB, 1) for k, v in sorted(sums["resident"].items())}
    res = dict(sums["total"])
    res.update(sums["resident"])
    out["vram_mib"] = round(sum(v for k, v in res.items() if k.startswith("vram")) / MIB, 1)
    out["host_gpu_mib"] = round(sum(v for k, v in res.items() if k in ("gtt", "system")) / MIB, 1)
    return out


# ---- cold ---------------------------------------------------------------------------

def cold_once(api: LocalAI, cgroup: str, kind: str, model: str, texts: Texts,
              poll_s: float) -> dict:
    if model in api.loaded():
        api.shutdown(model)
    before = set(localai_procs(cgroup))
    timeline: list[tuple[float, int]] = []
    marks: dict[str, float] = {}
    stop = threading.Event()
    t0 = time.monotonic()

    def poll() -> None:
        while not stop.is_set():
            now = time.monotonic() - t0
            try:
                if "listed" not in marks and model in api.loaded():
                    marks["listed"] = now
            except Exception:  # noqa: BLE001
                pass
            new = [p for p in localai_procs(cgroup) if p not in before]
            if new and "spawned" not in marks:
                marks["spawned"] = now
            if new:
                timeline.append((now, vram_of(new)))
            stop.wait(poll_s)

    th = threading.Thread(target=poll, daemon=True)
    th.start()
    answer = call(api, kind, model, MINIMAL[kind], texts)
    answered = time.monotonic() - t0
    time.sleep(poll_s * 2)
    stop.set()
    th.join()
    final = max((v for t, v in timeline if t <= answered + poll_s), default=0)
    first = next((t for t, v in timeline if v > 0), None)
    p95 = next((t for t, v in timeline if final and v >= 0.95 * final), None)
    return {"s": round(answered, 3), "status": answer["status"],
            **({"error": answer["error"]} if "error" in answer else {}),
            "phases_s": {"process_spawned": _r(marks.get("spawned")),
                         "vram_first": _r(first), "vram_95pct": _r(p95),
                         "listed_by_system": _r(marks.get("listed")),
                         "answered": round(answered, 3)},
            "vram_at_answer_mib": round(final / MIB, 1),
            "timeline": [(round(t, 2), round(v / MIB)) for t, v in timeline[::max(1, len(timeline) // 60)]]}


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 3)


def _spread(values: list[float]) -> dict:
    if not values:
        return {}
    return {"min": round(min(values), 3), "median": round(statistics.median(values), 3),
            "max": round(max(values), 3), "n": len(values)}


# ---- one model ---------------------------------------------------------------------

def profile(api: LocalAI, cgroup: str, model: str, kind: str, texts: Texts,
            cold_reps: int, warm_reps: int, poll_s: float, log: Callable[[str], None]) -> dict:
    out: dict[str, Any] = {"kind": kind, "cold": [], "warm": []}
    for i in range(cold_reps):
        c = cold_once(api, cgroup, kind, model, texts, poll_s)
        out["cold"].append(c)
        log(f"  cold {i + 1}: {c['s']} s (HTTP {c['status']}) phases {c['phases_s']}")
    out["cold_s"] = _spread([c["s"] for c in out["cold"] if c["status"] == 200])
    if model not in api.loaded():
        call(api, kind, model, MINIMAL[kind], texts)
    out["memory_after_load"] = memory(api, cgroup, model)
    log(f"  memory after load: vram {out['memory_after_load'].get('vram_mib')} MiB, "
        f"size_vram {out['memory_after_load'].get('size_vram_mib')}, "
        f"rss {out['memory_after_load'].get('rss_mib')}")
    for size in WARM[kind]:
        call(api, kind, model, size, texts)               # one unmeasured: first use of a shape
        runs = [call(api, kind, model, size, texts) for _ in range(warm_reps)]
        ok = [r for r in runs if r["status"] == 200]
        row: dict[str, Any] = {"size": size, **_spread([r["s"] for r in ok])}
        if len(ok) < len(runs):
            row["errors"] = [r.get("error") for r in runs if r["status"] != 200][:2]
        if kind == "chat" and ok:
            row["completion_tokens"] = ok[-1].get("completion_tokens")
            row["prompt_tokens"] = ok[-1].get("prompt_tokens")
        out["warm"].append(row)
        log(f"  warm {size}: {row.get('median')} s (min {row.get('min')}, max {row.get('max')})"
            + (f" ERR {row['errors'][0][:120]}" if row.get("errors") else ""))
    out["memory_after_warm"] = memory(api, cgroup, model)
    log(f"  memory after warm: vram {out['memory_after_warm'].get('vram_mib')} MiB, "
        f"rss {out['memory_after_warm'].get('rss_mib')}")
    return out


#: The first load of each model ever measured on the node, page cache cold
#: (docs/notes/vega.md §12, "first call, load included", 2026-10-08/09; §11
#: for LocalAGI's 2B chat model). A cold start here repeats a load the page
#: cache may still hold; a bound must also cover a load from disk.
EARLIER_FIRST_LOADS_S: dict[str, float] = {
    "vega-granite": 9.2, "vega-bge-m3": 11.9, "vega-medcpt-query": 6.3,
    "vega-medcpt-article": 6.3, "vega-medcpt-article-ov": 7.3,
    "vega-qwen3-embedding-4b": 33.7, "louise-rerank-bge": 8.1,
    "louise-rerank-medcpt": 4.7, "louise-translate-madlad": 25.0,
    "louise-chat-qwen3-8b": 9.4, "qwen3.8-2b-q4": 46.0,
}
#: A load bound: this many times the slowest load measured, never under the
#: floor, rounded up to 5 s. A load beside the card's other work (the worker
#: feeding the encoder, a reclaimer eviction) is slower than one alone.
BOUND_FACTOR = 3.0
BOUND_FLOOR_S = 30.0


def bounds(profile: dict, source: str) -> dict:
    """app/model_loads.json from a profile: per model the slowest cold start
    measured (this profile's, or the earlier first load from disk) and the
    load bound app/model_load.py gives it."""
    out: dict[str, Any] = {
        "source": source,
        "rule": (f"load_timeout_s = max({BOUND_FLOOR_S:g}, {BOUND_FACTOR:g} x cold_max_s), "
                 f"rounded up to 5 s; cold_max_s = the slowest of this profile's cold starts "
                 f"and the first load from disk in docs/notes/vega.md §11-§12"),
        "models": {}}
    for model, row in sorted((profile.get("models") or {}).items()):
        colds = [c["s"] for c in row.get("cold", []) if c.get("status") == 200]
        earlier = EARLIER_FIRST_LOADS_S.get(model)
        slowest = max(colds + ([earlier] if earlier else []), default=None)
        if slowest is None:
            continue
        bound = max(BOUND_FLOOR_S, BOUND_FACTOR * slowest)
        out["models"][model] = {"cold_max_s": round(slowest, 1),
                                "load_timeout_s": int(5 * -(-bound // 5))}
    return out


def main(argv: list[str] | None = None) -> int:
    if argv is None and len(sys.argv) > 1 and sys.argv[1] == "bounds":
        # tools/profile_models.py bounds PROFILE.json OUT.json
        _, _, src, dst = sys.argv
        Path(dst).write_text(json.dumps(bounds(json.loads(Path(src).read_text()), src),
                                        indent=1) + "\n", encoding="utf-8")
        return 0
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:30286")
    ap.add_argument("--models", default=",".join(LOUISE_MODELS))
    ap.add_argument("--localagi-model", default="",
                    help="LocalAGI's model; profiled as a chat model if LocalAI lists it")
    ap.add_argument("--cold-reps", type=int, default=3)
    ap.add_argument("--warm-reps", type=int, default=5)
    ap.add_argument("--poll-s", type=float, default=0.2)
    ap.add_argument("--unload-first", default="",
                    help="models loaded now that are shut down before profiling (comma list); "
                         "loaded again at the end")
    ap.add_argument("--keep", default="",
                    help="models left loaded after their profile (the pinned encoders); every "
                         "other one is shut down, so one model at a time is added to the card")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    api = LocalAI(args.base)
    cgroup = localai_cgroup()
    listed = set(api.models())
    models = {m: LOUISE_MODELS[m] for m in args.models.split(",") if m}
    if args.localagi_model:
        if args.localagi_model not in listed:
            raise SystemExit(f"{args.localagi_model}: not in LocalAI's model list")
        models[args.localagi_model] = "chat"
    before = sorted(api.loaded())
    keep = {m for m in args.keep.split(",") if m}
    out_path = Path(args.out)
    result: dict[str, Any] = {
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base": args.base, "loaded_before": before, "card_used_mib_before": card_used_mib(),
        "cold_reps": args.cold_reps, "warm_reps": args.warm_reps, "poll_s": args.poll_s,
        "localagi_model": args.localagi_model or None, "models": {}}

    def log(msg: str) -> None:
        print(time.strftime("%H:%M:%S ") + msg, flush=True)

    def save() -> None:
        out_path.write_text(json.dumps(result, indent=1))

    texts = Texts()
    try:
        for m in [m for m in args.unload_first.split(",") if m]:
            if m in api.loaded():
                log(f"unload first: {m} ({api.shutdown(m):.1f} s)")
        for model, kind in models.items():
            if model not in listed:
                result["models"][model] = {"kind": kind, "error": "not in LocalAI's model list"}
                continue
            log(f"{model} ({kind})")
            try:
                result["models"][model] = profile(api, cgroup, model, kind, texts,
                                                  args.cold_reps, args.warm_reps, args.poll_s, log)
            except Exception as e:  # noqa: BLE001 — one model's failure is recorded
                result["models"][model] = {"kind": kind, "error": f"{type(e).__name__}: {e}"}
                log(f"  FAILED: {e}")
            if model not in keep and model in api.loaded():
                api.shutdown(model)
            save()
    finally:
        # LocalAI holds what it held: what this run loaded goes, what it
        # unloaded comes back (pinned encoders first, as they were)
        now = api.loaded()
        for m in now:
            if m not in before:
                try:
                    api.shutdown(m)
                except Exception as e:  # noqa: BLE001
                    log(f"restore: {m} not shut down: {e}")
        for m in sorted(before, key=lambda m: m not in keep):   # pinned first
            if m not in api.loaded():
                kind = LOUISE_MODELS.get(m) or ("chat" if m == args.localagi_model else None)
                if kind is None:
                    log(f"restore: {m} — unknown kind, not reloaded")
                    continue
                t = time.monotonic()
                r = call(api, kind, m, MINIMAL[kind], texts)
                log(f"restore: {m} loaded again in {time.monotonic() - t:.1f} s (HTTP {r['status']})")
        result["loaded_after"] = sorted(api.loaded())
        result["card_used_mib_after"] = card_used_mib()
        result["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save()
    return 0


if __name__ == "__main__":
    sys.exit(main())

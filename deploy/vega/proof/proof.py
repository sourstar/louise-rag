"""VeGa node proof (docs/notes/vega.md §6, §10). Runs inside `vega-proof`
(the receipts service's container) on the node:

    docker exec vega-proof python /proof/proof.py [N]

LocalAI's `vega-bge-m3` (the wrapper in front of LocalAI's own SYCL f16
llama.cpp backend) against RAGE's `bge-m3`, on N (default 2,048) real texts:
the docstrings of Python's standard library, deterministic, nothing from a
Louise collection. Read-only use of RAGE. Prints one JSON report.
"""
from __future__ import annotations

import base64
import hashlib
import importlib
import inspect
import json
import math
import os
import pkgutil
import re
import statistics
import struct
import sys
import time
import warnings

import httpx

from app.vega import client, tag as vtag

LOCALAI = os.environ.get("LOCALAI_URL", "http://192.168.178.10:30286")
RAGE = os.environ.get("RAGE_URL", "http://192.168.178.10:30437")
VEGA = os.environ.get("VEGA_URL", "http://127.0.0.1:8090")
MODEL, RAGE_MODEL = "vega-bge-m3", "bge-m3"
BATCH = 32
SKIP = re.compile(r"^(test|idlelib|tkinter|turtle|lib2to3|ensurepip|antigravity|this|__)")


def texts(n: int) -> list[str]:
    """Docstrings of the standard library: modules, classes, functions."""
    warnings.simplefilter("ignore")
    seen: set[str] = set()
    for name in sorted(sys.stdlib_module_names):
        if SKIP.match(name):
            continue
        try:
            mod = importlib.import_module(name)
        except BaseException:                                  # noqa: BLE001
            continue
        objs = [mod] + [v for _, v in sorted(vars(mod).items(), key=lambda kv: kv[0])
                        if inspect.isclass(v) or inspect.isfunction(v)]
        for o in objs:
            doc = inspect.getdoc(o) or ""
            for para in doc.split("\n\n"):
                t = " ".join(para.split())
                if 40 <= len(t) <= 4000:
                    seen.add(t)
    out = sorted(seen, key=lambda t: hashlib.sha256(t.encode()).hexdigest())
    if len(out) < n:
        raise SystemExit(f"only {len(out)} texts")
    return out[:n]


def f32(v) -> bytes:
    return vtag.f32_bytes(v)


def floats(b: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(b) // 4}f", b))


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb)


def per_item(batch, vectors, receipts) -> tuple[int, int, int]:
    """(vector digests that match, that do not, receipts missing), each input
    on its own — `client.verify` stops at the first."""
    ok = bad = missing = 0
    for tid, want, v in zip(batch.ids, batch.input_digests, vectors):
        r = receipts.get(tid)
        if r is None:
            missing += 1
        elif r["input_digest"] == want and r["vector_digest"] == vtag.vector_digest(v):
            ok += 1
        else:
            bad += 1
    return ok, bad, missing


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2048
    http = httpx.Client(timeout=600)
    rep: dict = {"n": n, "batch": BATCH}
    corpus = texts(n)
    rep["chars"] = sum(len(t) for t in corpus)

    def localai(inputs, **extra):
        t0 = time.monotonic()
        r = http.post(f"{LOCALAI}/v1/embeddings", json={"model": MODEL, "input": list(inputs),
                                                        **extra})
        return r, time.monotonic() - t0

    def receipts(ids):
        t0 = time.monotonic()
        got = client.fetch_receipts(VEGA, ids, get=http.get)
        return got, time.monotonic() - t0

    # ---- refusals, before anything is loaded or after: the same answer ----
    health0 = http.get(f"{VEGA}/health").json()
    r, _ = localai(["an untagged text"])
    rep["refusal_untagged"] = {"status": r.status_code, "body": r.text[:400]}
    _, good = vtag.tag("text", "x")
    bad = good.replace('{"form":"text"', '{ "form":"text"', 1)       # not canonical
    r, _ = localai([bad])
    rep["refusal_noncanonical"] = {"status": r.status_code, "body": r.text[:400]}

    # ---- the first load (timed), and base64 honoured? ----
    b = client.tag_batch(["Base64 or decimals: what does LocalAI send?"])
    r, dt = localai(b.inputs, encoding_format="base64")
    first = json.loads(r.content)["data"][0]["embedding"] if r.status_code == 200 else None
    rep["first_call_s"] = round(dt, 2)
    rep["first_call_status"] = r.status_code
    rep["base64_honoured"] = isinstance(first, str)
    if r.status_code != 200:
        rep["first_call_body"] = r.text[:600]
        print(json.dumps(rep, indent=1))
        return 1
    rep["base64_bytes"] = len(base64.b64decode(first)) if isinstance(first, str) else None
    rep["usage_localai"] = json.loads(r.content).get("usage")
    vectors, run_ids = client.verify_response(b, r.content, base_url=VEGA, get=http.get)
    rep["vega_run_id"] = sorted(run_ids)
    health1 = http.get(f"{VEGA}/health").json()
    rep["receipts_held"] = {"before_refusals": health0["receipts"], "after_first": health1["receipts"]}

    # ---- warm RAGE too (one call) ----
    rr = http.post(f"{RAGE}/upstream/{RAGE_MODEL}/v1/embeddings",
                   json={"model": RAGE_MODEL, "input": ["warm"]})
    rage_run = rr.headers.get("louise-run")
    rep["rage_usage"] = rr.json().get("usage")

    # ---- the 2,000 ----
    t = {"b64_embed": 0.0, "b64_receipts": 0.0, "json_embed": 0.0, "json_receipts": 0.0,
         "rage": 0.0}
    c = {"b64_ok": 0, "b64_bad": 0, "b64_missing": 0, "json_ok": 0, "json_bad": 0,
         "json_missing": 0, "verify_batches_pass": 0, "verify_batches_fail": 0,
         "b64_vs_json_bitequal": 0, "vega_vs_rage_bitequal": 0, "json_negzero": 0, "rage_runs": set(), "vega_runs": set()}
    cos, maxabs, selfabs = [], 0.0, 0.0
    failures: list[str] = []
    for i in range(0, n, BATCH):
        chunk = corpus[i:i + BATCH]
        # base64
        bb = client.tag_batch(chunk)
        r, dt = localai(bb.inputs, encoding_format="base64")
        t["b64_embed"] += dt
        r.raise_for_status()
        vb = client.parse_response(r.content)
        got, dt = receipts(bb.ids)
        t["b64_receipts"] += dt
        ok, bad_, miss = per_item(bb, vb, got)
        c["b64_ok"] += ok; c["b64_bad"] += bad_; c["b64_missing"] += miss
        try:
            c["vega_runs"] |= client.verify(bb, vb, got)
            c["verify_batches_pass"] += 1
        except (client.Refusal, client.Deferral) as e:
            c["verify_batches_fail"] += 1
            failures.append(f"b64 batch {i}: {type(e).__name__}: {e}")
        # JSON decimals, read by parse_response (never response.json())
        bj = client.tag_batch(chunk)
        r, dt = localai(bj.inputs)
        t["json_embed"] += dt
        r.raise_for_status()
        c["json_negzero"] += r.text.count(",-0,") + r.text.count("[-0,") + r.text.count(",-0]")
        vj = client.parse_response(r.content)
        got, dt = receipts(bj.ids)
        t["json_receipts"] += dt
        ok, bad_, miss = per_item(bj, vj, got)
        c["json_ok"] += ok; c["json_bad"] += bad_; c["json_missing"] += miss
        try:
            c["vega_runs"] |= client.verify(bj, vj, got)
            c["verify_batches_pass"] += 1
        except (client.Refusal, client.Deferral) as e:
            c["verify_batches_fail"] += 1
            failures.append(f"json batch {i}: {type(e).__name__}: {e}")
        # RAGE, direct
        t0 = time.monotonic()
        rr = http.post(f"{RAGE}/upstream/{RAGE_MODEL}/v1/embeddings",
                       json={"model": RAGE_MODEL, "input": chunk})
        t["rage"] += time.monotonic() - t0
        rr.raise_for_status()
        c["rage_runs"].add(rr.headers.get("louise-run"))
        vr = client.parse_response(rr.content)
        for x, y, z in zip(vb, vj, vr):
            xb, yb = f32(x), f32(y)
            c["b64_vs_json_bitequal"] += xb == yb
            c["vega_vs_rage_bitequal"] += xb == f32(z)
            fx, fy, fz = floats(xb), floats(yb), floats(f32(z))
            cos.append(cosine(fx, fz))
            maxabs = max(maxabs, max(abs(p - q) for p, q in zip(fx, fz)))
            selfabs = max(selfabs, max(abs(p - q) for p, q in zip(fx, fy)))
    c["rage_runs"] = sorted(c["rage_runs"])
    c["vega_runs"] = sorted(c["vega_runs"])
    rep["counts"] = c
    rep["failures"] = failures[:10]
    rep["vega_vs_rage"] = {"cos_min": min(cos), "cos_median": statistics.median(cos),
                           "max_abs_diff": maxabs,
                           "vega_b64_vs_vega_json_max_abs_diff": selfabs}
    rep["seconds"] = {k: round(v, 2) for k, v in t.items()}
    rep["texts_per_s"] = {
        "rage": round(n / t["rage"], 1),
        "vega_b64_embed_only": round(n / t["b64_embed"], 1),
        "vega_b64_with_receipts": round(n / (t["b64_embed"] + t["b64_receipts"]), 1),
        "vega_json_with_receipts": round(n / (t["json_embed"] + t["json_receipts"]), 1)}
    rep["receipt_fetch_ms_per_batch"] = round(1000 * t["b64_receipts"] / math.ceil(n / BATCH), 2)

    # ---- tokens: LocalAI's tokenizer through the wrapper (TokenizeString passes as bytes) ----
    tok = 0
    try:
        for s in corpus:
            r = http.post(f"{LOCALAI}/v1/tokenize", json={"model": MODEL, "content": s})
            r.raise_for_status()
            tok += len(r.json().get("tokens") or [])
        rep["tokens"] = tok
        rep["tokens_per_s"] = {
            "rage": round(tok / t["rage"]),
            "vega_b64_with_receipts": round(tok / (t["b64_embed"] + t["b64_receipts"])),
            "vega_json_with_receipts": round(tok / (t["json_embed"] + t["json_receipts"]))}
    except Exception as e:                                        # noqa: BLE001
        rep["tokens"] = f"not counted: {type(e).__name__}: {str(e)[:200]}"

    # ---- the runs ----
    vrid = c["vega_runs"][0] if c["vega_runs"] else None
    rep["vega_run"] = http.get(f"{VEGA}/vega/runs/{vrid}").json() if vrid else None
    rep["rage_run_id"] = rage_run
    rep["rage_run"] = http.get(f"{RAGE}/louise/runs/{rage_run}").json() if rage_run else None
    print(json.dumps(rep, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

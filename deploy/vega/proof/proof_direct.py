"""VeGa node proof, §6.3 and §6.4 on ONE engine: the same LocalAI, the same
real backend (intel-sycl-f16-llama-cpp), the same YAML options and weights —
`vega-bge-m3` (behind the wrapper, tagged, receipts read) against
`vega-bge-m3-direct` (no wrapper, plain text). Interleaved per batch.

    docker exec vega-proof python /proof/proof_direct.py [N]
"""
from __future__ import annotations

import json
import statistics
import sys
import time

import httpx

sys.path.insert(0, "/proof")
from proof import BATCH, LOCALAI, VEGA, cosine, f32, floats, per_item, texts  # noqa: E402

from app.vega import client  # noqa: E402


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2048
    http = httpx.Client(timeout=600)
    corpus = texts(n)

    def post(model, inputs):
        t0 = time.monotonic()
        r = http.post(f"{LOCALAI}/v1/embeddings",
                      json={"model": model, "input": list(inputs), "encoding_format": "base64"})
        r.raise_for_status()
        return client.parse_response(r.content), time.monotonic() - t0

    t0 = time.monotonic()
    post("vega-bge-m3-direct", ["load"])
    load_s = time.monotonic() - t0
    post("vega-bge-m3", [client.tag_batch(["warm"]).inputs[0]])
    t = {"vega": 0.0, "receipts": 0.0, "direct": 0.0}
    ok = bad = miss = bitequal = 0
    cos, maxabs = [], 0.0
    for i in range(0, n, BATCH):
        chunk = corpus[i:i + BATCH]
        b = client.tag_batch(chunk)
        vv, dt = post("vega-bge-m3", b.inputs)
        t["vega"] += dt
        t1 = time.monotonic()
        got = client.fetch_receipts(VEGA, b.ids, get=http.get)
        t["receipts"] += time.monotonic() - t1
        o, x, m = per_item(b, vv, got)
        ok, bad, miss = ok + o, bad + x, miss + m
        vd, dt = post("vega-bge-m3-direct", chunk)
        t["direct"] += dt
        for a, c in zip(vv, vd):
            bitequal += f32(a) == f32(c)
            fa, fc = floats(f32(a)), floats(f32(c))
            cos.append(cosine(fa, fc))
            maxabs = max(maxabs, max(abs(p - q) for p, q in zip(fa, fc)))
    print(json.dumps({
        "n": n, "direct_first_call_with_load_s": round(load_s, 2),
        "receipts": {"ok": ok, "mismatch": bad, "missing": miss},
        "wrapper_vs_direct": {"bitequal": bitequal, "cos_min": min(cos),
                              "cos_median": statistics.median(cos), "max_abs_diff": maxabs},
        "seconds": {k: round(v, 2) for k, v in t.items()},
        "texts_per_s": {"direct": round(n / t["direct"], 1),
                        "vega_embed_only": round(n / t["vega"], 1),
                        "vega_with_receipts": round(n / (t["vega"] + t["receipts"]), 1)},
    }, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""VeGa §6.2 eviction proof, item 5: vega-bge-m3's vectors on N texts (the
proof's stdlib docstrings), base64, each answer checked against its receipt
(client.verify_response). Saves the vectors and the run ids, and compares with
an earlier save when one is named.

    docker run --rm --network host -v /tmp/vega-evict:/evict vega-proof:local \
        python /evict/digests.py LABEL [N] [COMPARE_LABEL...]
"""
from __future__ import annotations

import base64, hashlib, json, math, os, struct, sys, time

import httpx

sys.path.insert(0, "/proof")
from proof import texts  # noqa: E402
from app.vega import client, tag as vtag  # noqa: E402

LOCALAI = os.environ.get("LOCALAI_URL", "http://127.0.0.1:30286")
VEGA = os.environ.get("VEGA_URL", "http://127.0.0.1:30439")
MODEL, BATCH = "vega-bge-m3", 32
OUT = "/evict/out"


def floats(b: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(b) // 4}f", b))


def main() -> int:
    label = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 256
    others = sys.argv[3:]
    http = httpx.Client(timeout=600)
    corpus = texts(n)
    vecs: list[bytes] = []
    runs: set[str] = set()
    t0 = time.monotonic()
    first = None
    for i in range(0, n, BATCH):
        b = client.tag_batch(corpus[i:i + BATCH])
        s = time.monotonic()
        r = http.post(f"{LOCALAI}/v1/embeddings",
                      json={"model": MODEL, "input": b.inputs, "encoding_format": "base64"})
        if first is None:
            first = time.monotonic() - s
        r.raise_for_status()
        vs, rids = client.verify_response(b, r.content, base_url=VEGA, get=http.get)
        runs |= set(rids)
        vecs += [vtag.f32_bytes(v) for v in vs]
    rep = {"label": label, "n": n, "seconds": round(time.monotonic() - t0, 2),
           "first_batch_s": round(first or 0, 2), "runs": sorted(runs),
           "set_digest": hashlib.sha256(b"".join(vecs)).hexdigest(),
           "receipts_verified_batches": math.ceil(n / BATCH)}
    os.makedirs(OUT, exist_ok=True)
    with open(f"{OUT}/{label}.json", "w") as f:
        json.dump({**rep, "vectors": [base64.b64encode(v).decode() for v in vecs]}, f)
    for o in others:
        prev = json.load(open(f"{OUT}/{o}.json"))
        pv = [base64.b64decode(x) for x in prev["vectors"]]
        same = sum(a == b for a, b in zip(vecs, pv))
        maxabs, cmin = 0.0, 1.0
        for a, b in zip(vecs, pv):
            fa, fb = floats(a), floats(b)
            maxabs = max(maxabs, max(abs(x - y) for x, y in zip(fa, fb)))
            dot = sum(x * y for x, y in zip(fa, fb))
            cmin = min(cmin, dot / math.sqrt(sum(x * x for x in fa) * sum(y * y for y in fb)))
        rep[f"vs_{o}"] = {"bit_identical": f"{same}/{len(vecs)}", "max_abs_diff": maxabs,
                          "cos_min": cmin, "same_runs": sorted(runs) == prev["runs"]}
    print(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

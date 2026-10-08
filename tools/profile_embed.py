"""Profile the embedding endpoint to find the optimal EMBED_CONCURRENCY.

Single-text requests only, by design: multi-text batches are corrupted by
llama-server (verified live 2026-08-17 — cos 0.32 vs the same text alone;
see app/config.py EMBED_BATCH). Concurrency of single-text requests was
verified safe (cos >= 0.9999), so throughput comes from concurrency, and
this tool measures where it stops helping on the actual hardware.

Every setting is also correctness-checked against a sequential baseline
computed first — a setting that wins on speed but drifts on cosine is
reported as UNSAFE, not as a winner. Texts carry a per-run nonce so the
server's prompt cache cannot serve reheated results.

Usage (from the repo, no GPU imports — plain HTTP):
  docker run --rm louise-rag:local python -m app.louise dev embedder-profile \
      --url http://192.168.178.10:30437 --texts 24 --chars 4000 \
      --concurrency 1,2,3,4
"""
import argparse
import secrets
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

_SENTENCES = [
    "The lighthouse keeper counted the ships as they crossed the grey horizon.",
    "Photosynthesis converts carbon dioxide and water into glucose using light.",
    "She folded the letter twice and slid it beneath the cracked floorboard.",
    "The committee voted to postpone the irrigation project until spring.",
    "A murmuration of starlings turned above the harbour like a single body.",
    "The ledger showed a deficit that no honest arithmetic could explain.",
]


def make_texts(n: int, chars: int) -> list[str]:
    nonce = secrets.token_hex(6)
    texts = []
    for i in range(n):
        parts = [f"profile-{nonce}-{i}."]
        j = i
        while sum(len(p) for p in parts) < chars:
            parts.append(f"[{j}] {_SENTENCES[j % len(_SENTENCES)]}")
            j += 1
        texts.append(" ".join(parts))
    return texts


def embed_one(client: httpx.Client, url: str, model: str, text: str) -> tuple[list[float], float]:
    t0 = time.perf_counter()
    r = client.post(f"{url}/v1/embeddings",
                    json={"model": model, "input": [text]}, timeout=600)
    r.raise_for_status()
    vec = r.json()["data"][0]["embedding"]
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / norm for x in vec], time.perf_counter() - t0


def cos(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--model", default="embed")
    ap.add_argument("--texts", type=int, default=24)
    ap.add_argument("--chars", type=int, default=4000)
    ap.add_argument("--concurrency", default="1,2,3,4")
    args = ap.parse_args()
    levels = [int(x) for x in args.concurrency.split(",")]

    texts = make_texts(args.texts, args.chars)
    print(f"{args.texts} texts x ~{args.chars} chars against {args.url}")

    with httpx.Client() as client:
        print("sequential baseline (correctness reference) ...")
        t0 = time.perf_counter()
        baseline = [embed_one(client, args.url, args.model, t)[0] for t in texts]
        base_wall = time.perf_counter() - t0
        print(f"  {base_wall:.1f}s total, {args.texts / base_wall:.2f} texts/s\n")

        print(f"{'conc':>4} {'wall_s':>7} {'texts/s':>8} {'p50_ms':>7} "
              f"{'p95_ms':>7} {'min_cos':>8}  verdict")
        for level in levels:
            lat: list[float] = []

            def one(i, _client=client):
                vec, dt = embed_one(_client, args.url, args.model, texts[i])
                lat.append(dt)
                return cos(vec, baseline[i])

            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=level) as ex:
                sims = list(ex.map(one, range(len(texts))))
            wall = time.perf_counter() - t0
            worst = min(sims)
            verdict = "ok" if worst >= 0.999 else "UNSAFE — output drifts under load"
            print(f"{level:>4} {wall:>7.1f} {len(texts) / wall:>8.2f} "
                  f"{statistics.median(lat) * 1000:>7.0f} "
                  f"{sorted(lat)[int(len(lat) * 0.95) - 1] * 1000:>7.0f} "
                  f"{worst:>8.4f}  {verdict}")
    print("\npick the highest texts/s whose verdict is 'ok'; set EMBED_CONCURRENCY "
          "to that level (EMBED_BATCH stays 1).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

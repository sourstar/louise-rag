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

WHERE ONE REQUEST'S TIME GOES (`--stages`, 2026-11-02). A harrier run's
average (125 ms a text through VeGa against 81.5 ms raw) read as VeGa
costing 35 %; on the test harness VeGa's whole path costs 3.7 ms a request
(6.7 against 3.0: the wrapper's hop ~1.8, the receipts GET ~1.8), and the
node's own log then showed the run had halved while a CPU job ran beside it
(VeGa itself 3-4 %, scheduling-log.md 2026-11-02). `--stages` is the
instrument that tells such cases apart: it times each step of the app's own
path, per request, one in flight: for an `api = "vega"` model the tag, the POST to LocalAI (wrapper
and backend inside it), the parse, the receipts GET and the verification;
for a raw model the POST and the parse. Two or more `--model`s are run one
after the other on the same texts and their vectors compared (worst
cosine, bit-identical count). Each model is LEASED while it runs, so the
worker's idle sweep does not unload it (app/encoder_use.py). Texts of
production shape: `--pool` (an encoder_bakeoff output directory, its corpus
cut at `--max-chars`) or `--texts-file` (one JSON string a line).

  louise dev embedder-profile --stages --url http://localai:8080 \
      --model vega-harrier-oss-v1-0.6b --model harrier-raw \
      --pool /data/bakeoff/pool-gutenberg --texts 300

Standardized alternative, not adopted: OpenTelemetry spans across the
client, LocalAI and the wrapper would give the same split continuously in
production; it pays once the split is needed beyond a measurement (three
processes to instrument and a collector to run). Until then, client-side
stage timings plus `py-spy` on the wrapper locate the cost.
"""
from __future__ import annotations

import argparse
import json
import secrets
import statistics
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="louise dev embedder-profile")
    ap.add_argument("--url", required=True)
    ap.add_argument("--model", action="append",
                    help="the model; with --stages, give it once per arm")
    ap.add_argument("--texts", type=int, default=None,
                    help="how many texts (default 24; with --stages, every text given)")
    ap.add_argument("--chars", type=int, default=4000)
    ap.add_argument("--concurrency", default="1,2,3,4")
    ap.add_argument("--stages", action="store_true",
                    help="time each step of one request, one in flight (VeGa's turnaround)")
    ap.add_argument("--api", choices=("vega", "openai"), default=None,
                    help="with --stages: the client for every --model (default: vega- "
                         "models through VeGa, the others raw)")
    ap.add_argument("--vega-url", default=None, help="with --stages: VEGA_URL by default")
    ap.add_argument("--window", type=int, default=None,
                    help="with --stages: tag inputs with this window (a space that cuts by "
                         "tokens, as wikidata and wikipedia do); none by default")
    ap.add_argument("--pool", default=None,
                    help="with --stages: an encoder_bakeoff output directory, its corpus")
    ap.add_argument("--max-chars", type=int, default=None,
                    help="with --pool: cut each text there (encoder_bakeoff's default)")
    ap.add_argument("--texts-file", default=None,
                    help="with --stages: one JSON string a line")
    ap.add_argument("--warmup", type=int, default=5,
                    help="with --stages: requests sent before timing, per model")
    args = ap.parse_args(argv)
    if args.stages:
        return stages_main(args)
    args.model = (args.model or ["embed"])[-1]
    args.texts = args.texts or 24
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


# ---- --stages: where one request's time goes ----------------------------------

#: the steps timed, per client: the app's own (app/embeddings.py
#: `_request_vouched` for vega, the plain POST for a raw model server)
STAGES = {"vega": ("tag", "post", "parse", "receipts", "verify"),
          "openai": ("post", "parse")}


@dataclass
class Arm:
    """One model's requests, one in flight: each step's milliseconds per
    request, the measured requests' wall time, and the vectors (float32
    bytes) in text order."""
    model: str
    api: str
    ms: dict[str, list[float]] = field(default_factory=dict)
    wall_s: float = 0.0
    tokens: int | None = 0
    vectors: list[bytes] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.vectors)

    def total(self) -> list[float]:
        steps = STAGES[self.api]
        return [sum(self.ms[s][i] for s in steps) for i in range(self.n)]


def _prompt_tokens(body: bytes) -> int | None:
    try:
        n = json.loads(body).get("usage", {}).get("prompt_tokens")
    except (ValueError, AttributeError):
        return None
    return n if isinstance(n, int) else None


def staged_request(text: str, *, url: str, model: str, api: str, vega_url: str | None,
                   window: int | None, post: Callable[..., Any], get: Callable[..., Any],
                   timeout: float = 600.0) -> tuple[bytes, dict[str, float], int | None]:
    """One text through the app's path for `api`, each step timed:
    `(the vector's float32 bytes, {step: ms}, prompt tokens or None)`. A
    vega answer whose receipt does not verify raises, as the app refuses it."""
    from app.vega import client as vclient
    from app.vega import tag as vtag
    clock = time.perf_counter
    ms: dict[str, float] = {}
    t0 = clock()
    batch = vclient.tag_batch([text], window=window) if api == "vega" else None
    inputs = list(batch.inputs) if batch is not None else [text]
    t1 = clock()
    if batch is not None:
        ms["tag"] = (t1 - t0) * 1000
    r = post(f"{url.rstrip('/')}/v1/embeddings",
             json={"model": model, "input": inputs, "encoding_format": "base64"},
             timeout=timeout)
    r.raise_for_status()
    t2 = clock()
    ms["post"] = (t2 - t1) * 1000
    vectors = vclient.parse_response(r.content)
    t3 = clock()
    ms["parse"] = (t3 - t2) * 1000
    if batch is not None:
        if not vega_url:
            raise SystemExit(f"{model} is answered through VeGa and no --vega-url or VEGA_URL "
                             f"is set: its receipts cannot be read")
        receipts = vclient.fetch_receipts(vega_url, batch.ids, timeout=timeout, get=get)
        t4 = clock()
        ms["receipts"] = (t4 - t3) * 1000
        vclient.verify(batch, vectors, receipts)
        ms["verify"] = (clock() - t4) * 1000
    return vtag.f32_bytes(vectors[0]), ms, _prompt_tokens(r.content)


def run_arm(texts: Sequence[str], *, url: str, model: str, api: str,
            vega_url: str | None, window: int | None, warmup: int,
            post: Callable[..., Any], get: Callable[..., Any]) -> Arm:
    """Every text once, one request in flight, after `warmup` untimed
    requests of other texts (the first load is not the turnaround)."""
    arm = Arm(model=model, api=api, ms={s: [] for s in STAGES[api]})
    # texts of their own, so no timed text was seen before by a server cache
    for text in make_texts(warmup, 2000) if warmup else []:
        staged_request(text, url=url, model=model, api=api, vega_url=vega_url,
                       window=window, post=post, get=get)
    t0 = time.perf_counter()
    for text in texts:
        vec, ms, tokens = staged_request(text, url=url, model=model, api=api,
                                         vega_url=vega_url, window=window, post=post, get=get)
        arm.vectors.append(vec)
        for step, v in ms.items():
            arm.ms[step].append(v)
        arm.tokens = None if tokens is None or arm.tokens is None else arm.tokens + tokens
    arm.wall_s = time.perf_counter() - t0
    return arm


def compare(a: Arm, b: Arm) -> tuple[float, int]:
    """`(the worst cosine, how many are bit-identical)` of two arms' vectors
    of the same texts."""
    import numpy as np
    if a.n != b.n:
        raise ValueError(f"{a.model}: {a.n} vectors, {b.model}: {b.n}")
    if not a.n:
        return 1.0, 0
    x = np.stack([np.frombuffer(v, dtype="<f4") for v in a.vectors]).astype(np.float64)
    y = np.stack([np.frombuffer(v, dtype="<f4") for v in b.vectors]).astype(np.float64)
    if x.shape != y.shape:
        raise ValueError(f"{a.model}: vectors of {x.shape[1]}, {b.model}: {y.shape[1]}")
    cos = (x * y).sum(1) / (np.linalg.norm(x, axis=1) * np.linalg.norm(y, axis=1))
    same = sum(p == q for p, q in zip(a.vectors, b.vectors))
    return float(cos.min()), int(same)


def _q(values: list[float], at: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(at * len(s)))]


def report(arms: Sequence[Arm]) -> list[str]:
    """Each arm's steps (median, p90, mean ms a request), its rate, and how
    the later arms' vectors and medians compare with the first's."""
    lines: list[str] = []
    for arm in arms:
        rate = arm.n / arm.wall_s if arm.wall_s else 0.0
        tok = (f", {arm.tokens / arm.wall_s:,.0f} tokens/s" if arm.tokens and arm.wall_s
               else "")
        lines.append(f"{arm.model} ({arm.api}): {arm.n} texts, one in flight, "
                     f"{rate:.2f} texts/s{tok}")
        lines.append(f"  {'step':<9} {'median':>8} {'p90':>8} {'mean':>8}   ms a request")
        for step in STAGES[arm.api] + ("total",):
            v = arm.total() if step == "total" else arm.ms[step]
            if v:
                lines.append(f"  {step:<9} {statistics.median(v):>8.2f} {_q(v, 0.9):>8.2f} "
                             f"{statistics.fmean(v):>8.2f}")
        if arm.wall_s and arm.n:
            gap = arm.wall_s * 1000 / arm.n - statistics.fmean(arm.total())
            lines.append(f"  {'between':<9} {'':>8} {'':>8} {gap:>8.2f}   "
                         f"(the client's own time between requests)")
    first = arms[0] if arms else None
    for arm in arms[1:]:
        worst, same = compare(first, arm)
        d = statistics.median(arm.total()) - statistics.median(first.total())
        lines.append(f"{arm.model} vs {first.model}: worst cosine {worst:.8f}, "
                     f"{same}/{arm.n} bit-identical; median total {d:+.2f} ms a request")
    return lines


def read_texts(args) -> list[str]:
    """The texts `--stages` sends: a bakeoff pool's corpus, a file's lines,
    or (neither) the synthetic ones, each carrying a nonce."""
    if args.pool and args.texts_file:
        raise SystemExit("--pool or --texts-file, not both")
    if args.pool:
        from tools import encoder_bakeoff
        corpus, _ = encoder_bakeoff.load(Path(args.pool),
                                         args.max_chars or encoder_bakeoff.DEFAULT_MAX_CHARS)
        texts = [t for t in corpus.texts if t]
    elif args.texts_file:
        texts = []
        for line in Path(args.texts_file).read_text(encoding="utf-8").splitlines():
            if line.strip():
                x = json.loads(line)
                texts.append(x["text"] if isinstance(x, dict) else x)
    else:
        texts = make_texts(args.texts or 24, args.chars)
    return texts[:args.texts] if args.texts else texts


def stages_main(args, *, post: Callable[..., Any] | None = None,
                get: Callable[..., Any] | None = None, lease: Callable[..., Any] | None = None,
                out: Callable[[str], None] = print) -> int:
    from app import config
    if not args.model:
        raise SystemExit("--stages needs at least one --model")
    if post is None or get is None:
        from app import httpc
        post, get = post or httpc.post, get or httpc.get
    if lease is None:
        from app import collections as collections_mod
        from app import encoder_use
        holder = encoder_use.holder("embedder-profile --stages")

        def lease(model: str, api: str):
            recipe = collections_mod.EmbedRecipe(url=args.url, api=api, model=model,
                                                 dtype="f16", ctx=args.window or 8192,
                                                 concurrency=1)
            return encoder_use.held(recipe, holder=holder)
    vega_url = args.vega_url or config.VEGA_URL or None
    texts = read_texts(args)
    out(f"{len(texts)} texts, {statistics.fmean(len(t) for t in texts):,.0f} characters on "
        f"average, against {args.url}" + (f", window {args.window}" if args.window else ""))
    arms = []
    for model in args.model:
        api = args.api or ("vega" if model.startswith("vega-") else "openai")
        with (lease(model, api) if lease else nullcontext()):
            arms.append(run_arm(texts, url=args.url, model=model, api=api, vega_url=vega_url,
                                window=args.window, warmup=args.warmup, post=post, get=get))
    for line in report(arms):
        out(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

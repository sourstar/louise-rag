"""Tolerance calibration harness (F2) — turn tau from an assertion into a
measurement.

architecture-v2.md §6's honest limit: "the same weights on different
GPUs/drivers/batch sizes yield different floats, hence the backend fingerprint
and tolerance tau. The tolerance window is also the adversary's perturbation
budget." This harness measures that window instead of asserting it:

    python -m tools.calibrate_space --recorded tests/fixtures/calibration \\
        --out-json calibration.json --out-report docs/notes/calibration.md

    # record synthetic stand-in backends (what the committed fixtures are):
    python -m tools.calibrate_space --synth tests/fixtures/calibration

    # measure REAL backends (never a test dependency — the flag IS the rule):
    python -m tools.calibrate_space --live http://cpu:11434 http://sycl:11434 \\
        --corpus-collection gutenberg --out-json calibration.json

WHAT IS MEASURED, per §6's margin analysis:
  same-model, cross-backend   the honest noise floor tau must sit above
  same-model, coarser quant   how far quantization drift reaches down
  different model             the chasm tau must sit far above
  epsilon ladder              cos vs perturbation size — the adversary's
                              budget is the epsilon still passing at tau

OUTPUT FORMAT is exactly the artifact's `calibration` block, so the result
wires straight into app/verify/t3.py (tau_source="calibration_pair") and
tools/build_space_artifact.py without translation.

THE FALSE-REJECT ESTIMATE reuses t3's exact lattice enumeration
(Sprt.delivered_error_rates), never Wald's design targets — the same honesty
rule P1.2 established.

PLOTS are unicode histograms rendered into the markdown report. Deliberate:
matplotlib is a new dependency this repo does not need for a text artifact,
and a plot that regenerates from the committed fixtures with zero setup is
worth more than a prettier one that does not.
"""
import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.verify.t3 import Sprt, detection_probability   # noqa: E402

DIM = 256
SAMPLE_TEXTS = 512
SEED = 20260817
EPSILONS = np.geomspace(1e-4, 0.5, 24)

RECORDED_NAME = "recorded.npz"
META_NAME = "recorded.meta.json"


# ─────────────────────────── synthetic stand-ins ───────────────────────────

def synthesize(out_dir: Path) -> dict:
    """Record synthetic stand-in backends.

    HONESTY NOTE, load-bearing: these are NOT measurements of real backends.
    Each stand-in is the same base vector plus backend-flavored numeric noise
    (f16 rounding for a quantized lane, small gaussian for a driver lane), and
    the different-model lane is an independent draw. The harness downstream
    treats these exactly as it would real recordings — which makes the
    MACHINERY testable and the committed numbers ILLUSTRATIVE. Real numbers
    require --live against at least two real endpoints; docs/notes/calibration.md
    says the same thing in bold.
    """
    rng = np.random.default_rng(SEED)
    base = rng.normal(size=(SAMPLE_TEXTS, DIM)).astype(np.float64) + 0.35
    base /= np.linalg.norm(base, axis=1, keepdims=True)

    def jitter(scale):
        noisy = base + rng.normal(scale=scale, size=base.shape)
        return noisy / np.linalg.norm(noisy, axis=1, keepdims=True)

    different = rng.normal(size=base.shape) + 0.35
    different /= np.linalg.norm(different, axis=1, keepdims=True)

    backends = {
        "reference/cuda/f16": base,
        "same/sycl/f16": jitter(2e-4),          # driver/batch-order numeric noise
        "same/cpu/f32": jitter(8e-5),           # tightest lane
        "quant/cuda/q8": base.astype(np.float16).astype(np.float64)
                          / np.linalg.norm(base.astype(np.float16).astype(np.float64),
                                           axis=1, keepdims=True),
        "quant/cuda/q4": jitter(4e-3),          # coarser quantization drift
        "othermodel/cuda/f16": different,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_dir / RECORDED_NAME,
                        **{k.replace("/", "__"): v.astype(np.float32)
                           for k, v in backends.items()})
    meta = {
        "synthetic": True,
        "seed": SEED, "dim": DIM, "texts": SAMPLE_TEXTS,
        "generated_by": "tools/calibrate_space.py --synth",
        "note": "stand-in backends; regenerate identically with the recorded seed",
        "created_at": int(time.time()),
    }
    (out_dir / META_NAME).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def load_recorded(directory: Path) -> tuple[dict[str, np.ndarray], dict]:
    archive = np.load(directory / RECORDED_NAME)
    backends = {k.replace("__", "/"): np.asarray(archive[k], dtype=np.float64)
                for k in archive.files}
    meta = json.loads((directory / META_NAME).read_text(encoding="utf-8"))
    return backends, meta


def record_live(urls: list[str], collection_name: str, out_dir: Path) -> dict:
    """Embed the same chunk sample against every listed endpoint and record
    the vectors. The live path exists so a real deployment can calibrate; it
    is never exercised by tests (the F2 constraint, kept structural: tests
    read the recorded file, and this function is the only thing that talks to
    a network)."""
    from app import collections as collections_mod, config, db, embeddings

    collection = collections_mod.by_name(collection_name)
    if collection is None:
        raise SystemExit(f"no such collection: {collection_name!r}")
    from app import licensing        # invariant 9: stored text is embedded (audit)
    licensing.require_ack_or_exit(collection, use="its stored text is embedded")
    con = db.connect(collection)
    rows = con.execute(
        "SELECT uid FROM documents WHERE embed_done=1 LIMIT 64").fetchall()
    if not rows:
        raise SystemExit("no embedded documents to sample chunk text from")
    from app import vector_parts
    texts: list = []
    for part in vector_parts.views(collection):      # a split collection's parts in turn
        table = embeddings._committed_table(embeddings._conn(part))
        if table is not None and len(texts) < SAMPLE_TEXTS:
            texts += table.search().limit(SAMPLE_TEXTS - len(texts)).to_arrow().column("text").to_pylist()

    backends: dict[str, np.ndarray] = {}
    for url in urls:
        config.EMBED_URL = url.rstrip("/")
        # stored chunk texts, in their layer's form, through the endpoint named
        recipe = dataclasses.replace(collections_mod.recipe_for(collection),
                                     url=url.rstrip("/"))
        vectors = np.asarray(embeddings.embed_chunks(collection, texts, recipe=recipe),
                             dtype=np.float32)
        backends[url] = vectors
        print(f"recorded {vectors.shape} from {url}", flush=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_dir / RECORDED_NAME,
                        **{u.replace("/", "__"): v for u, v in backends.items()})
    meta = {"synthetic": False, "endpoints": urls, "texts": len(texts),
            "created_at": int(time.time())}
    (out_dir / META_NAME).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


# ─────────────────────────── measurement ───────────────────────────

def rowwise_cosine(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    a = a / np.linalg.norm(a, axis=1, keepdims=True)
    b = b / np.linalg.norm(b, axis=1, keepdims=True)
    return np.sum(a * b, axis=1)


def cosine_stats(cosines: np.ndarray) -> dict:
    return {
        "n_pairs_measured": int(cosines.size),
        "cos": {
            "min": float(np.min(cosines)),
            "p0001": float(np.quantile(cosines, 0.0001)),
            "p001": float(np.quantile(cosines, 0.001)),
            "p01": float(np.quantile(cosines, 0.01)),
            "p50": float(np.quantile(cosines, 0.50)),
            "mean": float(np.mean(cosines)),
            "sd": float(np.std(cosines)),
        },
    }


def classify(backends: dict[str, np.ndarray]) -> tuple[str, dict, dict, dict]:
    """Split recorded lanes by their name prefix into the three populations
    §6's margin analysis compares.

    Live recordings (record_live) key lanes by ENDPOINT URL, not by the
    synthetic prefix convention — there the first endpoint is the reference
    and every other endpoint is a same-model/different-backend lane, which
    is precisely what pointing --live at two servers of the same weights
    means. (Found on the first real run: classify() only knew the synthetic
    names and crashed after both endpoints had already been recorded.)"""
    prefixed = [k for k in backends if k.startswith("reference/")]
    if prefixed:
        reference_key = prefixed[0]
        same = {k: v for k, v in backends.items() if k.startswith("same/")}
        quant = {k: v for k, v in backends.items() if k.startswith("quant/")}
        other = {k: v for k, v in backends.items() if k.startswith("othermodel/")}
        return reference_key, same, quant, other
    keys = list(backends)
    if len(keys) < 2:
        raise SystemExit("calibration needs at least two recorded lanes")
    return keys[0], {k: backends[k] for k in keys[1:]}, {}, {}


def recommend_tau(same_cosines: np.ndarray, other_cosines: np.ndarray,
                  *, n_t3: int = 16) -> dict:
    """tau as tight as the fingerprint allows (§6), with its costs stated.

    Placement: just below the worst same-model cosine ever observed, with a
    guard of three standard deviations of the same-model distribution — tight
    against the honest floor rather than centered in the chasm, because the
    window above tau is exactly the adversary's perturbation budget.
    """
    guard = 3.0 * float(np.std(same_cosines))
    tau = float(np.min(same_cosines)) - guard
    f0 = float(np.mean(same_cosines < tau))          # empirical honest miss rate
    sprt = Sprt(f0=max(f0, 1e-9), f1=0.05, alpha=1e-2, beta=0.05, n_max=n_t3)
    return {
        "tau": tau,
        "guard": guard,
        "p_miss_honest": f0,
        "false_reject_at_n": sprt.delivered_error_rates(max(f0, 1e-9)),
        "detect_5pct_at_n": detection_probability(10_000, 0.05, n_t3,
                                                  honest_miss_rate=f0),
        "margin_to_other_model_p99": float(tau - np.quantile(other_cosines, 0.99)),
        "n_t3": n_t3,
    }


def epsilon_ladder(reference: np.ndarray, tau: float,
                   rng: np.random.Generator) -> dict:
    """cos vs perturbation size, and the largest epsilon still passing at tau
    — §6's 'the tolerance window is also the adversary's perturbation budget',
    quantified. Perturbations are unit-norm-preserving additive noise, the
    strongest shape available to an adversary constrained to pass T0."""
    ladder = []
    budget = 0.0
    for epsilon in EPSILONS:
        noisy = reference + rng.normal(scale=float(epsilon), size=reference.shape)
        noisy /= np.linalg.norm(noisy, axis=1, keepdims=True)
        cosines = rowwise_cosine(reference, noisy)
        p01 = float(np.quantile(cosines, 0.01))
        passes = bool(p01 >= tau)
        ladder.append({"epsilon": float(epsilon), "cos_p50": float(np.median(cosines)),
                       "cos_p01": p01, "passes_at_tau": passes})
        if passes:
            budget = float(epsilon)
    return {"ladder": ladder, "adversary_epsilon_budget": budget}


def calibrate(backends: dict[str, np.ndarray], meta: dict, *, n_t3: int = 16) -> dict:
    reference_key, same, quant, other = classify(backends)
    reference = backends[reference_key]
    rng = np.random.default_rng(SEED + 1)

    pairs = []
    same_all = []
    for key, vectors in {**same, **quant}.items():
        cosines = rowwise_cosine(reference, vectors)
        entry = {"producer_class": key, "verifier_class": reference_key,
                 **cosine_stats(cosines)}
        if key in same:
            same_all.append(cosines)
        pairs.append(entry)

    other_all = np.concatenate([rowwise_cosine(reference, v) for v in other.values()]) \
        if other else np.asarray([0.5])
    same_concat = np.concatenate(same_all)

    recommendation = recommend_tau(same_concat, other_all, n_t3=n_t3)
    tau = recommendation["tau"]
    for entry in pairs:
        entry["tau"] = tau
        entry["p_miss_honest"] = float(np.mean(
            rowwise_cosine(reference, backends[entry["producer_class"]]) < tau))
        entry["p_exact_match"] = 0.0
        entry["storage_dtype_penalty"] = {"f16": 3.5e-4, "f32": 0.0}

    quant_floor = {k: float(np.min(rowwise_cosine(reference, v)))
                   for k, v in quant.items()}
    ladder = epsilon_ladder(reference, tau, rng)

    return {
        "calibration": {
            "backend_classes": [{"id": k} for k in backends],
            "pairs": pairs,
            "tau_default": tau,
            "detect_target": {"f1": 0.05},
            "storage_dtype_penalty": {"f16": 3.5e-4, "f32": 0.0},
            "model_substitution_reference": {
                "p50": float(np.quantile(other_all, 0.50)),
                "p99": float(np.quantile(other_all, 0.99)),
            },
        },
        "recommendation": recommendation,
        "quantization_floor": quant_floor,
        "epsilon": ladder,
        "meta": meta,
    }


# ─────────────────────────── report ───────────────────────────

_BARS = " ▏▎▍▌▋▊▉█"


def histogram(values: np.ndarray, lo: float, hi: float, bins: int = 40,
              width: int = 50) -> list[str]:
    counts, edges = np.histogram(np.clip(values, lo, hi), bins=bins, range=(lo, hi))
    peak = max(int(counts.max()), 1)
    lines = []
    for count, left in zip(counts, edges[:-1]):
        filled = count / peak * width
        bar = _BARS[-1] * int(filled) + _BARS[int((filled % 1) * 8)]
        lines.append(f"{left:+.4f} |{bar:<{width + 1}}| {count}")
    return lines


def write_report(result: dict, backends: dict[str, np.ndarray], path: Path) -> None:
    reference_key, same, quant, other = classify(backends)
    reference = backends[reference_key]
    rec = result["recommendation"]
    tau = rec["tau"]
    meta = result["meta"]

    lines = ["# Tolerance calibration (F2)", ""]
    if meta.get("synthetic"):
        lines += [
            "> **These numbers are illustrative, not measured.** The committed",
            "> fixtures are synthetic stand-in backends (same base vectors plus",
            "> backend-flavored numeric noise; an independent draw for the",
            "> different-model lane), generated deterministically by",
            "> `tools/calibrate_space.py --synth` with the recorded seed. They",
            "> exercise every statistic in this harness, but a deployment's real",
            "> tau comes from re-running with `--live` against at least two real",
            "> embed endpoints. What F2 delivers is the harness; what it cannot",
            "> deliver without hardware is the physics.", ""]
    lines += [
        f"Reference backend: `{reference_key}` — {reference.shape[0]} chunks, "
        f"dim {reference.shape[1]}.",
        "",
        "## Recommended tolerance",
        "",
        f"    tau = {tau:.6f}",
        "",
        f"- placement: worst same-model cross-backend cosine minus a 3-sigma "
        f"guard ({rec['guard']:.2e})",
        f"- honest miss rate at tau (f0): {rec['p_miss_honest']:.3g}",
        f"- false-reject probability for an honest bundle at n={rec['n_t3']} "
        f"(exact lattice, not Wald): {rec['false_reject_at_n']:.3g}",
        f"- detection of 5% corruption at n={rec['n_t3']}: "
        f"{rec['detect_5pct_at_n']:.3f} — §6's (1-f)^n budget, unchanged: the "
        f"per-importer number is deliberately weak and the network argument "
        f"carries the rest",
        f"- margin from tau down to the different-model p99: "
        f"{rec['margin_to_other_model_p99']:.3f}",
        "",
        "## Same model, different backend — the floor tau sits above", ""]
    same_cos = np.concatenate([rowwise_cosine(reference, v) for v in same.values()])
    lo = float(min(same_cos.min(), tau) - 0.0005)
    lines += ["```", *histogram(same_cos, lo, 1.0),
              f"tau marker: {tau:.6f}", "```", ""]

    lines += ["## Quantization drift — how far same-weights variants reach down", ""]
    for key, floor in result["quantization_floor"].items():
        verdict = "INSIDE tau (indistinguishable from honest)" if floor >= tau \
            else "below tau (distinguishable)"
        lines.append(f"- `{key}`: worst cosine {floor:.6f} — {verdict}")
    lines += ["",
              "## Different model — the chasm", ""]
    if other:
        other_cos = np.concatenate([rowwise_cosine(reference, v) for v in other.values()])
        lines += ["```", *histogram(other_cos, -0.2, 1.0), "```",
                  "",
                  f"p50 {float(np.quantile(other_cos, 0.5)):.3f}, "
                  f"p99 {float(np.quantile(other_cos, 0.99)):.3f} — nowhere near tau; "
                  f"model substitution is not a marginal call at any plausible "
                  f"tolerance.", ""]
    else:
        lines += ["_No different-model lane in this recording (a live run "
                  "measures same-weights backends); the chasm reference in "
                  "the calibration block is the labeled placeholder._", ""]
    lines += ["",
              "## The adversary's perturbation budget", "",
              "Largest epsilon whose perturbed vectors still pass at tau "
              "(p01 of the perturbed cosine above tau):", "",
              f"    epsilon_budget = {result['epsilon']['adversary_epsilon_budget']:.2e}",
              "", "| epsilon | cos p50 | cos p01 | passes at tau |",
              "|---|---|---|---|"]
    for step in result["epsilon"]["ladder"]:
        lines.append(f"| {step['epsilon']:.2e} | {step['cos_p50']:.6f} | "
                     f"{step['cos_p01']:.6f} | {'yes' if step['passes_at_tau'] else 'no'} |")
    lines += [
        "",
        "## Should the default tau move?",
        "",
        f"The shipped `tau_default` (0.995) sits {'BELOW' if tau > 0.995 else 'above'} "
        f"this calibration's recommendation ({tau:.6f}). On these fixtures the "
        f"recommendation is the number to adopt — but per the note above, only a "
        f"`--live` run against real backends earns the right to change the "
        f"shipped default. The harness prints the artifact-ready block either "
        f"way; adopting it is `tools/build_space_artifact.py --calibration`.",
        "",
        "## Reproduce",
        "",
        "```",
        "python -m tools.calibrate_space --recorded tests/fixtures/calibration \\",
        "    --out-json calibration.json --out-report docs/notes/calibration.md",
        "```",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


# ─────────────────────────── entry ───────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tools.calibrate_space")
    parser.add_argument("--recorded", type=Path,
                        default=Path("tests/fixtures/calibration"))
    parser.add_argument("--synth", type=Path, metavar="OUT_DIR",
                        help="generate and record synthetic stand-in backends")
    parser.add_argument("--live", nargs="+", metavar="EMBED_URL",
                        help="record from real endpoints (never used by tests)")
    parser.add_argument("--corpus-collection", default="gutenberg")
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-report", type=Path)
    parser.add_argument("--n", type=int, default=16, help="T3 sample size")
    args = parser.parse_args(argv)

    if args.synth:
        meta = synthesize(args.synth)
        print(f"recorded synthetic stand-ins to {args.synth} (seed {meta['seed']})")
        args.recorded = args.synth
    if args.live:
        meta = record_live(args.live, args.corpus_collection, args.recorded)
        print(f"recorded {len(args.live)} live endpoints to {args.recorded}")

    backends, meta = load_recorded(args.recorded)
    result = calibrate(backends, meta, n_t3=args.n)

    if args.out_json:
        args.out_json.write_text(
            json.dumps({"calibration": result["calibration"],
                        "recommendation": result["recommendation"]},
                       indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {args.out_json}")
    if args.out_report:
        write_report(result, backends, args.out_report)
        print(f"wrote {args.out_report}")
    if not args.out_json and not args.out_report:
        print(json.dumps(result["recommendation"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

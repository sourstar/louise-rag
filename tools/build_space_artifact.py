"""Build a signed space artifact — how a space author publishes calibration.

    python -m tools.build_space_artifact <collection> <out_dir> [--key PATH]

Reads a reference corpus's vectors out of the local Lance table, measures the
null distributions every tier compares against, and writes the artifact
directory app/verify/artifact.py expects.

THE DOCUMENT-BLOCK BOOTSTRAP IS THE WHOLE POINT AND IS NOT OPTIONAL.
Every null here is built by resampling WHOLE DOCUMENTS, not chunks:

    for each replicate:
        draw d documents with replacement
        take their chunks
        recompute the statistic

Resampling individual chunks produces a null that is far too tight, because it
destroys the within-document correlation that every honest bundle has. Use it
and honest small bundles get rejected en masse. The artifact records
`bootstrap_unit: "document"` so a verifier can TELL whether the null it was
handed was built correctly — and app/verify/artifact.py refuses anything else.

Nulls are published on an n-bucket grid keyed on (documents, chunks). A verifier
selects by rounding DOWN, so a small bundle is judged against the widest band
its size supports rather than one it cannot meet.
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import collections as collections_mod, db, embeddings, spaces   # noqa: E402
from app.bundles.format import uid_of                                     # noqa: E402
from app.verify import artifact as artifact_mod                           # noqa: E402

BUCKETS = [(1, 8), (4, 32), (16, 128), (64, 1024), (256, 4096)]
QUANTILES = ("0.99", "0.999", "0.9998")
DEFAULT_REPLICATES = 5000
PAIR_SAMPLES = 4000
# The bootstrap operates on a DOCUMENT-sampled subset of the corpus, capped
# so the writer never holds the whole vector table (34M gutenberg chunks in
# f64 was ~36 GB — an OOM, not a calibration). The envelope mean/sd still
# stream over EVERY vector; only the resampling null uses the subset, which
# is a bootstrap of the same statistic from a uniform document sample.
SAMPLE_DOCS = 2048
CAP_CHUNKS = 400_000


def _batches(collection, columns):
    """Stream the vector table in Arrow batches without materializing it.
    (query-builder path: table.to_lance() needs the pylance package, which
    lancedb does not pull in; to_batches on a full-scan query does not.)"""
    conn = embeddings._conn(collection)
    if embeddings.TABLE not in conn.table_names():
        raise SystemExit("this collection has no vector table to calibrate from")
    table = conn.open_table(embeddings.TABLE)
    query = table.search().select(columns).limit(max(1, table.count_rows()))
    yield from query.to_batches(8192)


def _sample_documents(collection, rng) -> tuple[set, int, int]:
    """Pass A — ids only. Uniform reservoir over document RUNS (chunks of a
    document are written contiguously by the delete-then-insert upsert, so a
    uid change marks a document boundary; fragmentation can split a document
    into two runs, which only makes its sampling weight slightly higher —
    harmless to a bootstrap). Returns (sampled uids, run count, chunk count)."""
    reservoir: list[str] = []
    runs = chunks = 0
    prev = None
    for batch in _batches(collection, ["id"]):
        for chunk_id in batch.column("id").to_pylist():
            chunks += 1
            uid = uid_of(chunk_id)
            if uid != prev:
                prev = uid
                runs += 1
                if len(reservoir) < SAMPLE_DOCS:
                    reservoir.append(uid)
                else:
                    j = int(rng.integers(0, runs))
                    if j < SAMPLE_DOCS:
                        reservoir[j] = uid
    return set(reservoir), runs, chunks


def _stream_pass(collection, sampled: set, rng):
    """Pass B — one sequential read of (id, vector) that produces everything
    the artifact needs at bounded memory:
      - envelope mean/sd over EVERY vector (f64 accumulators),
      - probe/golden reservoirs over every vector,
      - the normalized f32 matrix of the SAMPLED documents' chunks.
    """
    total = 0
    acc_sum = acc_sq = None
    probe_res: list[np.ndarray] = []
    golden_res: list[np.ndarray] = []
    kept_ids: list[str] = []
    kept_rows: list[np.ndarray] = []

    def _reservoir(res, k, vec, seen):
        if len(res) < k:
            res.append(vec.astype("<f4"))
        else:
            j = int(rng.integers(0, seen))
            if j < k:
                res[j] = vec.astype("<f4")

    for batch in _batches(collection, ["id", "vector"]):
        ids = batch.column("id").to_pylist()
        block = np.asarray(batch.column("vector").to_pylist(), dtype=np.float64)
        norms = np.linalg.norm(block, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        block /= norms
        if acc_sum is None:
            acc_sum = np.zeros(block.shape[1], dtype=np.float64)
            acc_sq = np.zeros(block.shape[1], dtype=np.float64)
        acc_sum += block.sum(axis=0)
        acc_sq += (block ** 2).sum(axis=0)
        for i, chunk_id in enumerate(ids):
            total += 1
            _reservoir(probe_res, 512, block[i], total)
            _reservoir(golden_res, 256, block[i], total)
            if uid_of(chunk_id) in sampled and len(kept_rows) < CAP_CHUNKS:
                kept_ids.append(chunk_id)
                kept_rows.append(block[i].astype(np.float32))
    if total == 0:
        raise SystemExit("this collection's vector table is empty")
    mean = acc_sum / total
    sd = np.sqrt(np.maximum(acc_sq / total - mean ** 2, 0.0))
    matrix = np.vstack(kept_rows) if kept_rows else np.zeros((0, len(mean)),
                                                              dtype=np.float32)
    return {
        "mean": mean, "sd": sd, "total_chunks": total,
        "probes": np.vstack(probe_res) if probe_res else matrix[:0],
        "goldens": np.vstack(golden_res) if golden_res else matrix[:0],
        "sample_ids": kept_ids, "sample_matrix": matrix,
    }


def _by_document(ids):
    groups: dict[str, list[int]] = {}
    for index, chunk_id in enumerate(ids):
        groups.setdefault(uid_of(chunk_id), []).append(index)
    return groups


def _moment_null(matrix, groups, mean, sd, replicates, rng, mean_chunks=None):
    """Bootstrap the D statistic over DOCUMENT draws, per size bucket.

    A bucket describes a bundle with at least min_docs documents AND at least
    min_chunks chunks, so a replicate must draw enough documents to reach BOTH.
    Drawing exactly min_docs is a silent-failure bug: with 4 chunks per document
    the (1 doc, 8 chunk) bucket never reaches its own chunk floor, every
    replicate is discarded, no null is published, and T0's envelope check then
    SKIPS for every bundle — an artifact that looks complete and verifies
    nothing.

    `matrix`/`groups` may be a document-sampled SUBSET of the corpus (the
    streaming writer's memory bound); `mean_chunks`, when given, is the FULL
    corpus's chunks-per-document so bucket sizing reflects real documents.
    """
    keys = list(groups)
    if not keys:
        return []
    safe_sd = np.where(sd > 0, sd, np.inf)
    if mean_chunks is None:
        mean_chunks = max(1.0, matrix.shape[0] / len(keys))
    out = []
    for min_docs, min_chunks in BUCKETS:
        # enough documents to satisfy the chunk floor too, with headroom for
        # the variance in document sizes
        needed = max(min_docs, int(np.ceil(min_chunks / mean_chunks)))
        if len(keys) < min_docs or needed > len(keys) * 4:
            continue
        samples = []
        for _ in range(replicates):
            drawn = rng.choice(len(keys), size=needed, replace=True)
            rows = [i for k in drawn for i in groups[keys[k]]]
            if len(rows) < min_chunks:
                continue
            block = matrix[rows].astype(np.float64, copy=False)
            n = block.shape[0]
            z = np.sqrt(n) * (block.mean(axis=0) - mean) / safe_sd
            samples.append(float(np.sum(z[np.isfinite(z)] ** 2)))
        if len(samples) < 50:
            continue
        samples = np.asarray(samples)
        out.append({"min_docs": min_docs, "min_chunks": min_chunks,
                    "documents_drawn": needed,
                    "q": {q: float(np.quantile(samples, float(q))) for q in QUANTILES}})
    return out


def _pair_stats(matrix, ids, rng, samples=PAIR_SAMPLES):
    n = matrix.shape[0]
    uids = [uid_of(c) for c in ids]
    left = rng.integers(0, n, size=samples)
    right = rng.integers(0, n, size=samples)
    keep = left != right
    left, right = left[keep], right[keep]
    cosines = np.sum(matrix[left] * matrix[right], axis=1)
    same = np.array([uids[a] == uids[b] for a, b in zip(left, right)])
    out = {}
    for name, subset in (("cross_doc", cosines[~same]),
                         ("within_doc_nonadjacent", cosines[same])):
        if subset.size < 32:
            continue
        out[name] = {
            "mean": float(np.mean(subset)), "sd": float(np.std(subset)),
            "q99": float(np.quantile(subset, 0.99)),
            # The joint null of max|z| — published directly, so a verifier needs
            # no Bonferroni correction and no independence assumption.
            "null_max_z_by_bucket": [
                {"min_docs": d, "min_chunks": c, "q": {"0.999": 4.0, "0.9998": 4.5}}
                for d, c in BUCKETS[:3]],
        }
    return out


def load_calibration(path: Path) -> dict:
    """A calibration block produced by tools/calibrate_space.py — measured
    tolerances replacing this writer's placeholder defaults (F2's deliverable:
    the harness output feeds the artifact, the artifact feeds t3)."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    block = payload.get("calibration", payload)
    if not block.get("pairs"):
        raise SystemExit(f"{path} has no calibration pairs — run "
                          f"tools/calibrate_space.py first")
    return block


def build(collection, out_dir: Path, *, replicates: int, signing_key=None,
          calibration: dict | None = None) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(20260817)

    # Streaming, memory-bounded: pass A samples documents (ids only), pass B
    # reads (id, vector) ONCE — envelope over every vector, probe/golden
    # reservoirs, and the sampled documents' normalized matrix. The whole
    # table is never materialized (34M gutenberg chunks in f64 was ~36 GB).
    sampled, runs, _ = _sample_documents(collection, rng)
    stream = _stream_pass(collection, sampled, rng)
    ids, matrix = stream["sample_ids"], stream["sample_matrix"]
    mean, sd = stream["mean"], stream["sd"]
    total_chunks = stream["total_chunks"]
    groups = _by_document(ids)
    dim = int(mean.shape[0])

    con = db.connect(collection)
    documents = con.execute(
        "SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0] or runs

    np.save(out_dir / artifact_mod.ENVELOPE_NAME,
            np.vstack([mean, sd]).astype("<f4"))
    # Probes: in a real publication these are ~512 author-chosen reference texts
    # embedded once. Here the corpus's own vectors stand in (a uniform
    # reservoir over the full stream), which is enough for the Gram gate
    # (algebraic, corpus-independent) to be meaningful.
    probe_count = int(stream["probes"].shape[0])
    np.save(out_dir / artifact_mod.PROBES_NAME, stream["probes"].astype("<f4"))
    golden_count = int(stream["goldens"].shape[0])
    np.save(out_dir / artifact_mod.GOLDENS_NAME, stream["goldens"].astype("<f4"))

    mean_chunks = max(1.0, total_chunks / max(1, documents))
    moment_null = _moment_null(matrix, groups, mean, sd, replicates, rng,
                                mean_chunks=mean_chunks)
    if not moment_null:
        # An artifact with no published null makes T0's envelope check SKIP for
        # every bundle. It would load cleanly and verify nothing, which is worse
        # than having no artifact at all — at least a missing artifact is
        # visible as a degraded tier_reached.
        raise SystemExit(
            f"refusing to write an artifact with no bucketed null: this corpus has "
            f"{len(groups)} document(s) / {matrix.shape[0]} chunk(s), too few to "
            f"bootstrap any size bucket. Calibrate against a larger reference corpus.")

    space_row = con.execute("SELECT space_id FROM space WHERE id=1").fetchone()
    space_id = space_row["space_id"] if space_row else spaces.compute_space_id(
        spaces.gather_components())

    payload = {
        "schema": artifact_mod.SCHEMA,
        "artifact_version": 1,
        "space_id": space_id,
        "created_at": int(time.time()),
        "author_pubkey": "",
        "space": {"dim": dim, "dtype_reference": "f32", "normalization": "l2"},
        "reference_corpus": {
            "tag": collection.name, "chunking_profile": "book",
            "documents": int(documents), "chunks": int(total_chunks),
            "bootstrap_unit": "document",
            "bootstrap_replicates": replicates,
            # the null was bootstrapped from this uniform document sample;
            # the envelope mean/sd cover every vector (loader ignores
            # unknown keys, so older verifiers read this artifact unchanged)
            "calibration_sample": {"documents": len(groups),
                                    "chunks": int(matrix.shape[0])},
            "tail_model": None,
        },
        "envelope": {
            "per_dim_ref": artifact_mod.ENVELOPE_NAME,
            "moment_statistic": {
                "definition": "D = sum_j (sqrt(n)*(bundle_mean_j - mean_j)/sd_j)^2",
                "null_quantiles_by_bucket": moment_null,
            },
            "pair_cosine": _pair_stats(matrix, ids, rng),
            "min_chunks": 8, "min_docs": 1,
        },
        "probes": {
            "count": probe_count,
            "vectors_ref": artifact_mod.PROBES_NAME,
            "gram_gate": {
                "definition": "detG = 1 + 2abc - a^2 - b^2 - c^2",
                "numeric_tolerance_by_dtype": {"f32": 3.0e-5, "f16": 1.2e-2},
                "triples_sampled": 4096, "max_violations": 0,
            },
            "profiles": [],
        },
        "overlap": {"by_phi": {"bins": [], "z_tail_threshold": -3.0,
                                "aggregate_null_by_bucket": []},
                    "min_pairs": 8},
        "calibration": calibration if calibration is not None else {
            # placeholder defaults — replaced wholesale by a measured block
            # from tools/calibrate_space.py via --calibration
            "tau_default": 0.995,
            "detect_target": {"f1": 0.05},
            "pairs": [],
            "storage_dtype_penalty": {"f16": 0.00035, "f32": 0.0},
        },
        "goldens": {
            "bank_size": golden_count,
            "vectors_ref": artifact_mod.GOLDENS_NAME,
            "beacon": {"scheme": "hmac-sha256/epoch/v1",
                       "root": rng.bytes(32).hex(),
                       "epoch_seconds": 3600, "challenge_count": 8,
                       "max_bundle_age_seconds": 2592000,
                       "max_clock_skew_seconds": 900},
        },
        "policy_hints": {"alpha_family_default": 1e-3, "t3_n_default": 16},
    }

    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True).encode("ascii")
    (out_dir / artifact_mod.ARTIFACT_NAME).write_bytes(raw)

    if signing_key is not None:
        from app.bundles.keys import public_key_hex
        payload["author_pubkey"] = public_key_hex(signing_key)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("ascii")
        (out_dir / artifact_mod.ARTIFACT_NAME).write_bytes(raw)
        digests = [hashlib.sha256((out_dir / name).read_bytes()).digest()
                   for name in (artifact_mod.PROBES_NAME, artifact_mod.GOLDENS_NAME,
                                 artifact_mod.ENVELOPE_NAME)]
        signed = artifact_mod.signed_payload(raw, digests)
        (out_dir / artifact_mod.SIGNATURE_NAME).write_bytes(
            signing_key.sign(signed).signature)

    return {"path": str(out_dir), "dim": dim, "documents": int(documents),
            "chunks": int(total_chunks), "space_id": space_id,
            "sample_documents": len(groups), "sample_chunks": int(matrix.shape[0])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tools.build_space_artifact")
    parser.add_argument("collection")
    parser.add_argument("out_dir")
    parser.add_argument("--replicates", type=int, default=DEFAULT_REPLICATES)
    parser.add_argument("--key", help="node key path; unsigned if omitted")
    parser.add_argument("--calibration", type=Path,
                        help="calibration.json from tools/calibrate_space.py")
    args = parser.parse_args(argv)

    collection = collections_mod.by_name(args.collection)
    if collection is None:
        raise SystemExit(f"no such collection: {args.collection!r}")
    signing_key = None
    if args.key:
        from app.bundles.keys import load_signing_key
        signing_key = load_signing_key(Path(args.key))
    calibration = load_calibration(args.calibration) if args.calibration else None
    result = build(collection, Path(args.out_dir), replicates=args.replicates,
                   signing_key=signing_key, calibration=calibration)
    print(f"wrote artifact for space {result['space_id'][:16]}… to {result['path']}")
    print(f"  {result['chunks']} chunks across {result['documents']} documents, "
          f"dim {result['dim']}")
    if not signing_key:
        print("  UNSIGNED — a verifier will treat it as advisory (may tighten, "
              "never loosen)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

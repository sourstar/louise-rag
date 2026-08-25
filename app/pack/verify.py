"""`pack verify` — pack-format.md §15: the DETERMINISTIC local checks, run
by platform code from the pack's `validation.json` plan. Nothing in a
pack is executed; every check is a function here.

    roots     every shard's bytes hash to the manifest's sha256
    records   every record's content_hash is the hash of its own text
              (a sampled or exhaustive read of the shards)
    chunks    re-chunking each record with its layer's chunker reproduces
              the record's chunk count (ids are derivable from uid+layer)
    license   the terms text in the pack hashes to the manifest's
              terms_sha256 (byte-exact, invariant 9)
    signature the manifest's signature verifies against its publisher

A report is a list of {check, ok, detail, sampled}; the CLI prints it and
a verifier turns a passed report into an attestation (§16 — not built).
"""
import hashlib
import json
import random
from pathlib import Path

from .. import chunking
from . import manifest as manifest_mod, records

ALL_CHECKS = ("roots", "records", "chunks", "license", "signature")


def load_plan(pack_dir: Path) -> dict:
    p = Path(pack_dir) / "validation.json"
    if p.is_file():
        try:
            plan = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(plan, dict):
                return plan
        except ValueError:
            pass
    return {"checks": list(ALL_CHECKS), "sample": 0.05}


def _chunker_of(manifest: dict, layer: int):
    layers = manifest.get("layers") or []
    if not layers:
        return None
    spec = layers[min(max(layer, 0), len(layers) - 1)]["chunker"]
    fn = chunking._CHUNKERS.get(spec["id"])
    version = int(spec.get("version", 1))
    if fn is None:
        return None
    return lambda uid, nid, sections: fn(uid, nid, sections, version)


def _sample(items: list, fraction: float, seed: int, minimum: int = 1) -> list:
    if fraction >= 1.0 or not items:
        return list(items)
    rng = random.Random(seed)
    n = max(minimum, int(round(len(items) * fraction)))
    return rng.sample(items, min(n, len(items)))


def run(pack_dir: Path, *, checks=None, sample: float | None = None, seed: int = 0,
        log=None) -> list[dict]:
    pack_dir = Path(pack_dir)
    manifest = manifest_mod.load(pack_dir)
    plan = load_plan(pack_dir)
    checks = tuple(checks or plan.get("checks") or ALL_CHECKS)
    fraction = float(sample if sample is not None else plan.get("sample", 0.05))
    report: list[dict] = []
    shards = manifest.get("shards") or []

    if "signature" in checks:
        try:
            signed = manifest_mod.verify(manifest)
            report.append({"check": "signature", "ok": True,
                           "detail": f"verified against publisher {manifest.get('publisher')[:16]}…"
                           if signed else "unsigned manifest", "sampled": None})
        except manifest_mod.ManifestError as e:
            report.append({"check": "signature", "ok": False, "detail": str(e), "sampled": None})

    if "roots" in checks:
        bad = []
        for s in shards:
            p = pack_dir / s["path"]
            if not p.is_file():
                bad.append(f"{s['path']}: missing")
                continue
            if records.sha256_of(p) != s["sha256"]:
                bad.append(f"{s['path']}: sha256 mismatch")
        report.append({"check": "roots", "ok": not bad,
                       "detail": "; ".join(bad[:5]) if bad else f"{len(shards)} shard(s) match",
                       "sampled": len(shards)})

    if "records" in checks or "chunks" in checks:
        picked = _sample(shards, fraction, seed)
        bad_records: list[str] = []
        bad_chunks: list[str] = []
        n_records = 0
        for s in picked:
            p = pack_dir / s["path"]
            if not p.is_file():
                bad_records.append(f"{s['path']}: missing")
                continue
            for rec in records.iter_records(p):
                n_records += 1
                sections = [[a, b] for a, b in rec.get("sections") or []]
                if "records" in checks:
                    if records.content_hash(rec.get("title") or "", sections) != rec.get("content_hash"):
                        bad_records.append(f"{rec['uid']}: content_hash")
                if "chunks" in checks:
                    chunker = _chunker_of(manifest, int(rec.get("layer") or 0))
                    if chunker is None:
                        bad_chunks.append(f"{rec['uid']}: unknown chunker for layer {rec.get('layer')}")
                        continue
                    try:
                        n = sum(1 for _ in chunker(rec["uid"], int(rec["native_id"]),
                                                    [(a, b) for a, b in sections]))
                    except Exception as e:   # noqa: BLE001
                        bad_chunks.append(f"{rec['uid']}: {type(e).__name__}")
                        continue
                    if n != int(rec.get("chunks") or 0):
                        bad_chunks.append(f"{rec['uid']}: {n} chunks, record says {rec.get('chunks')}")
        if "records" in checks:
            report.append({"check": "records", "ok": not bad_records,
                           "detail": "; ".join(bad_records[:5]) if bad_records
                           else f"{n_records} record(s) in {len(picked)} shard(s) reproduce their content hash",
                           "sampled": len(picked)})
        if "chunks" in checks:
            report.append({"check": "chunks", "ok": not bad_chunks,
                           "detail": "; ".join(bad_chunks[:5]) if bad_chunks
                           else f"{n_records} record(s) re-chunk to their declared counts",
                           "sampled": len(picked)})

    if "license" in checks:
        want = (manifest.get("license") or {}).get("terms_sha256")
        terms = pack_dir / "TERMS.txt"
        if not want:
            ok, detail = True, "no terms declared (green)"
        elif not terms.is_file():
            ok, detail = False, "TERMS.txt missing from the pack"
        else:
            got = hashlib.sha256(terms.read_bytes()).hexdigest()
            ok = got == want
            detail = "terms text matches the manifest" if ok else "terms text does not match the manifest"
        report.append({"check": "license", "ok": ok, "detail": detail, "sampled": None})

    if log:
        for r in report:
            log(f"[pack verify] {r['check']}: {'ok' if r['ok'] else 'FAIL'} — {r['detail']}")
    return report

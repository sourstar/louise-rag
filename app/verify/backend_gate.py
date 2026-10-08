"""Behavioral backend gate — refuse to WRITE vectors when the live embedding
endpoint stops reproducing its recorded baseline.

Why the space pin is not enough: resolve_embed_model_hash() is WEAK for any
non-Ollama backend (llama-server has no /api/show), so invariant 1's boot
check cannot see a model, build, or pooling swap behind EMBED_URL. Verified
live against the production NAS on 2026-08-16/17: the llama-embed app's
moving image tag changed the serving build repeatedly, and a server-side
defect corrupted 714 books' vectors while every structural check passed.

The gate is the behavioral complement to the pin: on first use it embeds a
fixed set of probe texts and records the vectors (trust on first use);
before the worker embeds anything it re-embeds the probes and requires
cosine >= TAU against the baseline. A changed backend fails closed: the embed stage pauses, rdf/fts/search continue, documents are
never marked errored, and the operator either fixes the endpoint or accepts
the new backend explicitly with `louise hold embedder rebaseline
<collection> --yes`, knowing
that acceptance means the existing vectors belong to a different function.

Distinct from T3: T3 verifies imported vectors against calibrated tau; the
gate verifies the ENDPOINT against its own past self before any local write.
Distinct from boot validation: boot must never couple to endpoint liveness,
so the gate runs lazily in the worker, only when embed work exists.
"""
import hashlib
import json
import secrets
import time
from pathlib import Path

from .. import config, db

# Passing bar for probe reproduction. Observed same-backend jitter across
# restarts and concurrency is <= 1e-4 (cos >= 0.9998); a quant/build change
# lands 0.3-0.9; a different model 0.0-0.7. 0.995 sits far from both shores.
TAU = 0.995
PASS_TTL_S = 1800     # re-check a passing backend at most this often
FAIL_TTL_S = 300      # retry a failing/unreachable backend this often

_SENTENCES = [
    "The lighthouse keeper counted the ships as they crossed the grey horizon.",
    "Photosynthesis converts carbon dioxide and water into glucose using light.",
    "She folded the letter twice and slid it beneath the cracked floorboard.",
    "The committee voted to postpone the irrigation project until spring.",
    "A murmuration of starlings turned above the harbour like a single body.",
    "The ledger showed a deficit that no honest arithmetic could explain.",
]


def _long_probe() -> str:
    """~3800 chars, deterministic — long enough to exercise the long-sequence
    path where batching/pooling defects live."""
    parts = []
    i = 0
    while sum(len(p) for p in parts) < 3800:
        parts.append(f"[{i}] {_SENTENCES[i % len(_SENTENCES)]}")
        i += 1
    return " ".join(parts)


PROBES: dict[str, str] = {
    "short-en": "The quick brown fox jumps over the lazy dog near the riverbank.",
    "long-en": _long_probe(),
    "non-ascii": ("Päivänä kuin äitini oli kylässä, luin minä kirjaa — "
                   "l'été était déjà fini, und der Herbst kam früh. "
                   "Хлеб и соль. 春眠不覚暁。"),
    "typography": ("_Italics_ marked the old way -- with underscores -- and "
                    "“curly quotes” beside straight 'ones', em—dashes, and "
                    "ellipses… all of which tokenizers love to disagree on."),
}


class BackendGateError(RuntimeError):
    """The endpoint no longer reproduces its baseline (or corrupts batches).
    Terminal until the operator intervenes — never marks documents errored."""


class BackendUnavailable(RuntimeError):
    """The endpoint could not be reached — says nothing about its identity."""


def _default_embed_fn(collection=None):
    """The COLLECTION'S recipe, not the node's — a multi-space node gates
    each collection against its own endpoint (doc amendment 9)."""
    from .. import embeddings
    from ..collections import recipe_for
    recipe = recipe_for(collection)
    # the probes are not chunks: fixed texts, as text (`embed_calibration`)
    return lambda texts: embeddings.embed_calibration(recipe, texts)


def baseline_path(collection) -> Path:
    return collection.manifest_path.parent / f"backend_baseline-{collection.name}.json"


def _text_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


def _cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return 0.0 if na == 0 or nb == 0 else num / (na * nb)


def _embed_single(embed_fn, text: str) -> list[float]:
    """One text per call: the client turns this into a single-sequence
    request regardless of EMBED_BATCH, which is the shape verified safe."""
    try:
        return [float(x) for x in embed_fn([text])[0]]
    except Exception as e:
        raise BackendUnavailable(
            f"embed endpoint unreachable or failing ({type(e).__name__})") from e


def record_baseline(collection, embed_fn=None) -> dict:
    from ..collections import recipe_for
    embed_fn = embed_fn or _default_embed_fn(collection)
    recipe = recipe_for(collection)
    probes = {}
    for name, text in PROBES.items():
        vec = _embed_single(embed_fn, text)
        probes[name] = {"text_hash": _text_hash(text),
                        "vector": [round(x, 6) for x in vec]}
    baseline = {
        "version": 1,
        "recorded_at": int(time.time()),
        "embed_url": recipe.url,
        "embed_api": recipe.api,
        "embed_model": recipe.model,
        "dim": len(next(iter(probes.values()))["vector"]),
        "probes": probes,
    }
    path = baseline_path(collection)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(baseline), encoding="utf-8")
    tmp.replace(path)
    return baseline


def _check_batch_integrity(embed_fn) -> None:
    """Only meaningful when config asks for multi-text batches: nonce-fresh
    long texts embedded singly must equal the same texts embedded through the
    configured (batched) path. Caveat, stated honestly: a server-side prompt
    cache can replay the single-leg result for the batched leg and mask a
    real defect — the check is a tripwire, not a proof. The safe setting
    remains EMBED_BATCH=1 (see config.py).
    """
    if config.EMBED_BATCH <= 1:
        return
    nonce = secrets.token_hex(8)
    body = PROBES["long-en"][:2400]
    texts = [f"probe-{nonce}-{i}: {body}" for i in range(min(config.EMBED_BATCH, 4))]
    singles = [_embed_single(embed_fn, t) for t in texts]
    try:
        batched = embed_fn(texts)
    except Exception as e:
        raise BackendUnavailable(
            f"embed endpoint failed on a batched request ({type(e).__name__})") from e
    worst = min(_cos(s, [float(x) for x in b]) for s, b in zip(singles, batched))
    if worst < TAU:
        raise BackendGateError(
            f"backend corrupts multi-text embedding requests (batched vs single "
            f"cos {worst:.4f} < {TAU}) — set EMBED_BATCH=1; this exact defect "
            f"silently corrupted 714 books on 2026-07-20/21")


def check(collection, embed_fn=None) -> dict:
    """Raises BackendGateError (mismatch — terminal until operator action) or
    BackendUnavailable (endpoint down — retry later). Returns a summary dict
    on pass; records the baseline on first use."""
    embed_fn = embed_fn or _default_embed_fn(collection)
    path = baseline_path(collection)
    if not path.exists():
        baseline = record_baseline(collection, embed_fn)
        _check_batch_integrity(embed_fn)
        return {"ok": True, "recorded": True, "dim": baseline["dim"]}

    baseline = json.loads(path.read_text(encoding="utf-8"))
    cosines = {}
    for name, text in PROBES.items():
        entry = baseline["probes"].get(name)
        if entry is None or entry["text_hash"] != _text_hash(text):
            raise BackendGateError(
                f"probe set changed since the baseline was recorded (code "
                f"upgrade) — after confirming the endpoint is the intended one, "
                f"re-record it with `louise hold embedder rebaseline "
                f"{collection.name} --yes`")
        fresh = _embed_single(embed_fn, text)
        if len(fresh) != baseline["dim"]:
            raise BackendGateError(
                f"endpoint dim {len(fresh)} != baseline dim {baseline['dim']} — "
                f"the backend behind the collection's encoder is a different model")
        cosines[name] = _cos(fresh, entry["vector"])
    worst_name = min(cosines, key=cosines.get)
    if cosines[worst_name] < TAU:
        detail = " ".join(f"{k}={v:.4f}" for k, v in cosines.items())
        raise BackendGateError(
            f"embedding backend no longer reproduces its baseline ({detail}; "
            f"tau={TAU}). The model, build, or pooling behind the collection's "
            f"encoder changed. "
            f"Refusing to write vectors — fix the endpoint (pin its image!) or "
            f"accept the new backend with `louise hold embedder rebaseline "
            f"{collection.name} --yes` and re-embed the collection")
    _check_batch_integrity(embed_fn)
    return {"ok": True, "recorded": False, "cosines": cosines}


# in-process verdict cache: collection name -> (allowed, reason, expires_at)
_cache: dict[str, tuple[bool, str, float]] = {}

# The verdict above lives in the WORKER process. The web process has no
# access to it, so a gate pause was indistinguishable from a stalled
# progress bar on the Library card — the operator had no way to learn that
# embedding stopped, or why. The transition is therefore also written to
# the collection's control table: runtime state (like worker_state), never
# desired state, so invariant 10 is respected.
GATE_STATE_KEY = "embed_gate"


def gate_state(con) -> dict | None:
    """Last recorded gate verdict for this collection: {allowed, reason,
    ts}, or None if the gate has not reached a verdict yet (fresh manifest,
    or BACKEND_GATE=off)."""
    raw = db.get_control(con, GATE_STATE_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def embed_allowed(con, collection, embed_fn=None, *, stop=None) -> bool:
    """TTL-cached gate for the worker's embed stage. Logs transitions to the
    event log; never raises.

    A VERSION-3 COLLECTION IS HELD TO A VALIDATED RUN INSTEAD (03 §3.3,
    app/run_check.py): "what keeps later work honest is not a test repeated
    during the work" but the run every answer names, checked before every
    write. So this baseline is not taken for it, and BACKEND_GATE=off does
    not switch that check off — nothing does. `stop` lets a node that is
    stopping leave the calibration gate between two slices."""
    if getattr(collection, "codex", None) is not None:
        from .. import run_check
        if run_check.applies(collection):
            return run_check.embed_allowed(con, collection, stop=stop)
    if config.BACKEND_GATE == "off":
        return True
    now = time.time()
    cached = _cache.get(collection.name)
    if cached and cached[2] > now:
        return cached[0]
    was_allowed = cached[0] if cached else None
    try:
        result = check(collection, embed_fn)
        allowed, ttl = True, PASS_TTL_S
        reason = "baseline recorded" if result.get("recorded") else "baseline reproduced"
    except BackendUnavailable as e:
        allowed, ttl, reason = False, FAIL_TTL_S, str(e)
    except BackendGateError as e:
        allowed, ttl, reason = False, FAIL_TTL_S, str(e)
    _cache[collection.name] = (allowed, reason, now + ttl)
    if allowed != was_allowed:
        level = "info" if allowed else "error"
        db.log_event(con, level, "backend-gate",
                     (f"[{collection.name}] embedding "
                      f"{'enabled' if allowed else 'PAUSED'}: {reason}")[:500])
        try:
            db.set_control(con, GATE_STATE_KEY, json.dumps(
                {"allowed": allowed, "reason": reason[:400], "ts": int(now)}))
        except Exception:   # noqa: BLE001 — a status write must never break
            pass            # the gate; the next transition re-records it
    return allowed


def reset_cache() -> None:
    _cache.clear()

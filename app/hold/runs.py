"""Which runs this peer has validated, and the check that guards a write —
docs/03-checking.md §3.3 (dd9bef4); docs/09-formats.md §9.12.

    Validate once; then every answer says which run gave it.

A pass validates a RUN: one engine as it was actually launched — the image
by its running digest, the weights, the exact arguments and environment, the
device, its driver. The endpoint states that identity with every answer:
in a `Louise-Run` header (09 §9.12), or, for an `api = "vega"` encoder, in
the receipt VeGa's wrapper computes from what LocalAI launched. So
what keeps later work honest is not a test repeated during the work:

  - a vector is written ONLY if the answer that carried it names a run with
    a passing statement for the space — checked on every answer, before the
    write. Nothing unvalidated is ever stored, so stored vectors carry no
    mark of their run;
  - an answer from a run not yet validated is not written — a DEFERRAL,
    never an error on a document — and the gate runs against that run at
    once; if it passes, work resumes under it;
  - an endpoint that states no run identity cannot be held to a validated
    run: a peer may search with it and writes nothing through it.

THIS APPLIES FROM CODEX VERSION 3 (`applies`). A version-2 collection's
statement names an image, not a run (09 §9.12, version 1 of the body), and
every collection the node held when this landed is version 2 or legacy: they
keep the single `state/calibration.json` they have, and nothing here touches
them.

ONE STATEMENT PER RUN AND ROLE, under `state/calibration/` (07 §7.3: "keeps
its signed statement in `state/calibration/`, one per run. A failure is kept
too, so the run is not retried against the same set"). Read through
`embedder.read_statement`, which VERIFIES the envelope: the whole value of
the gate is that it cannot be satisfied by writing a file that says pass.

A SIGNATURE IS NOT ENOUGH: IT MUST BE THIS NODE'S (operator, 2026-09-28).
An envelope verifies under the key written inside it, so a file signed by
any key at all verifies. A statement counts only when its `key` is the
`signer` the caller names — this node's own public key, the one that signs
its statements when a gate passes. Any other is ignored, and named by
`foreign`, so that a validation cannot be planted in `state/` with a key
made for the purpose.

A KEPT STATEMENT IS RE-CHECKED ONCE PER PROCESS (`recheck`): the first time
it is relied on after start, the sixteen texts it says it sampled are
embedded again by the run it names and compared with the reference, as the
gate compared them. A statement that fails is `set_aside` and the run is
gated from scratch. Validations stay valid across restarts; this re-checks
the evidence they carry, it does not repeat the gate.

Like the rest of `app/hold/`, this module does no embedding: the callers
inject it. It does one HTTP GET, to ask the platform what a run stands for.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from . import embedder as emb
from .codex import ENGINE_VERSION, Codex, CodexError, canonical_json, digest

RUN_HEADER = "Louise-Run"
ROLES = ("document", "query")


class RunRefused(CodexError):
    """The endpoint states no run: nothing is written through it (03 §3.3).
    A refusal — exit 2 — and never a deferral: waiting does not change what
    the endpoint is."""


class RunUnvalidated(CodexError):
    """An answer named a run with no passing statement for this space. A
    DEFERRAL: nothing is written, the gate runs against that run, and work
    resumes under it if it passes. `run_id` is the run; `failed` says the
    gate has already run against it and it did not pass."""

    def __init__(self, message: str, run_id: str, *, failed: bool = False) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.failed = failed


def applies(codex: Codex) -> bool:
    return codex.version >= ENGINE_VERSION


# ---- the statements kept ------------------------------------------------------

def directory(root: Path, name: str) -> Path:
    return Path(root) / name / "state" / "calibration"


def path_for(root: Path, name: str, run_id: str, role: str = "document") -> Path:
    if role not in ROLES:
        raise CodexError(f"role is document or query, got {role!r}")
    return directory(root, name) / f"{role}-{run_id.split(':', 1)[-1][:16]}.json"


def _batching_file(root: Path, name: str) -> Path:
    return directory(root, name).parent / "gate-batching.json"


def record_batching(root: Path, name: str, role: str, run_id: str, measured: dict[str, int]) -> None:
    """The batching the gate's `contribute` pass measured `run_id` at — the
    node's EMBED_BATCH, EMBED_CONCURRENCY and the recipe's lanes. LOCAL, not
    in the statement (no wire change): a statement's `batch_invariant` says
    only "as this peer contributed then", and on a node at EMBED_BATCH=1 that
    was one text a request — no evidence for a batch (2026-10-30)."""
    p = _batching_file(root, name)
    try:
        got = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        got = {}
    got[f"{role}:{run_id}"] = {k: int(v) for k, v in measured.items()}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(got, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def batching_measured(root: Path, name: str, role: str, run_id: str) -> dict[str, int] | None:
    try:
        got = json.loads(_batching_file(root, name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return got.get(f"{role}:{run_id}")


def keep(root: Path, name: str, statement: dict[str, Any]) -> Path:
    """Write a statement, pass or fail, where its run and role put it."""
    body = statement["body"]
    if not (Path(root) / name).is_dir():
        raise CodexError(f"{Path(root) / name}: no such collection here (renamed or forgotten while its run "
                         f"was tested); the statement is not kept, and the run is tested again")
    out = path_for(root, name, body["run"], body.get("role", "document"))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_bytes(canonical_json(statement))
    os.replace(tmp, out)
    return out


def requires_exact(codex) -> bool:
    """Does the collection's Codex require bit-exact runs (09 §9.3
    `space.exactness`, EXACTNESS_VERSION)? Absent: the near-exact rule
    (CLAUDE.md invariant 12)."""
    from .codex import EXACT_REQUIRED
    space = getattr(codex, "space", None)
    return getattr(space, "exactness", None) == EXACT_REQUIRED


def passing(body: dict[str, Any], codex: Codex) -> bool:
    """03 §3.3, both conjuncts, from the numbers the statement carries — so
    anyone holding it can recheck the verdict.

    FROM VERSION 5, HOW EXACT THE RUN IS AND HOW IT STANDS AGAINST THE STORED
    CPU REFERENCE ARE PART OF THE VERDICT (docs/notes/near-exact-runs.md):
    an exact run's two single-text passes are byte-equal (`load_variation`
    "1"); a near-exact one measured several loads, varies by no more than
    NEAR_VARIATION, and reaches CPU_NEAR_LINE against a stored CPU reference;
    any run falls below its review line against one and fails
    (`embedder.cpu_review_line`: 0.995, 0.985 for 4-bit weights). Before version
    5 the gate's own-bytes verdict was never in the statement, so a run that
    did not give back its own bytes was admitted to write anyway (found
    2026-10-31 building this)."""
    try:
        ok = (int(body["passed"]) >= codex.space.calibration.min_pass
              and Decimal(body["median"]) >= Decimal(codex.space.tolerance))
        if not ok or int(body.get("version", 1)) < 5:
            return ok
        cpu = body["cpu_reference"]
        line = emb.cpu_review_line(body.get("engine"))      # 4-bit weights: their own line
        if cpu is not None and Decimal(cpu["worst"]) < Decimal(str(line)):
            return False
        variation = Decimal(body["load_variation"])
        if body["exactness"] == emb.EXACT:
            return variation == 1
        if requires_exact(codex):
            # the space requires bit-exact runs (09 §9.3, EXACTNESS_VERSION):
            # a near-exact run, however close, contributes nothing to it
            return False
        return (body["exactness"] == emb.NEAR and int(body["loads"]) >= emb.NEAR_LOADS
                and variation >= Decimal(str(emb.NEAR_VARIATION)) and cpu is not None
                and Decimal(cpu["worst"]) >= Decimal(str(emb.CPU_NEAR_LINE)))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False


#: (directory, role, space) -> (what the directory held, the statements read
#: from it). THE CHECK BEFORE A WRITE IS A DICTIONARY LOOKUP (migration plan,
#: 13e): it runs once per answer, and verifying a signature per answer would
#: be a per-item cost that grows with the statements kept. Keyed on the
#: directory's PATH, never the collection's name (CLAUDE.md), and redone
#: whenever a file in it appears, goes or changes.
_read: dict[tuple[str, str, str], tuple[tuple, dict[str, dict[str, Any]]]] = {}


def _listing(d: Path, role: str) -> tuple:
    try:
        with os.scandir(d) as it:
            return tuple(sorted(
                (e.name, e.stat().st_mtime_ns, e.stat().st_size) for e in it
                if e.name.startswith(f"{role}-") and e.name.endswith(".json")))
    except OSError:
        return ()


#: (directory, role) -> the files the last read IGNORED because another key
#: signed them. Reported by `foreign`; never read as statements.
_foreign: dict[tuple[str, str], tuple[str, ...]] = {}


def statements(root: Path, name: str, codex: Codex, role: str = "document", *,
               signer: str | None) -> dict[str, dict[str, Any]]:
    """run id -> verified body, for THIS space and role, signed by `signer`
    — this node's public key as an envelope spells it. A file that does not
    verify, speaks of another space, or was signed by any other key is not a
    statement this node made. `signer` None (a peer with no key) holds none."""
    d = directory(root, name)
    if signer is None:
        return {}
    held = _listing(d, role)
    key = (str(d), role, codex.space_id, signer)
    cached = _read.get(key)
    if cached is not None and cached[0] == held:
        return cached[1]
    out: dict[str, dict[str, Any]] = {}
    strangers: list[str] = []
    for fname, _, _ in held:
        try:
            body = emb.read_statement(json.loads((d / fname).read_bytes()))
        except Exception:                               # noqa: BLE001
            continue
        if body.get("key") != signer:
            strangers.append(fname)
            continue
        if (body.get("version", 1) >= 2 and body.get("space_id") == codex.space_id
                and body.get("role", "document") == role and body.get("run")):
            out[body["run"]] = body
    _read[key] = (held, out)
    _foreign[(str(d), role)] = tuple(strangers)
    return out


def foreign(root: Path, name: str, role: str = "document") -> tuple[str, ...]:
    """The files of this collection's `state/calibration/` that the last read
    ignored because another key signed them — for the caller to report."""
    return _foreign.get((str(directory(root, name)), role), ())


def statements_for_space(root: Path, name: str, codex: Codex, role: str = "document", *,
                         signer: str | None) -> dict[str, dict[str, Any]]:
    """run id -> statement, for this SPACE. A pass validates a run FOR THE
    SPACE (03 §3.3), so one kept under another collection of the same space
    on this peer counts here — which is what makes the gate run once per
    (space, run) and not once per collection, and what lets every collection
    of a space leave quarantine together (03 §3.6). This collection's own
    come first.

    ONE BORROWED IS ONE KEPT (2026-10-12): a statement found under another
    collection is copied, byte for byte, into this collection's own
    directory. The validation is the space's, but the file was the other
    collection's, and deleting a retired collection of the same space took
    gutenberg's statements with it and sent it back to quarantine."""
    out = dict(statements(root, name, codex, role, signer=signer))
    for other in _others(root, name):
        for run_id, body in statements(root, other, codex, role, signer=signer).items():
            if run_id in out:
                continue
            out[run_id] = body
            src, dst = path_for(root, other, run_id, role), path_for(root, name, run_id, role)
            try:
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dst.with_suffix(".json.tmp")
                    tmp.write_bytes(src.read_bytes())
                    os.replace(tmp, dst)
            except OSError:   # a copy is insurance; the statement still counts from where it is
                pass
    return out


def _others(root: Path, name: str) -> list[str]:
    try:
        return sorted(p.name for p in Path(root).iterdir() if p.name != name
                      and (p / "state" / "calibration").is_dir())
    except OSError:
        return []


# ---- the re-check of a kept statement ----------------------------------------------

def recheck(codex: Codex, body: dict[str, Any], *, reference, embed,
            role: str = "document", items=None) -> tuple[bool, int, float]:
    """(does it still pass, how many reached tolerance, their median): the
    statement's own `sampled` texts embedded again by `embed` and compared
    with `reference`, exactly as the gate compared them (03 §3.3, both
    conjuncts). A statement whose sample is not the space's quorum — a
    malformed or planted one — does not pass. Embedding is the caller's.

    `items`: the space's set as its encoder is fed it (`calibration.items`);
    a pair space's travels in its member and is given, as to the gate."""
    from . import calibration as cal
    if items is None:
        if cal.form(codex) != "text":
            raise emb.GateError("a pair space's calibration set travels in its member "
                                "(09 §9.12); the re-check is given it, and was not")
        items = cal.verify(codex.modality, codex.space.calibration.set)
    texts = list(items)
    if role == "query":
        texts = [cal.query_text(t) for t in texts]
        if codex.space.query.instruction:
            texts = [f"{codex.space.query.instruction}{t}" for t in texts]
    sampled = body.get("sampled")
    if (not isinstance(sampled, list) or len(sampled) != codex.space.calibration.n
            or len(reference) != len(texts)
            or not all(isinstance(i, int) and not isinstance(i, bool)
                       and 0 <= i < len(texts) for i in sampled)):
        return False, 0, 0.0
    got = embed([texts[i] for i in sampled])
    if len(got) != len(sampled) or any(len(v) != codex.space.dim for v in got):
        return False, 0, 0.0
    import statistics
    cosines = [emb.cosine(got[k], reference[i]) for k, i in enumerate(sampled)]
    tolerance = float(codex.space.tolerance)
    passed = sum(1 for c in cosines if c >= tolerance)
    median = statistics.median(cosines)
    ok = (passed >= codex.space.calibration.min_pass
          and Decimal(f"{median:.6f}") >= Decimal(codex.space.tolerance))
    return ok, passed, median


def set_aside(root: Path, name: str, codex: Codex, run_id: str,
              role: str = "document") -> list[Path]:
    """Move every statement this peer keeps for (`run_id`, `role`) in this
    space — under this collection or another of the space — into its
    directory's `set-aside/`. Kept, not deleted: it is the evidence of what
    failed. Returns where each went."""
    moved: list[Path] = []
    for who in [name, *_others(root, name)]:
        src = path_for(root, who, run_id, role)
        if not src.is_file():
            continue
        try:
            body = json.loads(src.read_bytes()).get("body", {})
        except (OSError, ValueError):
            body = {}
        if body.get("space_id") not in (None, codex.space_id):
            continue
        dest_dir = src.parent / "set-aside"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        n = 1
        while dest.exists():
            dest = dest_dir / f"{src.stem}.{n}{src.suffix}"
            n += 1
        os.replace(src, dest)
        moved.append(dest)
    return moved


# ---- what a run stands for ------------------------------------------------------

def base_url(encoder_url: str, fallback: str = "") -> str | None:
    """The platform behind a model's URL: everything before `/upstream/`
    (07 §7.8: `http://<node>:<port>/upstream/<model>`), else `fallback`."""
    head, sep, _ = encoder_url.partition("/upstream/")
    if sep:
        return head.rstrip("/")
    return fallback.rstrip("/") or None


def describe(encoder_url: str, run_id: str, *, get: Callable[..., Any],
             fallback: str = "", timeout: float = 10.0,
             at: str | None = None) -> emb.StatedRun:
    """`GET /louise/runs/<run_id>` → what the identity stands for, VERIFIED:
    09 §9.12 defines `run_id` as `H` of that very object, so a description
    that does not hash to the id it was asked for is not that run's.

    `at`: the description's URL where the platform is not found from the
    encoder's — VeGa's `<VEGA_URL>/vega/runs/<run_id>` (docs/notes/vega.md)."""
    if at is None:
        base = base_url(encoder_url, fallback)
        if base is None:
            raise RunRefused(
                f"{encoder_url} names a run ({run_id[:23]}…) and nothing says where to ask "
                f"what it stands for: the URL is not `…/upstream/<model>` and the encoder "
                f"is not behind VeGa (07 §7.8)")
        at = f"{base}/louise/runs/{run_id}"
    else:
        base = at.rsplit("/", 1)[0]
    r = get(at, timeout=timeout)
    r.raise_for_status()
    obj = r.json()
    if not isinstance(obj, dict) or set(obj) != {"driver", "engine", "weights"}:
        raise RunRefused(f"{base} describes run {run_id[:23]}… as something other than "
                         f"09 §9.12's object {{driver, engine, weights}}")
    if digest(canonical_json(obj)) != run_id:
        raise RunRefused(
            f"{base} describes run {run_id[:23]}… with an object that does not hash to "
            f"it. `run_id` IS the hash of its description (09 §9.12); a platform whose "
            f"two answers disagree states no run that can be held to anything")
    return emb.StatedRun(run_id=run_id, engine=obj["engine"], weights=obj["weights"],
                         driver=obj["driver"])


# ---- the check before a write ------------------------------------------------------

def check(root: Path, name: str, codex: Codex, answered: set, *,
          signer: str | None, role: str = "document", batched: bool = True,
          contributes: dict[str, int] | None = None) -> None:
    """May vectors from answers naming `answered` be written? Silent when
    they may. `answered` is the set of `Louise-Run` values seen on the
    answers that carried them — or, for an `api = "vega"` encoder, the runs
    their receipts named (docs/notes/vega.md); `None` in it is an answer that
    stated none.

    `batched`: the vectors came the way this peer contributes — several
    texts a request, or several requests at once — and not strictly one text
    at a time. A run whose gate found it NOT batch-invariant is validated
    for single-text work only (03 §3.3): the fault that silently corrupted
    714 books was exactly a run that answered differently in a batch."""
    if not applies(codex):
        return
    if not answered or None in answered:
        raise RunRefused(
            f"{name}: the encoder states no run identity (no `{RUN_HEADER}` on its "
            f"answers), so nothing is written through it (03 §3.3). Searching with it "
            f"works. Writing needs an encoder that states the run of every answer: "
            f"`api = \"vega\"`, whose receipts name it (docs/notes/vega.md)")
    # THE COMMON CASE IS ONE LOOKUP: the run is one this collection has a
    # statement for. Only a run it has none for sends the check to the other
    # collections of the space on this peer.
    known = statements(root, name, codex, role, signer=signer)
    for run_id in sorted(answered):
        body = known.get(run_id)
        if body is None:
            known = statements_for_space(root, name, codex, role, signer=signer)
            body = known.get(run_id)
        if body is None:
            raise RunUnvalidated(
                f"{name}: run {run_id[:23]}… answered and this peer has not tested it "
                f"for space {codex.space_id[:23]}…; nothing from it is written until "
                f"the gate has passed against it (03 §3.3)", run_id)
        if not passing(body, codex):
            raise RunUnvalidated(
                f"{name}: run {run_id[:23]}… FAILED the gate for this space "
                f"({body.get('passed')} of the sample, median {body.get('median')}, "
                f"tolerance {codex.space.tolerance}); its vectors are not this space's "
                f"function", run_id, failed=True)
        if batched and body.get("batch_invariant") is False:
            raise RunUnvalidated(
                f"{name}: run {run_id[:23]}… is validated for single-text work only — "
                f"its gate found that a batch does not answer as one text at a time "
                f"does. Embed one text a request (batch 1, concurrency 1), or "
                f"fix the engine's batching and let the gate run again", run_id,
                failed=True)
        if batched and contributes is not None:
            # INVARIANT AT THE BATCHING THIS PEER USES NOW: a verdict measured
            # one text a request (EMBED_BATCH=1, as the node ran until
            # 2026-10-30) is no evidence for a batch. Not a failure: the gate
            # runs again at this setting (`run_check._look`), and only then
            # is a batched answer written
            measured = batching_measured(root, name, role, run_id)
            if measured is None or any(int(measured.get(k, 0)) < int(v) for k, v in contributes.items()):
                raise RunUnvalidated(
                    f"{name}: run {run_id[:23]}… was gated at "
                    f"{measured or 'an unrecorded batching (one text a request)'}; this peer now "
                    f"contributes at {contributes}: nothing batched is written from it until the "
                    f"gate has run at this setting (03 §3.3)", run_id)


class Answers:
    """An embed function that remembers which runs answered its last call.

        embed = Answers(lambda texts, runs: embeddings.embed_calibration(recipe, texts, runs=runs))
        vectors = embed(texts);  runs.check(…, embed.runs)

    `runs` is fresh per call, so a check is about THESE vectors and no others."""

    def __init__(self, fn: Callable[[list[str], set], list[list[float]]]) -> None:
        self._fn = fn
        self.runs: set = set()

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.runs = set()
        return self._fn(texts, self.runs)


def validate(root: Path, name: str, codex: Codex, *, role: str, reference,
             embed, embed_one, contribute, stated: emb.StatedRun, key,
             now: str, items=None,
             on_run: Callable[[emb.Run], None] | None = None,
             cpu_reference=None, reload=None, exactness: str = emb.EXACT) -> dict[str, Any]:
    """Run the gate against one stated run and KEEP the statement — pass or
    fail: "a failure is kept too, so the run is not retried against the same
    set" (07 §7.3). Returns the statement's body. Embedding is the caller's:
    `embed` for the whole set, `embed_one` and `contribute` for the
    fingerprint and batch invariance (embedder.run_gate). `items`: the set as
    the space's encoder is fed it (a pair space's, from its member).
    `on_run` is given the pass itself once its statement is kept — the
    cosines of all 512 texts, which the statement does not carry (the
    operator's cross-vendor reference reads them, approved-encoders.md)."""
    run = emb.run_gate(codex, reference=reference, embed=embed, role=role,
                       stated=stated, embed_one=embed_one, contribute=contribute,
                       items=items, cpu_reference=cpu_reference, reload=reload,
                       exactness=exactness)
    # THE PASS IS ABOUT ONE RUN. If the model was relaunched while the 512
    # texts were going through — another flag, another image — some answers
    # came from another run, and the numbers describe neither. 03 §3.3: "An
    # answer that names another makes the check inconclusive": no statement,
    # and it is tried again.
    for fn in (embed, embed_one, contribute):
        seen = getattr(fn, "runs", None)
        if seen is not None and seen != {stated.run_id}:
            others = sorted(str(r)[:23] for r in seen if r != stated.run_id)
            raise emb.GateDeferred(
                f"{name}: the pass was for run {stated.run_id[:23]}… and answers came "
                f"from {others or 'an endpoint that stated none'} too; it is "
                f"inconclusive, nothing is kept, and it is tried again")
    statement = emb.statement(run, codex, key, now=now)
    keep(root, name, statement)
    if on_run is not None:
        on_run(run)
    return statement["body"]

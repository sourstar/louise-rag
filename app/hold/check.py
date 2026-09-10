"""`hold check` — the validation plan of 03 §3.2, run and attested.

Migration plan step 5. The plan is the Codex's `validation` block: which
checks apply, and how much each sampled one draws. NOTHING EXECUTABLE
TRAVELS INSIDE A COLLECTION — the checks are platform code, open, the
same for everyone and pinned by the Codex's toolchain digest — so a
publisher who wants weaker checks has to publish a different collection.

THREE OUTCOMES, NOT TWO (03 §3.1). `pass`, `fail` and INCONCLUSIVE:
"neither a failure nor a pass — it produces no attestation and no flag,
it is reported with its reason, and it is retried". Every check here can
return the third, and several of them do nothing else yet, because their
prerequisites are later steps. That is reported as what it is rather than
skipped silently: a plan naming `centres` on a peer with no vector index
must say so, not quietly shrink to the checks that happen to be built.

`validation.json` IS CHECKED AGAINST THE CODEX, not read instead of it.
02 §2.6 puts the block in the drag for tools; the Codex is the plan, and
a copy where the two differ is refused before any check runs. Otherwise
the file is a place to publish a weaker plan than the one the identity
covers.

AN ATTESTATION IS THE SIGNED RESULT OF ONE COMPLETED CHECK (09 §9.8),
written per check and never per run — a run that failed one check of six
still proves the other five, and a peer that could only attest whole runs
would publish nothing whenever one check was inconclusive.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from . import codex as cx
from . import drag as drag_mod
from . import envelope as env
from . import records as rec
from . import snapshot as snap
from .codex import CodexError

PASS, FAIL, INCONCLUSIVE = "pass", "fail", "inconclusive"

#: 03 §3.2's default: "the fraction of shards per sampled check … the
#: default is `"0.05"`, always at least one shard".
DEFAULT_FRACTION = "0.05"


class CheckError(CodexError):
    """The plan could not be run — not a verdict about the collection."""


@dataclass(frozen=True)
class Outcome:
    check: str
    result: str
    detail: dict[str, Any] = field(default_factory=dict)
    seed: bytes | None = None
    shards: tuple[str, ...] = ()
    why: str = ""

    @property
    def attestable(self) -> bool:
        """09 §9.8: "A check that could not run — *inconclusive* (03 §3.1)
        — produces no attestation.\""""
        return self.result in (PASS, FAIL)


@dataclass(frozen=True)
class Report:
    collection_id: str
    snapshot_id: str
    snapshot_hash: str
    outcomes: tuple[Outcome, ...]
    attestations: dict[str, str] = field(default_factory=dict)  # check -> id

    @property
    def ok(self) -> bool:
        return not any(o.result == FAIL for o in self.outcomes)

    def outcome(self, check: str) -> Outcome | None:
        return next((o for o in self.outcomes if o.check == check), None)

    def to_json(self) -> dict[str, Any]:
        """09 §9.8's `hold check --json`."""
        return {"collection_id": self.collection_id,
                "snapshot_id": self.snapshot_id,
                "snapshot_hash": self.snapshot_hash,
                "checks": [{"check": o.check, "result": o.result,
                            "detail": o.detail,
                            "attestation": self.attestations.get(o.check)}
                           for o in self.outcomes]}


# --------------------------------------------------------------------------
# the plan

def plan_of(root, name: str, codex) -> tuple[str, ...]:
    """The checks this collection's plan names, `validation.json` agreeing.

    03 §3.2: "`validation.json` in the drag (02 §2.6) is that block
    written out for tools, and `hold check` refuses a copy where the two
    differ". The Codex is the plan; the file is a convenience, and a
    convenience that could disagree with the identity would be a place to
    publish a weaker plan than the one the identity covers.
    """
    want = cx.canonical_json(codex.validation.to_dict())
    p = Path(root) / name / "drag" / drag_mod.VALIDATION_PATH
    if p.is_file():
        got = p.read_bytes()
        if got != want:
            raise CheckError(
                f"{name}: drag/{drag_mod.VALIDATION_PATH} differs from the "
                f"Codex's `validation` block. The Codex is the plan and the "
                f"file is a copy of it for tools (03 §3.2); a copy that may "
                f"differ is a place to publish a weaker plan than the "
                f"identity covers. `hold records validation {name}` rewrites "
                f"it")
    unknown = [c for c in codex.validation.checks if c not in cx.PLAN_CHECKS]
    if unknown:
        raise CheckError(
            f"{name}: the plan names {', '.join(unknown)}, which is no row of "
            f"03 §3.2's table. A plan may name only those, and a collection "
            f"whose plan names another is refused at create and at check "
            f"alike")
    return tuple(codex.validation.checks)


def fraction_for(codex, check: str, override: str | None = None) -> str:
    """The plan's shard fraction for a sampled check, raised by `--sample`.

    07 §7.3: "`--sample F` raises the plan's sample for this run and
    cannot lower it".
    """
    from decimal import Decimal
    s = codex.validation.sample or {}
    entry = s.get(check)
    if not isinstance(entry, dict) and check == "vectors-sample":
        entry = s.get("chunks")            # the recipe's spelling (09 §9.3)
    want = Decimal(str(entry.get("shards"))) if isinstance(entry, dict) \
        and entry.get("shards") else Decimal(DEFAULT_FRACTION)
    if override is not None:
        want = max(want, Decimal(str(override)))
    return str(want)


def sampled_shards(codex, snapshot: snap.Snapshot, check: str, *, seed: bytes,
                   override: str | None = None) -> list[snap.Shard]:
    names = {f"{s.slice}/{s.n:05d}": s for s in snapshot.shards}
    if not names:
        return []
    n = cx.sample_size(fraction_for(codex, check, override), len(names))
    return [names[k] for k in cx.sample(seed, n, iter(sorted(names)))]


# --------------------------------------------------------------------------
# the context every check gets

@dataclass(frozen=True)
class Ctx:
    root: Path
    name: str
    codex: Any
    snapshot: snap.Snapshot
    seed: bytes
    override: str | None = None
    embed: Callable[[list[str]], list[list[float]]] | None = None
    vectors_for: Callable[[list[str]], dict[str, list]] | None = None
    reference: Any = None
    origin_fetch: Callable[[str], bytes] | None = None

    @property
    def drag(self) -> Path:
        return self.root / self.name / "drag"

    def records_of(self, shard: snap.Shard) -> list[dict]:
        from app.pack import records as pack_records
        p = self.drag / shard.path
        if not p.is_file():
            raise FileNotFoundError(str(p))
        return list(pack_records.iter_records(p))


def _shard_ids(codex, shards: Sequence[snap.Shard], space: str | None = None
               ) -> tuple[str, ...]:
    """09 §9.8's `shards`: `"<collection_id>/<slice>/<n>"` for a record
    check and `"<collection_id>/<space>/<slice>/<n>"` for a vector one."""
    mid = f"/{space}" if space else ""
    return tuple(sorted(f"{codex.collection_id}{mid}/{s.slice}/{s.n:05d}"
                        for s in shards))


# --------------------------------------------------------------------------
# the checks

def check_roots(ctx: Ctx) -> Outcome:
    """"every shard's bytes have the `root` the snapshot lists" (03 §3.2).

    Exhaustive and deterministic: no sample, and `seed` is null in the
    attestation (09 §9.8). A shard this peer does not hold is not a
    failure — it is a shard this peer does not hold — so it is counted
    and the check stays honest about what it covered.
    """
    failed, held = [], []
    for s in ctx.snapshot.shards:
        p = ctx.drag / s.path
        if not p.is_file():
            continue
        held.append(s)
        if drag_mod.pieces_root_of(p) != s.root:
            failed.append(f"{s.slice}/{s.n:05d}")
    if not held:
        return Outcome("roots", INCONCLUSIVE,
                       why="this peer holds none of the snapshot's shards")
    return Outcome("roots", FAIL if failed else PASS, {"failed": sorted(failed)},
                   None, _shard_ids(ctx.codex, held),
                   why=f"{len(held)} shard(s) held, {len(failed)} whose bytes "
                       f"are not the snapshot's root")


def check_records(ctx: Ctx) -> Outcome:
    """"every sampled record's `content_hash` recomputes from its own title
    and sections and it carries an `origin_hash`; each sampled shard's
    `records_root` recomputes from its records' `content_hash` values in
    order" (03 §3.2)."""
    shards = sampled_shards(ctx.codex, ctx.snapshot, "records", seed=ctx.seed,
                            override=ctx.override)
    seen, failed, ran = 0, [], []
    for s in shards:
        try:
            records = ctx.records_of(s)
        except FileNotFoundError:
            continue
        ran.append(s)
        for r in records:
            seen += 1
            sections = [tuple(x) for x in r.get("sections") or []]
            if rec.content_hash(r.get("title") or "", sections) != r["content_hash"]:
                failed.append(r["uid"])
            elif not r.get("origin_hash"):
                # invariant 11: a record with no `origin_hash` is
                # permanently unverifiable against its source, which is the
                # worst artefact we could ship.
                failed.append(r["uid"])
        root = rec.records_root(r["content_hash"] for r in records)
        if root != s.records_root:
            failed.append(f"{s.slice}/{s.n:05d}:records_root")
    if not ran:
        return Outcome("records", INCONCLUSIVE,
                       why="this peer holds none of the sampled shards")
    return Outcome("records", FAIL if failed else PASS,
                   {"records": seen, "failed": sorted(failed)},
                   ctx.seed, _shard_ids(ctx.codex, ran),
                   why=f"{seen} record(s) over {len(ran)} sampled shard(s), "
                       f"{len(failed)} failing")


def check_chunks(ctx: Ctx) -> Outcome:
    """"recomputing chunks from the records reproduces each sampled shard's
    `chunks_root` and its `chunks` count … deriving each document's layer
    from the Codex's rules, never reading it from the record" (03 §3.2).

    THIS IS THE CHECK `chunks_root` EXISTS FOR. A count alone cannot see
    a chunker that moved its boundaries without changing how many pieces
    it cut — the vectors would then be of different text under the same
    ids, which is exactly the drift nothing else notices.
    """
    from .. import layers as layers_mod
    shards = sampled_shards(ctx.codex, ctx.snapshot, "chunks", seed=ctx.seed,
                            override=ctx.override)
    seen, failed, ran = 0, [], []
    for s in shards:
        try:
            records = ctx.records_of(s)
        except FileNotFoundError:
            continue
        ran.append(s)
        pairs: list[tuple[str, str]] = []
        for r in records:
            seen += 1
            row = {"uid": r["uid"], "native_id": r.get("native_id", 0)}
            sections = [tuple(x) for x in r.get("sections") or []]
            pairs.extend((c["id"], c["text"]) for c in
                         layers_mod.chunks_for_document(ctx.codex, row, sections))
        name = f"{s.slice}/{s.n:05d}"
        if len(pairs) != s.chunks:
            failed.append(f"{name}:chunks={len(pairs)} not {s.chunks}")
        if rec.chunks_root(pairs) != s.chunks_root:
            failed.append(f"{name}:chunks_root")
    if not ran:
        return Outcome("chunks", INCONCLUSIVE,
                       why="this peer holds none of the sampled shards")
    return Outcome("chunks", FAIL if failed else PASS,
                   {"records": seen, "failed": sorted(failed)},
                   ctx.seed, _shard_ids(ctx.codex, ran),
                   why=f"{seen} record(s) re-cut over {len(ran)} sampled "
                       f"shard(s), {len(failed)} failing")


def check_license(ctx: Ctx) -> Outcome:
    """"the terms member `LICENSE-TERMS` hashes to the Codex's
    `license.terms_sha256` (09 §9.13); absent for a green collection\"."""
    want = getattr(ctx.codex.license, "terms_sha256", None)
    p = ctx.drag / "LICENSE-TERMS"
    if not want:
        if p.is_file():
            return Outcome("license", FAIL, {},
                           why="the Codex records no terms hash and the drag "
                               "carries a LICENSE-TERMS: one of the two is "
                               "wrong, and a terms text nothing pins is a "
                               "terms text anyone may replace")
        return Outcome("license", PASS, {},
                       why="green: the Codex records no terms text")
    if not p.is_file():
        return Outcome("license", FAIL, {},
                       why=f"the Codex pins terms {want[:16]}… and the drag "
                           f"carries no LICENSE-TERMS")
    got = hashlib.sha256(p.read_bytes()).hexdigest()
    if got != want:
        return Outcome("license", FAIL, {},
                       why=f"LICENSE-TERMS hashes to {got[:16]}… and the "
                           f"Codex pins {want[:16]}…. Terms are byte-exact "
                           f"and never converted — a CRLF checkout would "
                           f"silently invalidate every recorded ack")
    return Outcome("license", PASS, {}, why="the terms are the Codex's")


def check_signature(ctx: Ctx) -> Outcome:
    """"the snapshot's sidecar carries a valid signature by the key that
    claims it" (03 §3.2, 09 §9.4).

    The sidecar is the one signed object in the design that is NOT §9.2's
    envelope: it is a set of `{key, time, sig}` that two peers merge by
    union, each `sig` over the domain and the snapshot's 32 raw hash
    bytes. A body signed whole could not gain a signature without
    invalidating the ones already there.
    """
    import json
    p = ctx.drag / drag_mod.snapshot_sig_path(ctx.snapshot.snapshot_id)
    if not p.is_file():
        return Outcome("signature", INCONCLUSIVE,
                       why=f"no sidecar at {p.name}: this copy carries the "
                           f"snapshot unsigned, which 09 §9.4 allows — such a "
                           f"snapshot is held and not adopted")
    try:
        doc = json.loads(p.read_bytes())
    except Exception as e:                              # noqa: BLE001
        return Outcome("signature", FAIL, {"keys": []},
                       why=f"the sidecar is not JSON: {e}")
    keys, bad = snap.sidecar_keys(doc, ctx.snapshot.hash)
    return Outcome("signature", FAIL if bad or not keys else PASS,
                   {"keys": sorted(keys)},
                   why="; ".join(bad) if bad else
                       f"{len(keys)} signature(s) over this snapshot's hash")


def check_calibration(ctx: Ctx) -> Outcome:
    """The gate of 03 §3.3, run for this collection's space."""
    from . import embedder as emb
    if ctx.embed is None:
        return Outcome("calibration", INCONCLUSIVE,
                       why="no compliant encoder reachable on this peer")
    if ctx.reference is None:
        return Outcome("calibration", INCONCLUSIVE,
                       why="this peer holds no calibration member (09 §9.12), "
                           "so there is nothing to compare against")
    try:
        run = emb.run_gate(ctx.codex, reference=ctx.reference, embed=ctx.embed,
                           seed=ctx.seed)
    except emb.GateDeferred as e:
        return Outcome("calibration", INCONCLUSIVE, why=str(e))
    return Outcome("calibration", PASS if run.ok else FAIL,
                   {"n": len(run.sampled), "passed": run.passed,
                    "median": f"{run.median:.6f}"},
                   ctx.seed, (), why=run.why())


def check_vectors_sample(ctx: Ctx) -> Outcome:
    """Step 4h's check, under the plan (03 §3.2)."""
    from . import land as landing
    from . import vectors_check as vck
    if ctx.vectors_for is None:
        return Outcome("vectors-sample", INCONCLUSIVE,
                       why="no vector store on this peer")
    cut: dict[str, dict] = {}

    def ids_for(entry):
        cut.clear()
        cut.update(landing.cut(ctx.codex, landing.records_for(
            ctx.root, ctx.name, ctx.snapshot,
            f"{entry.slice}/{entry.n:05d}", None)))
        return sorted(cut)

    r = vck.check(ctx.codex, ctx.snapshot, seed=ctx.seed,
                  chunk_ids_for=ids_for,
                  text_of=lambda cid: (cut.get(cid) or {}).get("text"),
                  vectors_for=ctx.vectors_for, embed=ctx.embed,
                  fraction=ctx.override)
    checked = sum(s.checked for s in r.shards)
    passed = sum(s.passed for s in r.shards)
    medians = [s.median for s in r.shards if s.checked]
    detail = {"chunks": checked, "passed": passed,
              "median": f"{sorted(medians)[len(medians) // 2]:.6f}" if medians
                        else None,
              "failed": list(r.failing), "producers": []}
    space = drag_mod._segment(ctx.codex.space_id)
    return Outcome("vectors-sample", r.verdict, detail, ctx.seed,
                   tuple(sorted(f"{ctx.codex.collection_id}/{space}/{s.shard}"
                                for s in r.shards)),
                   why=r.why)


def check_records_vs_origin(ctx: Ctx) -> Outcome:
    """"a seeded sample of shards, RE-FETCHED FROM THE ORIGIN by the
    locator each record carries and re-parsed with the Codex's toolchain,
    reproduces every record line of the shard byte for byte — the
    decompressed lines in order, not merely the same `records_root`"
    (03 §3.2).

    THIS CHECK CANNOT RUN TODAY, and the reason is a defect rather than a
    missing step: 09 §9.5 makes `origin` a required record field — "the
    locator of this record's own origin bytes" — and
    `app/pack/records.py::RECORD_KEYS` does not carry it, so no record
    this platform has ever written says where its origin unit is. Half of
    invariant 11 is implemented: every record carries `origin_hash`, and
    nothing carries the locator that hash is of.

    The consequence is precisely what invariant 11 exists to prevent. The
    peer that BUILT a shard can still re-parse it, because its manifest
    holds the paths; a peer who RECEIVED that shard cannot, ever, and 03
    §3.4's whole argument — that a stranger's collection can be checked
    against its origin — rests on this check. See
    `docs/notes/records-origin-gap.md`; fixing it changes the record
    bytes, so it is the operator's call and not mine.

    Written now, and not deferred with the rest, so that it reports the
    defect where a verifier meets it rather than in a note nobody reads.
    """
    shards = sampled_shards(ctx.codex, ctx.snapshot, "records-vs-origin",
                            seed=ctx.seed, override=ctx.override)
    ran, seen, missing = [], 0, 0
    for s in shards:
        try:
            records = ctx.records_of(s)
        except FileNotFoundError:
            continue
        ran.append(s)
        for r in records:
            seen += 1
            if not r.get("origin"):
                missing += 1
    if not ran:
        return Outcome("records-vs-origin", INCONCLUSIVE,
                       why="this peer holds none of the sampled shards")
    if missing:
        return Outcome(
            "records-vs-origin", INCONCLUSIVE,
            {"records": seen, "failed": [], "origin": "unlocatable"},
            ctx.seed, _shard_ids(ctx.codex, ran),
            why=f"{missing} of {seen} sampled record(s) carry no `origin` "
                f"locator, which 09 §9.5 requires of every record. There is "
                f"nothing to re-fetch: this platform's record writer omits "
                f"the field (docs/notes/records-origin-gap.md), so the check "
                f"is inconclusive rather than passing on records nobody "
                f"could ever verify against their source")
    if ctx.origin_fetch is None:
        return Outcome("records-vs-origin", INCONCLUSIVE,
                       {"records": seen, "failed": [], "origin": "not given"},
                       ctx.seed, _shard_ids(ctx.codex, ran),
                       why="no origin to re-fetch from; `--origin` names one")
    return Outcome("records-vs-origin", INCONCLUSIVE,
                   {"records": seen, "failed": [], "origin": "reachable"},
                   ctx.seed, _shard_ids(ctx.codex, ran),
                   why="the re-parse is not built; the locators are there")


def _not_yet(check: str, why: str):
    def run(_ctx: Ctx) -> Outcome:
        return Outcome(check, INCONCLUSIVE, why=why)
    return run


#: Every row of 03 §3.2's table, so a plan naming one that is not built
#: gets INCONCLUSIVE with a reason rather than silence. 03 §3.1: an
#: inconclusive check "is reported with its reason, and it is retried".
CHECKS: dict[str, Callable[[Ctx], Outcome]] = {
    "roots": check_roots,
    "records": check_records,
    "chunks": check_chunks,
    "license": check_license,
    "signature": check_signature,
    "calibration": check_calibration,
    "vectors-sample": check_vectors_sample,
    "records-vs-origin": lambda ctx: check_records_vs_origin(ctx),
    "centres": _not_yet(
        "centres", "needs `hold index build --vector` and the centres member "
                   "of 02 §2.4 (migration plan step 5); not built"),
    "labels": _not_yet(
        "labels", "needs the published centres this collection has none of "
                  "(02 §2.4); not built"),
    "figures": _not_yet(
        "figures", "needs the figures containers of 09 §9.5; not built"),
    "anchor": _not_yet("anchor", "a certifier's external anchor (03 §3.7) — "
                                 "migration plan step 10"),
    "origin-seal": _not_yet("origin-seal", "the origin's own seal (03 §3.7) — "
                                           "migration plan step 10"),
    "evaluation": _not_yet(
        "evaluation", "needs the suite `space.evaluation` names; this "
                      "collection declares none" ),
}


# --------------------------------------------------------------------------
# the run

def run(root, name: str, codex, snapshot: snap.Snapshot, *, seed: bytes,
        checks: Sequence[str] | None = None, sample: str | None = None,
        embed=None, vectors_for=None, reference=None, log=None) -> Report:
    """Run the plan and report. Writes nothing — `attest` does that."""
    say = log or (lambda *_a: None)
    planned = plan_of(root, name, codex)
    want = tuple(checks) if checks else planned
    unknown = [c for c in want if c not in CHECKS]
    if unknown:
        raise CheckError(
            f"{', '.join(unknown)}: no such check. 03 §3.2's table is "
            f"{', '.join(sorted(CHECKS))}")
    if checks:
        outside = [c for c in want if c not in planned]
        if outside:
            # Running a check the plan does not name is allowed and is what
            # `hold records check` does; it is said aloud because the
            # attestation it produces is about a check this collection does
            # not require, and a reader has to be able to tell.
            say(f"note: {', '.join(outside)} is not in this collection's plan")

    ctx = Ctx(Path(root), name, codex, snapshot, seed, sample, embed,
              vectors_for, reference)
    outcomes = []
    for c in want:
        o = CHECKS[c](ctx)
        say(f"{o.result.upper():<13} {o.check:<18} {o.why}")
        outcomes.append(o)
    return Report(codex.collection_id, snapshot.snapshot_id, snapshot.hash,
                  tuple(outcomes))


def attestation(outcome: Outcome, codex, snapshot: snap.Snapshot, key, *,
                platform_image: str, now: str) -> dict[str, Any]:
    """09 §9.8's body, signed under `louise/attestation/v1`."""
    statistical = outcome.check in ("vectors-sample", "calibration",
                                    "centres", "evaluation")
    body = {
        "kind": "louise-attestation", "version": 1,
        "collection_id": codex.collection_id,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_hash": snapshot.hash,
        "check": outcome.check,
        "seed": outcome.seed.hex() if outcome.seed else None,
        "shards": list(outcome.shards),
        "result": outcome.result,
        "detail": outcome.detail,
        "platform_image": platform_image,
        "encoder_image": codex.space.encoder.image if statistical else None,
        "encoder_weights": codex.space.encoder.weights if statistical else None,
        "key": env.key_text(key.verify_key),
        "time": now,
    }
    return env.sign(env.ATTESTATION, body, key)


def write_attestations(root, name: str, report: Report, codex,
                       snapshot: snap.Snapshot, key, *, platform_image: str,
                       now: str) -> dict[str, str]:
    """One per COMPLETED check, at 09 §9.8's path.

    Per check and never per run: a run that failed one check of six still
    proves the other five, and a peer that could only attest whole runs
    would publish nothing whenever one check was inconclusive.
    """
    out: dict[str, str] = {}
    d = (Path(root) / name / "state" / "attestations" /
         drag_mod._hex(codex.collection_id))
    for o in report.outcomes:
        if not o.attestable:
            continue
        doc = attestation(o, codex, snapshot, key, platform_image=platform_image,
                          now=now)
        aid = env.body_id(doc["body"])
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{drag_mod._hex(aid)}.json").write_bytes(cx.canonical_json(doc))
        out[o.check] = aid
    return out

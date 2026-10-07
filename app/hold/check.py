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
import json
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

#: What ONE `records-vs-origin` run may read of the origin, in bytes (03 §3.2;
#: operator, 2026-09-20: "10.5 GiB are too much, limit the test to some
#: hundred MB"). The shard sample alone does not bound it: a record shard is
#: cut by the COLLECTION's slices and an origin file by the ORIGIN's, so 5 % of
#: PubMed's shards — 68 of 1,358, sliced by year — reach into about 249 of its
#: baseline files, 10.5 GiB to fetch for every check on every peer. Fixed by
#: the platform and not by the plan, for 09 §9.3's reason: a creator who could
#: set it could set it backwards. A run may RAISE it (`--origin-mib`), never
#: lower it.
ORIGIN_BYTES = 256 << 20


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


def _shards(ctx, check: str, *, every: bool = False) -> list[snap.Shard]:
    """The shards a check runs on: the named ones when a claim is being
    reproduced (`only_shards`), else every shard (`roots`) or the seeded
    sample of the plan."""
    if ctx.only_shards is not None:
        # `slice/NNNNN`, or an attestation's full `<collection_id>/slice/NNNNN`
        want = {"/".join(str(x).split("/")[-2:]) for x in ctx.only_shards}
        return [s for s in ctx.snapshot.shards if f"{s.slice}/{s.n:05d}" in want]
    if every:
        return list(ctx.snapshot.shards)
    return sampled_shards(ctx.codex, ctx.snapshot, check, seed=ctx.seed, override=ctx.override)


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
    #: `(uid, origin locator) -> the record line that origin produces now`.
    #: Injected, like every other outside thing here: the re-parse needs
    #: the plugin and the corpus machinery, and `app/hold/` stays free of
    #: both so a peer that is only checking needs neither.
    reparse: Callable[[str, str], bytes] | None = None
    #: `origin locator -> (the origin unit it points into, that unit's size in
    #: bytes, or None where it cannot be had)`. A unit is what is FETCHED to
    #: answer for a record: a file of the origin. Given by whoever gives
    #: `reparse`; without it nothing can be bounded and every sampled record
    #: is compared, as before 2026-09-20.
    origin_unit: Callable[[str], tuple[str, int | None]] | None = None
    origin_bytes: int = ORIGIN_BYTES
    #: `image-equivalence`: the reference image's re-parse of a sample, as
    #: tools/reparse_lines.py wrote it under the Codex's own image —
    #: `{collection_id, snapshot_hash, image, lines: {uid: [origin, sha256]}}`.
    against: dict | None = None
    #: a received claim reproduced here (03 §3.6): every check runs on these
    #: shards alone — `slice/NNNNN` — in place of its sample
    only_shards: tuple[str, ...] | None = None

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
    for s in _shards(ctx, "roots", every=True):
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
    shards = _shards(ctx, "records")
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
    shards = _shards(ctx, "chunks")
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
    # 09 §9.2's id form, which is what the Codex stores and what
    # `collection.write_terms` compares against — `"sha256:" + hexdigest`.
    # This computed a BARE digest and compared it to the prefixed one, so
    # the check could never pass on any yellow or red collection. It
    # failed pubmed with the two halves of one identical hash side by side
    # in its own message: "hashes to 77d605f1c9080f8b… and the Codex pins
    # sha256:77d605f1c…". Invariant 9's check, unable to succeed.
    got = "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
    if got != want:
        return Outcome("license", FAIL, {},
                       why=f"LICENSE-TERMS hashes to {got[:23]}… and the "
                           f"Codex pins {want[:23]}…. Terms are byte-exact "
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


def check_anchor(ctx: Ctx) -> Outcome:
    """"a certifier's key against its anchor" (03 §3.7; 09 §9.11's
    `{state}`): judged from the document this peer last fetched
    (app/hold/anchor.py) — the check itself fetches nothing; `hold key
    anchor` and the worker's daily refresh do. `mismatch` fails;
    `anchored` and `stale` pass, stale being age and never a mismatch; no
    document yet is inconclusive."""
    from . import anchor as anchor_mod
    if ctx.codex.publication != "certified" or ctx.codex.certifier is None:
        return Outcome("anchor", INCONCLUSIVE, why="an open collection has no anchor to check (02 §2.8)")
    st = anchor_mod.status(ctx.root, ctx.name, ctx.codex)
    if st["state"] == anchor_mod.UNKNOWN:
        return Outcome("anchor", INCONCLUSIVE, why=f"{st['why']}; `hold key anchor` fetches it")
    return Outcome("anchor", FAIL if st["state"] == anchor_mod.MISMATCH else PASS,
                   {"state": st["state"]}, why=st["why"])


def check_origin_seal(ctx: Ctx) -> Outcome:
    """"the origin's own seal on the source file validates, and the record
    was parsed from that file" (03 §3.2, §3.7; 09 §9.15), ARCHIVAL mode:
    every entry of the snapshot's seals member — the signature over the
    digest it commits to, the certificate path at the captured moment
    against the captured chain, revocation and the trusted list this peer
    holds by the hash the entry names — and, over the sampled shards,
    every record's `extra.seal` naming an entry that is there. Live mode
    (re-fetching the sealed file) is owed. Nothing is fetched; a peer
    that holds no seals member, or not the trusted list, is inconclusive."""
    from . import seals as seals_mod
    if ctx.codex.publication != "certified":
        return Outcome("origin-seal", INCONCLUSIVE, why="an open collection carries no seals (09 §9.15)")
    try:
        doc = seals_mod.read(ctx.drag, ctx.snapshot.snapshot_id)
    except seals_mod.SealError as e:
        return Outcome("origin-seal", FAIL, {"mode": "archival", "entries": 0, "failed": []},
                       why=f"the seals member does not read: {e}")
    if doc is None:
        return Outcome("origin-seal", INCONCLUSIVE,
                       why=f"no seals member held for snapshot {ctx.snapshot.snapshot_id}; `share fetch "
                           f"--kinds seals` brings it, and a peer holding neither the sealed file nor the "
                           f"entry reports inconclusive (03 §3.7)")
    entries = {e["file_hash"]: e for e in doc["entries"]}
    failed: list[str] = []
    why: list[str] = []
    anchors_by_tl: dict[str, list[bytes] | None] = {}
    for fh, e in entries.items():
        tl = e["trusted_list"]["tl_sha256"]
        if tl not in anchors_by_tl:
            data = seals_mod.held_trusted_list(ctx.root, ctx.name, tl)
            anchors_by_tl[tl] = None if data is None else seals_mod.anchors_from_trusted_list(data)
        anchors = anchors_by_tl[tl]
        if anchors is None:
            return Outcome("origin-seal", INCONCLUSIVE, {"mode": "archival", "entries": len(entries), "failed": []},
                           why=f"the trusted list {tl[:23]}… the entries name is not held under "
                               f"state/{seals_mod.TRUSTED_LISTS}/; archival mode validates against it and "
                               f"nothing else (03 §3.7)")
        got = seals_mod.validate(e, anchors=anchors)
        if not got["ok"]:
            failed.append(fh)
            why.append(f"{fh[:12]}…: {got['why']}")
    # the records parsed from a sealed file name its entry (09 §9.5 extra.seal)
    shards = _shards(ctx, "origin-seal")
    ran: list[snap.Shard] = []
    unplaced = 0
    for s in shards:
        try:
            records = ctx.records_of(s)
        except FileNotFoundError:
            continue
        ran.append(s)
        for r in records:
            named = (r.get("extra") or {}).get("seal")
            if isinstance(named, str) and named not in entries:
                unplaced += 1
                if named not in failed:
                    failed.append(named)
    if unplaced:
        why.append(f"{unplaced} sampled record(s) name a seal entry the member does not carry")
    detail = {"mode": "archival", "entries": len(entries), "failed": sorted(failed)}
    return Outcome("origin-seal", FAIL if failed else PASS, detail, ctx.seed, _shard_ids(ctx.codex, ran),
                   why="; ".join(why) if why else f"{len(entries)} seal(s) verified offline against the captured "
                                                   f"evidence and the trusted list held")


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

    BYTE FOR BYTE, and the emphasis is the specification's. Comparing
    `records_root` would not do it: that root is over the records'
    `content_hash` values, and `content_hash` is over title and sections
    alone (09 §9.5), so every field outside it — `creators`, `date`,
    `subjects`, `keys`, `extra`, `origin` — could differ from what the
    origin says while the root still matched. The line is the unit.

    IT NEEDS AN ORIGIN, and the absence of one is INCONCLUSIVE and never a
    pass: 03 §3.2's `needs` column says "the origin, the platform image",
    and a check that passed for want of an origin would certify the copy
    against itself.

    A record with no `origin` locator is likewise inconclusive rather than
    failing. That was every record this platform wrote before 2026-09-10
    (`docs/notes/records-origin-gap.md`), and such a record is not a bad
    record — it is one nobody can put a question to.
    """
    shards = _shards(ctx, "records-vs-origin")
    ran, seen, missing = [], 0, 0
    lines: list[tuple[str, str, bytes]] = []      # (uid, origin, the line)
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
                continue
            from app.pack import records as pack_records
            # THE CODEX'S SHAPE, on both sides. `canonical_line` defaults to
            # `record_line` 1, which drops the `figures` key — so on a
            # plates collection this re-canonicalised a shape-2 record as
            # shape 1 and compared it against a shape-2 re-parse. Every
            # record differed, and the difference was this line.
            #
            # Caught on a six-book trial over real illustrated epubs before
            # the 48,000-book build: `figures` PASSED, `records-vs-origin`
            # failed 6 of 6, and the re-parse was byte-identical to the
            # shard all along.
            lines.append((r["uid"], r["origin"],
                          pack_records.canonical_line(
                              r, record_line=int(getattr(ctx.codex,
                                                          "record_line", 1) or 1))))
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
                f"nothing to put a question to; records built before "
                f"2026-09-10 are all of them "
                f"(docs/notes/records-origin-gap.md)")
    if ctx.reparse is None:
        return Outcome("records-vs-origin", INCONCLUSIVE,
                       {"records": seen, "failed": [], "origin": "not given"},
                       ctx.seed, _shard_ids(ctx.codex, ran),
                       why="no origin to re-fetch from — `--origin DIR` names "
                           "one. 03 §3.2 needs "
                           "\"the origin, the platform image\", and a check "
                           "that passed without one would certify the copy "
                           "against itself")

    sampled_records = len(lines)
    lines, bound = _within_the_origin_bound(ctx, lines)

    # "DIFFERS FROM ITS ORIGIN" AND "COULD NOT BE RE-PARSED" ARE NOT THE
    # SAME FINDING, and conflating them produces the most damaging kind of
    # wrong answer this check can give. A record that differs is a fidelity
    # failure and names the record; a re-parse that could not run proves
    # nothing at all, and 03 §3.1 has a word for that — inconclusive.
    #
    # Live 2026-09-12: a check run with the origin mounted READ-ONLY
    # reported "2,028,614 record(s) re-parsed from the origin, 1,640,709
    # differing". Not one of them differed. The pubmed plugin writes a
    # parse cache beside the origin, the mount refused it with errno 30,
    # and every OSError went onto the `failed` list. An operator reading
    # that had every reason to believe 1.6M records had drifted from
    # PubMed, and the collection's own records were perfect.
    failed, moved, errored, unreachable = [], [], [], 0
    for uid, locator, line in lines:
        try:
            got = ctx.reparse(uid, locator)
        except FileNotFoundError:
            unreachable += 1
            continue
        except Exception as e:                          # noqa: BLE001
            errored.append(f"{uid}: {type(e).__name__}: {e}"[:200])
            continue
        if got != line:
            if _moved_only(line, got):
                # MOVED, not failed (03 §3.2, amended 2026-10-07): the origin
                # unit changed since the build and the record re-parsed from
                # it is otherwise identical. The mirror rewrote half of a
                # sample of gutenberg's book folders on 2026-10-05 without
                # changing one parsed record, and every one of them failed.
                moved.append(uid)
                continue
            # The FIRST differing record, named — §9.8's detail asks for
            # exactly that, because a list of every difference between two
            # parsers is a diff and not a finding.
            failed.append(uid)
    if errored and not failed:
        return Outcome(
            "records-vs-origin", INCONCLUSIVE,
            {"records": seen, "failed": [], "errored": len(errored),
             "origin": "unreadable"},
            ctx.seed, _shard_ids(ctx.codex, ran),
            why=f"{len(errored)} of {len(lines)} sampled record(s) could not "
                f"be re-parsed from the origin, and none of the rest differed "
                f"— so nothing was proved either way. First: {errored[0]}")
    if unreachable == len(lines):
        return Outcome("records-vs-origin", INCONCLUSIVE,
                       {"records": seen, "failed": [],
                        "origin": "unreachable"},
                       ctx.seed, _shard_ids(ctx.codex, ran),
                       why=f"none of the {len(lines)} sampled record(s) could "
                           f"be re-fetched from the origin given")
    said = ""
    if bound:
        said = (f" — read {bound['origin_units']} of the {bound['origin_units_sampled']} "
                f"origin file(s) the sampled shards reach into, {bound['origin_bytes'] >> 20} "
                f"MiB of the {bound['origin_bytes_bound'] >> 20} MiB one check may read of "
                f"the origin ({len(lines)} of {sampled_records} sampled records)")
    return Outcome("records-vs-origin", FAIL if failed else PASS,
                   {"records": seen, "compared": len(lines),
                    "failed": sorted(failed)[:64], "moved": sorted(moved)[:64],
                    "errored": len(errored), "origin": "reachable", **bound},
                   ctx.seed, _shard_ids(ctx.codex, ran),
                   why=f"{len(lines) - unreachable - len(errored)} record(s) "
                       f"re-parsed from the origin, {len(failed)} differing"
                       + (f", {len(moved)} moved (their origin unit changed since the build, "
                          f"the record re-parsed from it identical but for origin_hash)"
                          if moved else "")
                       + (f", {unreachable} not there" if unreachable else "")
                       # named, never folded into the count above: a record
                       # that could not be read is not a record that agreed
                       + (f", {len(errored)} unreadable ({errored[0]})"
                          if errored else "")
                       + said)


def check_image_equivalence(ctx: Ctx) -> Outcome:
    """03 §3.4, amended 2026-10-07 (operator: "the source compliancy is
    after the parsing of the useless information, that only matters").
    THIS image re-parses the same origin bytes into the same record lines as
    the Codex's reference image did, record by record — a proof about the
    IMAGE that holds whatever the origin has become since the build. Where
    the origin moved in content (gutenberg: texts corrected and catalogue
    entries changed by Project Gutenberg after the build), `records-vs-origin`
    rightly fails under every image, the reference included, and so could
    never show a new one equivalent.

    It is not `records-vs-origin`: it says nothing about whether the records
    still match their origin, so no trust level counts it
    (`coverage.DETERMINISTIC` does not name it). Never planned; run on demand
    with `--against`, beside `chunks`."""
    ref = ctx.against
    if ref is None or ctx.reparse is None:
        return Outcome("image-equivalence", INCONCLUSIVE,
                       why="needs `--origin DIR` and `--against FILE`: the reference image's "
                           "re-parse of a sample (tools/reparse_lines.py run under the Codex's image)")
    if ref.get("collection_id") != ctx.codex.collection_id or ref.get("snapshot_hash") != ctx.snapshot.hash:
        return Outcome("image-equivalence", INCONCLUSIVE,
                       why="the reference lines are about another collection or snapshot")
    reference = ctx.codex.toolchain.platform_image
    if ref.get("image") != reference:
        return Outcome("image-equivalence", INCONCLUSIVE,
                       why=f"the reference lines were made by {str(ref.get('image'))[:19]}…, not by "
                           f"{reference[:19]}…, the image the Codex names")
    lines = ref.get("lines") or {}
    compared, failed, errored = 0, [], []
    for uid in sorted(lines):
        origin, digest = lines[uid]
        try:
            got = ctx.reparse(uid, origin)
        except FileNotFoundError:
            errored.append(f"{uid}: not there")
            continue
        except Exception as e:                          # noqa: BLE001
            errored.append(f"{uid}: {type(e).__name__}: {e}"[:200])
            continue
        compared += 1
        if hashlib.sha256(got).hexdigest() != digest:
            failed.append(uid)
    detail = {"against": "reference-image", "reference_image": reference, "records": len(lines),
              "compared": compared, "failed": sorted(failed)[:64], "errored": len(errored),
              "reference_errored": len(ref.get("errors") or {})}
    shards = tuple(ref.get("shards") or ())
    if failed:
        return Outcome("image-equivalence", FAIL, detail, ctx.seed, shards,
                       why=f"{len(failed)} of {compared} record(s) re-parsed otherwise than the reference "
                           f"image re-parsed them from the same origin bytes; first: {sorted(failed)[0]}")
    if errored or not compared:
        return Outcome("image-equivalence", INCONCLUSIVE, detail, ctx.seed, shards,
                       why=(f"{len(errored)} record(s) could not be re-parsed here — nothing proved either "
                            f"way. First: {errored[0]}" if errored else "the reference holds no lines"))
    return Outcome("image-equivalence", PASS, detail, ctx.seed, shards,
                   why=f"{compared} record(s) re-parsed byte for byte as the reference image "
                       f"{reference[:19]}… re-parsed them from the same origin bytes")


def _moved_only(line: bytes, got: bytes) -> bool:
    """Every field of the two record lines equal but `origin_hash`: the
    origin unit was rewritten and parses to the same record (03 §3.2)."""
    try:
        a, b = json.loads(line), json.loads(got)
    except ValueError:
        return False
    if not isinstance(a, dict) or not isinstance(b, dict) or a.get("origin_hash") == b.get("origin_hash"):
        return False
    a.pop("origin_hash", None)
    b.pop("origin_hash", None)
    return a == b


def _within_the_origin_bound(ctx: Ctx, lines: list) -> tuple[list, dict]:
    """The sampled records this run will answer for, and what it reads to do so.

    The sampled shards' records are grouped by ORIGIN UNIT — the file of the
    origin a record's locator points into. The units are ranked by the ONE
    sampling rule (09 §9.1), under the same seed the shards were drawn with,
    and taken in that order up to the last one that keeps their sizes within
    `ctx.origin_bytes`; the first is always taken, whatever its size. EVERY
    sampled record of a taken unit is compared, byte for byte, as before.

    So the seed still decides everything — a verifier cannot be steered toward
    files a producer prepared, and anyone can recompute from the attestation
    which files it read — and what a check costs is bounded by bytes, not by
    how the collection happens to be sliced against its origin."""
    if ctx.origin_unit is None or not lines:
        return lines, {}
    size_of: dict[str, int | None] = {}
    unit_of: list[str] = []
    for _uid, locator, _line in lines:
        unit, size = ctx.origin_unit(locator)
        size_of.setdefault(unit, size)
        unit_of.append(unit)
    taken: list[str] = []
    used = 0
    for unit in cx.sample(ctx.seed, len(size_of), iter(sorted(size_of))):
        size = size_of[unit] or 0
        if taken and used + size > ctx.origin_bytes:
            break
        taken.append(unit)
        used += size
    keep = set(taken)
    return ([item for item, unit in zip(lines, unit_of) if unit in keep],
            {"origin_units": len(taken), "origin_units_sampled": len(size_of),
             "origin_bytes": used, "origin_bytes_bound": ctx.origin_bytes})


def check_figures(ctx: Ctx) -> Outcome:
    """Every plate the sampled shards' records name and this peer HOLDS
    hashes to the name the record gave it (03 §3.2, 09 §9.8).

    `named - held` IS NOT A FAILURE, and that is the whole shape of this
    check. The figures containers are a member like any other: a peer may
    hold the records of a shard and not its plates — 04 §4.4 lets it ask
    to be spared them, and 51 GB is a reason someone would. What would be
    a failure is a plate whose bytes disagree with the record, because
    that is the one thing the hash on the record line exists to catch.

    A `carried: false` entry names a plate the ORIGIN has and this
    collection does not admit (a media type outside 08 §8.7, a figure over
    a cap). It is not counted as named: there is nothing to hold and never
    will be, and counting it would make `named - held` grow with every
    refused SVG.
    """
    from . import figures as figures_mod

    shards = _shards(ctx, "figures")
    if int(getattr(ctx.codex, "record_line", 1) or 1) != 2:
        return Outcome("figures", INCONCLUSIVE, {"shards": 0, "named": 0,
                                                 "held": 0, "failed": []},
                       why="this collection's `record_line` is 1, so its "
                           "records carry no `figures` key and there are no "
                           "plates to check (09 §9.5)")
    named = held = 0
    failed, ran = [], []
    for s in shards:
        try:
            records = ctx.records_of(s)
        except FileNotFoundError:
            continue
        ran.append(s)
        want: set[str] = set()
        for r in records:
            for f in r.get("figures") or []:
                if f.get("carried") is False:
                    continue
                want.add(figures_mod.bare(f["hash"]))
        named += len(want)
        container = ctx.drag / drag_mod.figures_path(s.slice, s.n)
        if not container.is_file():
            continue
        try:
            stored = set(figures_mod.read_index(container))
            read, bad = figures_mod.verify(container)
        except figures_mod.FigureError as e:
            # the container itself, not one plate in it
            failed.append(f"{s.slice}/{s.n:05d}: {e}")
            continue
        held += read
        failed += sorted(bad)
        # a plate in the container that no record names: the container is
        # the shard's, so this is the two coming apart in the other
        # direction, and it is as much a fault as a missing one
        failed += [f"{h}:unnamed" for h in sorted(stored - want)]
    if not ran:
        return Outcome("figures", INCONCLUSIVE,
                       why="this peer holds none of the sampled shards")
    return Outcome("figures", FAIL if failed else PASS,
                   {"shards": len(ran), "named": named, "held": held,
                    "failed": sorted(failed)[:64]},
                   ctx.seed, _shard_ids(ctx.codex, ran),
                   why=f"{named} plate(s) named over {len(ran)} sampled "
                       f"shard(s), {held} held, {len(failed)} failing")


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
    "image-equivalence": lambda ctx: check_image_equivalence(ctx),
    "centres": _not_yet(
        "centres", "needs `hold index build --vector` and the centres member "
                   "of 02 §2.4 (migration plan step 5); not built"),
    "labels": _not_yet(
        "labels", "needs the published centres this collection has none of "
                  "(02 §2.4); not built"),
    "figures": check_figures,
    "anchor": lambda ctx: check_anchor(ctx),
    "origin-seal": lambda ctx: check_origin_seal(ctx),
    "evaluation": _not_yet(
        "evaluation", "needs the suite `space.evaluation` names; this "
                      "collection declares none" ),
}


# --------------------------------------------------------------------------
# the run

def run(root, name: str, codex, snapshot: snap.Snapshot, *, seed: bytes,
        checks: Sequence[str] | None = None, sample: str | None = None,
        embed=None, vectors_for=None, reference=None, reparse=None,
        origin_unit=None, origin_bytes: int | None = None,
        against: dict | None = None,
        only_shards: Sequence[str] | None = None,
        log=None) -> Report:
    """Run the plan and report. Writes nothing — `attest` does that.

    `origin_bytes` RAISES what `records-vs-origin` may read of the origin for
    this run and cannot lower it — 07 §7.3's rule for `--sample`, for the same
    reason: a peer may always check more than the rule asks, never less."""
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
              vectors_for, reference, reparse, origin_unit,
              max(ORIGIN_BYTES, int(origin_bytes or 0)), against, tuple(only_shards) if only_shards else None)
    outcomes = []
    for c in want:
        try:
            o = CHECKS[c](ctx)
        except CheckError:
            # the plan itself is wrong, which is not one check's verdict
            raise
        except Exception as e:                          # noqa: BLE001
            # A CHECKER THAT CRASHED PROVED NOTHING — so inconclusive, with
            # the reason, which is what 03 §3.1 asks for ("reported with
            # its reason, and it is retried"). Never FAIL: a defect in the
            # checker is not a defect in the collection, and this is the
            # report an operator decides to replace a collection on.
            #
            # And never the whole run: an attestation is per check (09
            # §9.8), so the other eight are independently worth having.
            # One unreadable file denying every result is the same mistake
            # the worker's round loop made with a malformed TOML.
            o = Outcome(c, INCONCLUSIVE, {"error": f"{type(e).__name__}"},
                        why=f"the check itself failed: {type(e).__name__}: "
                            f"{e}"[:400])
        say(f"{o.result.upper():<13} {o.check:<18} {o.why}")
        outcomes.append(o)
    return Report(codex.collection_id, snapshot.snapshot_id, snapshot.hash,
                  tuple(outcomes))


def attestation(outcome: Outcome, codex, snapshot: snap.Snapshot, key, *,
                platform_image: str, now: str) -> dict[str, Any]:
    """09 §9.8's body, signed under `louise/attestation/v1`."""
    # ... and a `forgery` proof names the encoder that recomputed (09 §9.8,
    # amended 2026-10-07 with 03 §3.5: the recomputed hash means nothing
    # without the encoder that made it)
    statistical = outcome.check in ("vectors-sample", "calibration",
                                    "centres", "evaluation", "forgery")
    # 09 §9.8: from the engine amendment the body names the ENGINE used, by
    # its id, and is version 3 — "version 2 is this body with
    # `encoder_image`". A version-2 collection's attestations keep the shape
    # they have always been written in, image and all.
    from .codex import encoder_field
    engine_key, engine = encoder_field(codex)
    body = {
        "kind": "louise-attestation",
        "version": 3 if engine_key == "encoder_engine" else 1,
        "collection_id": codex.collection_id,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_hash": snapshot.hash,
        "check": outcome.check,
        "seed": outcome.seed.hex() if outcome.seed else None,
        "shards": list(outcome.shards),
        "result": outcome.result,
        "detail": outcome.detail,
        "platform_image": platform_image,
        engine_key: engine if statistical else None,
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

"""`hold vectors import` — the admission ladder of 03 §3.5.

Migration plan step 4e. This does NOT wrap `app/verify`: every tier of
that ladder disagrees with §3.5 in substance rather than in wording — its
T3 is a Wald SPRT over 64 chunks against a constant 0.95 where §3.5 draws
sixteen on the Codex's `tolerance`, its T2 is a beacon-and-age protocol
with a golden bank where §3.5 embeds the calibration set's first sixteen
texts, and `app/bundles/trust.py` implemented trust on first use and
refused every producer key after the first, which §3.5 and 08 §8.1 forbid
in as many words (it now reads the peer-wide trust list, 2026-10-08).

WHAT ADMITS A BUNDLE, and what merely describes it. Admissibility is
`space_id` plus per-record `content_hash`. `collection` and `snapshot_id`
are PROVENANCE — they say where the work was done and gate nothing — so a
bundle from a sister, or from a peer who calls the collection something
else, is admitted when the space and the records agree. A peer that
gated on `collection` would refuse exactly the sharing the design exists
for.

THE ORDER MATTERS AND IS NOT AN IMPLEMENTATION DETAIL.

  1. whole-bundle refusals first — a bundle for another space, or of
     another width, is not a bundle with bad rows;
  2. then the ACCEPTED ROW SET is fixed, before any tier sees a matrix:
     the sixteen challenge rows are sliced off (they are T2's answers,
     never chunks), and the rows of any record whose `content_hash`
     differs from this peer's copy are dropped. A producer who could pad
     a bundle with rows it knew would be refused would dilute every
     statistic computed over the chunks by the padding factor;
  3. only then the tiers, each over that fixed set.

A RE-ISSUED RECORD IS NOT A FAILURE. Its rows are refused, the refusal is
recorded, and the import exits 0 with the rest landed. The record was
re-issued since the producer embedded it; nobody did anything wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from . import bundle as bd
from . import calibration as cal
from . import codex as cx
from . import vectors as vec
from .codex import CodexError
from .embedder import GateDeferred

#: 03 §3.5's tiers, in the order they run.
T0, T1, T2, T3 = "T0", "T1", "T2", "T3"

#: Below this many accepted rows the envelope half of T0 is not applied.
#: A sample deviation carries a relative standard error of about
#: 1/sqrt(2n) — 12 % at 32 rows and 41 % at 3 — and against a band of
#: [0.4, 2.0] that noise alone refuses honest bundles. 04 §4.5 sizes a
#: real bundle at a few hundred chunks, so this is the tail.
ENVELOPE_MIN_ROWS = 32
PASS, FAIL, SKIPPED = "pass", "fail", "skipped"


class AdmitError(CodexError):
    """The bundle is refused WHOLE. Not a tier failure — a tier failure is
    a verdict about vectors, and this is a bundle that is not for this
    collection at all."""


@dataclass(frozen=True)
class Tier:
    tier: str
    verdict: str
    why: str
    stats: dict[str, Any] = field(default_factory=dict)
    #: A clause that failed and was OVERRULED by a later tier (T0's
    #: envelope by T2, below): the tier PASSES and this says what it saw.
    warning: str = ""

    @property
    def failed(self) -> bool:
        return self.verdict == FAIL


def envelope_only(t: Tier) -> bool:
    """Did T0 fail on nothing but the envelope? The structural half —
    duplicates, shape, NaN, norm — returns before any statistic is taken
    and carries no `envelope` stat; only the envelope half marks one."""
    return t.tier == T0 and t.failed and t.stats.get("envelope") == "outside"


def overruled(t0: Tier, t2: Tier) -> Tier:
    """T2 is the envelope's tie-breaker (operator, 2026-10-20; 03 §3.5).

    A bundle outside the envelope whose challenge rows pass was made by
    this space's oven on a HOMOGENEOUS part — fifty books of one kind
    cut by one chunker spread narrower than 512 probe texts drawn across
    everything, and their mean leans away from the probes' (the node's
    lang=en/00043, 00219, 00220: p2 0.289–0.392, one cosine 0.4981).
    The literals were measured over whole corpora, never over one part
    (03 §3.5, 2026-09-09), and T0 is "sanity only, never a security
    control". So the verdict is recorded and the bundle is admitted: the
    tier PASSES with the envelope's reasons as its warning, and
    `stats["envelope"]` says so.
    """
    return Tier(T0, PASS,
                f"{t0.stats.get('rows', '?')} rows OUTSIDE the space's envelope "
                f"({t0.why}) — a WARNING, not a refusal: T2's challenge rows "
                f"pass ({t2.why}), so the oven is this space's",
                {**t0.stats, "envelope": "warning"}, warning=t0.why)


@dataclass(frozen=True)
class Report:
    bundle_id: str
    producer: str
    tiers: tuple[Tier, ...]
    admitted: tuple[tuple[str, Sequence[float]], ...]
    refused: tuple[tuple[str, str], ...]        # (uid, why)
    padded: int                                  # rows dropped before the tiers

    @property
    def ok(self) -> bool:
        """Usable once T0, T1 and T2 pass (§3.5). T3 may be SKIPPED — a
        peer with no compliant encoder imports and marks the chunks
        *unverified here* — but never failed."""
        by = {t.tier: t for t in self.tiers}
        if any(t.failed for t in self.tiers):
            return False
        return all(by.get(n) is not None and by[n].verdict == PASS
                   for n in (T0, T1, T2))

    def tier(self, name: str) -> Tier | None:
        return next((t for t in self.tiers if t.tier == name), None)

    @property
    def warnings(self) -> tuple[tuple[str, str], ...]:
        """`(tier, what it saw)` for every clause a later tier overruled —
        the bundle is admitted, and the log must still say this."""
        return tuple((t.tier, t.warning) for t in self.tiers if t.warning)


# --------------------------------------------------------------------------
# 1. the whole-bundle refusals

def check_admissible(codex, header: dict, *, credits_admit=None) -> None:
    """Refuse a bundle that is not for this collection's space at all.

    Everything here is a property of the HEADER, so it costs nothing and
    runs before a single vector is looked at.
    """
    if header["space_id"] != codex.space_id:
        raise AdmitError(
            f"the bundle is for space {header['space_id'][:23]}… and this "
            f"collection is {codex.space_id[:23]}…. No vector crosses a space "
            f"boundary (02 §2.4); this is not a bad bundle, it is another "
            f"space's")
    if header["dim"] != codex.space.dim:
        raise AdmitError(
            f"the bundle's vectors are {header['dim']}-dimensional and the "
            f"space is {codex.space.dim}")
    if header["dtype"] != codex.space.dtype:
        raise AdmitError(
            f"the bundle's dtype is {header['dtype']!r} and the space's is "
            f"{codex.space.dtype!r}")
    if credits_admit is not None and not credits_admit(codex, header["producer_pubkey"]):
        # 03 §3.5's archive refusal: the key resolves through the platform's
        # registry to an archive this collection's `credits` does not name.
        # A key the registry does not know is an ordinary producer's and
        # never reaches here.
        raise AdmitError(
            "the producer key belongs to an archive this collection does not "
            "credit (06 §6.6). A Codex is written once, so a collection "
            "cannot come to stand on work it never declared")


# --------------------------------------------------------------------------
# 2. the accepted row set, fixed before any tier

def accept_rows(header: dict, rows, possession: dict[str, str],
                mine: dict[str, str]):
    """`(challenge, accepted, refused)` — §3.5's row set, decided once.

    `mine` is this peer's `content_hash` for the uids it holds. A uid it
    does NOT hold is not a refusal: it is a record we cannot check, and
    §3.5 admits a bundle on the records it can. `bd.refused_uids` is the
    one place that distinction lives — a hole in the map is malformed and
    refuses the bundle whole (`check_possession_covers`, already run by
    `vectors.read`), a MISMATCH costs one record, and an absence costs
    nothing.
    """
    n = header["challenge"]["n"]
    challenge = vec.challenge_of(rows, n)
    chunks = vec.chunks_of(rows, n)

    bad = set(bd.refused_uids(possession, mine))
    refused = tuple(
        (uid, f"re-issued since it was embedded: this peer holds "
              f"content_hash {mine[uid][:12]}… and the bundle claims "
              f"{possession[uid][:12]}…")
        for uid in sorted(bad))

    # The uid is the chunk id's first field; `row_order_key` parses it and
    # refuses a malformed id on the way, which is the shape §9.5 fixes.
    accepted = [(cid, v) for cid, v in chunks
                if bd.row_order_key(cid)[0].decode("utf-8") not in bad]
    return challenge, accepted, refused


# --------------------------------------------------------------------------
# 3. the tiers

def tier0(codex, accepted, envelope) -> Tier:
    """Algebraic: shape, norm, finiteness, duplicates, then the envelope.

    The cheap structural half first — a NaN or a duplicate is a defect no
    statistic should have to notice — and the envelope last, because it
    is the only part with thresholds and the only part that can be wrong
    about honest work (03 §3.5, measured 2026-09-09).
    """
    import numpy as np

    if not accepted:
        return Tier(T0, FAIL, "no rows left to admit after the refusals")
    ids = [cid for cid, _ in accepted]
    if len(set(ids)) != len(ids):
        dupes = len(ids) - len(set(ids))
        return Tier(T0, FAIL, f"{dupes} duplicate chunk id(s)")

    m = np.asarray([v for _, v in accepted], dtype=np.float64)
    if m.ndim != 2 or m.shape[1] != codex.space.dim:
        return Tier(T0, FAIL,
                    f"the rows are {m.shape} and the space is dim "
                    f"{codex.space.dim}")
    if not np.isfinite(m).all():
        n = int((~np.isfinite(m)).any(axis=1).sum())
        return Tier(T0, FAIL, f"{n} row(s) carry a NaN or an infinity")
    if codex.space.normalise:
        norms = np.linalg.norm(m, axis=1)
        off = int((np.abs(norms - 1.0) > 1e-3).sum())
        if off:
            return Tier(T0, FAIL,
                        f"{off} row(s) are not unit vectors and the space "
                        f"normalises (worst |v| {norms[np.argmax(np.abs(norms - 1))]:.4f})")

    if len(accepted) < ENVELOPE_MIN_ROWS:
        # THE ENVELOPE NEEDS A SAMPLE, and a handful of rows is not one.
        # It compares this bundle's per-dimension standard deviations
        # against the space's, and the relative standard error of a sample
        # deviation is about 1/sqrt(2n): 12 % at 32 rows, but 41 % at 3.
        # Against a band of [0.4, 2.0] that noise alone would refuse honest
        # bundles — the same multiple-comparison family of mistake the band
        # itself was fixed for on 2026-09-09, arriving from the other side.
        # 04 §4.5 sizes a real bundle at a few hundred chunks, so this
        # affects the tail and not the ordinary case, and 03 §3.5 is
        # explicit that a tier which could not run is recorded as not run.
        return Tier(T0, PASS,
                    f"{len(accepted)} rows: structurally sound; the envelope "
                    f"needs at least {ENVELOPE_MIN_ROWS} rows to mean "
                    f"anything and was NOT applied",
                    {"rows": len(accepted), "envelope": "skipped: too few rows"})

    verdict = cal.envelope_verdict(m, envelope)
    stats = {"rows": len(accepted), **{k: verdict[k] for k in
                                       ("cosine", "p_lo", "p_hi",
                                        "ratio_min", "ratio_max")}}
    if not verdict["passed"]:
        # `envelope: outside` is what lets `judge` tell this failure from
        # a structural one: only this half may be overruled by T2.
        return Tier(T0, FAIL, "; ".join(verdict["reasons"]),
                    {**stats, "envelope": "outside"})
    return Tier(T0, PASS,
                f"{len(accepted)} rows inside the space's envelope "
                f"(cosine {verdict['cosine']:.3f})", stats)


def tier1(codex, accepted, reference) -> Tier:
    """Geometric: are these vectors REALISABLE, whatever they mean?

    §3.5 keeps the cosine profile against the calibration vectors as
    EVIDENCE and makes one decision: sampled (vector, probe, probe)
    triples must satisfy the triangle inequality on the angles. Real
    vectors cannot fail it; mis-decoded bytes — a wrong dtype read, a
    truncated row, an endianness flip — do, because the result is not a
    point on the sphere at all.

    The `fixed`-layer overlap band §3.5 also describes is NOT run here:
    it needs a thousand adjacent pairs measured on THIS peer's own text,
    which a peer importing its first bundle does not have.
    """
    import numpy as np

    if reference is None or len(reference) == 0:
        return Tier(T1, SKIPPED, "no reference vectors to profile against")
    m = np.asarray([v for _, v in accepted], dtype=np.float64)
    r = np.asarray(reference, dtype=np.float64)
    m = m / np.clip(np.linalg.norm(m, axis=1, keepdims=True), 1e-12, None)
    r = r / np.clip(np.linalg.norm(r, axis=1, keepdims=True), 1e-12, None)

    # A deterministic spread of the bundle's rows against two probes each.
    take = min(len(m), 64)
    step = max(1, len(m) // take)
    rows = m[::step][:take]
    a, b = r[0], r[min(1, len(r) - 1)]
    if len(r) < 2:
        return Tier(T1, SKIPPED, "one reference vector: no triple to form")

    ang = lambda u, v: float(np.arccos(np.clip(u @ v, -1.0, 1.0)))  # noqa: E731
    ab = ang(a, b)
    worst = 0.0
    for v in rows:
        va, vb = ang(v, a), ang(v, b)
        # The triangle inequality on the sphere, both ways round.
        slack = min(va + vb - ab, va + ab - vb, vb + ab - va)
        worst = min(worst, slack) if worst else slack
        if slack < -1e-6:
            return Tier(T1, FAIL,
                        f"a sampled (vector, probe, probe) triple is not "
                        f"geometrically realisable (slack {slack:.2e}); the "
                        f"rows are not points on the sphere, which is what "
                        f"mis-decoded bytes look like",
                        {"slack": slack})
    return Tier(T1, PASS, f"{len(rows)} triples realisable",
                {"sampled": len(rows), "worst_slack": worst})


def tier2(codex, challenge, reference) -> Tier:
    """The challenge: the producer's own embedding of sixteen KNOWN texts.

    It costs the importer nothing — the answers travel in the bundle and
    the questions are the calibration set this peer already ships — and it
    proves a correct oven rather than each loaf (§3.5). Passes on the
    Codex's calibration quorum, the same numbers `hold embedder check`
    uses, and has no age rule.
    """
    import numpy as np

    if reference is None or len(reference) == 0:
        return Tier(T2, SKIPPED, "this peer holds no calibration member")
    n = len(challenge)
    ref = np.asarray(reference[:n], dtype=np.float64)
    got = np.asarray(challenge, dtype=np.float64)
    if ref.shape != got.shape:
        return Tier(T2, FAIL,
                    f"the challenge is {got.shape} and the reference "
                    f"{ref.shape}")
    ref = ref / np.clip(np.linalg.norm(ref, axis=1, keepdims=True), 1e-12, None)
    got = got / np.clip(np.linalg.norm(got, axis=1, keepdims=True), 1e-12, None)
    cos = np.sum(ref * got, axis=1)

    tol = float(codex.space.tolerance)
    passed = int((cos >= tol).sum())
    median = float(np.median(cos))
    need = codex.space.calibration.min_pass
    stats = {"n": n, "passed": passed, "min_pass": need,
             "median": median, "worst": float(cos.min()), "tolerance": tol}
    if passed < need or median < tol:
        return Tier(T2, FAIL,
                    f"{passed} of {n} challenge texts reached tolerance "
                    f"{tol} (needs {need}) and the median is {median:.4f} — "
                    f"the producer's encoder is not this space's",
                    stats)
    return Tier(T2, PASS,
                f"{passed}/{n} at tolerance {tol}, median {median:.4f}", stats)


def tier3(codex, accepted, *, text_of, embed, seed: bytes) -> Tier:
    """Spot recompute: sixteen chunks, re-embedded HERE.

    The sample is drawn with this peer's PRIVATE randomness and never
    from the bundle — a producer who could predict it would re-roll a
    nonce until the sample missed its corruption (§3.5).

    SKIPPED, never failed, when it cannot run: no encoder, or this peer
    holds none of the sampled chunks' text. §3.5 is explicit that a tier
    which could not run is recorded as not run, and that the bundle is
    imported with its chunks shown as *unverified here*.
    """
    import numpy as np

    if embed is None:
        return Tier(T3, SKIPPED, "no compliant encoder on this peer")
    n = codex.space.calibration.n
    ids = [cid for cid, _ in accepted]
    # 09 §9.1's seeded draw, the same function every sampled check uses —
    # never a second implementation of the rule, because two draws that
    # disagreed would make one peer's attestation uncheckable by another.
    picked = cx.sample(seed, min(n, len(ids)), ids)
    by_id = dict(accepted)

    texts, keep = [], []
    for cid in picked:
        t = text_of(cid)
        if t:
            texts.append(t)
            keep.append(cid)
    if not texts:
        return Tier(T3, SKIPPED,
                    "this peer holds the text of none of the sampled chunks")

    try:
        got = np.asarray(embed(texts), dtype=np.float64)
    except GateDeferred as e:
        # The encoder answered, and not under a run this peer has validated
        # (03 §3.3: such a check is inconclusive). For T3 that is "could not
        # run": SKIPPED, the chunks shown as *unverified here* — never a
        # verdict about the bundle, and never a reason to refuse an import
        # that a peer with no encoder at all is allowed to make.
        return Tier(T3, SKIPPED, f"no compliant encoder on this peer: {e}"[:400])
    mine = got / np.clip(np.linalg.norm(got, axis=1, keepdims=True), 1e-12, None)
    theirs = np.asarray([by_id[c] for c in keep], dtype=np.float64)
    theirs = theirs / np.clip(np.linalg.norm(theirs, axis=1, keepdims=True), 1e-12, None)
    cos = np.sum(mine * theirs, axis=1)

    tol = float(codex.space.tolerance)
    passed = int((cos >= tol).sum())
    median = float(np.median(cos))
    need = codex.space.calibration.min_pass
    stats = {"sampled": len(keep), "passed": passed, "min_pass": need,
             "median": median, "worst": float(cos.min()), "tolerance": tol,
             "seed": seed.hex()}
    if len(keep) < need:
        # Not enough checkable chunks to reach the quorum either way: a
        # verdict of "fail" here would be a verdict about this peer's
        # holdings, not about the bundle.
        return Tier(T3, SKIPPED,
                    f"only {len(keep)} of {len(picked)} sampled chunks have "
                    f"text here; the quorum needs {need}", stats)
    if passed < need or median < tol:
        return Tier(T3, FAIL,
                    f"{passed} of {len(keep)} re-embedded chunks reached "
                    f"tolerance {tol} (needs {need}), median {median:.4f}",
                    stats)
    return Tier(T3, PASS,
                f"{passed}/{len(keep)} at tolerance {tol}, median {median:.4f}",
                stats)


# --------------------------------------------------------------------------
# the ladder

def admit(codex, src, *, envelope, reference=None, mine=None,
          text_of=None, embed=None, seed: bytes = b"", credits_admit=None,
          strict_allow: set[str] | None = None) -> Report:
    """Read a bundle and run §3.5's ladder over it.

    `src` is the bundle DIRECTORY. Everything the ladder needs about this
    peer arrives as a callable, so the ladder itself is a pure function of
    the bundle plus the Codex plus what this peer happens to know.
    """
    return judge(codex, *vec.read(Path(src)), envelope=envelope,
                 reference=reference, mine=mine, text_of=text_of, embed=embed,
                 seed=seed, credits_admit=credits_admit,
                 strict_allow=strict_allow)


def judge(codex, header: dict, rows, possession: dict[str, str], bundle_id: str,
          *, envelope, reference=None, mine=None, text_of=None, embed=None,
          seed: bytes = b"", credits_admit=None,
          strict_allow: set[str] | None = None) -> Report:
    """The ladder over a bundle ALREADY READ by `vectors.read`.

    Separate from `admit` because a real importer must look at the
    verified header before it can say what it knows: which records it
    holds, their `content_hash`, the text behind each chunk id. Reading
    the bundle twice to learn that would mean parsing the parquet twice,
    and peeking at an UNVERIFIED header to avoid it would mean deciding
    what to look up from bytes nobody has vouched for yet.
    """
    check_admissible(codex, header, credits_admit=credits_admit)

    if strict_allow is not None and header["producer_pubkey"] not in strict_allow:
        # `--strict`: an empty allow list admits nothing, which is why it
        # is off by default (migration plan, step 4e).
        raise AdmitError(
            f"--strict: {header['producer_pubkey'][:23]}… is not on this "
            f"peer's allow list")

    before = len(rows) - header["challenge"]["n"]
    challenge, accepted, refused = accept_rows(
        header, rows, possession, dict(mine or {}))
    padded = before - len(accepted)

    t0 = tier0(codex, accepted, envelope)
    tiers = [t0]
    # A STRUCTURAL T0 failure ends the ladder: a NaN or a duplicate is a
    # defect no later tier should have to look past. An ENVELOPE failure
    # does not — T1 and T2 run, and a passing T2 overrules it (above).
    # T2 SKIPPED is not T2 passed: a peer holding no calibration member has
    # no tie-breaker, and the envelope's verdict stands.
    if not t0.failed or envelope_only(t0):
        t1 = tier1(codex, accepted, reference)
        t2 = tier2(codex, challenge, reference)
        if t0.failed and t2.verdict == PASS:
            tiers[0] = overruled(t0, t2)
        tiers.extend((t1, t2))
        if not any(t.failed for t in tiers):
            tiers.append(tier3(codex, accepted, text_of=text_of or (lambda _c: None),
                               embed=embed, seed=seed))

    return Report(bundle_id=bundle_id, producer=header["producer_pubkey"],
                  tiers=tuple(tiers),
                  admitted=tuple(accepted) if not any(t.failed for t in tiers) else (),
                  refused=refused, padded=padded)

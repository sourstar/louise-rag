"""A copy's state on this peer — *refused*, in *quarantine*, or *in use*
(docs/03-checking.md §3.6; docs/07-commands.md §7.8; migration plan, step 14).

    Quarantine: a peer's own tests come before any use.

A trust level says what strangers found. This says what THIS peer has found,
and it comes first: downloading a collection is its operator's decision,
using it waits for the tests.

ONE FUNCTION, called by the worker, the search, `share` and `hold collection
show` alike, so the four cannot disagree. IT IS DERIVED AND WRITTEN NOWHERE
(invariant 10 — a state file would be desired state in the wrong tier, and
one more thing that can disagree with the facts): every answer comes from
what the peer holds —

    its own attestations        state/attestations/<collection>/   (09 §9.8)
    its calibration statements  state/calibration/, state/calibration.json
    the licence acknowledgement app/licensing.py — THE resolution, called
                                here and never re-implemented (invariant 9)
    the origin acknowledgement  state/origin-ack.json
    the refuse-to-start checks  app/collections.py, app/spaces.py

A copy leaves quarantine when, on this peer (03 §3.6):

  1. `roots` and `chunks` have passed;
  2. every vector bundle held passed T0, T1 and T2 — true of every vector
     here by construction: nothing lands without them (app/hold/admit.py:
     `Report.ok`; app/hold/land.py), so this test has no open state;
  3. the gate has RUN against the run its encoder states, and its query
     encoder's in an asymmetric space. A run that FAILED has been tested: it
     withholds the VECTOR LEG and quarantines nothing (02 §2.4: "a peer that
     fails may still search with what it holds");
  4. the licence acknowledgement exists where the class requires one.

THE ORIGIN CHECK IS SHOWN, NEVER WAITED FOR (03 §3.6 amended, operator
2026-11-01: "The check against the source is optional, the gating of the
vectore is not"). Test 5, this peer's own `records-vs-origin`, is computed
and shown — passed, open, cannot run, failed — and its result stays in
`origin_check` and `hold collection show`; it holds nothing in quarantine
and returns nothing to it. CLAUDE.md asks the corpus to be VERIFIABLE
(invariant 11: `origin_hash`, refused at build) and the vectors to be GATED
(invariants 1, 12); a quarantine that withheld embedding behind the origin
check made it a vector gate. An `--origin-unchecked` acknowledgement already
recorded is still read and shown.

It RETURNS when its encoder states a run not yet tested. A `roots` or
`chunks` failure after a pass is one shard's local fault (03 §3.6), never this.

CODEX COLLECTIONS ONLY. A legacy collection carries nothing the tests need,
so it could never leave: it is always in use.

TWO DEPLOYS (`enforced`). The state is always computed and shown;
`platform.toml`'s `[node] quarantine = "enforce"` is what withholds. The day
this lands no Codex collection has a run statement or an origin check of its
own, so enforcing at once would take them all out of search together.

IN USE BEFORE (`in_use_before`; operator, 2026-09-20: "assume that the current
collections have already passed it; the next collection to be created will be
our guinea pig"). The copies a peer already used the day it began to enforce
were made before the tests existed: two of them have no record shards to take
roots of, and no origin check has a fetch step yet. `platform.toml` lists
them BY COLLECTION ID, and for those the operator's decision stands in for
the two tests that cannot run yet — roots-and-chunks and the origin — while
they are OPEN or CANNOT RUN. It stands in for nothing else: never the licence
(invariant 9), never the gate (a run not yet tested still withholds, and the
gate still clears it by itself), never a test that FAILED. Nothing is
pretended: the tests read `in-use-before` with what is still open beside
them, no attestation is written, and `origin_check` stays what it is. Every
copy NOT in the list is tested for real.

COST. One evaluation is a handful of lookups: two directory listings, whose
contents are re-read only when they change, and the licence resolution.
Cached a few seconds on the collection's PATH, never its name (CLAUDE.md).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .hold import shard as shard_mod, embedder as emb, envelope as env, runs as runs_mod
from .hold.codex import Codex, CodexError, canonical_json

REFUSED, QUARANTINE, IN_USE = "refused", "quarantine", "in-use"

# a test's status
PASSED, OPEN, FAILED = "passed", "open", "failed"
CANNOT_RUN, ACKNOWLEDGED, NOT_REQUIRED = "cannot-run", "acknowledged", "not-required"
IN_USE_BEFORE = "in-use-before"      # the operator's decision stands in (see the header)
#: tests shown and never waited for: the origin check (03 §3.6, amended 2026-11-01)
SHOWN_ONLY = ("origin",)

# 09 §9.3's `origin_check`
ORIGIN_PASSED, ORIGIN_PENDING = "passed", "pending"
ORIGIN_ACKED, ORIGIN_FAILED = "acknowledged-unchecked", "failed"
ORIGIN_NOT_PLANNED = "not-planned"   # a never-share collection: no origin to re-fetch (02 §2.9)

ORIGIN_ACK = "origin-ack.json"
ORIGIN_TRIED = "origin-check.json"
TTL_S = 5.0


@dataclass(frozen=True)
class Test:
    name: str           # roots-and-chunks · bundles · gate · licence · origin
    status: str
    detail: str         # what this peer found
    clears: str = ""    # the test or the command that clears it


@dataclass(frozen=True)
class CopyState:
    state: str
    reasons: tuple[str, ...]
    origin_check: str
    vector_leg: bool                 # False: a FAILED run; the text leg goes on
    tests: tuple[Test, ...] = ()
    vector_leg_reason: str = ""

    @property
    def in_use(self) -> bool:
        return self.state == IN_USE

    def test(self, name: str) -> Test | None:
        return next((t for t in self.tests if t.name == name), None)

    def to_json(self) -> dict[str, Any]:
        """The three fields 09 §9.3 adds to `hold collection show --json`."""
        return {"state": self.state, "reasons": list(self.reasons),
                "origin_check": self.origin_check}


ALWAYS_IN_USE = CopyState(IN_USE, (), ORIGIN_PASSED, True)


# ---- the switch ---------------------------------------------------------------

#: (the file's path, its mtime and size) -> what it said. Asked once per
#: collection per search, so it is a `stat`, and a parse only when the file
#: changed — an operator's edit is seen at once, with no restart.
_switch: dict[tuple, tuple[bool, frozenset[str]]] = {}


def _desired() -> tuple[bool, frozenset[str]]:
    """(does quarantine WITHHOLD, the collection ids in use before it did) —
    desired state (`platform.toml`), read when asked. A file that cannot be
    read enforces nothing: it must not take every copy out of search by
    accident."""
    nothing: tuple[bool, frozenset[str]] = (False, frozenset())
    try:
        from . import platform_config
        path = platform_config.platform_path()
        try:
            st = path.stat()
        except OSError:
            return nothing
        key = (str(path), st.st_mtime_ns, st.st_size)
        if key not in _switch:
            _switch.clear()
            cfg = platform_config.load()
            _switch[key] = (getattr(cfg, "quarantine", "report") == "enforce",
                            frozenset(getattr(cfg, "quarantine_in_use_before", ())))
        return _switch[key]
    except Exception:                                   # noqa: BLE001
        return nothing


def enforced() -> bool:
    """Does quarantine WITHHOLD on this node, or only report?"""
    return _desired()[0]


def in_use_before(codex: Codex | None) -> bool:
    """Is this copy one the peer already used the day it began to enforce?
    By collection id — a name can be given to another collection."""
    return codex is not None and codex.collection_id in _desired()[1]


# ---- this peer's key, as its attestations spell it -----------------------------

_own: list[str] = []


def own_key() -> str | None:
    """The public half, as an envelope's `key` spells it; None on a peer with
    no key (07 §7.8: it cannot sign, and checks and searches all the same).
    From THE resolution (app/identity.py), so the web process, which holds
    only the public file, never reads the seed.

    ONLY A KEY FOUND IS REMEMBERED (as `run_check.signer`): remembering "no
    key" outlived the key being put in place, and every statement this node
    signed after that was ignored until a restart."""
    if _own:
        return _own[0]
    try:
        from . import identity
        text = identity.own()
    except Exception:                                   # noqa: BLE001
        return None
    if text is not None:
        _own.append(text)
    return text


def reset_cache() -> None:
    _own.clear()
    _attested.clear()
    _v2_read.clear()
    _states.clear()


# ---- what this peer attested ------------------------------------------------------

class Attested(list):
    """The verified bodies held for a collection, and — per key — what that
    key's own checks come to, worked out ONCE per change of the directory."""

    def __init__(self, bodies=()) -> None:
        super().__init__(bodies)
        self._by_key: dict[str, dict[str, tuple[bool, dict | None]]] = {}
        self._by_image: dict[tuple[str, str], dict[str, tuple[bool, dict | None]]] = {}

    def by_image(self, key: str, check: str) -> dict[str, tuple[bool, dict[str, Any] | None]]:
        """image -> (has this key's `check` ever passed under it, its latest
        pass-or-fail under it). 03 §3.4: a verdict belongs to the image that
        ran it, and only some images' verdicts count (app/hold/equivalence.py)."""
        mine = self._by_image.get((key, check))
        if mine is None:
            mine = {}
            for b in self:
                if b.get("key") != key or b.get("check") != check \
                        or b.get("result") not in ("pass", "fail"):
                    continue
                image = str(b.get("platform_image") or "")
                ever, latest = mine.get(image, (False, None))
                if latest is None or str(b.get("time") or "") >= str(latest.get("time") or ""):
                    latest = b
                mine[image] = (ever or b.get("result") == "pass", latest)
            self._by_image[(key, check)] = mine
        return mine

    def of(self, key: str, check: str) -> tuple[bool, dict[str, Any] | None]:
        """(has this key's `check` ever passed, its latest pass-or-fail)."""
        mine = self._by_key.get(key)
        if mine is None:
            mine = {}
            for b in self:
                if b.get("key") != key or b.get("result") not in ("pass", "fail"):
                    continue
                ever, latest = mine.get(b.get("check"), (False, None))
                if latest is None or str(b.get("time") or "") >= str(latest.get("time") or ""):
                    latest = b
                mine[b.get("check")] = (ever or b.get("result") == "pass", latest)
            self._by_key[key] = mine
        return mine.get(check, (False, None))


#: directory -> (its mtime when read, was it still settling, what it held).
#: AN EVALUATION IS ONE `stat`, however many attestations are held: they
#: accumulate for a collection's whole life — a peer's own, and every one it
#: pulls (09 §9.8) — and a cost per evaluation that grew with them would be
#: the per-item cost CLAUDE.md forbids (measured before this: 0.2 ms at 10
#: files, 2.6 ms at 500). An attestation's file is named by the hash of its
#: body, so one is never edited in place: a file appears or goes, and either
#: moves the directory's mtime. A directory read within two seconds of its
#: own mtime is read again next time — a second file landing inside one
#: timestamp tick of a coarse filesystem must not be missed.
_attested: dict[str, tuple[int, bool, Attested]] = {}
_SETTLE_S = 2.0


def attestations(root: Path, name: str, codex: Codex) -> Attested:
    """Every attestation held for this collection, VERIFIED. A file that does
    not verify is not one."""
    d = Path(root) / name / "state" / "attestations" / shard_mod._hex(codex.collection_id)
    try:
        mtime = d.stat().st_mtime_ns
    except OSError:
        return Attested()
    cached = _attested.get(str(d))
    if cached is not None and cached[0] == mtime and not cached[1]:
        return cached[2]
    out = Attested()
    try:
        names = sorted(e.name for e in os.scandir(d) if e.name.endswith(".json"))
    except OSError:
        names = []
    for fname in names:
        try:
            body = env.verify(env.ATTESTATION, json.loads((d / fname).read_bytes()))
        except Exception:                               # noqa: BLE001
            continue
        if body.get("collection_id") == codex.collection_id:
            out.append(body)
    settling = time.time() - mtime / 1e9 < _SETTLE_S
    _attested[str(d)] = (mtime, settling, out)
    return out


def _own_latest(bodies: Attested, key: str, check: str) -> dict[str, Any] | None:
    return bodies.of(key, check)[1]


def _own_ever_passed(bodies: Attested, key: str, check: str) -> bool:
    return bodies.of(key, check)[0]


# ---- the origin: what was tried, what was decided -------------------------------------

def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_bytes())
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def origin_attempt(root: Path, name: str) -> dict[str, Any] | None:
    """The last time this peer's `records-vs-origin` COULD NOT RUN. An
    inconclusive check yields no attestation (09 §9.8), and 07 §7.7 has the
    peer show "when it was last tried": this is where that is kept."""
    return _read_json(Path(root) / name / "state" / ORIGIN_TRIED)


def note_origin_attempt(root: Path, name: str, codex: Codex, *, why: str, now: str) -> Path:
    """Called by whoever ran the plan, when `records-vs-origin` was
    inconclusive. Bookkeeping in `state/` — this peer's alone, never sent."""
    out = Path(root) / name / "state" / ORIGIN_TRIED
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_bytes(canonical_json({
        "kind": "louise-origin-attempt", "version": 1,
        "collection_id": codex.collection_id, "time": now, "why": why[:400]}))
    os.replace(tmp, out)
    return out


def clear_origin_attempt(root: Path, name: str) -> bool:
    """A VERDICT came (pass or fail): the note of an attempt that could not
    run says nothing now, and is removed rather than left to read as the
    copy's last word (wikipedia-v8, 2026-10-29: the pass that cleared its
    quarantine left the failed attempt of the evening before in place)."""
    out = Path(root) / name / "state" / ORIGIN_TRIED
    try:
        out.unlink()
    except FileNotFoundError:
        return False
    _states.clear()
    return True


def origin_ack(root: Path, name: str, codex: Codex) -> dict[str, Any] | None:
    raw = _read_json(Path(root) / name / "state" / ORIGIN_ACK)
    if raw and raw.get("kind") == "louise-origin-ack" \
            and raw.get("collection_id") == codex.collection_id:
        return raw
    return None


# ---- the five tests -------------------------------------------------------------------

def _unplanned(test: str, checks: str) -> Test:
    """A test with nothing to run on: a never-share collection holds no shard
    and its origin is the client that wrote it (02 §2.9), so roots, chunks
    and records-vs-origin have nothing to check, and nothing to wait for."""
    return Test(test, PASSED, f"a never-share collection has nothing for {checks} to "
                              f"check: no shard, and its origin is the client that "
                              f"wrote it (02 §2.9)")


def _roots_and_chunks(bodies, key: str | None, name: str) -> Test:
    clears = f"`louise hold check {name} --checks roots,chunks`"
    if key is None:
        return Test("roots-and-chunks", OPEN, "this peer has no key, so its checks "
                    "leave no attestation to read", "set NODE_KEY_PATH (07 §7.8)")
    missing = [c for c in ("roots", "chunks") if not _own_ever_passed(bodies, key, c)]
    if not missing:
        return Test("roots-and-chunks", PASSED, "this peer's own roots and chunks passed")
    failed = [c for c in missing if _own_latest(bodies, key, c) is not None]
    detail = (f"{' and '.join(failed)} failed here and never passed: a shard's local "
              f"fault is re-fetched and re-checked (03 §3.6)" if failed else
              f"{' and '.join(missing)} not yet run on this peer")
    return Test("roots-and-chunks", FAILED if failed else OPEN, detail, clears)


def _bundles() -> Test:
    return Test("bundles", PASSED, "every vector here passed T0, T1 and T2: nothing "
                                   "lands without them (03 §3.5)")


#: the statement's path -> ((mtime, size), its verified body or None)
_v2_read: dict[str, tuple[tuple[int, int], dict[str, Any] | None]] = {}


def _statement_v2_shape(root: Path, name: str, codex: Codex) -> dict[str, Any] | None:
    """A version-2 collection's single statement, verified and about THIS
    space (app/hold/vectors_build.py: `require_statement`). The signature is
    verified when the file changes, not at every evaluation."""
    path = Path(root) / name / "state" / "calibration.json"
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    cached = _v2_read.get(str(path))
    if cached is None or cached[0] != sig:
        body = None
        raw = _read_json(path)
        if raw is not None:
            try:
                body = emb.read_statement(raw)
            except Exception:                           # noqa: BLE001
                body = None
        cached = _v2_read[str(path)] = (sig, body)
    body = cached[1]
    return body if body is not None and body.get("space_id") == codex.space_id else None


def space_statements(root: Path, name: str, codex: Codex, role: str) -> dict[str, dict]:
    """run id -> statement, for this SPACE (`runs.statements_for_space`)."""
    return runs_mod.statements_for_space(root, name, codex, role, signer=own_key())


def _gate(root: Path, name: str, codex: Codex,
          answering: Mapping[str, str | None]) -> tuple[Test, bool, str]:
    """The test, whether the vector leg may be used, and why not."""
    if not runs_mod.applies(codex):
        body = _statement_v2_shape(root, name, codex)
        if body is None:
            return (Test("gate", OPEN, "the calibration gate has not run for this space "
                                       "on this peer",
                         f"`louise hold embedder check {name} --accept`"), True, "")
        return Test("gate", PASSED, "the calibration gate passed for this space"), True, ""

    roles = ["document"] + (["query"] if codex.space.query.encoder is not None else [])
    leg, why = True, ""
    for role in roles:
        run_id = answering.get(role)
        # this collection's own statements first — one directory; the other
        # collections of the space are asked only when these do not settle it
        known = runs_mod.statements(root, name, codex, role, signer=own_key())
        if not known or (run_id is not None and run_id not in known):
            known = space_statements(root, name, codex, role)
        clears = ("the gate, which runs by itself the moment the encoder answers; or "
                  f"`louise hold embedder check {name}`")
        if run_id is not None and run_id not in known:
            return (Test("gate", OPEN, f"the {role} encoder states run {run_id[:23]}…, "
                                       f"which this peer has not tested", clears), leg, why)
        if not known:
            return (Test("gate", OPEN, f"the gate has not run against the {role} "
                                       f"encoder's run", clears), leg, why)
        judged = [known[run_id]] if run_id is not None else list(known.values())
        if not any(runs_mod.passing(b, codex) for b in judged):
            leg = False
            why = (f"the {role} encoder's run FAILED the gate for this space: the vector "
                   f"leg is withheld and the text leg goes on (03 §3.3)")
    return Test("gate", PASSED, "the gate has run against the encoder's run"
                + ("" if leg else " — and it FAILED: see the vector leg")), leg, why


def _licence(licence: str, name: str) -> Test:
    if licence in ("acknowledged", "not-required", "not_required"):
        return Test("licence", PASSED if licence == "acknowledged" else NOT_REQUIRED,
                    "acknowledged" if licence == "acknowledged" else
                    "this licence class needs no acknowledgement")
    return Test("licence", OPEN, "the licence is not acknowledged (04 §4.4)"
                if licence == "missing" else f"the licence state cannot be judged: {licence}",
                f"`louise hold collection ack {name}`")


def _origin_verdicts(bodies, key: str | None, codex: Codex
                     ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(the verdict that decides, the latest failure that flags nothing).

    03 §3.4: a failure under an image that is neither the Codex's reference
    nor proven equivalent "is an equivalence failure of that image for this
    collection, not a defect in the collection ... it flags nothing". So the
    latest verdict among the images that count decides; failing that, a pass
    under any image shows the records reproduce, and a failure elsewhere is
    only said."""
    if key is None:
        return None, None
    from app.hold import equivalence
    verdicts = bodies.by_image(key, "records-vs-origin")
    counted = equivalence.reference_images(bodies, key, codex)

    def newest(items):
        items = [b for b in items if b is not None]
        return max(items, key=lambda b: str(b.get("time") or "")) if items else None

    deciding = newest(latest for image, (_, latest) in verdicts.items() if image in counted)
    if deciding is None:
        deciding = newest(latest for _, (_, latest) in verdicts.items()
                          if latest is not None and latest.get("result") == "pass")
    elsewhere = newest(latest for image, (_, latest) in verdicts.items()
                       if image not in counted and latest is not None
                       and latest.get("result") == "fail")
    return deciding, elsewhere


def _origin(root: Path, name: str, codex: Codex, bodies, key: str | None) -> tuple[Test, str]:
    clears = f"`louise hold check {name} --checks records-vs-origin`"
    latest, elsewhere = _origin_verdicts(bodies, key, codex)
    if latest is not None and latest.get("result") == "fail":
        shards = ", ".join(latest.get("shards") or []) or "a shard"
        return (Test("origin", FAILED,
                     f"this peer's own records-vs-origin FAILED on {shards} "
                     f"({latest.get('time')}): a copy that does not reproduce its origin "
                     f"is not used here until a person decides (03 §3.6)",
                     f"a later pass of {clears}, or `louise hold collection forget {name}`"),
                ORIGIN_FAILED)
    if latest is not None:
        return Test("origin", PASSED, f"this peer's own records-vs-origin passed "
                                      f"({latest.get('time')})"), ORIGIN_PASSED
    ack = origin_ack(root, name, codex)
    if ack is not None:
        return (Test("origin", ACKNOWLEDGED,
                     f"the origin could not be checked and its operator decided to use "
                     f"the copy ({ack.get('time')}); the check still runs when it can"),
                ORIGIN_ACKED)
    # 03 §3.6: the fifth test CANNOT RUN when "this peer's platform image is
    # neither the Codex's nor equivalent (§3.4), so that its failure would
    # say nothing about the collection" — and then the operator decides.
    from app.hold import equivalence
    ran = equivalence.running_image()
    if key is not None and ran is not None \
            and ran not in equivalence.reference_images(bodies, key, codex):
        failed_here = (f"; under it the check failed ({elsewhere.get('time')}), which "
                       f"flags nothing" if elsewhere is not None
                       and elsewhere.get("platform_image") == ran else "")
        return (Test("origin", CANNOT_RUN,
                     f"this peer runs {ran[:19]}…, neither the Codex's image nor one "
                     f"equivalent to it, so its records-vs-origin would say nothing "
                     f"about the collection (03 §3.4){failed_here} — awaiting your "
                     f"decision",
                     f"{clears} run in the Codex's own image; or decide: `louise hold "
                     f"collection ack {name} --origin-unchecked`, or `louise hold "
                     f"collection forget {name}`"), ORIGIN_PENDING)
    tried = origin_attempt(root, name)
    if tried is not None:
        return (Test("origin", CANNOT_RUN,
                     f"records-vs-origin could not run (last tried {tried.get('time')}: "
                     f"{tried.get('why')}) — awaiting your decision",
                     f"{clears} when the origin answers; or decide: `louise hold "
                     f"collection ack {name} --origin-unchecked`, or `louise hold "
                     f"collection forget {name}`"), ORIGIN_PENDING)
    if elsewhere is not None:
        image = str(elsewhere.get("platform_image") or "?")
        return (Test("origin", OPEN,
                     f"this peer's own records-vs-origin has not run under the Codex's "
                     f"image or one equivalent to it; under {image[:19]}… it failed "
                     f"({elsewhere.get('time')}), which is that image failing "
                     f"equivalence for this collection and flags nothing (03 §3.4)",
                     f"{clears}, run in the Codex's own image"), ORIGIN_PENDING)
    return (Test("origin", OPEN, "this peer's own records-vs-origin has not run",
                 clears), ORIGIN_PENDING)


def _holds_snapshot(root: Path, name: str) -> bool:
    """Whether the copy's SHARD holds a snapshot — one directory listing."""
    d = Path(root) / name / "shard" / "snapshots"
    try:
        return any(p.suffix == ".json" and not p.name.endswith(".sig.json") for p in d.iterdir())
    except OSError:
        return False


def _awaits_snapshot(t: Test, name: str) -> Test:
    """A test that has not run because there is nothing yet to run it on:
    roots, chunks and records-vs-origin draw over a snapshot's parts (03
    §3.2), and `hold check` refuses a copy that holds none. Said so, with
    what makes the first snapshot — the check's own command could only be
    refused (wikipedia, 2026-10-13: "tests open" naming a check it refuses)."""
    if t.status not in (OPEN, CANNOT_RUN):
        return t
    return Test(t.name, t.status,
                f"{t.detail}: this copy holds no snapshot yet, and the check draws over "
                f"a snapshot's parts (03 §3.2)",
                f"the first snapshot — `louise hold records build {name}`, then "
                f"`louise hold snapshot create {name}` — and then {t.clears}")


# ---- the state ------------------------------------------------------------------------------

_STANDS_IN_FOR = ("roots-and-chunks", "origin")


def _decided(t: Test) -> Test:
    """The operator's decision for a copy in use before quarantine was enforced
    (the header). It stands in for a test that HAS NOT RUN or CANNOT RUN —
    never for one that failed, never for the gate, never for the licence."""
    if t.name not in _STANDS_IN_FOR or t.status not in (OPEN, CANNOT_RUN):
        return t
    return Test(t.name, IN_USE_BEFORE,
                "in use before this peer enforced quarantine, by its operator's decision "
                "(platform.toml: quarantine_in_use_before); still open: " + t.detail,
                t.clears)


def evaluate(root: Path, name: str, codex: Codex | None, *, licence: str,
             refusal: str | None = None, key: str | None = None,
             answering: Mapping[str, str | None] | None = None,
             before: bool = False) -> CopyState:
    """The state of the copy at `<root>/<name>/`, from what is there NOW.

    `licence` is `app/licensing.py`'s answer, passed in (invariant 9: one
    resolution). `refusal` is the refuse-to-start reason, if any. `key` is
    this peer's, as an envelope spells it. `answering` maps a role to the run
    its encoder last stated, where this process has heard one. `before`: the
    copy is one the peer already used the day it began to enforce
    (`in_use_before`)."""
    if refusal:
        return CopyState(REFUSED, (refusal,), ORIGIN_PENDING, False)
    if codex is None:
        return ALWAYS_IN_USE
    bodies = attestations(root, name, codex)
    if getattr(codex, "annotates", None) is not None:
        return _annotations_state(root, name, codex, bodies, licence=licence, key=key,
                                  before=before)
    gate, leg, why = _gate(root, name, codex, answering or {})
    # 03 §3.6's tests 1 and 5 have nothing to run on for a NEVER-SHARE
    # collection (02 §2.9): a deposit collection holds no shard for roots and
    # chunks, and its origin is the client that wrote it, so no records-vs-
    # origin can re-fetch anything — its plan runs calibration and
    # vectors-sample "and nothing else". It is not kept in quarantine for a
    # check it will never run. Every other collection is judged by all five,
    # whatever its plan lists (the plan sets the samples, not the tests).
    # Found by the first deposit collection on the node (2026-10-06, 16f).
    from .hold import deposit as deposit_mod
    if deposit_mod.is_deposit(codex):
        origin, origin_check = _unplanned("origin", "records-vs-origin"), ORIGIN_NOT_PLANNED
        roots = _unplanned("roots-and-chunks", "roots or chunks")
    else:
        origin, origin_check = _origin(root, name, codex, bodies, key)
        roots = _roots_and_chunks(bodies, key, name)
        if not _holds_snapshot(root, name):
            roots, origin = _awaits_snapshot(roots, name), _awaits_snapshot(origin, name)
    tests = (roots, _bundles(), gate, _licence(licence, name), origin)
    if before:
        tests = tuple(_decided(t) for t in tests)
    # the origin test is shown and never waited for (header; 03 §3.6 amended
    # 2026-11-01): only the other four hold a copy in quarantine
    gating = [t for t in tests if t.name not in SHOWN_ONLY]
    waiting = [t for t in gating if t.status in (OPEN, FAILED, CANNOT_RUN)]
    reasons = tuple(f"{t.detail} — cleared by {t.clears}" if t.clears else t.detail
                    for t in waiting)
    if not waiting:
        # in use BY A DECISION is said, never passed over: the tests it stands in
        # for are still open, and `show` names them and what clears each
        reasons = tuple(f"{t.detail} — cleared by {t.clears}" for t in gating
                        if t.status == IN_USE_BEFORE)
    return CopyState(QUARANTINE if waiting else IN_USE, reasons, origin_check,
                     leg, tests, why)


def _roots(bodies, key: str | None, name: str) -> Test:
    """`roots` alone: an annotations collection's parts cut no chunks."""
    clears = f"`louise hold check {name} --checks roots,records`"
    if key is None:
        return Test("roots", OPEN, "this peer has no key, so its checks leave no attestation "
                    "to read", "set NODE_KEY_PATH (07 §7.8)")
    if _own_ever_passed(bodies, key, "roots"):
        return Test("roots", PASSED, "this peer's own roots passed")
    if _own_latest(bodies, key, "roots") is not None:
        return Test("roots", FAILED, "roots failed here and never passed: a part's local fault "
                    "is re-fetched and re-checked (03 §3.6)", clears)
    return Test("roots", OPEN, "roots not yet run on this peer", clears)


def _annotations_state(root: Path, name: str, codex: Codex, bodies, *, licence: str,
                       key: str | None, before: bool) -> CopyState:
    """03 §3.6's tests for an ANNOTATIONS collection (02 §2.10): its parts'
    roots, and its licence. There is no space, so no gate and no vector leg;
    no chunks; and its origin is the annotator, so no records-vs-origin can
    re-fetch it — those are said, never waited for."""
    tests = (_roots(bodies, key, name),
             Test("bundles", NOT_REQUIRED, "an annotations collection holds no vectors"),
             Test("gate", NOT_REQUIRED, "an annotations collection has no space to gate"),
             _licence(licence, name),
             Test("origin", NOT_REQUIRED, "an annotations collection's origin is its annotator, "
                                          "so no records-vs-origin can re-fetch it (02 §2.10)"))
    if before:
        tests = tuple(_decided(t) for t in tests)
    waiting = [t for t in tests if t.status in (OPEN, FAILED, CANNOT_RUN)]
    reasons = tuple(f"{t.detail} — cleared by {t.clears}" if t.clears else t.detail
                    for t in waiting)
    return CopyState(QUARANTINE if waiting else IN_USE, reasons, ORIGIN_NOT_PLANNED, False,
                     tests, "an annotations collection has no vector leg")


#: place -> (state, fresh until)
_states: dict[str, tuple[CopyState, float]] = {}


def of(collection) -> CopyState:
    """The state of a collection THE NODE RUNS (a `collections.Collection`).
    A refused one is never among them — it did not load — so this answers
    quarantine or in use."""
    codex = getattr(collection, "codex", None)
    if codex is None:
        return ALWAYS_IN_USE
    from . import collections as collections_mod, licensing, run_check
    root = collections_mod.louise_root() / "collections"
    where = str(root / collection.name)
    now = time.monotonic()
    cached = _states.get(where)
    if cached is not None and cached[1] > now:
        return cached[0]
    try:
        licence = licensing.state_for(collection)
    except Exception as e:                              # noqa: BLE001
        licence = f"{type(e).__name__}: {e}"            # fail closed (invariant 9)
    state = evaluate(root, collection.name, codex, licence=licence, key=own_key(),
                     answering=run_check.answering(collection),
                     before=in_use_before(codex))
    _states[where] = (state, now + TTL_S)
    return state


def withholds(collection) -> str | None:
    """Why this copy may not be USED now, or None. None while the node only
    reports (`enforced`)."""
    if not enforced():
        return None
    state = of(collection)
    if state.in_use:
        return None
    return f"{state.state}: " + "; ".join(state.reasons)


def vector_leg_withheld(collection) -> str | None:
    """Why this copy's VECTOR leg may not be searched — its run failed the
    gate — or None. The text leg goes on either way."""
    if not enforced():
        return None
    state = of(collection)
    return None if state.vector_leg else state.vector_leg_reason


# ---- the command line, which works on a root and a name ---------------------------------

class InQuarantine(CodexError):
    """A rule stood in the way (07 §7.7's exit 2): the copy is not in use."""


def held(root: Path, name: str, codex: Codex, local=None, *,
         refusal: str | None = None) -> CopyState:
    """The state of the copy at `<root>/<name>/`, for a command. `local` is
    the collection object `app/licensing.py` judges (invariant 9: its
    resolution, never a second one).

    WITHOUT ONE, THE SAME RESOLUTION IS STILL ASKED, of a stand-in that
    carries the Codex. It was first judged from the Codex's class alone —
    "yellow, so missing" — and on the reference node `hold collection show
    wikipedia` then read "the licence is not acknowledged" while the worker,
    asking `licensing.state_for`, read acknowledged: its terms are
    acknowledged by a record held elsewhere on this peer (04 §4.4), which a
    class cannot know. Two answers to one question, found the day it was
    deployed (2026-09-19)."""
    from types import SimpleNamespace

    from . import licensing
    judged = local if local is not None else SimpleNamespace(
        name=name, plugin=codex.plugin, codex=codex, license_ack=None)
    try:
        licence = licensing.state_for(judged)
    except Exception as e:                              # noqa: BLE001
        licence = f"{type(e).__name__}: {e}"            # fail closed
    return evaluate(Path(root), name, codex, licence=licence, refusal=refusal,
                    key=own_key(), before=in_use_before(codex))


def require_in_use(root: Path, name: str, codex: Codex, local=None, *, use: str) -> None:
    """At a command's door, beside the licence gate: a copy in quarantine is
    not embedded for, exported, imported into or certified (03 §3.6). Its
    tests are not asked — they are what clears it. Silent while the node only
    reports."""
    if not enforced():
        return
    state = held(root, name, codex, local)
    if not state.in_use:
        raise InQuarantine(
            f"{name} is in {state.state} on this peer, so {use} waits: "
            + "; ".join(state.reasons)
            + f". `louise hold collection show {name}` prints every test and what "
              f"clears it (03 §3.6)")


def origin_ack_refusal(state: CopyState) -> str | None:
    """Why `hold collection ack --origin-unchecked` may NOT be recorded now,
    or None (07 §7.7): "a decision stands in for a test that cannot run,
    never for one that failed" — nor for one that has not been tried, nor
    while another test is still open."""
    if state.state == REFUSED:
        return "the copy is refused: " + "; ".join(state.reasons)
    others = [t for t in state.tests if t.name != "origin"
              and t.status in (OPEN, FAILED, CANNOT_RUN)]
    if others:
        return ("another test is still open — " + "; ".join(t.detail for t in others)
                + ". The origin decision comes last, when everything else has passed")
    origin = state.test("origin")
    if origin is None:
        return "this copy has no Codex, and nothing to acknowledge"
    if origin.status in (PASSED, FAILED):
        return ("this peer's records-vs-origin HAS run (" + origin.detail + "): a "
                "decision stands in for a test that cannot run, never for one that ran")
    if origin.status == ACKNOWLEDGED:
        return "the decision is already recorded: " + origin.detail
    if origin.status == IN_USE_BEFORE:
        return ("the copy is already in use by the decision in platform.toml "
                "(quarantine_in_use_before): there is nothing to acknowledge. " + origin.clears
                + " is what clears the test itself")
    if origin.status == OPEN:
        return ("records-vs-origin has not been tried on this peer, so nothing says it "
                "cannot run: " + origin.clears + " first")
    return None


def record_origin_ack(root: Path, name: str, codex: Codex, *, now: str) -> Path:
    """The operator's informed decision, "with its time and the peer's key
    beside the licence acknowledgement" (07 §7.7). State, never sent."""
    out = Path(root) / name / "state" / ORIGIN_ACK
    out.parent.mkdir(parents=True, exist_ok=True)
    tried = origin_attempt(root, name) or {}
    tmp = out.with_suffix(".json.tmp")
    tmp.write_bytes(canonical_json({
        "kind": "louise-origin-ack", "version": 1,
        "collection_id": codex.collection_id, "time": now, "key": own_key(),
        "last_tried": tried.get("time"), "why": tried.get("why")}))
    os.replace(tmp, out)
    _states.clear()
    return out

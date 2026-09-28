"""The node's side of "validate once; then every answer says which run gave
it" — docs/03-checking.md §3.3; migration-plan step 13e.

`app/hold/runs.py` knows what a validated run is and refuses a write that
does not name one. It embeds nothing: `app/hold/` stays free of the corpus
machinery. THIS module is where the node's embedding client meets it, for
the command line and the worker alike, so there is one way a run gets
validated and one way a write is guarded.

    check_write(...)    the worker, before a write: a DICTIONARY LOOKUP. It
                        never runs the gate — several document threads reach
                        it at once, and the gate is triggered once per
                        (space, run), not once per answer. It raises, the
                        document is deferred untouched, and what it found is
                        forgotten so that `embed_allowed` looks again.
    embed_allowed(...)  the worker, before the stage: which run answers now?
                        One it has not tested is gated AT ONCE, here, by the
                        one thread that owns the stage. A run that fails, or
                        an endpoint that states none, PAUSES the collection's
                        embedding with its reason — never an error on a
                        document, which would take it out of every selection.
    guard(...)          the command line, before a write: the same check, and
                        the gate inline, since there is one thread.
    validate(...)       the gate against the run that answers now, and the
                        statement kept — pass or fail.

VERSION-3 COLLECTIONS ONLY (`runs.applies`). Every collection the node held
when this landed is version 2 or legacy: for them all of this returns at
once and the behavioural backend gate stays what it was. For a version-3
collection it REPLACES that gate — "what keeps later work honest is not a
test repeated during the work" (03 §3.3) — and no setting turns it off.

EVERY WAIT IS THE EMBEDDING CLIENT'S, which bounds each request. The gate is
some 550 of them, sent a slice at a time with `stop` asked between slices, so
a node that is stopping does not start the next (CLAUDE.md: a worker that
cannot finish cannot honour SIGTERM).
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import httpx

from . import collections as collections_mod
from . import config, embeddings
from .hold import calibration as cal, drag, embedder as emb, runs as runs_mod
from .hold.codex import Codex

#: a text short enough to cost nothing, sent to learn which run answers
PROBE = "louise"
#: texts a request-group of the gate; `stop` is asked between two of them
SLICE = 32
PASS_TTL_S = 1800     # ask a validated endpoint which run answers this often
FAIL_TTL_S = 300      # and one that is paused, this often


# ---- the embedding client, remembering who answered ---------------------------

def _stopping(stop: Callable[[], bool] | None, name: str) -> None:
    if stop is not None and stop():
        raise emb.GateDeferred(f"{name}: the node is stopping; the gate is not finished "
                               f"and nothing is kept")


def answers(recipe, *, query: bool = False, how: str = "normalised",
            stop: Callable[[], bool] | None = None, name: str = "") -> runs_mod.Answers:
    """The node's embedding client as an `Answers`.

    `how`: "normalised" — what a write stores; "one" — each text alone, as
    the endpoint returned it (the fingerprint); "contribute" — the same raw
    vectors, sent the way THIS peer contributes: its EMBED_BATCH, its
    EMBED_CONCURRENCY, and as many requests at once as its worker keeps
    documents in flight (`recipe.concurrency`). That last one is the setting
    the batching fault of 2026-07 lived in, so it is reproduced, not assumed."""
    def normalised(texts: list[str], seen: set) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), SLICE):
            _stopping(stop, name)
            out += embeddings.embed_texts(texts[i:i + SLICE], recipe=recipe,
                                          query=query, runs=seen)
        return out

    def one(texts: list[str], seen: set) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            _stopping(stop, name)
            out += embeddings.embed_one_at_a_time([text], recipe=recipe, query=query,
                                                  runs=seen)
        return out

    def contribute(texts: list[str], seen: set) -> list[list[float]]:
        _stopping(stop, name)
        lanes = max(1, min(int(getattr(recipe, "concurrency", 1) or 1), len(texts)))
        if lanes == 1:
            return embeddings.embed_texts(texts, recipe=recipe, query=query,
                                          raw=True, runs=seen)
        # as the worker does it: `lanes` documents in flight, each its own call
        groups = [list(range(k, len(texts), lanes)) for k in range(lanes)]
        with ThreadPoolExecutor(max_workers=lanes) as ex:
            parts = list(ex.map(
                lambda idx: embeddings.embed_texts([texts[i] for i in idx], recipe=recipe,
                                                   query=query, raw=True, runs=seen),
                groups))
        out: list[Any] = [None] * len(texts)
        for idx, vectors in zip(groups, parts):
            for i, v in zip(idx, vectors):
                out[i] = v
        return out

    return runs_mod.Answers({"normalised": normalised, "one": one,
                             "contribute": contribute}[how])


def batched(recipe) -> bool:
    """Does this peer contribute otherwise than strictly one text at a time?"""
    return (config.EMBED_BATCH > 1 or config.EMBED_CONCURRENCY > 1
            or int(getattr(recipe, "concurrency", 1) or 1) > 1)


def _endpoint(recipe, query: bool) -> str:
    return recipe.query_url if query and recipe.query_url else recipe.url


def answering_run(recipe, *, query: bool = False) -> str:
    """Which run answers at this endpoint now: one tiny request."""
    probe = answers(recipe, query=query)
    probe([PROBE])
    if len(probe.runs) != 1 or None in probe.runs:
        raise runs_mod.RunRefused(
            f"{_endpoint(recipe, query)} states no run identity (no "
            f"`{runs_mod.RUN_HEADER}` on its answer). A pass validates a RUN, so there "
            f"is nothing here to validate: searching with this endpoint works and "
            f"nothing is written through it (03 §3.3). Writing needs the node's model "
            f"container, RAGE")
    return next(iter(probe.runs))


def stated_run(recipe, *, query: bool = False) -> emb.StatedRun:
    """The run that answers now AND what it stands for — the platform's own
    description, verified against the id (runs.describe)."""
    return runs_mod.describe(_endpoint(recipe, query), answering_run(recipe, query=query),
                             get=httpx.get, fallback=config.RAGE_URL)


# ---- the gate against the run that answers ------------------------------------

def reference(root: Path, name: str, codex: Codex, role: str):
    member = Path(root) / name / "drag" / drag.calibration_paths(
        codex.space.calibration.set)[0]
    if not member.is_file():
        raise emb.GateError(
            f"{name}: no calibration member at {member.name}. It is written by `hold "
            f"collection create` (09 §9.12); without it there is nothing to compare "
            f"an encoder against")
    if role == "query":
        return cal.read_query_vectors(
            member, expect=codex.space.calibration.query_vectors).tolist()
    return cal.read_member(member, expect_vectors=codex.space.calibration.vectors,
                           expect_envelope=codex.space.envelope)[0].tolist()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def validate(root: Path, name: str, codex: Codex, recipe, *, role: str = "document",
             key=None, now: str | None = None,
             stop: Callable[[], bool] | None = None) -> dict[str, Any]:
    """The gate against the run that answers now, and its statement KEPT,
    pass or fail. Returns the statement's body.

    Unreachable, stopping, or the run changed during the pass:
    `GateDeferred` — no statement, and it is tried again (03 §3.1)."""
    query = role == "query"
    # FIRST, and before the network: without the member there is nothing to
    # compare against, which is a rule in the way (exit 2) and no deferral —
    # an unreachable encoder must not be what the operator reads instead
    reference_vectors = reference(root, name, codex, role)
    try:
        stated = stated_run(recipe, query=query)
        _stopping(stop, name)
        if key is None:
            from .bundles import keys as node_keys
            key = node_keys.load_signing_key()
        kw = {"query": query, "stop": stop, "name": name}
        body = runs_mod.validate(
            root, name, codex, role=role,
            reference=reference_vectors,
            embed=answers(recipe, **kw),
            embed_one=answers(recipe, how="one", **kw),
            contribute=answers(recipe, how="contribute", **kw),
            stated=stated, key=key, now=now or _now())
        _mark(codex, role, body["run"])      # measured now: nothing to re-check
        return body
    except (runs_mod.RunRefused, emb.GateError):
        raise
    except Exception as e:                              # noqa: BLE001
        raise emb.GateDeferred(
            f"{name}: the encoder was unreachable or failed during the gate: "
            f"{type(e).__name__}: {e}") from e


def signer(key=None) -> str | None:
    """The key a statement must be signed by to count here (runs.statements):
    `key`'s public half when the caller signs with one, else this node's
    own — the key `validate` signs with. None on a peer with no key, which
    holds no statements of its own.

    ONLY A KEY FOUND IS REMEMBERED. Remembering "no key" would outlive the
    key being put in place, and every statement this node then signs would
    be ignored and gated again, for ever."""
    from .hold import envelope as env
    if key is not None:
        return env.key_text(key.verify_key)
    if _me:
        return _me[0]
    try:
        from .bundles import keys as node_keys
        text = env.key_text(node_keys.load_signing_key().verify_key)
    except Exception:                                   # noqa: BLE001
        return None
    _me.append(text)
    return text


_me: list[str] = []


#: (space, role, run) re-checked, or freshly gated, since THIS process
#: started. A kept statement is relied on only once it is here (`recheck`).
_rechecked: set[tuple[str, str, str]] = set()


def _mark(codex: Codex, role: str, run_id: str) -> None:
    _rechecked.add((codex.space_id, role, run_id))


def rechecked(codex: Codex, role: str, run_id: str) -> bool:
    return (codex.space_id, role, run_id) in _rechecked


def recheck(root: Path, name: str, codex: Codex, recipe, run_id: str, *,
            role: str = "document", key=None,
            stop: Callable[[], bool] | None = None,
            log: Callable[[str], None] | None = None) -> bool:
    """THE FIRST TIME A KEPT STATEMENT IS RELIED ON IN THIS PROCESS: its own
    sixteen sampled texts, embedded again by the run it names, compared with
    the reference (runs.recheck). True: it holds, and is not asked again
    until the process restarts. False: it did not; every statement of this
    peer for that run and space is set aside, and the caller gates the run
    from scratch. An answer from another run, or the encoder away:
    `GateDeferred` — nothing is decided, and it is asked again."""
    if rechecked(codex, role, run_id):
        return True
    body = runs_mod.statements_for_space(root, name, codex, role,
                                         signer=signer(key)).get(run_id)
    if body is None or not runs_mod.passing(body, codex):
        return True            # nothing kept to re-check; `check` decides
    reference_vectors = reference(root, name, codex, role)
    embed = answers(recipe, query=role == "query", stop=stop, name=name)
    try:
        ok, passed, median = runs_mod.recheck(codex, body, reference=reference_vectors,
                                              embed=embed, role=role)
    except (runs_mod.RunRefused, emb.GateError):
        raise
    except Exception as e:                              # noqa: BLE001
        raise emb.GateDeferred(
            f"{name}: the encoder was unreachable or failed during the re-check: "
            f"{type(e).__name__}: {e}") from e
    if embed.runs != {run_id}:
        raise emb.GateDeferred(
            f"{name}: the re-check of run {run_id[:23]}… was answered by "
            f"{sorted(str(r)[:23] for r in embed.runs if r != run_id) or 'none'}; "
            f"it is inconclusive and asked again")
    if ok:
        _mark(codex, role, run_id)
        if log is not None:
            log(f"{role} run {run_id[:23]}…: its kept statement re-checked on start "
                f"({passed} of the sample, median {median:.6f})")
        return True
    moved = runs_mod.set_aside(root, name, codex, run_id, role)
    if log is not None:
        log(f"{role} run {run_id[:23]}…: its kept statement did NOT hold on re-check "
            f"({passed} of the sample, median {median:.6f}); set aside "
            f"{[p.name for p in moved]}, and the run is gated from scratch")
    return False


def _foreign(root: Path, name: str, role: str, log: Callable[[str], None] | None) -> None:
    """Report, once per file per process, a statement ignored because another
    key signed it (runs.statements)."""
    for fname in runs_mod.foreign(root, name, role):
        if log is not None and (name, fname) not in _told_foreign:
            _told_foreign.add((name, fname))
            log(f"ignored {fname}: signed by a key that is not this node's; a "
                f"validation counts only when this node made it")


_told_foreign: set[tuple[str, str]] = set()


def _told(body: dict[str, Any]) -> str:
    return (f"run {str(body.get('run'))[:23]}…: {body.get('passed')} of the sample "
            f"passed, median {body.get('median')}, batch-invariant "
            f"{body.get('batch_invariant')}")


def guard(root: Path, name: str, codex: Codex, recipe, answered: set, *,
          role: str = "document", key=None,
          stop: Callable[[], bool] | None = None,
          log: Callable[[str], None] | None = None) -> None:
    """THE COMMAND LINE, before a write: silent when vectors from `answered`
    may be stored. An untested run is gated at once, here, and the write goes
    ahead under it if it passes — nobody has to accept anything (operator,
    2026-09-20). `RunRefused`: exit 2. `RunUnvalidated`: the run was tested
    and is not this space's. `GateDeferred`: exit 3."""
    if not runs_mod.applies(codex):
        return
    together = batched(recipe)
    who = signer(key)
    try:
        runs_mod.check(root, name, codex, answered, signer=who, role=role,
                       batched=together)
        _foreign(root, name, role, log)
        stale = [r for r in sorted(r for r in answered if r)
                 if not recheck(root, name, codex, recipe, r, role=role, key=key,
                                stop=stop, log=log)]
        if not stale:
            return
    except runs_mod.RunUnvalidated as e:
        if e.failed:
            raise
        _foreign(root, name, role, log)
        if log is not None:
            log(f"{name}: run {e.run_id[:23]}… is new to this peer; running the gate")
    body = validate(root, name, codex, recipe, role=role, key=key, stop=stop)
    if log is not None:
        log(f"{name}: {_told(body)}")
    runs_mod.check(root, name, codex, answered, signer=who, role=role, batched=together)


def checking(root: Path, name: str, codex: Codex, recipe, *, role: str = "document",
             key=None, log: Callable[[str], None] | None = None
             ) -> Callable[[list[str]], list[list[float]]]:
    """An embed function for a CHECK, or for vectors that LEAVE this peer —
    T3, T4, `vectors-sample`, `hold vectors check`, an export's challenge
    (03 §3.3): "a check runs under a validated run. An answer that names
    another makes the check inconclusive, and an inconclusive check yields no
    attestation."

    For a version-3 collection every answer must name a run validated for the
    space. One this peer has not tested is gated at once, as `guard` does; one
    that failed, or an endpoint that states none, makes the check
    INCONCLUSIVE — `GateDeferred`, exit 3 — and never a verdict about the
    collection: an encoder that is not the space's says nothing about vectors
    that are. Older collections embed as they always did."""
    if not runs_mod.applies(codex):
        return lambda texts: embeddings.embed_texts(texts, recipe=recipe,
                                                    query=role == "query")
    embed = answers(recipe, query=role == "query")

    def fn(texts: list[str]) -> list[list[float]]:
        vectors = embed(texts)
        try:
            guard(root, name, codex, recipe, embed.runs, role=role, key=key, log=log)
        except (runs_mod.RunRefused, runs_mod.RunUnvalidated) as e:
            raise emb.GateDeferred(
                f"inconclusive — the check did not run under a validated run: {e}") from e
        return vectors
    return fn


# ---- the worker ------------------------------------------------------------------

def applies(collection) -> bool:
    codex = getattr(collection, "codex", None)
    return codex is not None and runs_mod.applies(codex)


def _root() -> Path:
    return collections_mod.louise_root() / "collections"


def _where(collection) -> str:
    """The cache's key: the collection's PLACE, never its name (CLAUDE.md —
    two objects can share a name and point at different files)."""
    return str(runs_mod.directory(_root(), collection.name))


#: place -> (allowed, reason, fresh until, the run the verdict is about)
_verdicts: dict[str, tuple[bool, str, float, str | None]] = {}
#: (space, endpoint) -> the lock of whoever is looking at that endpoint now
_looking: dict[str, threading.Lock] = {}
_guard = threading.Lock()


#: (endpoint URL, role) -> the run its last answer named. Keyed on the
#: ENDPOINT, never a collection's name: every collection that reaches a URL
#: meets the same run, so a run relaunched under one of them is seen by all at
#: once (03 §3.6: "every collection of that space on the peer goes with it").
_answering: dict[tuple[str, str], str] = {}


def heard(recipe, answered: set, *, role: str = "document") -> None:
    """Remember which run this endpoint's answers named."""
    named = [r for r in answered if r]
    if named:
        _answering[(_endpoint(recipe, role == "query"), role)] = sorted(named)[-1]


def answering(collection) -> dict[str, str | None]:
    """role -> the run this collection's encoder last stated, as far as THIS
    process has heard. Empty for a collection older than version 3."""
    if not applies(collection):
        return {}
    try:
        recipe = collections_mod.recipe_for(collection)
    except Exception:                                   # noqa: BLE001
        return {}
    return {role: _answering.get((_endpoint(recipe, role == "query"), role))
            for role in runs_mod.ROLES}


def _flight(collection) -> str:
    """What ONE gate at a time means: one per (space, encoder endpoint). Two
    collections of a space that reach one encoder meet the same run, and the
    second finds the first's statement instead of gating it again."""
    try:
        url = collections_mod.recipe_for(collection).url
    except Exception:                                   # noqa: BLE001
        url = _where(collection)
    return f"{collection.codex.space_id} {url}"


def reset_cache() -> None:
    _verdicts.clear()
    _answering.clear()
    _rechecked.clear()
    _told_foreign.clear()
    _me.clear()


def check_write(collection, recipe, answered: set) -> None:
    """THE WORKER, before a write (`embeddings._rows_for`): may vectors from
    these answers be stored? A lookup; the gate never runs here.

    It raises `RunUnvalidated` or `RunRefused`. Both DEFER the document — it
    is untouched, keeps no error, and stays selectable — because neither is
    the document's fault (`worker.transient_failure`). And the verdict is
    forgotten, so the stage's next `embed_allowed` asks the endpoint again
    and gates the new run at once, or pauses with the reason."""
    if not applies(collection):
        return
    heard(recipe, answered)
    codex = collection.codex
    try:
        runs_mod.check(_root(), collection.name, codex, answered, signer=signer(),
                       role="document", batched=batched(recipe))
        # A KEPT STATEMENT NOT YET RE-CHECKED IN THIS PROCESS is not relied
        # on: a deferral, and the stage's next `embed_allowed` re-checks it
        for run_id in sorted(r for r in answered if r):
            if not rechecked(codex, "document", run_id):
                raise runs_mod.RunUnvalidated(
                    f"{collection.name}: run {run_id[:23]}… has a kept statement not "
                    f"yet re-checked since this process started; nothing is written "
                    f"until it is", run_id)
    except (runs_mod.RunUnvalidated, runs_mod.RunRefused):
        _verdicts.pop(_where(collection), None)
        raise


def check_query(collection, recipe, answered: set) -> None:
    """A SEARCH, before the query vector is used (03 §3.3, searching): "a run
    not yet tested answers no query … an answer that names such a run is
    discarded"; a run that FAILED withholds the vector leg, and the text leg
    goes on. It raises `RunUnvalidated`; `retrieval.search` degrades to the
    text leg and says so.

    An endpoint that states no run is searched with (03 §3.3). And while the
    node only REPORTS quarantine (`copy_state.enforced`), this remembers who
    answered and withholds nothing."""
    if not applies(collection):
        return
    codex = collection.codex
    role = "query" if codex.space.query.encoder is not None else "document"
    heard(recipe, answered, role=role)
    from . import copy_state
    if not copy_state.enforced():
        return
    known = copy_state.space_statements(_root(), collection.name, codex, role)   # this node's key only
    for run_id in sorted(r for r in answered if r):
        body = known.get(run_id)
        if body is None:
            raise runs_mod.RunUnvalidated(
                f"{collection.name}: the {role} encoder answered from run "
                f"{run_id[:23]}…, which this peer has not tested: the answer is "
                f"discarded and the vector leg waits for the gate, which runs at once "
                f"(03 §3.3)", run_id)
        if not runs_mod.passing(body, codex):
            raise runs_mod.RunUnvalidated(
                f"{collection.name}: the {role} encoder's run {run_id[:23]}… FAILED the "
                f"gate for this space: the vector leg is withheld and the text leg goes "
                f"on (03 §3.3)", run_id, failed=True)


def _look(collection, stop: Callable[[], bool] | None,
          log: Callable[[str], None]) -> tuple[bool, str, str | None]:
    """Ask the endpoint which run answers, and settle it: (may it embed, why,
    the run that answered). Never raises."""
    name, codex = collection.name, collection.codex
    run_id = None
    try:
        recipe = collections_mod.recipe_for(collection)
        run_id = answering_run(recipe)
        heard(recipe, {run_id})
        who = signer()
        try:
            runs_mod.check(_root(), name, codex, {run_id}, signer=who,
                           batched=batched(recipe))
            _foreign(_root(), name, "document", log)
            if not recheck(_root(), name, codex, recipe, run_id, stop=stop, log=log):
                log(_told(validate(_root(), name, codex, recipe, stop=stop)))
                runs_mod.check(_root(), name, codex, {run_id}, signer=who,
                               batched=batched(recipe))
        except runs_mod.RunUnvalidated as e:
            if e.failed:
                raise
            _foreign(_root(), name, "document", log)
            log(f"run {run_id[:23]}… is new to this peer; running the gate against it")
            log(_told(validate(_root(), name, codex, recipe, stop=stop)))
            runs_mod.check(_root(), name, codex, {run_id}, signer=who,
                           batched=batched(recipe))
        return True, f"run {run_id[:23]}… is validated for this space", run_id
    except (runs_mod.RunRefused, runs_mod.RunUnvalidated, emb.GateError) as e:
        return False, str(e), run_id
    except Exception as e:                              # noqa: BLE001
        # the endpoint away: a deferral, retried; nothing is flagged
        return False, f"the encoder could not be asked: {type(e).__name__}: {e}", run_id


def _look_at_the_query_side(collection, stop: Callable[[], bool] | None,
                            log: Callable[[str], None]) -> None:
    """AN ASYMMETRIC SPACE'S QUERY ENCODER IS A RUN OF ITS OWN (03 §3.3),
    gated "at once, and by itself" like the document one: until it is tested
    the space's collections answer no query (§3.6). Nobody embeds a document
    with it, so nothing here decides whether embedding may go on — it only
    sees to it that the statement exists. Never raises."""
    name, codex = collection.name, collection.codex
    if codex.space.query.encoder is None:
        return
    try:
        recipe = collections_mod.recipe_for(collection)
        run_id = answering_run(recipe, query=True)
        heard(recipe, {run_id}, role="query")
        if run_id in runs_mod.statements_for_space(_root(), name, codex, "query",
                                                   signer=signer()):
            if recheck(_root(), name, codex, recipe, run_id, role="query", stop=stop,
                       log=log):
                return
        log(f"query run {run_id[:23]}… is new to this peer; running the gate against it")
        log("query " + _told(validate(_root(), name, codex, recipe, role="query",
                                      stop=stop)))
    except Exception as e:                              # noqa: BLE001
        log(f"the query encoder's run could not be settled: {type(e).__name__}: {e}")


def embed_allowed(con, collection, *, stop: Callable[[], bool] | None = None) -> bool:
    """THE WORKER, before the embed stage of a version-3 collection. Cached,
    logs its transitions where the backend gate logs its own, never raises."""
    from . import db
    from .verify import backend_gate

    where = _where(collection)
    now = time.time()
    cached = _verdicts.get(where)
    # A VERDICT IS ABOUT A RUN. When any collection that reaches this encoder
    # has since heard another run from it, the verdict is about a run that no
    # longer answers, however fresh it is: look again — for every collection
    # of the encoder, not only the one that noticed.
    now_answering = answering(collection).get("document")
    if cached is not None and cached[2] > now and (
            now_answering is None or cached[3] is None or cached[3] == now_answering):
        return cached[0]
    with _guard:
        lock = _looking.setdefault(_flight(collection), threading.Lock())
    if not lock.acquire(blocking=False):
        # someone is asking (or gating) already: ONE gate per (space, run).
        # Nothing to wait for — this round has no embedding, the next asks
        return False
    try:
        def log(message: str) -> None:
            db.log_event(con, "info", "run-check", f"[{collection.name}] {message}"[:500])

        allowed, reason, run_id = _look(collection, stop, log)
        _look_at_the_query_side(collection, stop, log)
        _verdicts[where] = (allowed, reason,
                            now + (PASS_TTL_S if allowed else FAIL_TTL_S), run_id)
        if cached is None or cached[0] != allowed or cached[1] != reason:
            db.log_event(con, "info" if allowed else "error", "run-check",
                         (f"[{collection.name}] embedding "
                          f"{'enabled' if allowed else 'PAUSED'}: {reason}")[:500])
            try:
                db.set_control(con, backend_gate.GATE_STATE_KEY, json.dumps(
                    {"allowed": allowed, "reason": reason[:400], "ts": int(now)}))
            except Exception:                           # noqa: BLE001
                pass        # a status write must never break the gate
        return allowed
    finally:
        lock.release()

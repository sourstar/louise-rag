"""`hold records fetch <collection> [--slice … | --profile P] [--snapshot ID]
[--dry-run]` (07 §7.3): the acquisition spec run for slices or a profile
(06 §6.2), to a snapshot's cutoff when one is named, drawing from the
peer's one budget (04 §4.3) through the engine — no byte moves here. Was
`corpus add` / `corpus grow` (07 §7.6), carried over from app/cli.py
whole: the same engine calls, the same stop, the same output. `--slice`
and `--snapshot` (2026-10-25) resolve to a profile and a cutoff before
anything moves (`select`), and the slices a complete fetch took are
recorded in the collection's file through its one writer
(app/collection_file.py), so the node's cycle continues them.

The node's worker fetches on its own; this is the by-hand door, and the
Library's grow dialog shows its dry run as its command line
(tests/acquire/test_web_shared_cli_path.py: the same `plan_acquisition`).
Concurrent with the worker it is safe: files are claimed
(app/acquire/claim.py).
"""
from __future__ import annotations

import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.acquire import engine
from app.acquire.spec import DiskPreflightError, LicenseAckRequiredError
from app.acquire.specs import ACQUIRE_REGISTRY
from app.transfer.manager import get_manager

#: Set by a stop signal, threaded into run_acquisition and observed by the
#: workers parked in the bandwidth bucket (they are woken to see it).
_stop = threading.Event()


def _handle_sigterm(signum, frame) -> None:
    _stop.set()
    try:
        get_manager().wake_all()
    except Exception:                                   # noqa: BLE001
        pass


#: What ends the command the graceful way. SIGHUP too: rsync leads its own
#: session (app/acquire/transport_rsync.py), so a terminal that goes away —
#: a dropped ssh, a `docker exec -it` — no longer takes rsync with it, and
#: a hangup left at its default killed the command and left rsync writing
#: its .part with no lease and nobody to stop it.
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def route_stop_signals(handler=_handle_sigterm) -> None:
    for sig in STOP_SIGNALS:
        signal.signal(sig, handler)


def hangup_is_ctrl_c() -> None:
    """Before any stop is bound (a dry run lists without one): a hangup
    arrives as the KeyboardInterrupt the transport cleans up after — it
    kills rsync's group before letting it past — not as the default that
    kills this process and strands rsync."""
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGHUP, signal.default_int_handler)


def print_plan(plan, profile: str, name: str) -> None:
    print(f"profile: {profile}")
    print(f"files:   {plan.files_pending} pending / {plan.files_total} total")
    print(f"bytes:   {plan.bytes_pending} pending / {plan.bytes_total} total")
    print(f"eta:     ~{plan.eta_seconds:.0f}s (placeholder rate; no live bandwidth "
          f"measurement until the transfer manager lands)")
    blocked = (f" — BLOCKS acquisition: louise hold collection ack {name}"
               if plan.license_state == "missing" else "")
    print(f"license: {plan.license_state}{blocked}")


@dataclass(frozen=True)
class Selection:
    """What one `records fetch` takes, resolved before a byte moves."""
    profile: str                     # what the engine fetches by
    record: Any = None               # the file's `slices` after a complete fetch; None: unchanged
    until: datetime | None = None    # an origin of update files: published before this cutoff
    notes: tuple[str, ...] = ()      # said on stderr: what was taken in place of what was named


def select(collection, spec, *, slices=None, profile: str | None = None,
           snapshot_id: str | None = None) -> Selection:
    """07 §7.3's `[--slice … | --profile P] [--snapshot ID]`, against the
    acquisition spec (06 §6.2). Refuses (CodexError, exit 2) what the origin
    cannot deliver, naming what it can — never half a selection.

    `--slice` names slices as the SHARD's paths name them. The origin
    delivers whole the slices its spec declares (`slice_profiles`); a slice
    under one of them is fetched as that one, and said. `all` is every
    slice. What the fetch completes is recorded in the file.

    `--snapshot ID` runs the spec to that snapshot's cutoff, by the origin's
    cutoff rule (06 §6.2): an origin of update files takes the files
    published before it; a dated-dump origin takes the dump its Codex pins,
    refused when that dump is not before the cutoff; an origin that keeps
    only its current state (a full resync, a zip rebuilt in place) can be
    fetched only as it is now — `records build --snapshot ID` applies the
    cutoff to each record, and that is said."""
    from app import collection_file
    from app.acquire import spec as acquire_spec
    from app.hold import shard as shard_mod
    from app.hold.codex import CodexError
    notes: list[str] = []
    record = None
    if slices is not None:
        if "all" in slices:
            if len(slices) > 1:
                raise CodexError("--slice all is every slice: name it alone")
            profile = acquire_spec.everything_profile(spec)
            if profile is None:
                raise CodexError(f"{collection.name}: the acquisition spec of {collection.plugin} "
                                 f"declares no profile that selects every slice")
            delivered = "all"
        else:
            delivered = []
            for s in slices:
                try:
                    shard_mod.check_slice(s)
                except Exception as e:      # noqa: BLE001 — ShardError: a name, refused
                    raise CodexError(f"--slice {s!r}: {e}") from None
                key = acquire_spec.delivered_slice(spec, s)
                if key is None:
                    whole = sorted(spec.slice_profiles)
                    raise CodexError(
                        f"--slice {s}: {collection.plugin}'s origin does not deliver it apart — "
                        + (f"the slices its files divide into are {', '.join(whole)}; "
                           if whole else "every one of its files may hold records of any slice; ")
                        + "`--slice all` fetches the origin whole (06 §6.2)")
                if key != s:
                    notes.append(f"{s} lies in {key}, which the origin delivers whole: "
                                 f"fetching {key}")
                if key not in delivered:
                    delivered.append(key)
            profile = acquire_spec.profile_for_slices(spec, delivered)
            if profile is None:
                raise CodexError(f"--slice {', '.join(delivered)}: their profiles do not combine "
                                 f"on {collection.plugin}'s origin ({spec.profile_mode}); "
                                 f"fetch them one at a time")
        c, raw, _ = collection_file.load(collection.name)
        try:
            record = collection_file.selection_after_fetch(c, raw, spec, delivered)
        except collection_file.SetRefused as e:
            raise CodexError(str(e)) from None
    else:
        profile = profile or collection.profile or "demo"

    until = None
    if snapshot_id is not None:
        until = _cutoff(collection, spec, snapshot_id, notes)
    return Selection(profile=profile, record=record, until=until, notes=tuple(notes))


def _cutoff(collection, spec, snapshot_id: str, notes: list[str]) -> datetime | None:
    from app.hold import snapshot as sn
    from app.hold.codex import CodexError
    codex = collection.codex
    if codex is None:
        raise CodexError(f"--snapshot {snapshot_id}: {collection.name} names no Codex, "
                         f"so it has no cutoff (09 §9.4)")
    instant = sn.cutoff_instant(snapshot_id, cadence=codex.update.cadence,
                                day=codex.update.day, clock=codex.origin.clock)
    mode = spec.delta.mode
    if mode == "updatefiles-dir":
        return instant
    if mode == "dated-dumps":
        pinned = collection.snapshot
        if not pinned:
            raise CodexError(
                f"--snapshot {snapshot_id}: {collection.name}'s Codex pins no dump, so its fetch is "
                f"whatever the origin calls latest and no cutoff selects it; a collection that holds "
                f"a dated dump names it (09 §9.17)")
        if str(pinned)[:8] >= instant.strftime("%Y%m%d"):
            raise CodexError(
                f"--snapshot {snapshot_id}: {collection.name}'s Codex pins dump {pinned}, which is not "
                f"before that snapshot's cutoff ({instant.date().isoformat()}); its snapshots begin "
                f"after the dump (06 §6.2)")
        notes.append(f"the dump this Codex pins, {pinned}, is before the cutoff of {snapshot_id}")
        return None
    if instant.timestamp() < time.time():
        notes.append(f"{collection.plugin}'s origin keeps only its current state ({mode}): the fetch "
                     f"is today's, and `hold records build {collection.name} --snapshot {snapshot_id}` "
                     f"applies the cutoff to each record (06 §6.2)")
    return None


def fetch(collection, con, profile: str, *, dry_run: bool = False,
          until: datetime | None = None, record: Any = None) -> int:
    """07 §7.7's exit: 0 done, 1 a file failed, 2 refused (the licence, the
    disk), 130 stopped by a signal after the unit in flight committed.
    `until` is a `Selection`'s cutoff; `record`, the slices written into the
    collection's file once the fetch completes (exit 0) — never before, and
    never for a dry run."""
    spec = ACQUIRE_REGISTRY.get(collection.plugin)
    if spec is None:
        print(f"louise: {collection.name}: no acquisition spec is registered for "
              f"plugin {collection.plugin!r}", file=sys.stderr)
        return 2
    cut = {} if until is None else {"until": until.timestamp()}
    if dry_run:
        hangup_is_ctrl_c()
        print_plan(engine.plan_acquisition(con, collection, spec, profile, **cut), profile,
                   collection.name)
        if until is not None:
            print(f"cutoff:  files published before {until.isoformat()}")
        if record is not None:
            print(f"records: slices = {_spelt(record)} in the file once the fetch completes")
        return 0

    route_stop_signals()
    try:
        result = engine.run_acquisition(con, collection, spec, profile, stop=_stop, **cut)
    except (LicenseAckRequiredError, DiskPreflightError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    rc = _report(result)
    complete = rc == 0 and not result.get("stopped") and not result.get("aborted")
    if complete and record is not None:
        from app import collection_file
        try:
            out = collection_file.record_fetched(collection.name, record)
        except collection_file.SetRefused as e:
            print(f"louise: {collection.name}: fetched, and not recorded in the file: {e}",
                  file=sys.stderr)
            return 1
        then = (f"the node's monthly cycle continues it (profile {out['profile']})"
                if out["acquire_enabled"] else
                "the node does not fetch it on its own: the file states acquire_enabled = false")
        print(f"recorded: slices = {_spelt(record)} in {out['file']}; {then}")
    return rc


def _spelt(record) -> str:
    return '"all"' if record == "all" else "[" + ", ".join(f'"{s}"' for s in record) + "]"


def _report(result: dict) -> int:
    aborted = result.get("aborted", 0)
    adopted = result.get("adopted", 0)
    print(f"fetched={result['fetched']} skipped={result['skipped']} "
          f"failed={result['failed']} aborted={aborted} total={result['total']}")
    if adopted:
        # "fetched" counts these, and an operator watching a 200 GB profile
        # finish in seconds deserves to know why
        print(f"  ({adopted} of them already on disk: verified and recorded, "
              f"not downloaded)")
    if result.get("stopped"):
        # stopped before any transfer — most often inside the listing, which
        # is ~12 minutes on gutenberg. Said outright: "total=0" alone reads
        # as an origin with no files.
        print(f"interrupted before any transfer ({result['stopped']}) — nothing "
              f"was recorded; re-run to resume")
    elif aborted:
        print("interrupted — partial downloads were checkpointed; re-run to resume")
    # 07 §7.7 and invariant 2: a signal finishes the unit, commits, and
    # exits 130. Only genuine failures are 1.
    if _stop.is_set():
        return 130
    return 1 if result["failed"] else 0

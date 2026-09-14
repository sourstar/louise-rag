"""CLI: python -m app.cli corpus add|status|grow — architecture-v2.md §14's
verbs. No console-scripts entry point exists in this repo (every process is
launched `python -m app.<module>`, see docker-compose.yaml's worker command)
so this is the concrete realization of the doc's "app corpus add|status|grow"
shorthand.
"""
import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path

from . import collections as collections_mod, db
from .acquire import engine
from .acquire.spec import DiskPreflightError, LicenseAckRequiredError
from .acquire.specs import ACQUIRE_REGISTRY
from .transfer.manager import get_manager

# Before P0.7 this was set by the handler and then never read anywhere, so
# Ctrl-C / SIGTERM during `corpus add` did nothing until the whole acquisition
# finished. It is now threaded into run_acquisition and observed by workers
# parked in the bandwidth bucket.
_stop = threading.Event()


def _handle_sigterm(signum, frame):
    _stop.set()
    try:
        get_manager().wake_all()
    except Exception:
        pass


#: What ends a command the graceful way. SIGHUP too: rsync leads its own
#: session (app/acquire/transport_rsync.py), so a terminal that goes away —
#: a dropped ssh, a `docker exec -it` — no longer takes rsync with it, and
#: a hangup left at its default killed the command and left rsync writing
#: its .part with no lease and nobody to stop it.
_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def _route_stop_signals(handler=_handle_sigterm) -> None:
    for sig in _STOP_SIGNALS:
        signal.signal(sig, handler)


def _refuse_unacknowledged(collection, use: str) -> bool:
    """True (and printed) when this collection may not be EXCHANGED.

    Invariant 9's "not exchanged" had no code gate anywhere on the
    outbound path: `torrent publish` hashed and published a corpus, and
    `torrent seed` served one, without ever consulting app/licensing.py.
    Both run without the worker, so gating `seeding.tick` alone left them
    open.
    """
    from . import licensing
    if licensing.state_for(collection) == licensing.MISSING:
        print(f"REFUSED: {collection.name}: this source's license requires an "
              f"acknowledgement before {use} (invariant 9). Record one with "
              f"`corpus add --ack-terms` or the Library's accept dialog.",
              file=sys.stderr)
        return True
    return False


def _collection_by_name(name: str):
    for c in collections_mod.load_all():
        if c.name == name:
            return c
    raise SystemExit(f"no such collection: {name!r} (check collections/*.toml)")


def _print_plan(plan, profile) -> None:
    print(f"profile: {profile}")
    print(f"files:   {plan.files_pending} pending / {plan.files_total} total")
    print(f"bytes:   {plan.bytes_pending} pending / {plan.bytes_total} total")
    print(f"eta:     ~{plan.eta_seconds:.0f}s (placeholder rate; no live bandwidth "
          f"measurement until the transfer manager lands)")
    blocked = " — BLOCKS acquisition, run with --ack-terms" if plan.license_state == "missing" else ""
    print(f"license: {plan.license_state}{blocked}")


def cmd_add(args) -> int:
    collection = _collection_by_name(args.collection)
    spec = ACQUIRE_REGISTRY.get(collection.plugin)
    if spec is None:
        raise SystemExit(f"no AcquisitionSpec registered for plugin {collection.plugin!r}")
    con = db.connect(collection)

    if args.ack_terms:
        if spec.terms_text_path is None:
            raise SystemExit(f"{spec.source_id} declares no terms_text_path — nothing to ack")
        ack = engine.make_ack(Path(args.ack_terms))
        print(f"Computed ack for {spec.terms_text_path}: {ack}")
        print(f'Add this to collections/{collection.name}.toml:\n  license_ack = "{ack}"')
        print("(then commit the file — this is versioned Tier-2 config, not a DB write)")
        return 0

    profile = args.profile
    if args.dry_run:
        plan = engine.plan_acquisition(con, collection, spec, profile)
        _print_plan(plan, profile)
        return 0

    _route_stop_signals()
    try:
        result = engine.run_acquisition(con, collection, spec, profile, stop=_stop)
    except LicenseAckRequiredError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    except DiskPreflightError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
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
    # docs/07 §7.7 and CLAUDE.md invariant 2: a signal finishes the unit,
    # commits, and exits 130. Only genuine failures are 1.
    if _stop.is_set():
        return 130
    return 1 if result["failed"] else 0


def cmd_grow(args) -> int:
    """Same mechanism as `add`, presented as growing an existing collection
    to a larger profile — profiles aren't ordered in the type system, so
    this doesn't enforce "bigger than before," it just names the intent."""
    return cmd_add(args)


def cmd_embed_materialize(args) -> int:
    """Env→TOML migration for the embed recipe (invariant 10: TOML is
    desired state). Writes the collection's EFFECTIVE recipe into its TOML
    explicitly. The recipe is byte-equal to what the collection already
    uses, so the space id — and therefore the pin — does not change; what
    changes is that a later env edit can no longer silently retarget this
    collection's space."""
    collection = _collection_by_name(args.collection)
    toml_path = collections_mod.config_toml_path(collection.name)
    if collection.embed is not None:
        print(f"{collection.name}: [embed] is already explicit in "
              f"{toml_path} — nothing to do")
        return 0
    if not toml_path.exists():
        print(f"{collection.name}: no TOML file (synthesized default) — "
              f"declare the collection first (the web UI's create flow, or "
              f"corpus add)")
        return 1
    recipe = collections_mod.recipe_for(collection)
    from .web import _write_collection_toml, _codex_id
    _write_collection_toml(
        toml_path, codex=_codex_id(collection),
        plugin=collection.plugin, profile=collection.profile,
        acquire_enabled=collection.acquire_enabled,
        corpus_dir=collection.corpus_dir,
        manifest=collection.manifest_path, fts=collection.fts_dir,
        lancedb=collection.lance_dir, license_ack=collection.license_ack,
        genesis_archive=collection.genesis_archive, embed=recipe,
        # Everything else the TOML already declared has to be carried, or
        # "make the recipe explicit" quietly un-declares the rest: layers
        # are part of space_id (invariant 1), a dropped snapshot moves the
        # collection back to "whatever the origin calls latest", a dropped
        # embed_mode resets it to 'lazy' at the next boot, and a dropped
        # peer_source sends acquisition back to the origin.
        embed_mode=collection.embed_mode_default, layers=collection.layers,
        snapshot=collection.snapshot, extras=collection.extras,
        peer_source=collection.peer_source)
    print(f"{collection.name}: materialized the env recipe into {toml_path}")
    print(f"  model={recipe.model} url={recipe.url} api={recipe.api} "
          f"dtype={recipe.dtype} ctx={recipe.ctx} dim={recipe.dim}")
    print("  space id unchanged (byte-equal recipe); hot-reload applies it "
          "within one round")
    return 0


def cmd_space_repin(args) -> int:
    """Adopt a NEW embedding space for one collection — the explicit,
    operator-driven step behind every "wipe and re-embed" refusal the boot
    check prints (invariant 1/3: never automatic). What it does, in one
    manifest transaction and then on disk:

      - drops the space pin (the worker pins the current recipe + chunker
        at its next boot);
      - marks every document embed_done=0 AND fts_done=0 — the chunk ids
        the lexical index holds were cut by the old chunker and must not
        outlive it (same id, different text, would mislead every hybrid
        hit), so the fts stage re-cuts and rewrites them;
      - clears the chunk verification ledger (provenance of vectors that
        no longer exist);
      - resets the genesis ledger: shards still on disk go back to
        `fetched` (they re-apply against the new chunks — no re-download),
        finished or failed rows are dropped;
      - removes the Lance vector table.

    Stop the worker first: it must not be mid-embed while this runs.
    Refuses without --yes."""
    import shutil
    collection = _collection_by_name(args.collection)
    con = db.connect(collection)
    pinned = con.execute("SELECT space_id FROM space WHERE id=1").fetchone()
    embedded = con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0]
    indexed = con.execute("SELECT COUNT(*) FROM documents WHERE fts_done=1").fetchone()[0]
    genesis_rows = con.execute("SELECT COUNT(*) FROM genesis_imports").fetchone()[0]
    print(f"{collection.name}: pinned space {pinned['space_id'] if pinned else '(none)'}; "
          f"{embedded} embedded, {indexed} indexed documents, {genesis_rows} genesis rows, "
          f"vectors at {collection.lance_dir}")
    if not args.yes:
        print("  would: drop the pin, reset embed_done and fts_done for every document, "
              "clear chunk_verification, reset genesis rows, delete the Lance table. "
              "Re-run with --yes (worker stopped).")
        return 2
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DELETE FROM space")
        con.execute("UPDATE documents SET embed_done=0, embed_version=0, fts_done=0")
        con.execute("DELETE FROM chunk_verification")
        con.execute("DELETE FROM genesis_imports WHERE state NOT IN "
                    "('fetching','fetched','applying','applied')")
        con.execute("UPDATE genesis_imports SET state='fetched', "
                    "pending=NULL, next_pass_at=0, detail=NULL WHERE state IN "
                    "('applying','applied')")
        # EVERY row kept, not only applying/applied: the ledger counts per
        # flush, so a fetched row (a rejection retried, a deferral lifted)
        # or a fetching one carries docs too, and chunk_verification — what
        # those docs stand for — was just emptied. The proof goes as well:
        # it was taken against the space this drops, and the next pass
        # proves the archive against the new one.
        con.execute("UPDATE genesis_imports SET docs=0, chunks=0, proven_at=NULL")
        db.log_event(con, "info", "spaces",
                     f"space re-pin requested by the operator: {embedded} vectors and "
                     f"{indexed} lexical entries reset — the next worker boot pins the "
                     f"current recipe and chunker")
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    if collection.lance_dir.exists():
        shutil.rmtree(collection.lance_dir)
    print(f"  done: pin dropped, {embedded} documents to re-embed, {indexed} to re-index, "
          f"Lance table removed. Start the worker; it pins the new space at boot.")
    return 0


def cmd_status(args) -> int:
    names = [args.collection] if args.collection else [c.name for c in collections_mod.load_all()]
    for name in names:
        collection = _collection_by_name(name)
        spec = ACQUIRE_REGISTRY.get(collection.plugin)
        print(f"=== {name} ({collection.plugin}) ===")
        print(f"  acquire_enabled: {collection.acquire_enabled}")
        if spec is None:
            print("  no AcquisitionSpec registered for this plugin")
            continue
        con = db.connect(collection)
        counts = con.execute(
            "SELECT COUNT(*) total, SUM(fetch_done) done FROM fetch_files "
            "WHERE source_id=?", (spec.source_id,)).fetchone()
        print(f"  fetch_files: {counts['done'] or 0} done / {counts['total'] or 0} tracked")
        if not collection.acquire_enabled:
            # Never lists from the origin for a collection that hasn't opted
            # in — a status check must not itself trigger outbound network
            # access on an otherwise fully local, hand-populated deployment.
            print("  (acquisition disabled — run with a collection that has "
                  "acquire_enabled=true to see a live plan)")
            continue
        profile = collection.profile or "demo"
        plan = engine.plan_acquisition(con, collection, spec, profile)
        _print_plan(plan, profile)
    return 0


# ---------------- bundles ----------------

def cmd_bundle_keygen(args) -> int:
    """Generate a node signing key. Written with 0600 where the OS supports
    it, and the SEED IS NEVER PRINTED — only the public key, which is the
    producer identity peers see (CLAUDE.md invariant 8)."""
    from .bundles import keys as bundle_keys
    from nacl import signing
    path = Path(args.path)
    if path.exists():
        raise SystemExit(f"refusing to overwrite an existing key at {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = bundle_keys.generate_seed()
    path.write_bytes(seed)
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass
    public = bundle_keys.public_key_hex(signing.SigningKey(seed))
    print(f"wrote node key to {path}")
    print(f"public key: {public}")
    print(f"Set NODE_KEY_PATH={path} to export bundles. Share the PUBLIC key "
          f"with peers; never the file.")
    return 0


def cmd_bundle_export(args) -> int:
    from .bundles import export as bundle_export
    collection = _collection_by_name(args.collection)
    # invariant 9, "not exchanged": a bundle is a signed, shareable copy of
    # the collection's vectors (verification pass, 2026-09-13)
    if _refuse_unacknowledged(collection, "its vectors are exported as a bundle"):
        return 1
    con = db.connect(collection)
    if args.uids:
        uids = args.uids
    else:
        uids = [r["uid"] for r in con.execute(
            "SELECT uid FROM documents WHERE in_scope=1 AND embed_done=1 "
            "ORDER BY CAST(native_id AS INTEGER) LIMIT ?", (args.limit,))]
    if not uids:
        raise SystemExit("no embedded documents to export")
    try:
        result = bundle_export.export_bundle(con, collection, uids, Path(args.dest))
    except Exception as e:
        print(f"REFUSED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"bundle {result['bundle_id'][:16]}… -> {result['path']}")
    print(f"  {result['chunks']} chunks across {result['uids']} document(s), "
          f"{result['dim']}d {result['dtype']}, {result['bytes']} bytes")
    print(f"  producer: {result['producer']}")
    return 0


def cmd_bundle_import(args) -> int:
    from .bundles import importer as bundle_importer
    collection = _collection_by_name(args.collection)
    # invariant 9, "not imported": the worker's inbox asks; this door did not
    if _refuse_unacknowledged(collection, "vectors are imported into it"):
        return 1
    con = db.connect(collection)
    _route_stop_signals()
    result = bundle_importer.import_bundle(
        con, collection, Path(args.path), stop=_stop,
        allow_first_use=not args.no_first_use)
    if result.get("ok"):
        print(f"imported {result.get('chunks', 0)} chunks across "
              f"{result.get('uids', 0)} document(s)")
        if result.get("admission") == "first-use":
            print(f"NOTE: trusted this producer on first use: {result['producer']}")
        if result.get("verdict"):
            print(f"  checks: {result['verdict']}")
        return 0
    print(f"REFUSED ({result['error_class']}): {result['error']}", file=sys.stderr)
    # A transient failure is not the operator's fault and not the bundle's:
    # exit 0 so a retry loop does not treat it as a permanent rejection.
    return 0 if result.get("retryable") else 1


def cmd_bundle_trust(args) -> int:
    from .bundles import trust as bundle_trust
    collection = _collection_by_name(args.collection)
    con = db.connect(collection)
    if args.quarantine:
        affected = bundle_trust.quarantine_key(con, args.pubkey, args.reason or "manual")
        print(f"quarantined {args.pubkey}")
        if affected:
            print(f"  it had written {sum(affected.values())} chunks across "
                  f"{len(affected)} document(s):")
            for uid, count in sorted(affected.items())[:20]:
                print(f"    {uid}: {count}")
            print("  re-embed them locally with "
                  "`python -m app.cli bundle reembed`, or reset the embed stage.")
        return 0
    bundle_trust.trust_key(con, args.pubkey, note=args.reason or "trusted by operator")
    print(f"trusted {args.pubkey}")
    return 0


def cmd_bundle_resweep(args) -> int:
    """Re-verify bundles admitted at less than full verification — e.g. while
    the embed endpoint was down (T3 skipped) or the local space unconfirmed."""
    from .bundles import verify_on_use
    collection = _collection_by_name(args.collection)
    # invariant 9: re-verifying re-embeds corpus text — use of the corpus
    if _refuse_unacknowledged(collection, "its text is re-embedded to verify bundles"):
        return 1
    con = db.connect(collection)
    result = verify_on_use.resweep(con, collection)
    print(f"swept {result['swept']} bundle(s): {result['promoted']} promoted to "
          f"tier 3, {len(result['failed'])} failed")
    for bundle_id in result["failed"]:
        print(f"  FAILED {bundle_id[:16]}… — producer quarantined, affected "
              f"documents re-queued for local embedding")
    return 1 if result["failed"] else 0


def cmd_bundle_status(args) -> int:
    from .bundles import ledger as bundle_ledger, trust as bundle_trust
    collection = _collection_by_name(args.collection)
    con = db.connect(collection)
    print(f"=== {collection.name} ===")
    print("producers:")
    for row in con.execute("SELECT * FROM providers ORDER BY first_seen"):
        flag = "QUARANTINED" if row["quarantined"] else ("trusted" if row["trusted"] else "-")
        print(f"  {row['pubkey'][:16]}…  {flag}  "
              f"accepted={row['bundles_accepted']} rejected={row['bundles_rejected']}")
    print("recent bundles:")
    for row in bundle_ledger.recent(con, limit=args.limit):
        print(f"  {row['bundle_id'][:16]}…  {row['state']:<9} "
              f"chunks={row['chunk_count'] or 0}  {row['error'] or ''}"[:160])
    counts = con.execute(
        "SELECT state, COUNT(*) c FROM chunk_verification GROUP BY state").fetchall()
    if counts:
        print("imported chunks: " + ", ".join(f"{r['state']}={r['c']}" for r in counts))
    return 0


def cmd_contribute(args) -> int:
    """One lease -> embed -> pack -> upload round against a coordinator."""
    from . import contribute
    collection = _collection_by_name(args.collection)
    # invariant 9: a contribution embeds the corpus and uploads the vectors —
    # use AND exchange (verification pass, 2026-09-13)
    if _refuse_unacknowledged(collection, "vectors are computed from it and uploaded"):
        return 1
    con = db.connect(collection)
    _route_stop_signals()
    try:
        if args.report_demand:
            result = contribute.report_demand(con, collection, args.coordinator)
            print(f"reported demand for {result.get('recorded', 0)} uid(s)")
            return 0
        result = contribute.run_round(con, collection, args.coordinator,
                                       count=args.count, stop=_stop)
    except contribute.ContributeError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    if not result.get("ok"):
        print(f"no contribution made: {result.get('reason')}", file=sys.stderr)
        return 1
    if not result.get("uploaded"):
        print(f"nothing to do: {result.get('reason', 'no work available')}")
        return 0
    print(f"uploaded {result['uploaded']} chunks across {result['uids']} document(s)"
          + (" (duplicate — another producer got there first)"
             if result.get("duplicate") else ""))
    return 0


def cmd_genesis_import(args) -> int:
    """Admit a published vector archive (or any compatible parquet of
    id+vector rows) into a collection's space — reuse of already-paid
    GPU-hours, verified as the same function before a single row lands."""
    from .bundles import genesis
    collection = _collection_by_name(args.collection)
    con = db.connect(collection)

    if args.parquet:
        archive = genesis.GenesisArchive(
            archive_id=args.archive_id or f"parquet:{Path(args.parquet).name}",
            base_url="", adapter="parquet",
            shards=(str(Path(args.parquet).resolve()),),
            uid_prefix=args.uid_prefix or f"{collection.plugin}:")
        import shutil
        fetch = lambda name, dest: shutil.copyfile(name, dest)  # noqa: E731 — local file, no network
    else:
        archive = genesis.ARCHIVES.get(args.archive or "")
        if archive is None:
            known = ", ".join(sorted(genesis.ARCHIVES))
            raise SystemExit(f"unknown archive {args.archive!r} — known: {known}; "
                             f"or use --parquet for a local file")
        fetch = None

    shards = args.shards.split(",") if args.shards else None
    _route_stop_signals()
    try:
        result = genesis.import_archive(con, collection, archive, shards=shards,
                                         fetch=fetch, stop=_stop,
                                         keep_cache=args.keep_cache)
    except genesis.GenesisError as e:
        print(f"REJECTED: {e}", file=sys.stderr)
        return 1
    except genesis.GenesisUnavailable as e:
        print(f"UNAVAILABLE (retry later): {e}", file=sys.stderr)
        return 2
    print(f"imported {result['chunks']} vectors across {result['docs']} documents "
          f"({len(result['shards'])} shard(s))")
    for r in result["shards"]:
        print(f"  {r['shard']}: {r['state']}"
              + (f" docs={r['docs']} skipped_absent={r['skipped_absent']} "
                 f"skipped_shape={r['skipped_shape']}" if "docs" in r else ""))
    return 0


def cmd_genesis_status(args) -> int:
    collection = _collection_by_name(args.collection)
    con = db.connect(collection)
    rows = con.execute(
        "SELECT archive, shard, state, docs, chunks, detail FROM genesis_imports "
        "ORDER BY archive, shard").fetchall()
    if not rows:
        print("no genesis imports recorded")
        return 0
    for r in rows:
        line = f"{r['archive']}/{r['shard']}: {r['state']} docs={r['docs']} chunks={r['chunks']}"
        if r["detail"]:
            line += f" — {r['detail']}"
        print(line)
    return 0


def cmd_genesis_retry(args) -> int:
    """Put REJECTED shards back in line. A rejection recorded before the
    per-archive proof (STATUS: pubmed shard 1, 2026-08-26 — a verdict on a
    sample of one) is not evidence; under the current rule a retry either
    imports or records a verdict on real evidence."""
    from .bundles import genesis
    collection = _collection_by_name(args.collection)
    con = db.connect(collection)
    reset = genesis.retry_rejected(con, shard=args.shard)
    if not reset:
        print("nothing rejected" + (f" for shard {args.shard}" if args.shard else ""))
        return 0
    for archive_id, shard in reset:
        print(f"{archive_id}/{shard}: rejected -> fetched (the worker retries it)")
    return 0


def cmd_health(args) -> int:
    """Exit 1 when a stage has work it cannot start — so this is a CHECK,
    not only a page to read. A stage with nothing to do exits 0 whether
    or not it is switched off: "off and finished" is not a problem, and
    reporting it would make the check cry wolf on every idle collection."""
    from . import health
    if args.collection:
        cols = [_collection_by_name(args.collection)]
    else:
        got = collections_mod.load_all(skip_unreadable=True)
        cols = got[0] if isinstance(got, tuple) else got
    reports, blocking = health.check(cols, errors=args.errors)
    print(health.render(reports), end="")
    if not blocking:
        return 0
    print("\nBLOCKED:", file=sys.stderr)
    for line in blocking:
        print(f"  {line}", file=sys.stderr)
    return 1


def cmd_genesis_recheck(args) -> int:
    """Reopen FINAL shards that still have documents the archive could
    serve — the one-shot repair for shards finalised before the fts hook
    existed (genesis.reopen_finals_for_range). Costs one indexed range
    count per final shard, which is why it is a command and not a round."""
    from .bundles import genesis
    collection = _collection_by_name(args.collection)
    con = db.connect(collection)
    con.execute("BEGIN IMMEDIATE")
    found = genesis.reopen_finals_with_unserved(con, collection)
    con.commit()
    if not found:
        print("every final shard has served every document in its range")
        return 0
    for shard, n in sorted(found.items(), key=lambda kv: -kv[1]):
        print(f"shard {shard}: {n:,} unserved -> reopened")
    print(f"{len(found)} shard(s) reopened, {sum(found.values()):,} documents; "
          f"the fetcher re-fetches them and the worker imports their vectors")
    return 0


def cmd_genesis_recount(args) -> int:
    """Recompute the archive ledger's docs/chunks from chunk_verification
    — the one-shot repair for rows counted before the ledger moved into
    the flush's transaction (genesis.recount_ledger). A dry run unless
    --yes; with --yes, stop the worker first."""
    import time
    from .bundles import genesis
    collection = _collection_by_name(args.collection)
    con = db.connect(collection)
    t0 = time.monotonic()
    out = genesis.recount_ledger(con, write=args.yes)
    # the walk's cost on THIS manifest — what --yes will hold the write
    # lock for, so the dry run is the measurement
    print(f"walked chunk_verification in {time.monotonic() - t0:.1f} s")
    changed = [rc for rc in out if rc.changed]
    if not changed:
        print(f"{len(out)} ledger row(s) already match chunk_verification")
        return 0
    for rc in changed:
        print(f"{rc.archive}/{rc.shard}: docs {rc.docs:,} -> {rc.held_docs:,}, "
              f"chunks {rc.chunks:,} -> {rc.held_chunks:,}")
    if not args.yes:
        print(f"{len(changed)} row(s) differ; nothing written. Re-run with --yes "
              f"(worker stopped) to set them.")
        return 2
    print(f"{len(changed)} row(s) set from chunk_verification")
    return 0


def cmd_pack_build(args) -> int:
    """A collection's documents -> a pack (docs/notes/pack-format.md): sliced
    record shards + signed manifest, resumable."""
    from .pack import build as pack_build
    collection = _collection_by_name(args.collection)
    _route_stop_signals()
    try:
        result = pack_build.build(collection, Path(args.out), snapshot=args.snapshot, name=args.name,
                                  records_per_shard=args.records_per_shard, until=args.until,
                                  sign=not args.unsigned, stop=_stop)
    except (pack_build.BuildError, ValueError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    if result.interrupted:
        print("interrupted — re-run to resume", file=sys.stderr)
        return 130
    print(f"{result.slices} slice(s), {result.shards} shard(s), {result.records} records, "
          f"{result.chunks} chunks, {result.skipped} skipped -> {result.manifest_path}")
    return 0


# `pack verify`, `pack cat` and `pack dump` are NOT license-gated, by
# operator decision (2026-09-13): reading a pack by path on local disk is
# inspection, not use. They take a path, not a collection, encode nothing
# and write nothing to the node; its data enters the node only through
# discovery, import or adopt, which ask `licensing` (invariant 9). `verify`
# is also the check a pack gets BEFORE anyone acknowledges its terms.
# Do not "fix" them into the gate.
def cmd_pack_verify(args) -> int:
    from .pack import verify as pack_verify
    checks = args.checks.split(",") if args.checks else None
    report = pack_verify.run(Path(args.pack), checks=checks, sample=args.sample, seed=args.seed, log=print)
    return 0 if all(r["ok"] for r in report) else 1


def cmd_pack_status(args) -> int:
    from .pack import manifest as manifest_mod
    m = manifest_mod.load(Path(args.pack))
    signed = "signed" if m.get("signature") else "unsigned"
    print(f"{m['collection_id']} ({m['name']}, plugin {m['plugin']}, snapshot {m['snapshot']}, {signed})")
    print(f"license {m['license']['class']}; layers "
          + ", ".join(f"{l['chunker']['id']}@{l['chunker']['version']}[{l['rule']}]" for l in m["layers"]))
    print(f"{m.get('records', 0)} records, {m.get('chunks', 0)} chunks, {len(m['shards'])} shards, "
          f"{m.get('skipped', 0)} skipped")
    for name, s in sorted(m["slices"].items()):
        print(f"  {name}: {s['shards']} shard(s), {s['records']} records, {s['bytes'] >> 20} MiB")
    return 0


def cmd_pack_cat(args) -> int:
    """One record and its chunks as JSON — the text-editor test, by hand."""
    from .pack import manifest as manifest_mod, records as pack_records, verify as pack_verify
    pack_dir = Path(args.pack)
    m = manifest_mod.load(pack_dir)
    for s in m["shards"]:
        try:
            rec = pack_records.read_record(pack_dir / s["path"], args.uid)
        except KeyError:
            continue
        chunker = pack_verify._chunker_of(m, int(rec.get("layer") or 0))
        chunks = list(chunker(rec["uid"], int(rec["native_id"]),
                              [(a, b) for a, b in rec["sections"]])) if chunker else []
        print(json.dumps({"shard": s["path"], "record": rec, "chunks": chunks},
                         ensure_ascii=False, indent=1))
        return 0
    print(f"{args.uid}: not in this pack", file=sys.stderr)
    return 1


def cmd_pack_dump(args) -> int:
    from .pack import records as pack_records
    n = pack_records.dump_jsonl(Path(args.shard), sys.stdout.buffer)
    print(f"{n} records", file=sys.stderr)
    return 0


def cmd_vindex_build(args) -> int:
    """Build (or rebuild) a collection's local indexes now: the book_id
    index every write filters on, then the vector index queries probe."""
    from . import vector_index
    collection = _collection_by_name(args.collection)
    if not args.vector_only:
        out = vector_index.build_scalar(collection, log=print)
        if not out.get("built"):
            print(f"book_id index not built: {out.get('reason')}", file=sys.stderr)
            return 1
    if args.book_id_only:
        return 0
    out = vector_index.build(collection, index_type=args.type, partitions=args.partitions, log=print)
    if not out.get("built"):
        print(f"vector index not built: {out.get('reason')}", file=sys.stderr)
        return 1
    return 0


def cmd_vindex_prune(args) -> int:
    """Drop superseded Lance versions now, and report what it freed.

    The worker does this hourly per collection (vector_index.prune). This
    is for the case where it has not run for a long time and someone
    wants the disk back today: on the reference node 2026-09-10, pubmed
    held 29,007 versions costing 36.6 GiB of manifests, and one pass took
    it to 2 versions and freed 37.1 GiB in eleven minutes without
    touching a row.
    """
    from . import config, vector_index
    collection = _collection_by_name(args.collection)
    if args.keep_hours is not None:
        config.VECTOR_INDEX_KEEP_VERSIONS_H = float(args.keep_hours)
    st = vector_index.stats(collection)
    if st is None:
        print("no vector table")
        return 0
    size = vector_index.table_bytes(collection) or 0
    free = vector_index.free_bytes(collection)
    print(f"{st['rows']:,} rows, {size / 2**30:.1f} GiB on disk, "
          f"{(free or 0) / 2**30:.0f} GiB free")
    if args.force:
        # The estimate is a heuristic from one measurement; an operator
        # who knows the pool has room may say so. It still writes before
        # it deletes, so this can fill a full disk — that is the risk
        # being accepted, and it is named here rather than hidden.
        vector_index.headroom_ok = lambda *a, **k: True
    out = vector_index.prune(collection, log=print)
    if not out.get("pruned"):
        print(f"not pruned: {out.get('reason')}", file=sys.stderr)
        return 1
    after = vector_index.table_bytes(collection) or 0
    print(f"versions {out.get('versions_before')} -> {out.get('versions_after')}; "
          f"{(size - after) / 2**30:+.1f} GiB freed in {out.get('seconds')}s")
    st2 = vector_index.stats(collection)
    if st2 and st2["rows"] != st["rows"]:
        print(f"ROW COUNT CHANGED: {st['rows']} -> {st2['rows']} — investigate",
              file=sys.stderr)
        return 1
    return 0


def cmd_vindex_status(args) -> int:
    from . import config, vector_index
    collection = _collection_by_name(args.collection)
    st = vector_index.stats(collection)
    if st is None:
        print("no vector table")
        return 0
    if not st["indexed"]:
        print(f"{st['rows']} rows, NO vector index (flat scan per query; built automatically at "
              f"{config.VECTOR_INDEX_MIN_ROWS} rows, or `index vector build`)")
    else:
        print(f"{st['rows']} rows, index {st['name']} ({st['type']}), "
              f"{st['unindexed_rows']} rows not yet folded in")
    if st.get("book_id_indexed"):
        print("book_id index: yes")
    else:
        print("book_id index: NO — every delete-then-insert write scans the table "
              "(`index vector build --book-id-only`)")
    return 0


def cmd_backend_check(args) -> int:
    from .verify import backend_gate
    collection = _collection_by_name(args.collection)
    try:
        result = backend_gate.check(collection)
    except backend_gate.BackendUnavailable as e:
        print(f"UNAVAILABLE: {e}", file=sys.stderr)
        return 2
    except backend_gate.BackendGateError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    if result.get("recorded"):
        print(f"baseline recorded (dim {result['dim']}) — trust-on-first-use; "
              f"future checks compare against it")
    else:
        detail = " ".join(f"{k}={v:.4f}" for k, v in result["cosines"].items())
        print(f"PASS: endpoint reproduces its baseline ({detail})")
    return 0


def cmd_backend_rebaseline(args) -> int:
    """Explicit operator acceptance of a CHANGED backend. Existing vectors
    were made by the old one — accepting means planning their re-embed."""
    from .verify import backend_gate
    collection = _collection_by_name(args.collection)
    path = backend_gate.baseline_path(collection)
    existed = path.exists()
    if existed and not args.yes:
        print("A baseline already exists. Re-recording declares the CURRENT "
              "endpoint authoritative; vectors embedded under the old backend "
              "become foreign to every future query. Re-run with --yes if that "
              "is intended (and plan a re-embed).", file=sys.stderr)
        return 1
    try:
        baseline = backend_gate.record_baseline(collection)
    except backend_gate.BackendUnavailable as e:
        print(f"UNAVAILABLE: {e}", file=sys.stderr)
        return 2
    backend_gate.reset_cache()
    print(f"{'re-recorded' if existed else 'recorded'} baseline "
          f"(dim {baseline['dim']}, {len(baseline['probes'])} probes) at {path}")
    return 0



def cmd_torrent_publish(args) -> int:
    """Hash a collection's corpus into a .torrent and print its magnet.

    Reads every byte, so it is minutes on a large corpus and deliberately
    explicit rather than something the worker does on a timer.
    """
    from .acquire import torrent_publish
    from .acquire.spec import TransportError

    collection = _collection_by_name(args.collection)
    if _refuse_unacknowledged(collection, "a corpus is published as a torrent"):
        return 1
    out = Path(args.out) if args.out else (
        collection.manifest_path.parent / f"{collection.name}.torrent")
    # POLICY (spec.extras): extra formats are a local convenience and are
    # NEVER p2p material. Excluded UNCONDITIONALLY — not just when opted in,
    # because files from a past opt-in stay on disk after the opt-out.
    spec = ACQUIRE_REGISTRY.get(collection.plugin)
    exclude = tuple(pat for flt in (spec.extras.values() if spec else ())
                    for pat in flt.include)
    from .acquire import seeding
    con = db.connect(collection)

    # (root, out path, name, include patterns, slice) per torrent to publish
    jobs: list[tuple[Path, Path, str, tuple[str, ...], str | None]] = []
    per_language = bool(getattr(args, "per_language", False))
    per_slice = bool(getattr(args, "per_slice", False))
    collection_only = bool(getattr(args, "collection_only", False))
    recorded_members: list[dict] = []
    if collection_only:
        # the members were hashed earlier (their records carry everything
        # the index needs); only the index torrent is (re)built. Records
        # written before the `kind` field (5h's first day) are recognised
        # by their name — `<collection>-<slice>` — and completed from the
        # spec: slice and its include patterns.
        prefix = f"{collection.name}-"
        recorded_members = []
        for e in seeding.read_published(con):
            kind = e.get("kind")
            name = e.get("name") or ""
            if kind == "member" or (kind is None and name.startswith(prefix)
                                    and not name.endswith("-collection")):
                slice_name = e.get("slice") or name[len(prefix):]
                include = e.get("include") or list(
                    spec.profiles[slice_name].include
                    if spec and slice_name in spec.profiles else ())
                member = {**e, "kind": "member", "slice": slice_name, "include": include,
                          "peer_hint": e.get("peer_hint") or args.peer_hint or ""}
                recorded_members.append(member)
                if member != e:
                    seeding.record_published(con, member)   # upgrade the legacy record
        if not recorded_members:
            print("REFUSED: no member torrents recorded for this collection — publish "
                  "with --per-language first", file=sys.stderr)
            return 1
        if not args.peer_hint:
            args.peer_hint = next((e.get("peer_hint") for e in recorded_members
                                   if e.get("peer_hint")), "")
    elif per_language:
        # STATUS 5h: one torrent per data/<lang>, ALL rooted at `data` so a
        # peer's files land in the plugin's layout and the torrents combine
        data_root = collection.corpus_dir / "data"
        if not data_root.is_dir():
            print(f"REFUSED: {data_root} is not a directory (per-language needs "
                  f"the data/<lang>/ layout)", file=sys.stderr)
            return 1
        wanted = {s.strip() for s in args.languages.split(",") if s.strip()}
        langs = sorted(p.name for p in data_root.iterdir()
                       if p.is_dir() and not p.name.startswith(".")
                       and (not wanted or p.name in wanted))
        if not langs:
            print("REFUSED: no language directories under data/", file=sys.stderr)
            return 1
        for lang in langs:
            out_lang = (Path(args.out).with_name(f"{Path(args.out).stem}-{lang}.torrent")
                        if args.out else
                        collection.manifest_path.parent / f"{collection.name}-{lang}.torrent")
            jobs.append((data_root, out_lang, f"{collection.name}-{lang}",
                         (f"*data/{lang}/*",), lang))
    elif per_slice:
        # pack-format.md §10: member = slice x kind. One torrent per
        # records/<slice>, all rooted at the PACK directory so a peer's
        # files land in the pack layout and the members combine; plus one
        # small "manifest" member (manifest, validation plan, terms).
        from .pack import manifest as pack_manifest
        try:
            m = pack_manifest.load(collection.corpus_dir)
        except pack_manifest.ManifestError as e:
            print(f"REFUSED: {e} (per-slice needs a pack corpus)", file=sys.stderr)
            return 1
        base = collection.manifest_path.parent
        jobs.append((collection.corpus_dir, base / f"{collection.name}-manifest.torrent",
                     f"{collection.name}-manifest", ("manifest.json", "validation.json", "TERMS.txt"),
                     "manifest"))
        for slice_name in sorted(m.get("slices") or {}):
            safe = slice_name.replace("/", "+")
            jobs.append((collection.corpus_dir, base / f"{collection.name}-{safe}.torrent",
                         f"{collection.name}-{safe}", (f"*records/{slice_name}/*",), slice_name))
    else:
        root = collection.corpus_dir / args.subdir if args.subdir else collection.corpus_dir
        jobs.append((root, out, collection.name, (), None))

    from .acquire import torrent_collection
    members: list[dict] = []
    for root, out_path, name, includes, slice_name in jobs:
        try:
            published = torrent_publish.publish_corpus(
                root, out_path, name=name,
                trackers=tuple(args.tracker or ()),
                web_seeds=tuple(args.web_seed or ()),
                comment=f"louise-rag collection {collection.name}",
                exclude_patterns=exclude, include_patterns=includes)
        except TransportError as e:
            print(f"REFUSED: {e}", file=sys.stderr)
            return 1
        # the worker's seeder reads this record and serves the torrent from
        # `root` (app/acquire/seeding.py); deleting the .torrent stops it
        entry = {**published.as_catalog_entry(),
                 "torrent_path": str(published.torrent_path), "root": str(root),
                 "kind": "member" if (per_language or per_slice) else "corpus",
                 "slice": slice_name,
                 # the spec's own patterns for this slice: what a peer's
                 # profile resolves to, so it can pick members by profile
                 "include": list((spec.profiles.get(slice_name).include
                                  if spec and slice_name in spec.profiles else includes)),
                 "peer_hint": args.peer_hint or ""}
        seeding.record_published(con, entry)
        members.append(entry)
        magnet = torrent_collection.with_hint(published.magnet, args.peer_hint)
        print(f"torrent:   {published.torrent_path}")
        print(f"root:      {root}")
        print(f"files:     {published.files}")
        print(f"bytes:     {published.total_bytes:,}")
        print(f"info hash: {published.info_hash}")
        print(f"magnet:    {magnet}")
        print()
    print("Recorded for the worker's seeder (starts within its next telemetry "
          "beat while the node runs, downloads/seeding only, or seeding only).")
    if per_language or per_slice or collection_only:
        # THE TORRENT COLLECTION (STATUS 5l): one index torrent — a tiny
        # directory of collection.json plus every member .torrent — whose
        # magnet is the only thing a peer needs; its profile picks members.
        members = members or recorded_members
        index_dir = collection.manifest_path.parent / "collection"
        torrent_collection.write_index(index_dir, collection=collection.name,
                                       plugin=collection.plugin, members=members,
                                       peer_hint=args.peer_hint or "")
        index_out = collection.manifest_path.parent / f"{collection.name}.collection.torrent"
        try:
            index_pub = torrent_publish.publish_corpus(
                index_dir, index_out, name=f"{collection.name}-collection",
                trackers=tuple(args.tracker or ()),
                comment=f"louise-rag torrent collection {collection.name}")
        except TransportError as e:
            print(f"REFUSED (collection index): {e}", file=sys.stderr)
            return 1
        seeding.record_published(con, {
            **index_pub.as_catalog_entry(),
            "torrent_path": str(index_pub.torrent_path), "root": str(index_dir),
            "kind": "collection", "slice": None,
            "members": [m["info_hash"] for m in members],
            "peer_hint": args.peer_hint or ""})
        magnet = torrent_collection.with_hint(index_pub.magnet, args.peer_hint)
        print(f"TORRENT COLLECTION: {index_pub.torrent_path} ({len(members)} members)")
        print("Give a peer THIS magnet alone — its profile chooses the languages, and")
        print("growing the profile later adds members without touching its TOML:")
        print(magnet)
    if len(jobs) == 1:
        print("Give a peer either the .torrent file above or this magnet"
              + (" (with the direct-peer hint):" if args.peer_hint else ":"))
        print(magnet)
    if not args.tracker:
        print()
        print("No tracker is embedded. A peer that cannot reach this node "
              "through the DHT needs it added directly — see "
              "docs/notes/p2p-discovery.md.")
    return 0


def cmd_torrent_seed(args) -> int:
    """Serve an already-published torrent from the corpus on disk, until
    interrupted. libtorrent re-checks the pieces first, which doubles as a
    free integrity audit of what this node claims to hold."""
    from .acquire import torrent_publish, transport_torrent

    collection = _collection_by_name(args.collection)
    if _refuse_unacknowledged(collection, "a corpus is seeded to peers"):
        return 1
    torrent = Path(args.torrent) if args.torrent else (
        collection.manifest_path.parent / f"{collection.name}.torrent")
    if not torrent.is_file():
        print(f"REFUSED: {torrent} does not exist — run `torrent publish` first",
              file=sys.stderr)
        return 1

    handle = torrent_publish.seed(torrent, collection.corpus_dir)
    session = transport_torrent.get_session()
    print(f"seeding {collection.name} from {collection.corpus_dir}")
    print(f"listening on {session.listen_port()}  (Ctrl-C to stop)")

    stop = threading.Event()
    _route_stop_signals(lambda *_: stop.set())
    while not stop.wait(5):
        st = handle.status()
        print(f"  peers={st.num_peers} up={st.upload_payload_rate / 1000:.0f} kB/s "
              f"uploaded={st.total_payload_upload / 1e6:.1f} MB "
              f"state={st.state}", flush=True)
    transport_torrent.shutdown_session()
    print("stopped seeding")
    return 0



# ---------------- snapshots and deltas ----------------

def _snapshot_or_fail(collection):
    if not collection.snapshot:
        raise SystemExit(
            f"{collection.name} pins no snapshot. Add a `snapshot` line to "
            f"its TOML — an unpinned collection tracks whatever the origin "
            f"calls latest, which cannot be frozen, diffed or shared.")
    return collection.snapshot


def cmd_snapshot_record(args) -> int:
    """Freeze what this collection currently holds, as a manifest file."""
    from . import snapshots

    collection = _collection_by_name(args.collection)
    snapshot = args.snapshot or _snapshot_or_fail(collection)
    con = db.connect(collection)
    path, digest = snapshots.record(con, collection, snapshot)
    entries = sum(1 for _ in snapshots.read_manifest(path))
    print(f"snapshot:  {snapshot}")
    print(f"manifest:  {path}")
    print(f"documents: {entries:,}")
    print(f"digest:    {digest}")
    return 0


def cmd_snapshot_list(args) -> int:
    from . import snapshots

    collection = _collection_by_name(args.collection)
    rows = snapshots.recorded(collection)
    if not rows:
        print(f"{collection.name}: no snapshots recorded "
              f"(run: snapshot record {collection.name})")
        return 0
    print("snapshot          documents  digest")
    for snapshot, path in rows:
        entries = sum(1 for _ in snapshots.read_manifest(path))
        marker = " *" if snapshot == collection.snapshot else ""
        digest = snapshots.manifest_digest(path)[:32]
        print(f"{snapshot:16} {entries:>10,}  {digest}{marker}")
    if collection.snapshot:
        print()
        print("* = the snapshot this collection is pinned to")
    return 0


def cmd_snapshot_delta(args) -> int:
    """Diff two recorded snapshots into a shareable delta."""
    from . import config, snapshots

    collection = _collection_by_name(args.collection)
    # invariant 9, "not exchanged": the delta is written to be shared
    if _refuse_unacknowledged(collection, "a shareable delta of it is written"):
        return 1
    parent = snapshots.manifest_path_for(collection, args.from_snapshot)
    child = snapshots.manifest_path_for(collection, args.to_snapshot)
    for path, name in ((parent, args.from_snapshot), (child, args.to_snapshot)):
        if not path.is_file():
            raise SystemExit(
                f"no recorded manifest for {name!r} at {path} — "
                f"`snapshot list {collection.name}` shows what exists")
    out = Path(args.out) if args.out else (
        snapshots.snapshot_dir(collection)
        / f"{args.from_snapshot}..{args.to_snapshot}.delta.jsonl")

    con = db.connect(collection)
    row = con.execute("SELECT space_id FROM space WHERE id=1").fetchone()
    header = snapshots.build_delta(
        parent, child, out,
        collection=collection.name, plugin=collection.plugin,
        parent_snapshot=args.from_snapshot, child_snapshot=args.to_snapshot,
        chunk_version=config.CHUNK_VERSION,
        cleaning_version=config.CLEANING_VERSION,
        space_id=(row["space_id"] if row else None))

    counts = header.counts
    print(f"delta:   {out}")
    print(f"added:   {counts.get('added', 0):,}")
    print(f"changed: {counts.get('changed', 0):,}")
    print(f"removed: {counts.get('removed', 0):,}  (tombstones)")
    print(f"parent:  {header.parent_digest[:32]}")
    print()
    print("Vectors for the added+changed documents:")
    print(f"  python -m app.cli snapshot vectors {collection.name} "
          f"--delta {out}")
    return 0


def cmd_snapshot_vectors(args) -> int:
    """Export a VECTOR delta: a signed bundle covering exactly the documents
    a corpus delta added or changed.

    Deliberately an ordinary bundle rather than a new artifact — it inherits
    the signature, the per-uid raw hashes that bind vectors to a text
    revision, the verification ladder and genesis admission, none of which
    would be worth rebuilding.
    """
    from . import snapshots
    from .bundles.export import ExportError, export_bundle
    from .bundles.keys import NodeKeyError

    collection = _collection_by_name(args.collection)
    # invariant 9, "not exchanged": the vector delta is an ordinary bundle
    if _refuse_unacknowledged(collection, "its vectors are exported as a bundle"):
        return 1
    delta = Path(args.delta)
    if not delta.is_file():
        raise SystemExit(f"no such delta: {delta}")
    uids = snapshots.vector_uids(delta)
    if not uids:
        print("this delta adds or changes no documents — nothing to embed, "
              "and nothing to ship")
        return 0

    out = Path(args.out) if args.out else (
        snapshots.snapshot_dir(collection) / f"{delta.stem}.vectors")
    con = db.connect(collection)
    try:
        summary = export_bundle(con, collection, uids, out)
    except (ExportError, NodeKeyError) as e:
        # A missing signing key is an ordinary operator situation on a
        # consumer-role node, not a bug: a bundle is signed or it is not
        # shareable, so say so rather than raising a traceback at someone.
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    covered = len(summary.get("uids", ()))
    print(f"bundle:    {out}")
    print(f"documents: {len(uids):,} requested, {covered:,} covered")
    print(f"chunks:    {summary.get('chunk_count', 0):,}")
    if len(uids) - covered > 0:
        print()
        print(f"{len(uids) - covered:,} of them have no vectors yet — embed "
              f"them first, then re-export. A partial vector delta is valid; "
              f"the consumer simply gets less.")
    print()
    print("A consumer applies the CORPUS delta first and the vector delta "
          "second. Reversed, the possession check compares new vectors "
          "against old text and correctly refuses every document.")
    return 0


def _deprecated(fn, old: str, new: str):
    """Name the replacement on stderr, and run the old command anyway.

    07 §7.6 asks for more than this — "each dispatching to the new function"
    — and for these four that is NOT YET SAFE, which is why it is a notice
    and not a redirect:

      * `pack build` takes `--out DIR` and writes a pack there; `hold
        records build` writes into the collection's DRAG under `--root`.
        Dispatching would ignore `--out` and write somewhere else, on a
        node that is running.
      * `snapshot record` and `corpus status` read the legacy manifest;
        their replacements read a Codex, and no collection here has one yet.

    Both halves become one implementation when each step finishes wrapping
    the old code — which is what migration steps 2 and 5 are for. Until
    then the user-facing half lands, because a user on an old name learning
    the new one is worth having a release early, and the `louise <old …>`
    aliases in `app/louise.py` DO dispatch, because there the new command is
    the only implementation.
    """
    def run(args):
        print(f"deprecated: use louise {new}", file=sys.stderr)
        return fn(args)
    run.__name__ = getattr(fn, "__name__", "run")
    return run


def main(argv=None) -> int:
    # A hangup is Ctrl-C until a command routes both to its stop
    # (_route_stop_signals). rsync leads its own session, so a command that
    # lists without a stop (`corpus status`, `corpus add --dry-run`) died by
    # the default SIGHUP while its rsync --list-only ran on; as a
    # KeyboardInterrupt it reaches the transport, which kills rsync's group
    # before letting it past.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGHUP, signal.default_int_handler)
    p = argparse.ArgumentParser(prog="app.cli")
    top = p.add_subparsers(dest="command", required=True)

    contribute_parser = top.add_parser("contribute", help="compute vectors for the network")
    contribute_parser.add_argument("collection")
    contribute_parser.add_argument("coordinator")
    contribute_parser.add_argument("--count", type=int, default=64)
    contribute_parser.add_argument("--report-demand", action="store_true",
                                    help="publish this node's lazy queue instead")
    contribute_parser.set_defaults(func=cmd_contribute)

    bundle_parser = top.add_parser("bundle", help="signed vector bundles")
    bundle = bundle_parser.add_subparsers(dest="verb", required=True)

    b_keygen = bundle.add_parser("keygen")
    b_keygen.add_argument("path")
    b_keygen.set_defaults(func=cmd_bundle_keygen)

    b_export = bundle.add_parser("export")
    b_export.add_argument("collection")
    b_export.add_argument("dest")
    b_export.add_argument("--uid", dest="uids", action="append", default=[])
    b_export.add_argument("--limit", type=int, default=100)
    b_export.set_defaults(func=cmd_bundle_export)

    b_import = bundle.add_parser("import")
    b_import.add_argument("collection")
    b_import.add_argument("path")
    b_import.add_argument("--no-first-use", action="store_true",
                          help="refuse an unknown producer instead of trusting it")
    b_import.set_defaults(func=cmd_bundle_import)

    b_trust = bundle.add_parser("trust")
    b_trust.add_argument("collection")
    b_trust.add_argument("pubkey")
    b_trust.add_argument("--quarantine", action="store_true")
    b_trust.add_argument("--reason")
    b_trust.set_defaults(func=cmd_bundle_trust)

    b_status = bundle.add_parser("status")
    b_status.add_argument("collection")
    b_status.add_argument("--limit", type=int, default=20)
    b_status.set_defaults(func=cmd_bundle_status)

    b_resweep = bundle.add_parser("resweep")
    b_resweep.add_argument("collection")
    b_resweep.set_defaults(func=cmd_bundle_resweep)

    genesis_parser = top.add_parser("genesis", help="import published vector archives")
    genesis_sub = genesis_parser.add_subparsers(dest="verb", required=True)

    g_import = genesis_sub.add_parser("import")
    g_import.add_argument("collection")
    g_import.add_argument("--archive", help="known archive id (e.g. medcpt-pubmed)")
    g_import.add_argument("--parquet", help="local parquet file with id+vector columns")
    g_import.add_argument("--archive-id", help="provenance label for --parquet imports")
    g_import.add_argument("--uid-prefix", help="id -> uid prefix (default: '<plugin>:')")
    g_import.add_argument("--shards", help="comma-separated shard subset")
    g_import.add_argument("--keep-cache", action="store_true",
                           help="keep downloaded shard files after applying")
    g_import.set_defaults(func=cmd_genesis_import)

    g_status = genesis_sub.add_parser("status")
    g_status.add_argument("collection")
    g_status.set_defaults(func=cmd_genesis_status)

    g_retry = genesis_sub.add_parser("retry", help="put rejected shards back in line")
    g_retry.add_argument("collection")
    g_retry.add_argument("--shard", help="one shard (default: every rejected shard)")
    g_retry.set_defaults(func=cmd_genesis_retry)

    g_recheck = genesis_sub.add_parser(
        "recheck", help="reopen final shards that still have unserved documents")
    g_recheck.add_argument("collection")
    g_recheck.set_defaults(func=cmd_genesis_recheck)

    g_recount = genesis_sub.add_parser(
        "recount", help="recompute the archive ledger's counts from chunk provenance "
                        "(dry run unless --yes)")
    g_recount.add_argument("collection")
    g_recount.add_argument("--yes", action="store_true",
                           help="write the recounted docs/chunks (stop the worker first; "
                                "it holds the manifest's write lock as long as the dry "
                                "run's walk took)")
    g_recount.set_defaults(func=cmd_genesis_recount)

    pack_parser = top.add_parser("pack", help="the pack: build, verify, inspect (docs/notes/pack-format.md)")
    pack_sub = pack_parser.add_subparsers(dest="verb", required=True)
    p_build = pack_sub.add_parser("build", help="pack a collection's documents into record shards")
    p_build.add_argument("collection")
    p_build.add_argument("--out", required=True, help="pack directory (a new dataset)")
    p_build.add_argument("--name")
    p_build.add_argument("--snapshot")
    p_build.add_argument("--records-per-shard", type=int)
    p_build.add_argument("--until", help="a CLOSED snapshot: keep records that existed at the "
                                         "origin on this date (YYYY-MM-DD) and nothing later — "
                                         "gutenberg by release date, pubmed by DateRevised")
    p_build.add_argument("--unsigned", action="store_true")
    p_build.set_defaults(func=_deprecated(
        cmd_pack_build, "pack build", "hold records build"))
    p_verify = pack_sub.add_parser("verify", help="run the pack's validation plan")
    p_verify.add_argument("pack")
    p_verify.add_argument("--checks", help="comma-separated subset: roots,records,chunks,license,signature")
    p_verify.add_argument("--sample", type=float)
    p_verify.add_argument("--seed", type=int, default=0)
    p_verify.set_defaults(func=cmd_pack_verify)
    p_status = pack_sub.add_parser("status", help="what a pack holds")
    p_status.add_argument("pack")
    p_status.set_defaults(func=cmd_pack_status)
    p_cat = pack_sub.add_parser("cat", help="print one record and its chunks as JSON")
    p_cat.add_argument("pack")
    p_cat.add_argument("uid")
    p_cat.set_defaults(func=cmd_pack_cat)
    p_dump = pack_sub.add_parser("dump", help="stream a shard as plain JSONL")
    p_dump.add_argument("shard")
    p_dump.set_defaults(func=cmd_pack_dump)

    health_parser = top.add_parser(
        "health", help="is the pipeline stuck, and if so why (exit 1 if blocked)")
    health_parser.add_argument("collection", nargs="?",
                               help="one collection (default: every one)")
    health_parser.add_argument(
        "--errors", action="store_true",
        help="also count errored documents (43 s on a 40M manifest: "
             "`error` is in no index, so proving there are none walks "
             "the whole scope range)")
    health_parser.set_defaults(func=cmd_health)

    vindex_parser = top.add_parser("index", help="local indexes")
    vindex_sub = vindex_parser.add_subparsers(dest="verb", required=True)
    v_vec = vindex_sub.add_parser("vector", help="the local vector (IVF) index")
    v_sub = v_vec.add_subparsers(dest="action", required=True)
    v_build = v_sub.add_parser("build", help="build or rebuild it now (book_id, then vector)")
    v_build.add_argument("collection")
    v_build.add_argument("--type", choices=["ivf_flat", "ivf_pq"])
    v_build.add_argument("--partitions", type=int)
    v_build.add_argument("--book-id-only", action="store_true",
                          help="only the book_id index every write filters on (minutes)")
    v_build.add_argument("--vector-only", action="store_true",
                          help="skip the book_id index")
    v_build.set_defaults(func=cmd_vindex_build)
    v_status = v_sub.add_parser("status")
    v_status.add_argument("collection")
    v_status.set_defaults(func=cmd_vindex_status)

    v_prune = v_sub.add_parser(
        "prune", help="drop superseded Lance versions and compact (frees disk)")
    v_prune.add_argument("collection")
    v_prune.add_argument("--keep-hours", type=float, default=None,
                          help="retention window; 0 keeps only the current "
                               "version (default: VECTOR_INDEX_KEEP_VERSIONS_H)")
    v_prune.add_argument("--force", action="store_true",
                          help="run even when free space is below what a "
                               "compaction of this table is estimated to need")
    v_prune.set_defaults(func=cmd_vindex_prune)

    backend_parser = top.add_parser("backend", help="embedding backend gate")
    backend = backend_parser.add_subparsers(dest="verb", required=True)

    be_check = backend.add_parser("check", help="verify the endpoint against its baseline")
    be_check.add_argument("collection")
    be_check.set_defaults(func=cmd_backend_check)

    be_rebase = backend.add_parser("rebaseline",
                                    help="accept the current endpoint as the new baseline")
    be_rebase.add_argument("collection")
    be_rebase.add_argument("--yes", action="store_true")
    be_rebase.set_defaults(func=cmd_backend_rebaseline)

    snapshot_parser = top.add_parser(
        "snapshot", help="freeze a corpus and diff frozen snapshots")
    snapshot = snapshot_parser.add_subparsers(dest="verb", required=True)

    s_rec = snapshot.add_parser("record", help="freeze current holdings")
    s_rec.add_argument("collection")
    s_rec.add_argument("--snapshot",
                       help="id to record under (default: the collection pin)")
    s_rec.set_defaults(func=_deprecated(
        cmd_snapshot_record, "snapshot record", "hold snapshot create"))

    s_list = snapshot.add_parser("list", help="recorded snapshots")
    s_list.add_argument("collection")
    s_list.set_defaults(func=_deprecated(
        cmd_snapshot_list, "snapshot list", "hold snapshot list"))

    s_delta = snapshot.add_parser("delta", help="diff two recorded snapshots")
    s_delta.add_argument("collection")
    s_delta.add_argument("--from", dest="from_snapshot", required=True)
    s_delta.add_argument("--to", dest="to_snapshot", required=True)
    s_delta.add_argument("--out")
    s_delta.set_defaults(func=cmd_snapshot_delta)

    s_vec = snapshot.add_parser(
        "vectors", help="signed bundle covering a delta's documents")
    s_vec.add_argument("collection")
    s_vec.add_argument("--delta", required=True)
    s_vec.add_argument("--out")
    s_vec.set_defaults(func=cmd_snapshot_vectors)

    torrent_parser = top.add_parser(
        "torrent", help="publish and seed a corpus over BitTorrent")
    torrent = torrent_parser.add_subparsers(dest="verb", required=True)

    t_pub = torrent.add_parser("publish", help="hash a corpus into a .torrent")
    t_pub.add_argument("collection")
    t_pub.add_argument("--out", help="where to write the .torrent "
                                      "(default: beside the manifest)")
    t_pub.add_argument("--tracker", action="append",
                       help="tracker announce URL; repeatable, none by default")
    t_pub.add_argument("--web-seed", action="append",
                       help="BEP 19 HTTP seed — the origin URL of this corpus, "
                            "so a peer can finish even with nobody seeding")
    t_pub.add_argument("--subdir", default="",
                       help="publish this SUBDIRECTORY of the corpus (its name "
                            "becomes the torrent root, so a peer's corpus_dir/"
                            "<subdir>/... lands in the plugin's own layout — "
                            "e.g. 'data' for the pre-embedded corpus)")
    t_pub.add_argument("--peer-hint", default="",
                       help="host:port a peer can dial directly (no tracker/DHT): "
                            "appended to the printed magnet as x.pe=")
    t_pub.add_argument("--per-language", action="store_true",
                       help="pre-embedded corpus (STATUS 5h): one torrent PER "
                            "LANGUAGE directory under data/, all with root 'data', "
                            "so a peer fetches and starts on one language while "
                            "the rest arrives; every torrent is recorded for seeding")
    t_pub.add_argument("--languages", default="",
                       help="with --per-language: only these (comma-separated), "
                            "default every data/<lang> directory")
    t_pub.add_argument("--per-slice", action="store_true",
                       help="a PACK corpus (docs/notes/pack-format.md): one torrent per "
                            "records/<slice> plus a small manifest member, all rooted "
                            "at the pack, and the torrent collection over them")
    t_pub.add_argument("--collection-only", action="store_true",
                       help="(re)build and publish only the TORRENT COLLECTION index "
                            "from the members already recorded — no re-hashing")
    t_pub.set_defaults(func=cmd_torrent_publish)

    t_seed = torrent.add_parser("seed", help="serve a published torrent")
    t_seed.add_argument("collection")
    t_seed.add_argument("--torrent", help="path to the .torrent "
                                           "(default: beside the manifest)")
    t_seed.set_defaults(func=cmd_torrent_seed)

    corpus_parser = top.add_parser("corpus")
    corpus = corpus_parser.add_subparsers(dest="verb", required=True)

    p_add = corpus.add_parser("add")
    p_add.add_argument("collection")
    p_add.add_argument("--profile", default="demo")
    p_add.add_argument("--ack-terms", help="path to a terms text file to acknowledge")
    p_add.add_argument("--dry-run", action="store_true")
    p_add.set_defaults(func=cmd_add)

    p_grow = corpus.add_parser("grow")
    p_grow.add_argument("collection")
    p_grow.add_argument("--profile", required=True)
    p_grow.add_argument("--ack-terms")
    p_grow.add_argument("--dry-run", action="store_true")
    p_grow.set_defaults(func=cmd_grow)

    p_status = corpus.add_parser("status")
    p_status.add_argument("collection", nargs="?")
    p_status.set_defaults(func=_deprecated(
        cmd_status, "corpus status", "hold collection show"))

    embed_parser = top.add_parser("embed", help="embedding recipe management")
    embed_sub = embed_parser.add_subparsers(dest="verb", required=True)
    e_mat = embed_sub.add_parser(
        "materialize",
        help="write the collection's EFFECTIVE embed recipe into its TOML "
             "(env→TOML migration: Tier-2 desired state, byte-equal recipe, "
             "space pin unchanged)")
    e_mat.add_argument("collection")
    e_mat.set_defaults(func=cmd_embed_materialize)

    space_parser = top.add_parser("space", help="embedding space pins")
    space_sub = space_parser.add_subparsers(dest="verb", required=True)
    sp_repin = space_sub.add_parser(
        "repin",
        help="adopt a new space for a collection: drop the pin, reset embed "
             "and lexical marks, clear vectors (operator-driven; worker stopped)")
    sp_repin.add_argument("collection")
    sp_repin.add_argument("--yes", action="store_true",
                          help="actually do it (without it: report the plan)")
    sp_repin.set_defaults(func=cmd_space_repin)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

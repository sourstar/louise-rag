"""CLI: python -m app.cli corpus add|status|grow — architecture-v2.md §14's
verbs. No console-scripts entry point exists in this repo (every process is
launched `python -m app.<module>`, see docker-compose.yaml's worker command)
so this is the concrete realization of the doc's "app corpus add|status|grow"
shorthand.
"""
import argparse
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

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
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
    if aborted:
        print("interrupted — partial downloads were checkpointed; re-run to resume")
    # A clean shutdown exits 0 (CLAUDE.md invariant 2). Only genuine failures
    # are a nonzero exit.
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
    from .web import _write_collection_toml
    _write_collection_toml(
        toml_path, plugin=collection.plugin, profile=collection.profile,
        acquire_enabled=collection.acquire_enabled,
        corpus_dir=collection.corpus_dir,
        manifest=collection.manifest_path, fts=collection.fts_dir,
        lancedb=collection.lance_dir, license_ack=collection.license_ack,
        genesis_archive=collection.genesis_archive, embed=recipe)
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
        con.execute("UPDATE genesis_imports SET state='fetched', docs=0, chunks=0, "
                    "pending=NULL, next_pass_at=0, detail=NULL WHERE state IN "
                    "('applying','applied')")
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
    con = db.connect(collection)
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
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
    con = db.connect(collection)
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
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
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
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

    # (root, out path, name, include patterns) per torrent to publish
    jobs: list[tuple[Path, Path, str, tuple[str, ...]]] = []
    if getattr(args, "per_language", False):
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
                         (f"*data/{lang}/*",)))
    else:
        root = collection.corpus_dir / args.subdir if args.subdir else collection.corpus_dir
        jobs.append((root, out, collection.name, ()))

    for root, out_path, name, includes in jobs:
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
        seeding.record_published(con, {
            **published.as_catalog_entry(),
            "torrent_path": str(published.torrent_path), "root": str(root)})
        magnet = published.magnet
        if args.peer_hint:
            magnet += f"&x.pe={args.peer_hint}"
        print(f"torrent:   {published.torrent_path}")
        print(f"root:      {root}")
        print(f"files:     {published.files}")
        print(f"bytes:     {published.total_bytes:,}")
        print(f"info hash: {published.info_hash}")
        print(f"magnet:    {magnet}")
        print()
    print("Recorded for the worker's seeder (starts within its next telemetry "
          "beat while the node runs, downloads/seeding only, or seeding only).")
    if len(jobs) > 1:
        print("A peer lists these magnets in its TOML `peer_source = [...]` in the "
              "order it wants them; they are fetched one after the other.")
    if len(jobs) == 1:
        print("Give a peer either the .torrent file above or this magnet"
              + (" (with the direct-peer hint):" if args.peer_hint else ":"))
        print(magnet)
    if not args.tracker:
        print()
        print("No tracker is embedded. A peer that cannot reach this node "
              "through the DHT needs it added directly — see "
              "docs/p2p-discovery.md.")
    return 0


def cmd_torrent_seed(args) -> int:
    """Serve an already-published torrent from the corpus on disk, until
    interrupted. libtorrent re-checks the pieces first, which doubles as a
    free integrity audit of what this node claims to hold."""
    import signal as _signal
    from .acquire import torrent_publish, transport_torrent

    collection = _collection_by_name(args.collection)
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
    _signal.signal(_signal.SIGINT, lambda *_: stop.set())
    _signal.signal(_signal.SIGTERM, lambda *_: stop.set())
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


def main(argv=None) -> int:
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
    s_rec.set_defaults(func=cmd_snapshot_record)

    s_list = snapshot.add_parser("list", help="recorded snapshots")
    s_list.add_argument("collection")
    s_list.set_defaults(func=cmd_snapshot_list)

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
    p_status.set_defaults(func=cmd_status)

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

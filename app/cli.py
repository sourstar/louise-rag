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

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

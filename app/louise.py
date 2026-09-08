"""louise — the command line. docs/07-commands.md.

    louise hold collection create <plugin> --recipe FILE [--space-from CODEX]
                                    [--platform-image D] [--root DIR]
    louise hold collection derive <codex> (--space-from CODEX | --space FILE)
                                    [--layers FILE] [--update FILE] [--work FILE] --accept
    louise hold collection continue <codex> --platform-image D --continues-through <snapshot>
                                    [--origin FILE] [--origin-reachable-until <snapshot>]
    louise hold collection replace <codex> --defect FILE [--platform-image D] [--space FILE]
    louise hold collection list
    louise hold collection show <codex>
    louise hold records build <collection> --corpus DIR [--snapshot ID]
    louise hold records validation <collection>
    louise hold snapshot create <collection> [--snapshot ID]
    louise hold snapshot list <collection>

Only this branch of the tree exists so far. The rest arrives branch by
branch, each with its tests, and the old `python -m app.cli` keeps working
until §7.6's migration is complete.

One flag of §7.3's tree is accepted and refused here rather than
silently doing something else, because what it needs is not built yet:
`--encoder-image` / `--encoder-weights`, which must re-embed the
calibration set to fill the space's `calibration` and `envelope` (migration
step 3). Until then a new space is given whole, by `--space` or
`--space-from`.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tomllib
from pathlib import Path

from app.hold import codex as cx, collection as col, template as tpl
from app.hold.codex import CodexError, Defect, Origin, Update, Work

DEFAULT_ROOT = Path(os.environ.get("LOUISE_ROOT", "collections"))


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True))


def _json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _not_yet(flag: str, step: str) -> None:
    raise CodexError(f"{flag} is not built yet: it needs {step}. Give the whole "
                     f"space with --space or --space-from until it is.")


def _recipe(path: str) -> dict:
    """A recipe is TOML (07 §7.3). JSON is accepted for a file that is
    already a whole Codex — what `create` took before the template existed,
    and what a test fixture is easiest to write."""
    p = Path(path)
    if p.suffix == ".json":
        return _json(path)
    return tomllib.loads(p.read_text(encoding="utf-8"))


def cmd_create(a) -> int:
    if a.plugin is None:
        # the pre-template form: the file IS a Codex
        spec = _json(a.recipe)
        spec.setdefault("kind", cx.CODEX_KIND)
        spec.setdefault("version", cx.CODEX_VERSION)
        print(col.create(a.root, cx.from_dict(spec)).collection_id)
        return 0

    recipe = _recipe(a.recipe)
    if a.space_from:
        source = col.read(a.root, a.space_from)
        want = recipe.get("cleaning_version", 1)
        if source.cleaning_version != want:
            raise CodexError(
                f"--space-from names a Codex whose cleaning_version is "
                f"{source.cleaning_version} and this collection's is {want}. "
                f"The flag exists so that two collections land in one space by "
                f"construction (02 §2.7), and it copies no cleaner version the "
                f"plugin could not run")
        space = source.space.to_dict()
        # The two computed fields do not travel: `create` re-embeds
        # the set with this node's encoder and writes its OWN
        # reference. Neither is inside `space_id` (02 §2.4), so the
        # two collections still land in one space by construction —
        # and a difference here is real news about the encoder build,
        # which is why it is re-measured rather than copied.
        drifted = (space.pop("envelope"), space["calibration"].pop("vectors"))
        recipe["space"] = space
        recipe["layers"] = [l.to_dict() for l in source.layers]

    credits = tpl.expand_archives(recipe.get("archives") or ())
    # The creator's key: 09 §9.12 makes the calibration member's `key`
    # header required and §9.4 lists its signature among a snapshot's files
    # without allowing null, so there is no unsigned collection. Loaded by
    # PATH (invariant 8) and never printed.
    from app.bundles import keys as node_keys
    codex = col.create_from_recipe(a.root, a.plugin, recipe,
                                   key=node_keys.load_signing_key(), now=_utc_now(),
                                   platform_image=a.platform_image,
                                   embed=_embed_for_recipe(recipe["space"]),
                                   credits=credits)
    if a.space_from and (codex.space.envelope,
                         codex.space.calibration.vectors) != drifted:
        print(f"warning: --space-from {a.space_from} recorded reference "
              f"vectors {drifted[1]} and this node's encoder produced "
              f"{codex.space.calibration.vectors}. Both collections are "
              f"in space {codex.space_id} — calibration is not inside "
              f"space_id — but the two builds are not the same build.",
              file=sys.stderr)
    print(codex.collection_id)
    return 0


def _utc_now() -> str:
    """09 §9.1's time: RFC 3339 in UTC to the second.

    THE ONE PLACE the clock is read. Nothing under `app/hold/` reads it —
    every signed body takes its `time` from a caller — so that a test can
    pin what was signed, and so that two objects signed in one command
    cannot disagree about when it ran.
    """
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


def _embed_for_recipe(space: dict):
    """The encoder `create` embeds the calibration set with (06 §6.6).

    There is no Codex yet — the two hashes this produces are Codex
    fields — so the endpoint is resolved from the RECIPE's space block.
    Same three-step resolution as `_recipe_for`, and for now the same
    single available step: EMBED_URL.

    An unreachable encoder here is not `GateDeferred`: there is no
    collection to defer a check on. It is a plain refusal to create.
    """
    from app import collections as collections_mod, embeddings

    url = os.environ.get("EMBED_URL")
    if not url:
        raise CodexError(
            "EMBED_URL is unset. `hold collection create` embeds the "
            "512-text calibration set with the encoder the recipe names "
            "and writes the reference vectors' hash into the Codex "
            "(06 §6.6); there is no offline path that produces a valid "
            "Codex, because a placeholder there is a collection no peer "
            "can ever join")
    enc = space.get("encoder") or {}
    recipe = collections_mod.EmbedRecipe(
        url=url, api="openai", model=enc.get("label") or "",
        dtype=space["dtype"], ctx=space["ctx"], dim=space["dim"])

    def embed(texts):
        try:
            return embeddings.embed_texts(texts, recipe=recipe)
        except Exception as e:                      # noqa: BLE001
            raise CodexError(
                f"the encoder at {url} could not embed the calibration "
                f"set: {e}. Nothing was created") from e

    return embed


def cmd_derive(a) -> int:
    if a.encoder_image or a.encoder_weights:
        _not_yet("--encoder-image/--encoder-weights",
                 "`hold embedder calibrate`, which re-embeds the calibration "
                 "set for the new encoder (migration step 3)")

    mother = col.read(a.root, a.codex_id)
    space_from = col.read(a.root, a.space_from) if a.space_from else None
    space = cx.space_from_dict(_json(a.space)) if a.space else None
    layers = (tuple(cx.layer_from_dict(l, i) for i, l in enumerate(_json(a.layers)))
              if a.layers else None)
    update = Update(**_json(a.update)) if a.update else None
    work = Work(**_json(a.work)) if a.work else None

    sister = col.derive(a.root, a.codex_id, space=space, space_from=space_from,
                        layers=layers, update=update, work=work,
                        credits=tpl.expand_archives(a.archives) if a.archives else None)
    _acceptance(mother, sister)
    print(sister.collection_id)
    return 0


def _acceptance(mother: cx.Codex, sister: cx.Codex) -> None:
    """Creating a sister is an acceptance, of the space and of what follows
    from it (02 §2.7). What can be said without the drag on disk is said
    here; how many chunks are already covered arrives with the coverage map."""
    same = sister.space_id == mother.space_id
    verdict = "matches" if same else "DIFFERS FROM"
    consequence = ("her mother's vectors carry over; there is nothing to re-embed"
                   if same else
                   "her mother's vectors do NOT carry over: this Codex breaks "
                   "with every vector that exists")
    print(f"space_id {verdict} the mother's ({sister.space_id[:23]}…)",
          file=sys.stderr)
    print(f"  {consequence}", file=sys.stderr)
    if sister.credits:
        print("  credits inherited: " + ", ".join(c.archive for c in sister.credits),
              file=sys.stderr)
    else:
        print("  credits: none — she owes nothing and starts from zero",
              file=sys.stderr)


def cmd_continue(a) -> int:
    origin = Origin(**_json(a.origin)) if a.origin else None
    d = col.continue_(a.root, a.codex_id, platform_image=a.platform_image,
                      continues_through=a.continues_through, origin=origin,
                      origin_reachable_until=a.origin_reachable_until)
    print(d.collection_id)
    return 0


def cmd_replace(a) -> int:
    if a.encoder_image or a.encoder_weights:
        _not_yet("--encoder-image/--encoder-weights",
                 "`hold embedder calibrate` (migration step 3)")
    df = _json(a.defect)
    defect = Defect(df["check"], df["toolchain"], tuple(df["witnesses"]),
                    df["expected"], df["observed"], df["rationale"])
    c = col.replace(a.root, a.codex_id, defect=defect,
                    platform_image=a.platform_image,
                    space=cx.space_from_dict(_json(a.space)) if a.space else None)
    print(c.collection_id)
    return 0


def cmd_list(a) -> int:
    res = col.resolver(a.root)
    for c in col.list_all(a.root):
        line = (f"{c.collection_id}  {c.relation:<17} {c.plugin:<12} "
                f"lineage={cx.lineage_id(c, res)}")
        if c.replaces:
            line += f"  replaces={c.replaces.codex}"
        elif c.derived_from:
            line += f"  derived_from={c.derived_from}"
        elif c.continues:
            line += f"  continues={c.continues} through={c.continues_through}"
        print(line)
    return 0


def cmd_show(a) -> int:
    _print(col.show(a.root, a.codex_id))
    return 0


def cmd_records_build(a) -> int:
    from app.hold import records as rec
    rec.build(a.root, a.collection, corpus_dir=a.corpus,
              snapshot_id=a.snapshot_id, records_per_shard=a.records_per_shard)
    return 0


def cmd_records_validation(a) -> int:
    from app.hold import records as rec
    n = rec.write_validation(a.root, a.collection)
    print(f"validation.json: {n} bytes")
    return 0


def cmd_snapshot_create(a) -> int:
    from app.hold import snapshot as sn
    snap = sn.create(a.root, a.collection, snapshot_id=a.snapshot_id)
    print(snap.hash)
    return 0


def cmd_embedder_check(a) -> int:
    """`hold embedder check` — the calibration gate of 03 §3.3.

    Exit 0 on a pass, 1 on a fail, 3 when the encoder was unreachable:
    03 §3.1's third outcome is INCONCLUSIVE, "neither a failure nor a
    pass", and 07 §7.7 reserves 3 for exactly that. A gate that exited 1
    on an unreachable endpoint would flag a collection for a network
    problem.
    """
    from app.hold import embedder as emb
    try:
        run, codex = _gate(a)
    except emb.GateDeferred as e:
        print(f"louise: deferred: {e}", file=sys.stderr)
        return 3
    verdict = "PASS" if run.ok else "FAIL"
    print(f"{verdict}  {run.why()}")
    print(f"  space       {run.space_id[:23]}…")
    print(f"  encoder     {codex.space.encoder.label or codex.space.encoder.image[:23]}")
    print(f"  tolerance   {run.tolerance}")
    print(f"  seed        {run.seed.hex()}")
    print(f"  sampled     {len(run.sampled)} of 512, worst {run.worst:.4f}, "
          f"median {run.median:.4f}")
    print(f"  whole set   worst {min(run.all_cosines):.4f}, "
          f"median {__import__('statistics').median(run.all_cosines):.4f}")
    print(f"  benchmark   {run.tokens_per_s} tokens/s")
    if not run.ok:
        return 1
    if a.accept:
        _write_statement(a, run, codex)
    return 0


def cmd_embedder_benchmark(a) -> int:
    """`hold embedder benchmark` — the same run, reporting the rate.

    03 §3.3 measures it on the same pass as the gate, so this is not a
    second measurement: it runs the gate and reports the number the
    statement would carry.
    """
    from app.hold import embedder as emb
    try:
        run, _ = _gate(a)
    except emb.GateDeferred as e:
        print(f"louise: deferred: {e}", file=sys.stderr)
        return 3
    print(f"{run.tokens_per_s} tokens/s over {512} calibration texts "
          f"({'compliant' if run.ok else 'NOT COMPLIANT — the rate of a '
             'non-compliant encoder weights nothing (04 §4.5)'})")
    return 0 if run.ok else 1


def cmd_embedder_calibrate(a) -> int:
    _not_yet("--against",
             "two live encoder endpoints to compare, which is the one part "
             "of step 3 that wants the node rather than a mock (migration "
             "plan, step 3)")
    return 2


def _gate(a):
    """Run the gate for a collection, resolving its encoder the way 07 §7.8
    resolves one: the collection's own endpoint, then the platform default,
    then EMBED_URL."""
    from app import embeddings
    from app.hold import collection as col, drag, embedder as emb

    name, codex = col.resolve(a.root, a.collection)
    member = a.root / name / "drag" / \
        drag.calibration_paths(codex.space.calibration.set)[0]
    if not member.is_file():
        raise CodexError(
            f"{name}: no calibration member at {member.name}. It is written "
            f"by `hold collection create` (09 §9.12) and is a member of every "
            f"snapshot; without it there is nothing to compare against")
    reference = _read_reference(member, codex)

    recipe = _recipe_for(codex, name)      # raises GateDeferred if unset

    def embed(texts):
        try:
            return embeddings.embed_texts(texts, recipe=recipe)
        except emb.GateDeferred:
            raise
        except Exception as e:                      # noqa: BLE001
            # 03 §3.1: an unreachable encoder is INCONCLUSIVE, not a
            # failure — "it produces no attestation and no flag, it is
            # reported with its reason, and it is retried".
            raise emb.GateDeferred(
                f"the encoder for {name} was unreachable or failed: {e}") from e

    return emb.run_gate(codex, reference=reference, embed=embed), codex


def _read_reference(member, codex):
    from safetensors.numpy import load_file
    tensors = load_file(str(member))
    if "vectors" not in tensors:
        raise CodexError(f"{member.name}: no `vectors` tensor (09 §9.12)")
    return tensors["vectors"].tolist()


def _recipe_for(codex, name):
    """The endpoint that serves this space's encoder.

    07 §7.8 resolves it in three steps, "each a fallback for the one before
    it, never an override": the collection's `[encoder]`, then
    `platform.toml`'s `[encoder_defaults.<plugin>]`, then `EMBED_URL`.
    Only the third is available here, because the first two live in
    `collections/<name>.toml` and `platform.toml`, which `hold config` will
    write and this branch does not yet read. So this is the ENV fallback
    alone, and the two file tiers arrive with `hold config`.

    The rest of the recipe comes from the Codex and not from any file: the
    dtype, ctx and dim are inside `space_id`, so a file that disagreed with
    them would be describing another space (02 §2.4).
    """
    from app import collections as collections_mod
    url = os.environ.get("EMBED_URL")
    if not url:
        from app.hold import embedder as emb
        raise emb.GateDeferred(
            "EMBED_URL is unset and no collection endpoint is configured; "
            "07 §7.8: everything that needs an encoder defers (exit 3)")
    return collections_mod.EmbedRecipe(
        url=url, api="openai", model=codex.space.encoder.label or "",
        dtype=codex.space.dtype, ctx=codex.space.ctx, dim=codex.space.dim)


def _write_statement(a, run, codex):
    """`--accept`. 09 §9.12: the statement is "kept in `state/`"."""
    from app.bundles import keys as node_keys
    from app.hold import collection as col, embedder as emb

    name, _ = col.resolve(a.root, a.collection)
    key = node_keys.load_signing_key()
    now = _utc_now()
    stmt = emb.statement(run, codex, key, now=now)
    out = a.root / name / "state" / "calibration.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(cx.canonical_json(stmt))
    from app.hold import envelope as env
    print(f"  statement   {out}")
    print(f"              {env.body_id(stmt['body'])}")


def cmd_snapshot_list(a) -> int:
    from app.hold import collection as c, snapshot as sn
    name, _ = c.resolve(a.root, a.collection)
    d = a.root / name / "drag" / "snapshots"
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        if f.name.endswith(".sig.json"):
            continue
        s = sn.load(f)
        print(f"{s.snapshot_id}  {s.hash}  {s.records:>9} records  "
              f"{len(s.shards):>5} shards")
    return 0


def build_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="louise", description="Louise — knowledge, kept.")
    surfaces = top.add_subparsers(dest="surface", required=True, metavar="hold|share|ask")

    hold = surfaces.add_parser("hold", help="your own copy: complete, current, checked")
    nouns = hold.add_subparsers(dest="noun", required=True)
    coll = nouns.add_parser("collection", help="a Codex and what derives from it")
    verbs = coll.add_subparsers(dest="verb", required=True)

    def root_arg(p):
        p.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                       help="where collections live (default: ./collections or $LOUISE_ROOT)")

    p = verbs.add_parser("create", help="write a Codex from a plugin and a recipe; print its id")
    root_arg(p)
    p.add_argument("plugin", nargs="?", default=None,
                   help="the plugin the collection is of; omit when the file is a whole Codex")
    p.add_argument("--recipe", required=True,
                   help="TOML with every Codex field the plugin does not default (06 §6.6)")
    p.add_argument("--space-from", help="a Codex whose space and layers to copy byte for byte")
    p.add_argument("--platform-image", default=cx.digest(b""),
                   help="the digest of the platform this command runs in")
    p.set_defaults(fn=cmd_create)

    p = verbs.add_parser("derive", help="a sister: her mother's corpus, a new space")
    root_arg(p); p.add_argument("codex_id")
    p.add_argument("--space-from", help="a Codex whose space and layers to copy byte for byte")
    p.add_argument("--space", help="JSON file with the space block")
    p.add_argument("--encoder-image"); p.add_argument("--encoder-weights")
    p.add_argument("--layers", help="JSON file with the layers list")
    p.add_argument("--update", help="JSON file: cadence, day, grace_days")
    p.add_argument("--work", help="JSON file: tokens, per")
    p.add_argument("--archives", nargs="*", default=None,
                   help="archive ids a sister in a new space draws on")
    p.add_argument("--accept", action="store_true", required=True,
                   help="creating a sister is an acceptance (02 §2.7)")
    p.set_defaults(fn=cmd_derive)

    p = verbs.add_parser("continue", help="a daughter: a new toolchain, the same space")
    root_arg(p); p.add_argument("codex_id")
    p.add_argument("--platform-image", required=True)
    p.add_argument("--continues-through", required=True,
                   help="the mother's LAST snapshot — the boundary, named from "
                        "the side that already exists")
    p.add_argument("--origin", help="JSON file with the new origin block")
    p.add_argument("--origin-reachable-until")
    p.set_defaults(fn=cmd_continue)

    p = verbs.add_parser("replace", help="a corrective sister, with its reproducible defect")
    root_arg(p); p.add_argument("codex_id")
    p.add_argument("--defect", required=True,
                   help="JSON file: check, toolchain, witnesses, expected, observed, rationale")
    p.add_argument("--platform-image")
    p.add_argument("--space", help="JSON file with the space block")
    p.add_argument("--encoder-image"); p.add_argument("--encoder-weights")
    p.set_defaults(fn=cmd_replace)

    p = verbs.add_parser("list", help="collections held, with their lineage")
    root_arg(p); p.set_defaults(fn=cmd_list)

    p = verbs.add_parser("show", help="a Codex and the ids derived from it")
    root_arg(p); p.add_argument("codex_id"); p.set_defaults(fn=cmd_show)

    # ---- hold records ---------------------------------------------------
    recs = nouns.add_parser("records", help="the parsed text of documents")
    rverbs = recs.add_subparsers(dest="verb", required=True)

    p = rverbs.add_parser("build", help="parse, slice, shard, hash")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--corpus", help="the corpus directory to parse (until `records fetch`)")
    p.add_argument("--snapshot", dest="snapshot_id", help="the cutoff to build to")
    p.add_argument("--shard-records", type=int, dest="records_per_shard",
                   help="override the Codex's slicing.shard_records (tests)")
    p.set_defaults(fn=cmd_records_build)

    p = rverbs.add_parser("validation", help="write validation.json from the Codex")
    root_arg(p); p.add_argument("collection")
    p.set_defaults(fn=cmd_records_validation)

    # ---- hold snapshot --------------------------------------------------
    snaps = nouns.add_parser("snapshot", help="a collection's records at one cutoff")
    sverbs = snaps.add_subparsers(dest="verb", required=True)

    p = sverbs.add_parser("create", help="freeze the current records as this period's snapshot")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--snapshot", dest="snapshot_id",
                   help="the id; by default the period that closed, from the Codex's clock")
    p.set_defaults(fn=cmd_snapshot_create)

    p = sverbs.add_parser("list", help="the chain this peer holds")
    root_arg(p); p.add_argument("collection"); p.set_defaults(fn=cmd_snapshot_list)

    # ---- hold embedder --------------------------------------------------
    embd = nouns.add_parser("embedder", help="is this peer's encoder the space's?")
    everbs = embd.add_subparsers(dest="verb", required=True)

    p = everbs.add_parser("check", help="the calibration gate (03 §3.3)")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--accept", action="store_true",
                   help="record a passing run as this peer's statement (09 §9.12)")
    p.set_defaults(fn=cmd_embedder_check)

    p = everbs.add_parser("benchmark", help="tokens per second on the calibration set")
    root_arg(p); p.add_argument("collection"); p.set_defaults(fn=cmd_embedder_benchmark)

    p = everbs.add_parser("calibrate", help="the tolerance a space can bear")
    root_arg(p)
    p.add_argument("--encoder-image"); p.add_argument("--encoder-weights")
    p.add_argument("--against", nargs="*", default=[])
    p.add_argument("--set", dest="set_id")
    p.set_defaults(fn=cmd_embedder_calibrate)
    return top


def main(argv=None) -> int:
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # `| head` is not an error
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except CodexError as e:
        print(f"louise: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"louise: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

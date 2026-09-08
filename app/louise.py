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
        recipe["space"] = source.space.to_dict()
        recipe["layers"] = [l.to_dict() for l in source.layers]

    credits = tpl.expand_archives(recipe.get("archives") or ())
    codex = col.create_from_recipe(a.root, a.plugin, recipe,
                                   platform_image=a.platform_image,
                                   credits=credits)
    print(codex.collection_id)
    return 0


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

"""louise — the command line. docs/07-commands.md.

    louise hold collection create <spec.json> [--root DIR]
    louise hold collection derive <codex-id> --encoder-image DIGEST [--root DIR]
    louise hold collection continue <codex-id> --platform-image DIGEST --from-snapshot YYYY-MM
                                    [--origin FILE] [--origin-reachable-until YYYY-MM] [--root DIR]
    louise hold collection replace <codex-id> --defect FILE [--platform-image DIGEST]
                                    [--encoder-image DIGEST] [--root DIR]
    louise hold collection list [--root DIR]
    louise hold collection show <codex-id> [--root DIR]

Only this branch of the tree exists so far. The rest arrives branch by
branch, each with its tests, and the old `python -m app.cli` keeps working
until §7.6's migration is complete.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

from app.hold import codex as cx, collection as col
from app.hold.codex import CodexError, Defect

DEFAULT_ROOT = Path(os.environ.get("LOUISE_ROOT", "collections"))


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True))


def cmd_create(a) -> int:
    spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    spec.setdefault("kind", cx.CODEX_KIND)
    spec.setdefault("version", cx.CODEX_VERSION)
    codex = cx.from_dict(spec)
    codex = col.create(a.root, codex)
    print(codex.collection_id)
    return 0


def cmd_derive(a) -> int:
    print(col.derive(a.root, a.codex_id, encoder_image=a.encoder_image).collection_id)
    return 0


def cmd_continue(a) -> int:
    origin = json.loads(Path(a.origin).read_text(encoding="utf-8")) if a.origin else None
    d = col.continue_(a.root, a.codex_id, platform_image=a.platform_image, from_snapshot=a.from_snapshot,
                      origin=origin, origin_reachable_until=a.origin_reachable_until)
    print(d.collection_id)
    return 0


def cmd_replace(a) -> int:
    df = json.loads(Path(a.defect).read_text(encoding="utf-8"))
    defect = Defect(df["check"], df["toolchain"], tuple(df["witnesses"]), df["expected"],
                    df["observed"], df["rationale"])
    c = col.replace(a.root, a.codex_id, platform_image=a.platform_image, encoder_image=a.encoder_image,
                    defect=defect)
    print(c.collection_id)
    return 0


def cmd_list(a) -> int:
    res = col.resolver(a.root)
    for c in col.list_all(a.root):
        line = f"{c.collection_id}  {c.relation:<17} {c.plugin:<12} lineage={cx.lineage_id(c, res)}"
        if c.replaces:
            line += f"  replaces={c.replaces}"
        elif c.derived_from:
            line += f"  derived_from={c.derived_from}"
        print(line)
    return 0


def cmd_show(a) -> int:
    _print(col.show(a.root, a.codex_id))
    return 0


def build_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="louise", description="Louise — knowledge, kept.")
    surfaces = top.add_subparsers(dest="surface", required=True, metavar="hold|share|ask")

    hold = surfaces.add_parser("hold", help="your own copy: complete, current, checked")
    nouns = hold.add_subparsers(dest="noun", required=True)
    coll = nouns.add_parser("collection", help="a Codex and what derives from it")
    verbs = coll.add_subparsers(dest="verb", required=True)

    def root_arg(p):
        p.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="where collections live (default: ./collections or $LOUISE_ROOT)")

    p = verbs.add_parser("create", help="write a Codex from a spec file; print its id"); root_arg(p)
    p.add_argument("spec"); p.set_defaults(fn=cmd_create)

    p = verbs.add_parser("derive", help="a sister: same corpus, new space"); root_arg(p)
    p.add_argument("codex_id"); p.add_argument("--encoder-image", required=True); p.set_defaults(fn=cmd_derive)

    p = verbs.add_parser("continue", help="a daughter: new toolchain, same space"); root_arg(p)
    p.add_argument("codex_id"); p.add_argument("--platform-image", required=True)
    p.add_argument("--from-snapshot", required=True); p.add_argument("--origin", help="JSON file with the new origin block")
    p.add_argument("--origin-reachable-until"); p.set_defaults(fn=cmd_continue)

    p = verbs.add_parser("replace", help="a corrective sister, with its reproducible defect"); root_arg(p)
    p.add_argument("codex_id"); p.add_argument("--defect", required=True, help="JSON file: check, toolchain, witnesses, expected, observed, rationale")
    p.add_argument("--platform-image"); p.add_argument("--encoder-image"); p.set_defaults(fn=cmd_replace)

    p = verbs.add_parser("list", help="collections held, with their lineage"); root_arg(p); p.set_defaults(fn=cmd_list)

    p = verbs.add_parser("show", help="a Codex and the ids derived from it"); root_arg(p)
    p.add_argument("codex_id"); p.set_defaults(fn=cmd_show)
    return top


def main(argv=None) -> int:
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

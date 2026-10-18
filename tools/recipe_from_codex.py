"""The recipe that rebuilds a ROOT Codex, read from the Codex — for a corrected
Codex of a live collection (docs/notes/char-cut-truncation.md §5; the
"recipe from the old Codex" of docs/notes/v5-migration.md).

    python -m tools.recipe_from_codex <codex.json> <recipe.json> [--cut tokens]

Every field is restated as the Codex holds it, except what `hold collection
create` computes (`space.calibration.vectors`, `query_vectors`, `envelope`)
and what it derives (`credits`: the corpus entry is the plugin's, an
archive's comes from the registry by its id, the community entry from
`embedding`). The platform image is the Codex's: a seeded target's parts are
the source's, built by that image. Before writing, the recipe is built back
with the Codex's own computed hashes and must give the Codex BYTE FOR BYTE,
so nothing is lost or reworded in between (exit 1 otherwise).

`--cut tokens`: the same recipe with its space declaring the token cut (09
§9.3, Codex version 8), checked to build.

`--engine RUN.json`, `--query-engine RUN.json`: a side's engine replaced by
the run `hold embedder run` states for its endpoint now (`{run, engine,
weights, driver}`), since `create` holds the reference to the run that
computes it (G5); refused where the run's weights are not the side's — other
weights are another space, never a corrected Codex of this one.

A JSON recipe: `hold collection create <plugin> --recipe FILE.json` reads it
(07 §7.3), and JSON keeps the nulls TOML cannot write.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.hold import codex as cx, template as tpl  # noqa: E402

#: Codex fields a recipe restates as they stand
_VERBATIM = ("descriptive", "work", "origin", "validation", "publication", "certifier",
             "embedding", "succession", "content", "layers", "slicing", "facets", "update",
             "license", "privacy", "erasure", "modality", "profile", "parser",
             "cleaning_version", "protocol_version", "record_line")


def recipe_of(codex: cx.Codex) -> dict[str, Any]:
    if codex.relation != "root":
        raise cx.CodexError(f"a recipe makes a root, and this Codex is a {codex.relation}: "
                            f"it is made by `derive`, `continue` or `replace` (07 §7.3)")
    d = codex.to_dict()
    space = json.loads(json.dumps(d["space"]))
    space.pop("envelope")
    space["calibration"].pop("vectors")
    space["calibration"].pop("query_vectors", None)
    recipe = {k: d[k] for k in _VERBATIM}
    recipe.update(space=space, toolchain={"platform_image": d["toolchain"]["platform_image"]},
                  archives=[c["archive"] for c in d["credits"] if c["kind"] == "archive"])
    return recipe


def build(codex: cx.Codex, recipe: dict[str, Any], *, hashes=None) -> cx.Codex:
    """`recipe` built as `create` builds it, with `hashes` (default: the
    Codex's own) for the three it computes."""
    sp = codex.space
    hashes = hashes or (sp.calibration.vectors, sp.envelope, sp.calibration.query_vectors)
    return tpl.from_recipe(codex.plugin, json.loads(json.dumps(recipe)),
                           platform_image=codex.toolchain.platform_image, calibration=hashes,
                           credits=tpl.expand_archives(recipe["archives"]))


def with_run(recipe: dict[str, Any], run: dict[str, Any], *, query: bool = False) -> None:
    """The side's engine is `run`'s (`hold embedder run`'s JSON), its weights
    unchanged."""
    side = recipe["space"]["query"]["encoder"] if query else recipe["space"]["encoder"]
    where = "space.query.encoder" if query else "space.encoder"
    if side is None:
        raise cx.CodexError(f"{where} is null: a symmetric space has no query engine")
    if run.get("weights") != side["weights"]:
        raise cx.CodexError(
            f"the run {str(run.get('run'))[:23]}… loads weights {run.get('weights')} and "
            f"{where} names {side['weights']}: other weights are another space (02 §2.4), "
            f"not a corrected Codex of this one")
    side["engine"] = run["engine"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tools.recipe_from_codex")
    p.add_argument("codex", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--cut", choices=[cx.TOKEN_CUT], default=None)
    p.add_argument("--engine", type=Path, default=None, metavar="RUN.json")
    p.add_argument("--query-engine", type=Path, default=None, metavar="RUN.json")
    a = p.parse_args(argv)
    codex = cx.loads(a.codex.read_bytes())
    recipe = recipe_of(codex)
    again = build(codex, recipe)
    if again.canonical() != codex.canonical():
        theirs, ours = codex.to_dict(), again.to_dict()
        print(f"the recipe does not rebuild {codex.collection_id}: "
              f"{sorted(k for k in theirs if theirs[k] != ours.get(k))} differ",
              file=sys.stderr)
        return 1
    print(f"recipe: {codex.collection_id} rebuilt byte for byte", file=sys.stderr)
    for path, query in ((a.engine, False), (a.query_engine, True)):
        if path is not None:
            with_run(recipe, json.loads(path.read_text(encoding="utf-8")), query=query)
    if a.cut:
        recipe["space"]["cut"] = a.cut
    if a.cut or a.engine or a.query_engine:
        twin = build(codex, recipe)
        cx.validate(twin)
        print(f"recipe: written for a version {twin.version} Codex in space "
              f"{twin.space_id[:23]}… (was {codex.space_id[:23]}…)", file=sys.stderr)
    a.out.write_text(json.dumps(recipe, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

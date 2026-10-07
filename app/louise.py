"""louise â the command line. docs/07-commands.md.

    louise hold collection create <plugin> --recipe FILE [--space-from CODEX]
                                    [--platform-image D] [--root DIR]
    louise hold collection derive <codex> (--space-from CODEX | --space FILE)
                                    [--layers FILE] [--update FILE] [--work FILE] --accept
    louise hold collection continue <codex> --platform-image D --continues-through <snapshot>
                                    [--origin FILE] [--origin-reachable-until <snapshot>]
    louise hold collection replace <codex> --defect FILE [--platform-image D] [--space FILE]
    louise hold collection list
    louise hold collection show <codex>
    louise hold records build <collection> [--corpus DIR] [--snapshot ID]
    louise hold records validation <collection>
    louise hold snapshot create <collection> [--snapshot ID]
    louise hold snapshot list <collection>
    louise hold embedder check|benchmark <collection> [--accept]
    louise hold vectors build <collection> [--snapshot ID | --delta ID] [--shard S | --slice V...]
                                    [--dry-run]
    louise hold vectors export <collection> --shard S --out DIR [--snapshot ID]
    louise hold vectors import <collection> --bundle DIR [--snapshot ID]
                                    [--strict [--allow KEY...]] [--require-t3] [--seed HEX]
                                    [--budget]
    louise hold vectors check <collection> [--snapshot ID] [--sample F] [--seed HEX] [--drop]
    louise hold check <collection> [--checks A,B] [--sample F] [--seed HEX] [--snapshot ID] [--json]

Only this branch of the tree exists so far. The rest arrives branch by
branch, each with its tests, and the old `python -m app.cli` keeps working
until Â§7.6's migration is complete.

One flag of Â§7.3's tree is accepted and refused here rather than
silently doing something else, because what it needs is not built yet:
`--encoder-image` / `--encoder-weights`, which must re-embed the
calibration set to fill the space's `calibration` and `envelope` (migration
step 3). Until then a new space is given whole, by `--space` or
`--space-from`.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import signal
import sys
import threading
import tomllib
from pathlib import Path

from app.hold import codex as cx, collection as col, template as tpl
from app.hold.codex import CodexError, Defect, Origin, Update, Work

def _default_root() -> Path:
    """`<LOUISE_ROOT>/collections` — the directory app/collections.py
    resolves a Codex collection under. Resolved per call, never at import:
    tests set LOUISE_ROOT after importing this module."""
    from app import config as _config
    return _config.louise_root() / "collections"


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True))


def _json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _not_yet(flag: str, step: str) -> None:
    raise CodexError(f"{flag} is not built yet: it needs {step}. Give the whole "
                     f"space with --space or --space-from until it is.")


def _recipe(path: str) -> dict:
    """A recipe is TOML (07 Â§7.3). JSON is accepted for a file that is
    already a whole Codex â what `create` took before the template existed,
    and what a test fixture is easiest to write."""
    p = Path(path)
    if p.suffix == ".json":
        return _json(path)
    return tomllib.loads(p.read_text(encoding="utf-8"))


def cmd_create(a) -> int:
    if a.plugin == "deposit":
        return cmd_create_deposit(a)
    if not a.recipe:
        raise CodexError("--recipe is required: a collection is a plugin and a "
                         "recipe (06 §6.6); only `create deposit` needs none")
    if a.plugin is None:
        # the pre-template form: the file IS a Codex
        spec = _json(a.recipe)
        spec.setdefault("kind", cx.CODEX_KIND)
        spec.setdefault("version", cx.CODEX_VERSION)
        codex = col.create(a.root, cx.from_dict(spec))
        print(f"held: {_write_held_file(a.root, codex, a.encoder)}", file=sys.stderr)
        print(codex.collection_id)
        return 0
    codex, held = create_from(a.root, a.plugin, _recipe(a.recipe),
                              platform_image=a.platform_image,
                              space_from=a.space_from, encoder=a.encoder,
                              query_encoder=a.query_encoder)
    print(f"held: {held}", file=sys.stderr)
    print(codex.collection_id)
    return 0


def create_deposit(root, name: str, *, platform_image: str | None = None,
                   encoder: str | None = None, space_from: str | None = None,
                   space: str | None = None):
    """`hold collection create deposit --name N` (07 §7.3; 02 §2.9) — for the
    command line and for the LocalAGI route alike. Whatever the creator does
    not name comes from `platform.toml`'s `[deposit]`. Returns the Codex and
    the collection file it wrote."""
    from app import platform_config
    from app.hold import deposit
    from app.bundles import keys as node_keys

    cfg = platform_config.load().deposit
    encoder = encoder or cfg.encoder
    space_from = space_from or cfg.by_name.get(name) or cfg.space_from
    source = col.read(root, space_from) if space_from else None
    block = None
    if source is None and deposit.space_source(root, encoder) is None:
        path = space or (str(cfg.space) if cfg.space else None)
        block = _recipe(path) if path else None
        if block is not None and "space" in block:
            block = block["space"]
    codex = deposit.create(
        root, name, platform_image=preflight_create({}, platform_image),
        encoder_url=encoder, space_from=source, space=block,
        embed=(_embed_for_recipe(block, encoder) if block is not None else None),
        # the key signs the calibration member, which only the first
        # deposit collection on an encoder writes; the others copy it
        key=node_keys.load_signing_key() if block is not None else None,
        now=_utc_now())
    return codex, deposit.write_held_file(root, codex, encoder)


def cmd_create_deposit(a) -> int:
    if not a.name:
        raise CodexError("create deposit takes --name: a deposit collection is "
                         "named by whoever creates it (07 §7.3)")
    codex, held = create_deposit(a.root, a.name, platform_image=a.platform_image,
                                 encoder=a.encoder, space_from=a.space_from,
                                 space=a.space)
    print(f"held: {held}", file=sys.stderr)
    print(codex.collection_id)
    return 0


def cmd_deposit_add(a) -> int:
    """Queued for the node's worker unless `--here` (app/jobs.py DEPOSIT):
    on a running node only the worker writes a collection's indexes."""
    from app.hold import deposit
    path = Path(a.file)
    key, data = a.key or path.name, path.read_bytes()
    if a.here:
        _take_write_lock(None, a.collection, "hold deposit add --here")
        _print(deposit.add(a.collection, key, data))
        return 0
    from app.plugins.deposit import source as src
    try:
        src.check_content(key, data)          # refused before anything is queued
    except src.DepositError as e:
        raise CodexError(str(e)) from e
    _print(deposit.via_worker("add", a.collection, key=key,
                              staged=deposit.stage(a.collection, data)))
    return 0


def cmd_deposit_erase(a) -> int:
    from app.hold import deposit
    if not a.yes:
        raise CodexError("erase removes a deposit leaving no trace (02 §2.9): "
                         "pass --yes")
    if a.here:
        _take_write_lock(None, a.collection, "hold deposit erase --here")
    _print({"remaining": deposit.erase(a.collection, a.key) if a.here
            else deposit.via_worker("erase", a.collection, key=a.key)["remaining"]})
    return 0


def cmd_source_add(a) -> int:
    from app.hold import deposit, sources
    if a.here:
        _take_write_lock(None, a.collection, "hold source add --here")
    _print(sources.add(a.collection, a.url, a.interval) if a.here else
           deposit.via_worker("source-add", a.collection, url=a.url, interval=a.interval))
    return 0


def cmd_source_erase(a) -> int:
    from app.hold import deposit, sources
    if not a.yes:
        raise CodexError("erase removes the source's deposit leaving no trace "
                         "(02 §2.9): pass --yes")
    if a.here:
        _take_write_lock(None, a.collection, "hold source erase --here")
    _print(sources.erase(a.collection, a.url) if a.here else
           deposit.via_worker("source-erase", a.collection, url=a.url))
    return 0


def cmd_source_list(a) -> int:
    from app.hold import sources
    rows = sources.listing(a.collection)
    if a.json:
        _print(rows)
    else:
        for r in rows:
            print(f"{r['url']}\tevery {r['update_interval']} min\t{r['last_update'] or 'not yet'}")
    return 0


def cmd_deposit_list(a) -> int:
    from app.hold import deposit
    keys = deposit.keys(a.collection)
    if a.json:
        _print(keys)
    else:
        for k in keys:
            print(k)
    return 0


def cmd_deposit_show(a) -> int:
    from app.hold import deposit
    _print(deposit.show(a.collection, a.key))
    return 0


def preflight_create(recipe: dict, platform_image: str | None) -> str:
    """The platform image `create` pins, or its refusal — made before any
    work. The web asks it at once, with `NO_ENCODER`, before it queues the
    create for the worker (app/jobs.py); `create_from` asks it again."""
    stated = (recipe.get("toolchain") or {}).get("platform_image")
    platform_image = (stated or platform_image
                      or os.environ.get("LOUISE_PLATFORM_IMAGE") or None)
    if not platform_image or platform_image == cx.digest(b""):
        raise CodexError(
            "no platform image to pin: 07 §7.3 makes `toolchain.platform_image` "
            "the digest of the platform the command runs in unless the recipe "
            "states it — the deploy sets it as LOUISE_PLATFORM_IMAGE, or give "
            "--platform-image. The SHA-256 of empty bytes is a placeholder "
            "that names no build, and a Codex is never edited")
    return platform_image


def create_from(root, plugin: str, recipe: dict, *, platform_image: str | None = None,
                space_from: str | None = None, encoder: str | None = None,
                query_encoder: str | None = None):
    """`hold collection create <plugin> --recipe FILE` — for the command
    line and for its route alike (07 §7.4: the web interface is a client of
    the same function, not a second implementation). Returns the Codex and
    the collection file it wrote (07 §7.8)."""
    # `toolchain.platform_image` is hashed into collection_id (09 §9.3), and
    # it is "the digest of the platform the command runs in unless the
    # recipe states it" (07 §7.3): the recipe's own value, else
    # --platform-image, else the digest the deploy hands the container
    # (LOUISE_PLATFORM_IMAGE — the image it started; a container cannot see
    # its own image id). The flag used to default to digest(b""), the
    # SHA-256 of nothing, and the live gutenberg Codex carries exactly that
    # placeholder (found 2026-09-14), so no identity may name no build again.
    platform_image = preflight_create(recipe, platform_image)
    if space_from:
        source = col.read(root, space_from)
        want = recipe.get("cleaning_version", 1)
        if source.cleaning_version != want:
            raise CodexError(
                f"--space-from names a Codex whose cleaning_version is "
                f"{source.cleaning_version} and this collection's is {want}. "
                f"The flag exists so that two collections land in one space by "
                f"construction (02 Â§2.7), and it copies no cleaner version the "
                f"plugin could not run")
        space = source.space.to_dict()
        # The two computed fields do not travel: `create` re-embeds
        # the set with this node's encoder and writes its OWN
        # reference. Neither is inside `space_id` (02 Â§2.4), so the
        # two collections still land in one space by construction â
        # and a difference here is real news about the encoder build,
        # which is why it is re-measured rather than copied.
        drifted = (space.pop("envelope"), space["calibration"].pop("vectors"))
        space["calibration"].pop("query_vectors", None)     # computed here too (09 §9.12)
        recipe["space"] = space
        recipe["layers"] = [l.to_dict() for l in source.layers]

    credits = tpl.expand_archives(recipe.get("archives") or ())
    # The creator's key: 09 Â§9.12 makes the calibration member's `key`
    # header required and Â§9.4 lists its signature among a snapshot's files
    # without allowing null, so there is no unsigned collection. Loaded by
    # PATH (invariant 8) and never printed.
    from app.bundles import keys as node_keys
    codex = col.create_from_recipe(root, plugin, recipe,
                                   key=node_keys.load_signing_key(), now=_utc_now(),
                                   platform_image=platform_image,
                                   embed=_embed_for_recipe(recipe["space"], encoder),
                                   embed_query=(_embed_for_recipe(recipe["space"], query_encoder)
                                                if query_encoder else None),
                                   space_version=source.version if space_from else None,
                                   credits=credits)
    if space_from and (codex.space.envelope,
                       codex.space.calibration.vectors) != drifted:
        print(f"warning: --space-from {space_from} recorded reference "
              f"vectors {drifted[1]} and this node's encoder produced "
              f"{codex.space.calibration.vectors}. Both collections are "
              f"in space {codex.space_id} â calibration is not inside "
              f"space_id â but the two builds are not the same build.",
              file=sys.stderr)
    return codex, _write_held_file(root, codex, encoder, query_encoder)


def _write_held_file(root, codex, encoder: str | None = None,
                     query_encoder: str | None = None) -> Path:
    """07 §7.8: `hold collection create` writes `collections/<name>.toml`,
    and the file existing is the collection being held.

    It says what a new collection holds before anything is fetched: no
    slice yet (`records fetch` records the ones it takes, 07 §7.3), the
    default kinds, nothing spent on embedding, nothing shared — and the
    encoder it was created with, in `[encoder]`: a collection names its
    own, and a file that names none is refused (no default encoder,
    operator 2026-09-18).

    Where: `<root>/<name>.toml`, the spec's path. On a node whose files
    live in CONFIG_DIR rather than under LOUISE_ROOT (the reference node:
    /config beside /data), a create in the node's own root writes where the
    node reads, `collections.config_toml_path(name)`.

    A name holds one collection for its life: a file already naming this
    Codex is left alone, and one naming another is refused.
    """
    from app import collections as collections_mod
    name = col.name_of(root, codex.collection_id)
    node_root = collections_mod.louise_root() / "collections"
    path = (collections_mod.config_toml_path(name)
            if Path(root).resolve() == node_root.resolve()
            else Path(root) / f"{name}.toml")
    if path.exists():
        with open(path, "rb") as f:
            held = str(tomllib.load(f).get("codex") or "")
        if held.removeprefix("sha256:") == codex.collection_id.removeprefix("sha256:"):
            return path
        raise CodexError(
            f"{path} holds {held or 'no codex'} and this is "
            f"{codex.collection_id}: a name holds one collection for its "
            f"life, and another collection is another name (07 §7.8)")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(f'codex = "{codex.collection_id}"\n'
                   "slices = []\n"
                   'kinds = ["records", "vectors"]\n'
                   'embed = "off"\n'
                   "share = false\n"
                   + (f'\n[encoder]\nurl = "{encoder}"\napi = "openai"\n'
                      # a bi-encoder's query side, where the collection has one
                      + (f'query_url = "{query_encoder}"\n' if query_encoder else "")
                      if encoder else ""),
                   encoding="utf-8")
    tmp.replace(path)
    return path


def _utc_now() -> str:
    """09 Â§9.1's time: RFC 3339 in UTC to the second.

    THE ONE PLACE the clock is read. Nothing under `app/hold/` reads it â
    every signed body takes its `time` from a caller â so that a test can
    pin what was signed, and so that two objects signed in one command
    cannot disagree about when it ran.
    """
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


NO_ENCODER = (
    "name the encoder: --encoder URL. There is no default encoder "
    "(operator, 2026-09-18), and the new collection's file names the one "
    "it was created with. `hold collection create` embeds the "
    "512-text calibration set with the encoder the recipe names "
    "and writes the reference vectors' hash into the Codex "
    "(06 Â§6.6); there is no offline path that produces a valid "
    "Codex, because a placeholder there is a collection no peer "
    "can ever join")


def _embed_for_recipe(space: dict, url: str | None):
    """The encoder `create` embeds the calibration set with (06 Â§6.6).

    There is no Codex yet â the two hashes this produces are Codex
    fields â so the endpoint is resolved from the RECIPE's space block.
    The endpoint is `--encoder URL`, the one the new file will name. It
    was EMBED_URL; there is no default encoder now (operator, 2026-09-18).

    An unreachable encoder here is not `GateDeferred`: there is no
    collection to defer a check on. It is a plain refusal to create.
    """
    from app import collections as collections_mod, embeddings

    if not url:
        raise CodexError(NO_ENCODER)
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
    from it (02 Â§2.7). What can be said without the drag on disk is said
    here; how many chunks are already covered arrives with the coverage map."""
    same = sister.space_id == mother.space_id
    verdict = "matches" if same else "DIFFERS FROM"
    consequence = ("her mother's vectors carry over; there is nothing to re-embed"
                   if same else
                   "her mother's vectors do NOT carry over: this Codex breaks "
                   "with every vector that exists")
    print(f"space_id {verdict} the mother's ({sister.space_id[:23]}â¦)",
          file=sys.stderr)
    print(f"  {consequence}", file=sys.stderr)
    if sister.credits:
        print("  credits inherited: " + ", ".join(c.archive for c in sister.credits),
              file=sys.stderr)
    else:
        print("  credits: none â she owes nothing and starts from zero",
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
    # SKIPPED, NOT HIDDEN. `_held` refuses an unreadable Codex on its own
    # behalf rather than failing the command, which is invariant 1's rule
    # — but a collection that silently stops being listed is how one gets
    # forgotten. Named here, with what to run.
    skipped = sorted(col.unreadable().items())
    if skipped:
        # ON STDOUT TOO, and that is the point. The detail belongs on
        # stderr, but a caller that reads only stdout — a script, a
        # health check, `| wc -l` — would otherwise see an empty listing
        # and exit 0, which is indistinguishable from "this node holds
        # no collections". Measured 2026-09-13: with every Codex on the
        # node unreadable, stdout was zero lines and the exit was 0.
        # That is the "idle — pipeline is caught up" failure wearing a
        # different hat, and it gets the same answer here as in
        # app/health.py: silence and success must not look alike.
        print(f"# {len(skipped)} collection(s) held but NOT listed above "
              f"(unreadable Codex) — see stderr")
    for name, reason in skipped:
        print(f"  !! {name}: not listed — {reason}", file=sys.stderr)
        print(f"     try: louise hold collection migrate {name}", file=sys.stderr)
    return 0


def _refusal_for(name: str) -> str | None:
    """Why the NODE refuses to start this collection, if it does (07 §7.8):
    its file names another Codex, its vector table is not the Codex's space
    or width, its state database is another platform's. Asked of the node's
    own loader and boot check, never re-derived; a command run where there
    is no node configuration finds none, which is the truth there."""
    import contextlib
    try:
        from app import collections as collections_mod, spaces
        # The node's loaders log as they go, on stdout. This command's stdout
        # is ONE JSON object (09 §9.3) that a script parses, so what they say
        # goes to stderr for as long as they are asked.
        with contextlib.redirect_stdout(sys.stderr):
            loaded = collections_mod.load_all(skip_unreadable=True)
            for r in collections_mod.refusals():
                if r.name == name:
                    return r.reason
            mine = [c for c in loaded if c.name == name and c.codex is not None]
            for _c, reason in spaces.refuse_boot(mine) if mine else []:
                return reason
    except Exception:                                   # noqa: BLE001
        return None
    return None


def _copy_state(root, name, codex):
    """The copy's state on this peer (03 §3.6), derived now, written nowhere."""
    from app import copy_state
    from app.hold import records as rec
    try:
        local = rec._local_collection(root, name, codex)
    except Exception:                                   # noqa: BLE001
        local = None
    return copy_state.held(root, name, codex, local, refusal=_refusal_for(name))


def cmd_collection_forget(a) -> int:
    """`hold collection forget` (07 §7.3, §7.7): queued for the node's worker
    — the one writer of a collection's indexes, which retires the
    collection's threads first — unless `--here`, for a node whose worker is
    not running. `--dry-run` only names what would go."""
    from app.hold import deposit, forget as forget_mod
    try:
        name = col.name_of(a.root, a.collection)
    except (CodexError, FileNotFoundError):
        name = a.collection                 # not held: forget says so
    kinds = tuple(k for k in (a.kinds or "").split(",") if k)
    if not a.dry_run and not a.yes:
        if sys.stdin.isatty():
            answer = input(f"forget {name!r}: remove this peer's copy and its state? [y/N] ")
            if answer.strip().lower() != "y":
                print("nothing removed", file=sys.stderr)
                return 0
        else:
            raise CodexError("--yes is required when stdin is not a terminal (07 §7.7)")
    if a.here or a.dry_run:
        if not a.dry_run:
            _take_write_lock(a.root, name, "hold collection forget", exit_code=2)
        out = forget_mod.forget(a.root, name, kinds=kinds, dry_run=a.dry_run)
    else:
        try:
            out = deposit.via_worker("forget", name, kinds=list(kinds))
        except deposit.DepositError as e:
            raise CodexError(str(e)) from e
    _print(out)
    return 0


# ---- the one writer per collection (07 §7.7) ---------------------------------
_cli_locks: list = []


def _take_write_lock(root, name: str, what: str, *, exit_code: int = 3):
    """A `hold` command that writes takes the collection's lock or exits 3
    naming the holder — 2 for the four whose later retry is not the answer
    (07 §7.7). Held until the command ends: `main` releases."""
    from app.hold import lock as lock_mod
    root = Path(root) if root else _default_root()
    lock = lock_mod.take(lock_mod.state_dir(root, name), name, what, exit_code=exit_code)
    _cli_locks.append(lock)
    return lock


def _release_cli_locks() -> None:
    while _cli_locks:
        _cli_locks.pop().release()


def _run_releasing(a) -> int:
    try:
        return a.fn(a)
    finally:
        _release_cli_locks()


def cmd_node_pause(a) -> int:
    """`node pause [<collection>] [--acquire-only | --seed-only]` (07 §7.3):
    the node's mode, or one collection — whose write lock the worker releases
    at the end of its current unit and runs nothing for until `resume`."""
    from app import node_state
    if a.collection:
        names = node_state.set_paused(a.collection, True)
        _print({"paused": sorted(names)})
        return 0
    state = "acquire-only" if a.acquire_only else "seed-only" if a.seed_only else "paused"
    node_state.set_worker_state(state)
    _print({"node": state})
    return 0


def cmd_node_resume(a) -> int:
    from app import node_state
    if a.collection:
        names = node_state.set_paused(a.collection, False)
        _print({"paused": sorted(names)})
        return 0
    node_state.set_worker_state("running")
    _print({"node": "running"})
    return 0


#: 07 §7.3 writes `share <collection>`: the collection stands where a verb would
_SHARE_VERBS = ("collection", "fetch", "contribute", "attest", "peers", "-h", "--help")


def cmd_share(a) -> int:
    """`share <collection> [--stop]` (07 §7.3; 04 §4.3): the desired state —
    `share = true` (or false) in the collection's file — which the worker
    serves at its next beat (app/share/seeder.py). A `hold` command never
    opens a port (07 §7.4), and neither does this one. `--slices` and
    `--kinds` narrow what is served: not yet (step 9d)."""
    import re
    from app.hold import deposit
    name, codex = col.resolve(a.root, a.collection)
    if a.slices or a.kinds:
        raise CodexError("share --slices / --kinds: not yet — the worker serves every "
                         "member of the current snapshot this copy holds (step 9d "
                         "narrows it)")
    if codex.privacy != "public":
        raise CodexError(f"{name!r} is {codex.privacy}: only a public collection is "
                         f"shared (04 §4.4)")
    path = deposit.held_file(a.root, name)
    if not path.is_file():
        raise CodexError(f"{name!r} is not held: no {path}")
    text = path.read_text(encoding="utf-8")
    want = "share = false" if a.stop else "share = true"
    if re.search(r"^share\s*=", text, flags=re.M):
        text = re.sub(r"^share\s*=.*$", want, text, count=1, flags=re.M)
    else:
        head, sep, tail = text.partition("\n[")
        text = head.rstrip("\n") + "\n" + want + "\n" + (sep + tail if sep else "")
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    _print({"collection": name, "share": not a.stop, "file": str(path),
            "note": "the worker serves and announces it at its next beat; "
                    "`hold collection show` names the state it publishes"})
    return 0


def _set_held_selection(path, slices, kinds) -> dict:
    """07 §7.8: the file's `slices` and `kinds` — what `share fetch` was asked
    for, joined with what the file already said (`"all"` wins); the worker
    fetches what the drag lacks of it. Returns what the file says now."""
    import json
    import re
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    had_slices = raw.get("slices")
    if slices == "all" or had_slices == "all":
        now_slices = "all"
    else:
        now_slices = list(had_slices or [])
        now_slices += [s for s in slices if s not in now_slices]
    now_kinds = list(raw.get("kinds") or ["records", "vectors"])
    now_kinds += [k for k in (kinds or ()) if k not in now_kinds]

    def put(key, value_text):
        nonlocal text
        line = f"{key} = {value_text}"
        if re.search(rf"^{key}\s*=", text, flags=re.M):
            text = re.sub(rf"^{key}\s*=.*$", line, text, count=1, flags=re.M)
        else:
            head, sep, tail = text.partition("\n[")
            text = head.rstrip("\n") + "\n" + line + "\n" + (sep + tail if sep else "")
    put("slices", '"all"' if now_slices == "all" else "[" + ", ".join(json.dumps(s) for s in now_slices) + "]")
    put("kinds", "[" + ", ".join(json.dumps(k) for k in now_kinds) + "]")
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return {"slices": now_slices, "kinds": now_kinds}


def cmd_share_fetch(a) -> int:
    """`share fetch <collection>` (07 §7.3; 04 §4.2, §4.3): the peers that
    hold it found, its Codex chain and snapshot chain pulled and verified,
    the collection held — codex.json and snapshots/*, the
    pre-acknowledgement set — and the slices and kinds asked for written
    into its file; the worker fetches the members at its next beat, or
    `--here` does, in this process, for a node whose worker is not
    running."""
    from app.share import fetch as fetch_mod
    locked = False
    try:                                   # held here already: a name or a prefix resolves (07 §7.6)
        name, codex = col.resolve(a.root, a.collection)
        ref = codex.collection_id
        _take_write_lock(a.root, name, "share fetch")
        locked = True
    except (CodexError, FileNotFoundError, OSError):
        ref = a.collection if a.collection.startswith("sha256:") else "sha256:" + a.collection
    session = None
    if getattr(a, "dht", False):
        from app.acquire import transport_torrent as _tt
        session = _tt.get_session()
    slices = "all" if a.slice is None else tuple(a.slice)
    kinds = tuple(k for k in (a.kinds or "records").split(",") if k)
    out = fetch_mod.fetch(a.root, ref, snapshot_id=a.snapshot, from_address=getattr(a, "from_", None),
                          encoder=a.encoder, name=a.name, session=session,
                          log=lambda m: print(m, file=sys.stderr))
    out["selection"] = _set_held_selection(out["file"], slices, kinds)
    if a.here:
        if not locked:
            _take_write_lock(a.root, out["name"], "share fetch --here")
        out["members"] = _fetch_members_here(a.root, out["name"], slices=out["selection"]["slices"],
                                             kinds=out["selection"]["kinds"], session=session)
        out["note"] = "the members fetched here; `hold collection show` lists the holdings"
    else:
        out["note"] = ("the worker fetches the members the file asks for at its next beat; "
                       "`hold collection show` lists the holdings")
    _print(out)
    return 0


def _fetch_members_here(root, name: str, *, slices, kinds, session=None) -> dict:
    """`share fetch --here`: the members in this process, on this process's
    session and budget (07 §7.7: the one writer — the caller holds the lock)."""
    import sqlite3
    import time
    from app import collections as collections_mod, licensing
    from app.acquire import transport_torrent as _tt
    from app.share import transport
    root = Path(root)
    codex = col.read(root, name)
    c = collections_mod.by_name(name) if root.resolve() == (collections_mod.louise_root() / "collections").resolve() else None
    if c is not None:
        ack_missing = licensing.state_for(c) == licensing.MISSING
    else:
        state = root / name / "state"
        ack_missing = codex.license.cls != "green" and collections_mod._codex_ack(codex, state) is None
    manifest = root / name / "state" / "manifest.sqlite"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(manifest, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        with peers_connection() as node_con:
            hints = transport.hints_for(node_con, codex.collection_id, now=time.time())
        return transport.fetch_members(root, name, codex, slices=slices, kinds=tuple(kinds),
                                       ack_missing=ack_missing, session=session or _tt.get_session(),
                                       con=con, hints=hints, log=lambda m: print(m, file=sys.stderr))
    finally:
        con.close()


def peers_connection():
    from contextlib import closing
    from app.share import peers
    return closing(peers.connect())


def cmd_share_attest(a) -> int:
    """`share attest <collection> [--checks …]` (07 §7.3; 04 §4.2): `hold
    check` with the named checks and a fresh seed, then every attestation
    it wrote published as an `attestation` event for the worker's beat to
    forward — once each; an attestation published before is never
    republished. Exit 0 when nothing failed, 1 when a check did."""
    from app import identity
    from app.bundles import keys as node_keys
    from app.share import attest as attest_mod, peers
    name, codex, snapshot, seed, report = run_check(a)
    try:
        key = node_keys.load_signing_key()
        own_key = identity.own()
    except Exception as e:   # noqa: BLE001 — a peer with no key attests nothing (07 §7.8)
        raise CodexError(f"share attest publishes signed events and this peer has no key: "
                         f"{e}") from None
    with _closing(peers.connect()) as con:
        out = attest_mod.publish(a.root, name, codex, list(report.attestations.values()),
                                 key=key, own_key=own_key, con=con,
                                 log=lambda m: print(m, file=sys.stderr))
    _print({"check": report.to_json(), "seed": seed.hex(), **out,
            "note": "the worker forwards the events to every neighbour at its next beat"})
    return 0 if report.ok else 1


def _embedding_callables(root, name: str, codex, *, stop):
    """What `vectors build` embeds with (03 §3.3): the collection's own
    encoder through its recipe and, for a collection gated per answer, the
    guard that stands between a batch's answers and its write. Raises
    `embedder.GateDeferred` when the encoder is not there to ask."""
    from app import embeddings
    from app.hold import embedder as emb, runs as hold_runs
    by_run = hold_runs.applies(codex)
    recipe = _recipe_for(codex, name)
    seen: set = set()

    def embed(texts):
        seen.clear()
        try:
            if by_run:
                return embeddings.embed_texts(texts, recipe=recipe, runs=seen)
            return embeddings.embed_texts(texts, recipe=recipe)
        except Exception as e:                   # noqa: BLE001
            raise emb.GateDeferred(f"the encoder at {recipe.url} could not embed: {e}") from e

    before_write = None
    if by_run:
        from app import run_check

        def before_write():
            run_check.guard(root, name, codex, recipe, set(seen), stop=stop.is_set,
                            log=lambda m: print(m, file=sys.stderr))
    return embed, before_write


def cmd_share_contribute(a) -> int:
    """`share contribute [<collection>] [--once] [--interval S]` (07 §7.3;
    04 §4.5): for the collection named, or every public one held here, the
    shards this peer leads — built, bundled, passed through its own ladder
    and announced (app/share/contribute.py). `--once` runs one pass and
    exits; otherwise the pass repeats every interval until a signal, which
    finishes the batch in flight and exits 130 (07 §7.7). Exit 2 when a
    precondition stands in the way, 3 when the encoder is not there."""
    import json
    import threading
    from app import collections as collections_mod, config, copy_state, embeddings, identity
    from app.bundles import keys as node_keys
    from app.hold import embedder as emb, records as rec
    from app.share import contribute as contrib, coverage as cov, peers
    if a.collection:
        refs = [a.collection]
    else:
        node_root = (collections_mod.louise_root() / "collections").resolve()
        if Path(a.root).resolve() != node_root:
            raise CodexError("share contribute without a collection takes the node's own root; "
                             "name a collection here")
        refs = [c.name for c in collections_mod.load_all()
                if getattr(c, "codex", None) is not None and c.codex.privacy == "public"]
    try:
        key = node_keys.load_signing_key()
        own_key = identity.own()
    except Exception as e:   # noqa: BLE001
        raise CodexError(f"share contribute signs bundles and this peer has no key: {e}") from None
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_a: stop.set())
        except (ValueError, OSError):                # not the main thread
            pass
    locked: set[str] = set()

    def one_pass() -> dict:
        results: dict[str, object] = {}
        for ref in refs:
            name, codex = col.resolve(a.root, ref)
            if codex.privacy != "public":
                results[name] = {"skipped": f"{codex.privacy}: bundles of it leave for nobody (04 §4.4)"}
                continue
            if name not in locked:
                _take_write_lock(a.root, name, "share contribute")
                locked.add(name)
            local = rec.local_collection(a.root, name, codex)      # invariant 9: embedding is a use
            copy_state.require_in_use(a.root, name, codex, local, use="contributing vectors for it")
            embed, before_write = _embedding_callables(a.root, name, codex, stop=stop)
            now = __import__("time").time()
            with _closing(peers.connect()) as node_con:
                live = cov.live_peers(codex, node_con, now=now, own_key=own_key,
                                      own_benchmarks=cov.own_benchmarks(a.root, name, codex, own_key))
                from app import db as _db
                with _closing(_db.connect(local)) as mcon:
                    results[name] = contrib.once(
                        a.root, name, codex, key=key, own_key=own_key, embed=embed,
                        vectors_for=lambda ids: embeddings.vectors_by_chunk_id(ids, collection=local),
                        write=lambda rows: embeddings.upsert_chunks(rows, collection=local,
                                                                    dtype=codex.space.dtype),
                        live=live, con=mcon, node_con=node_con, now=now, stop=stop,
                        log=lambda m: print(m, file=sys.stderr), batch=config.EMBED_SLICE_CHUNKS,
                        before_write=before_write)
            if stop.is_set():
                break
        return results

    try:
        if a.once:
            _print(one_pass())
            return 0
        while not stop.is_set():
            out = one_pass()
            print(json.dumps(out, sort_keys=True), file=sys.stderr)
            if stop.is_set():
                break
            stop.wait(max(1.0, float(a.interval)))
        return 130
    except emb.GateDeferred as e:
        print(f"louise: deferred: {e}", file=sys.stderr)
        return 3


# ---- ask (07 §7.3; 05) ----------------------------------------------------------------

def cmd_ask_search(a) -> int:
    """`ask search <query>`: 05 §5.4's `louise_search`, printed."""
    from app.ask import results
    filters = None
    if a.filter:
        filters = {}
        for item in a.filter:
            field, _, value = item.partition("=")
            if not field or not value:
                raise CodexError(f"--filter takes <facet>=<value>, got {item!r}")
            filters[field] = {"not": value[1:]} if value.startswith("!") else value
    out = results.search(" ".join(a.query), collections=a.collections or None, space=a.space, filters=filters,
                         limit=a.limit, rerank_depth=a.rerank_depth, as_of=a.as_of, freshness=a.freshness,
                         translate=a.translate, all_languages=a.all_languages, root=getattr(a, "root", None))
    _print(out)
    return 0


def cmd_ask_facets(a) -> int:
    from app.ask import facets
    _print(facets.listing(a.collection, a.field, a.under, root=a.root))
    return 0


def cmd_ask_lookup(a) -> int:
    from app.ask import lookup
    _print(lookup.lookup(a.id))
    return 0


def cmd_ask_traverse(a) -> int:
    from app.ask import traverse
    _print(traverse.traverse(a.id, relation=a.relation, depth=a.depth, collection=a.collection, root=a.root))
    return 0


def cmd_ask_serve(a) -> int:
    """`ask serve`: the three tools of 05 §5.4 over MCP on stdio; needs
    nothing but what is held (07 §7.3)."""
    from app.ask import mcp
    return mcp.serve_stdio()


def cmd_ask_chat(a) -> int:
    """`ask chat <question>`: the chat model with the three tools of 05
    §5.4; needs CHAT_URL (07 §7.8). The answer, then every tool call made
    on stderr; `--json` prints the whole object, results included."""
    from app.ask import chat
    out = chat.ask(" ".join(a.question), collections=a.collections or None)
    if a.json:
        _print(out)
        return 0
    print(out["answer"])
    for t in out["turns"]:
        line = f"[{t['tool']} {json.dumps(t['arguments'], ensure_ascii=False)}]"
        if t.get("error"):
            line += f" refused: {t['error']}"
        print(line, file=sys.stderr)
    if out.get("exhausted"):
        print(f"[{out['rounds']} rounds: the model was asked to answer with what it had]", file=sys.stderr)
    return 0


def _collections_root(a) -> Path:
    from app import collections as collections_mod
    root = getattr(a, "root", None)
    return Path(root) if root else collections_mod.louise_root() / "collections"


def cmd_collection_certify(a) -> int:
    """`hold collection certify <collection> <snapshot>` (07 §7.3): the
    certifier's signature into the sidecar and the pointer of 09 §9.11
    written for `share` to publish; refused when this peer's key is not
    the Codex's `certifier.key`."""
    import time
    from app.bundles import keys as node_keys
    from app.hold import certify
    root = _collections_root(a)
    name, codex = col.resolve(root, a.collection)
    out = certify.certify(root, name, codex, a.snapshot, node_keys.load_signing_key(), now=time.time())
    _print(out)
    return 0


def cmd_key_create(a) -> int:
    """`hold key create`: this peer's key at NODE_KEY_PATH, once (07 §7.3)."""
    from app.hold import node_key
    _print({"key": node_key.create()})
    return 0


def cmd_key_trust(a) -> int:
    """`hold key trust <key> [--deny | --remove | --restore]` over
    `state/trust.json` (09 §9.8)."""
    from app.hold import node_key
    _print(node_key.trust(a.key, deny=a.deny, remove=a.remove, restore=a.restore,
                          root=_collections_root(a).parent))
    return 0


def cmd_key_show(a) -> int:
    """`hold key show`: this peer's public key and the trust list with each
    key's state."""
    from app.hold import node_key
    _print(node_key.show(_collections_root(a).parent))
    return 0


def cmd_key_anchor(a) -> int:
    """`hold key anchor <collection>`: fetch the certifier's anchor document
    now and judge the key against it (03 §3.7; 09 §9.11). Exit 0 anchored
    or stale, 2 mismatch, 3 when nothing could be fetched and nothing is
    held."""
    import time
    from app.hold import anchor
    root = _collections_root(a)
    name, codex = col.resolve(root, a.collection)
    if codex.publication != "certified" or codex.certifier is None:
        raise CodexError(f"{name}: an open collection has no anchor (02 §2.8)")
    st = anchor.refresh(root, name, codex, now=time.time(), log=lambda m: print(m, file=sys.stderr))
    _print({"collection_id": codex.collection_id, "certifier": codex.certifier.key,
            "anchor": codex.certifier.anchor, **st})
    return {"anchored": 0, "stale": 0, "mismatch": 2}.get(st["state"], 3)


def cmd_share_peers(a) -> int:
    """`share peers add <address> [--key]` writes the bootstrap of 04 §4.2
    into platform.toml's `[peers]` (07 §7.8) — desired state, in the file;
    the worker sends its next digest there. `share peers show` lists the
    peer list and every peer heard from, with what its last digest says it
    holds."""
    from app import platform_config
    from app.share import peers
    if a.peers_verb == "add":
        out = peers.add_to_peer_list(platform_config.platform_path(), a.address, key=a.key)
        out["note"] = ("the worker sends its digest there at its next beat; the peer is a "
                       "neighbour once its own digest arrives")
        _print(out)
        return 0
    _print(peers.with_connection(peers.show, now=peers.now()))
    return 0


def cmd_show(a) -> int:
    out = col.show(a.root, a.codex_id)
    name, codex = col.resolve(a.root, a.codex_id)
    # 09 §9.3: `state`, `reasons` and `origin_check`, where this object
    # carried `refused: string or null` until 2026-09-19
    out.pop("refused", None)
    out.update(_copy_state(a.root, name, codex).to_json())
    out.update(_show_drag(a.root, name))
    _print(out)
    return 0


def _show_drag(root, name: str) -> dict:
    """09 §9.3's `snapshots` and `holdings`, read off the drag: every
    snapshot in the chain with its verified signatures and whether its
    record members are all held; what the drag holds of the current one."""
    import json
    from app.hold import snapshot as sn
    from app.share import holdings, members as mem, seeder
    drag_dir = col.drag_of(Path(root), name)
    sdir = drag_dir / "snapshots"
    snaps = []
    for p in sorted(sdir.glob("*.json")) if sdir.is_dir() else []:
        if p.name.endswith(".sig.json"):
            continue
        try:
            snap = sn.load(p)
        except Exception:   # noqa: BLE001 — a file that is no snapshot is not listed
            continue
        n = 0
        sig = sdir / f"{snap.snapshot_id}.sig.json"
        if sig.is_file():
            try:
                n = len(sn.sidecar_keys(json.loads(sig.read_bytes()), snap.hash)[0])
            except Exception:   # noqa: BLE001
                n = 0
        records = [m for m in mem.members_of(snap) if holdings.kind_of(m.name) == "records"]
        snaps.append({"snapshot_id": snap.snapshot_id, "hash": snap.hash, "signatures": n,
                      "held": bool(records) and all(holdings.held(drag_dir, m) for m in records)})
    current = seeder.current_snapshot(drag_dir)
    from app.hold import anchor
    codex = col.read(Path(root), name)
    return {"snapshots": snaps, "holdings": holdings.summary(drag_dir, current),
            **_trust_level(root, name, current),
            # 05 §5.3, 03 §3.8: certification beside the trust level — two axes
            "certification": anchor.certification(Path(root), name, codex, current),
            "anchor": anchor.status(Path(root), name, codex)}


def _trust_level(root, name: str, current) -> dict:
    """09 §9.3's `trust_level` (03 §3.6), from the attestations held and the
    bundles accepted here; `trust_detail` says what holds it where it is."""
    from app import copy_state, identity
    from app.hold import equivalence, trust_file
    from app.share import coverage as cov, levels
    codex = col.read(Path(root), name)
    bodies = copy_state.attestations(Path(root), name, codex)
    try:
        own = identity.own()
    except Exception:   # noqa: BLE001 — no key: the Codex's image alone counts
        own = None
    rows = []
    if (Path(root) / name / "state" / "manifest.sqlite").is_file():
        with _closing(cov.manifest(root, name)) as mcon:
            rows = cov.rows(mcon)
    out = levels.level(codex, current, bodies, rows, own_key=own,
                       trust=trust_file.as_filter(Path(root).parent),
                       reference_images=equivalence.reference_images(bodies, own, codex))
    return {"trust_level": out["level"], "trust_detail": out}


def cmd_collection_migrate(a) -> int:
    if a.apply:
        _take_write_lock(a.root, a.name, "hold collection migrate --apply", exit_code=2)
    got = col.migrate(a.root, a.name, apply=a.apply)
    if not got["added"]:
        print(f"{got['name']}: already carries every §9.3 field")
        return 0
    print(f"{got['name']}: adding {', '.join(got['added'])} as null")
    print(f"  collection_id  {got['before']}")
    print(f"              -> {got['after']}")
    # WHAT ELSE NAMES THE OLD ID. Not a refusal — the identity change is
    # the point — but an operator told "collection_id moves" deserves to
    # know what else says it, rather than meeting it later as an error.
    for consequence in got.get("carries_old_id") or ():
        print(f"  note: {consequence}")
    if not got["written"]:
        print("  (dry run — re-run with --apply to write it)")
        return 0
    print(f"  written: {got['path']}")
    print(f"\nNow set this in the collection's TOML, or it is refused:\n"
          f'  codex = "{got["after"]}"')
    return 0


def cmd_records_build(a) -> int:
    from app.hold import equivalence, records as rec
    name, codex = col.resolve(a.root, a.collection)
    _take_write_lock(a.root, name, "hold records build")
    # 03 §3.4: records are built by the Codex's image or an equivalent one
    equivalence.require_reference(a.root, name, codex, use="build its records")
    # 07 §7.3's `build <collection> [--snapshot ID]`: the origin is the
    # collection's own, `<root>/<name>/origin` (07 §7.8); --corpus names
    # another, for a node whose origins are not there yet
    corpus = a.corpus or (Path(a.root) / name / "origin")
    # Invariant 2: SIGTERM or SIGINT finishes the current unit and exits 130.
    # Without this a stop KILLED the build: its parse pool's children were
    # re-parented to init and kept running (2026-09-21).
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_a: stop.set())
        except (ValueError, OSError):                # not the main thread
            pass
    result = rec.build(a.root, a.collection, corpus_dir=corpus,
                       snapshot_id=a.snapshot_id,
                       records_per_shard=a.records_per_shard, stop=stop)
    return 130 if result.interrupted else 0


def cmd_records_validation(a) -> int:
    from app.hold import records as rec
    n = rec.write_validation(a.root, a.collection)
    print(f"validation.json: {n} bytes")
    return 0


def cmd_snapshot_create(a) -> int:
    from app.hold import collection as c, equivalence, snapshot as sn
    ref_name, ref_codex = c.resolve(a.root, a.collection)
    _take_write_lock(a.root, ref_name, "hold snapshot create")
    equivalence.require_reference(a.root, ref_name, ref_codex,
                                  use="create its snapshots")
    snap = sn.create(a.root, a.collection, snapshot_id=a.snapshot_id)
    print(snap.hash)
    # 09 §9.4's sidecar, and 02 §2.2's meaning: "a signature says *I
    # computed this too*" — so it is written where the snapshot was
    # computed, and merged rather than replaced, because a sidecar is a
    # set two peers join by union. A peer with no key still produces the
    # snapshot: 07 §7.8 says an unsigned peer's check and search work, and
    # §9.4 holds an unsigned snapshot rather than refusing it.
    name, _ = c.resolve(a.root, a.collection)
    from app.bundles import keys as node_keys
    try:
        key = node_keys.load_signing_key()
    except Exception as e:                              # noqa: BLE001
        print(f"note: unsigned — {e}", file=sys.stderr)
        return 0
    doc = sn.sign_snapshot(a.root, name, snap, key, now=_utc_now())
    print(f"  signed by   {len(doc['signatures'])} key(s)")
    return 0


def cmd_vectors_export(a) -> int:
    """`hold vectors export` — one shard's vectors as 09 §9.7's bundle.

    Exit 3 when the encoder is unreachable, not 1: a bundle needs the
    sixteen challenge rows embedded now (§9.7's T2), so an absent encoder
    makes this INCONCLUSIVE in 03 §3.1's sense — nothing is wrong with
    the collection — and 07 §7.7 reserves 3 for that.
    """
    from pathlib import Path

    from app import embeddings
    from app.bundles.keys import load_signing_key
    from app.hold import collection as col, embedder as emb, export as exp, \
        records as rec

    name, codex = col.resolve(a.root, a.collection)
    # The local side of the collection — manifest, index, vector table —
    # built the one way every stage builds it (07 §7.8, records.local_collection).
    local = rec.local_collection(a.root, name, codex)
    # Invariant 9, "not exchanged": the one `hold vectors` command that never
    # asked — import and build do (verification pass, 2026-09-13)
    from app import licensing
    licensing.require_ack(local, use="its vectors are exported as a bundle")
    from app import copy_state
    copy_state.require_in_use(a.root, name, codex, local,
                              use="exporting its vectors")     # 03 §3.6: not served
    try:
        recipe = _recipe_for(codex, name)
    except emb.GateDeferred as e:
        print(f"louise: deferred: {e}", file=sys.stderr)
        return 3

    # the challenge's vectors LEAVE this peer: from a version-3 collection
    # they come from a validated run or not at all (03 §3.3)
    from app import run_check
    under_a_validated_run = run_check.checking(a.root, name, codex, recipe)

    def embed(texts):
        try:
            return under_a_validated_run(texts)
        except emb.GateDeferred:
            raise
        except Exception as e:                      # noqa: BLE001
            raise emb.GateDeferred(
                f"the encoder at {recipe.url} could not embed the challenge "
                f"texts: {e}") from e

    try:
        out = exp.export(
            a.root, a.collection, shard=a.shard, dest=Path(a.out),
            vectors_for=lambda ids: embeddings.vectors_by_chunk_id(
                ids, collection=local),
            embed=embed, key=load_signing_key(), snapshot_id=a.snapshot,
            max_chunks=a.max_chunks or exp.BUNDLE_CHUNKS)
    except emb.GateDeferred as e:
        print(f"louise: deferred: {e}", file=sys.stderr)
        return 3

    # 09 §9.10: a `local` row per bundle, accepted once this peer's own ladder
    # has run over it (`share contribute`), not before
    from app import db as _db
    from app.share import coverage as cov
    with _closing(_db.connect(local)) as mcon:
        cov.record_exported(mcon, codex, out, producer=env_key_text(load_signing_key()),
                            now=_utc_now())
    for b in out["bundles"]:
        print(f"{b['bundle_id']}")
        print(f"  {b['path']}  —  {b['chunks']} chunks over {b['uids']} records")
    print(f"  shard       {out['shard']}  of snapshot {out['snapshot_id']}")
    print(f"  carries     {out['chunks']} chunks over {out['uids']} of "
          f"{out['records_in_shard']} records in "
          f"{len(out['bundles'])} bundle(s)")
    print(f"  space       {out['dim']} x {out['dtype']}")
    return 0


def _ack_origin_unchecked(a, name, codex) -> int:
    """`hold collection ack <collection> --origin-unchecked` — the other thing
    an operator may acknowledge (07 §7.7; 03 §3.6): that this peer's own
    `records-vs-origin` COULD NOT RUN while every other test of the copy
    passed. It prints what passed, what could not run and when it was last
    tried, waits for consent, and records the decision with its time and the
    peer's key beside the licence acknowledgement. The copy is then in use
    like any other, with no mark.

    Refused (exit 2) while another test is open or has failed, when the
    origin check has in fact run, and when it has never been tried: "a
    decision stands in for a test that cannot run, never for one that
    failed". There is no timeout and no default, as there is none for a
    licence."""
    from app import copy_state
    state = _copy_state(a.root, name, codex)
    for t in state.tests:
        print(f"  {t.status:<13} {t.name:<17} {t.detail}")
    why_not = copy_state.origin_ack_refusal(state)
    if why_not:
        raise CodexError(f"{name}: not recorded — {why_not}")
    if not a.yes:
        print("-" * 72)
        reply = input(f"Use {name} although its origin could not be checked? The "
                      f"check still runs when it can. [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("not acknowledged; nothing was written. `louise hold collection "
                  f"forget {name}` rejects the copy", file=sys.stderr)
            return 2
    out = copy_state.record_origin_ack(a.root, name, codex, now=_utc_now())
    print(f"acknowledged  origin unchecked  {out}")
    return 0


def _ack_web_sources(a) -> int:
    """`hold collection ack --web-sources`: the node's one acceptance of the
    web-sources disclaimer (04 §4.4, 09 §9.13). Printed unless `--yes`, like
    every acknowledgement: one nobody read is a checkbox."""
    from app import licensing
    if not a.yes:
        print(licensing.WEB_DISCLAIMER.read_text(encoding="utf-8"))
    ack = licensing.accept_web_sources(_utc_now())
    print(f"accepted: {ack['terms_sha256']} at {ack['time']}", file=sys.stderr)
    return 0


def cmd_collection_ack(a) -> int:
    """`hold collection ack <collection>` — record 09 §9.13's acknowledgement.

    Invariant 9: a yellow- or red-class collection's data is not USED —
    not built, not imported, not exchanged — until a peer has recorded
    that it read the terms. The record is a timestamp and the sha256 of
    the terms TEXT, so it is an acknowledgement OF A TEXT and not of a
    collection: "a collection whose Codex names a hash this peer has
    acknowledged under any collection is acknowledged" (§9.13).

    The terms are PRINTED unless `--yes`, because an acknowledgement
    nobody read is a checkbox, and 07 §7.7 says `ack --yes` "records
    without printing the terms" — which is the flag a script uses, not
    the default a person gets.
    """
    import json

    from app.hold import collection as col, drag as drag_mod

    if getattr(a, "web_sources", False):
        return _ack_web_sources(a)
    if not a.collection:
        raise CodexError("ack names a collection, or takes --web-sources (04 §4.4)")
    name, codex = col.resolve(a.root, a.collection)
    if getattr(a, "origin_unchecked", False):
        return _ack_origin_unchecked(a, name, codex)
    want = codex.license.terms_sha256
    if not want:
        print(f"{name} is {codex.license.cls}-class and names no terms text; "
              f"there is nothing to acknowledge (09 §9.13)")
        return 0

    terms = a.root / name / "drag" / "LICENSE-TERMS"
    if not terms.is_file():
        raise CodexError(
            f"{name}: the Codex pins terms {want[:23]}… and this copy has no "
            f"drag/LICENSE-TERMS. It is one of the three members fetchable "
            f"BEFORE an acknowledgement (04 §4.4), so a copy without it "
            f"cannot be acknowledged — fetch it first")

    import hashlib
    got = "sha256:" + hashlib.sha256(terms.read_bytes()).hexdigest()
    if got != want:
        raise CodexError(
            f"{name}: drag/LICENSE-TERMS hashes to {got[:23]}… and the Codex "
            f"pins {want[:23]}…. Terms are byte-exact and never converted — a "
            f"CRLF checkout silently invalidates every recorded ack (09 §9.13). "
            f"Nothing is recorded")

    if not a.yes:
        print(terms.read_text(encoding="utf-8", errors="replace"))
        print("-" * 72)
        reply = input(f"Acknowledge these terms for {name}? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("not acknowledged; nothing was written", file=sys.stderr)
            return 2

    out = a.root / name / "state" / "ack.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(cx.canonical_json({
        "kind": "louise-ack", "version": 1,
        "terms_sha256": want, "time": _utc_now(),
        "collection_id": codex.collection_id,
    }))
    print(f"acknowledged  {want}")
    print(f"  {out}")
    return 0


def archive_is_local(archive_id: str) -> bool:
    """True when the archive's vectors live in the CORPUS rather than in
    downloaded shards — `adapter="corpus-embedded"`, which is how the
    Upstash Wikipedia set is carried. Such an archive is unreachable
    without a corpus directory, and saying so beats finding nothing."""
    from app.bundles import genesis
    archive = genesis.ARCHIVES.get(archive_id)
    return bool(archive is not None and getattr(archive, "local", False))


def cmd_vectors_archive_import(a) -> int:
    """`hold vectors import <collection> --archive ID` — a published
    pre-embedded set, through the archive route.

    THIS ONE WRAPS, and deliberately: 07 §7.3 gives `import` one shape for
    "a signed bundle, or a public pre-embedded archive", but they are not
    one mechanism. A bundle is 09 §9.7's four files and goes through
    `app/hold/admit.py`'s ladder; an archive is millions of vectors in
    someone else's format, discovered or downloaded shard by shard, and
    `app/bundles/genesis.py` already does that — resumably, with a
    same-function proof per archive and a cache bound. The migration
    plan's own rule is wrap, do not rewrite, and step 4's note says this
    path SURVIVES the ladder rewrite.

    What the new command adds is the gate the old path never had: 03
    §3.5's archive refusal. "A key that resolves through the platform's
    registry to an archive this collection's `credits` does not name" is
    refused, because a Codex is written once and a collection cannot come
    to stand on work it never declared.
    """
    from app import db, embeddings, licensing
    from app.bundles import genesis
    from app.hold import collection as col, embedder as emb, records as rec, \
        template as tpl

    name, codex = col.resolve(a.root, a.collection)
    # `corpus_dir` matters for a LOCAL archive — one whose vectors live in
    # the corpus itself (`adapter="corpus-embedded"`, the Upstash set).
    # `_local_shards` reads `collection.corpus_dir`, so without one this
    # command could not reach such an archive at all: it resolved None and
    # found nothing, silently. A fetched archive is unaffected, which is
    # why it went unnoticed.
    local = rec.local_collection(a.root, name, codex, corpus_dir=a.corpus)
    if archive_is_local(a.archive) and not a.corpus:
        raise CodexError(
            f"archive {a.archive!r} keeps its vectors in the corpus itself, "
            f"so --corpus DIR is required: there is nowhere else to read "
            f"them from")
    licensing.require_ack(
        local, use="an archive's vectors are imported into this collection")
    from app import copy_state
    copy_state.require_in_use(a.root, name, codex, local,
                              use="importing an archive's vectors into it")

    archive = genesis.ARCHIVES.get(a.archive)
    if archive is None:
        raise CodexError(
            f"no archive {a.archive!r} in the platform's registry; it has "
            f"{', '.join(sorted(genesis.ARCHIVES)) or 'none'}")

    # 06 §6.6 and 03 §3.5: the archive must be one this Codex CREDITS.
    # `credits` is hashed into `collection_id`, so this cannot be satisfied
    # after the fact by editing anything — which is the point.
    credited = {c.archive for c in cx.archive_credits(codex)}
    if archive.archive_id not in credited:
        raise CodexError(
            f"{name} does not credit archive {archive.archive_id!r} (06 "
            f"§6.6). It credits {', '.join(sorted(credited)) or 'no archive'}. "
            f"A Codex is written once, so a collection cannot come to stand "
            f"on work it never declared; a collection that should draw on "
            f"this archive is a different collection, created naming it")

    embed_fn = None
    try:
        recipe = _recipe_for(codex, name)
        embed_fn = _checking_embed(a.root, name, codex, recipe)
    except emb.GateDeferred as e:
        # The same-function proof needs the encoder. genesis DEFERS a shard
        # rather than admitting it unproven, which is the right refusal —
        # so say so here rather than letting it look like a failure.
        print(f"louise: deferred: {e}", file=sys.stderr)
        return 3

    con = db.connect(local)
    try:
        out = genesis.import_archive(
            con, local, archive, shards=a.shards or None, embed_fn=embed_fn)
    finally:
        con.close()

    ok = [s for s in out["shards"] if s.get("ok")]
    print(f"{archive.archive_id}")
    print(f"  shards      {len(ok)} of {len(out['shards'])} admitted")
    print(f"  landed      {out['chunks']} chunks over {out['docs']} documents")
    for s in out["shards"]:
        if not s.get("ok"):
            print(f"  refused     {s.get('shard')}: {s.get('detail') or s}",
                  file=sys.stderr)
    return 0 if len(ok) == len(out["shards"]) else 1


def cmd_vectors_import(a) -> int:
    """`hold vectors import` — a peer's bundle through 03 §3.5's ladder.

    Exit 0 when the bundle landed (T3 may be SKIPPED — the chunks are
    then *unverified here*, which §3.5 allows and names), 1 when a tier
    refused it, 2 on a malformed or inadmissible bundle, 3 when the
    encoder was unreachable AND `--require-t3` asked for the spot
    recompute. Without that flag an absent encoder is not a deferral: the
    other three tiers are what make a bundle usable, and refusing to
    import for want of a GPU would make importing a privilege of the
    nodes that need it least.
    """
    import os
    from pathlib import Path

    from app import embeddings, licensing
    from app.hold import admission as adms, admit as adm, collection as col, \
        drag, embedder as emb, land as landing, records as rec, vectors as vec

    if getattr(a, "archive", None):
        return cmd_vectors_archive_import(a)

    name, codex = col.resolve(a.root, a.collection)
    _take_write_lock(a.root, name, "hold vectors import")
    local = rec.local_collection(a.root, name, codex)
    # Invariant 9, the IMPORTER half (migration plan step 4f). However the
    # bundle arrived — a peer copy, a hand-populated folder — a yellow- or
    # red-class collection without a recorded acknowledgement is not
    # imported. The archive half already sits at `genesis.import_shard`.
    licensing.require_ack(
        local, use="a peer's vectors are imported into this collection")
    from app import copy_state
    copy_state.require_in_use(a.root, name, codex, local,
                              use="importing a peer's vectors into it")

    member = a.root / name / "drag" / \
        drag.calibration_paths(codex.space.calibration.set)[0]
    if not member.is_file():
        raise CodexError(
            f"{name}: no calibration member at {member.name}. T0's envelope "
            f"and T2's challenge are both read from it (09 §9.12), so a peer "
            f"without one cannot judge a bundle at all")
    reference, envelope = _read_member(member)

    node = adms.connect(a.root)
    header, rows, possession, bundle_id = vec.read(Path(a.bundle))

    # 08 §8.2's aggregate limit. OFF unless asked for, because an operator
    # naming a path is not material "arriving from other peers": §8.2
    # governs what this peer accepts and forwards on its own initiative,
    # and rate-limiting the operator's own command would refuse the one
    # arrival nobody needs protecting from. Step 9's `share fetch` is the
    # caller that passes it always.
    if a.budget:
        limits = _admission_limits()
        verdict = adms.offer(node, header["producer_pubkey"],
                             _bundle_bytes(Path(a.bundle)), kind="bundle",
                             ref=bundle_id, limits=limits)
        if verdict in (adms.QUEUED, adms.REFUSED):
            b, n = adms.spent(node)
            print(f"louise: {verdict}: 08 §8.2's hourly allowance for keys "
                  f"whose work this peer has not seen reproduced is spent "
                  f"({b} of {limits.bytes_per_hour} bytes, {n} of "
                  f"{limits.objects_per_hour} objects). This is not a "
                  f"judgement about the sender and is not remembered.",
                  file=sys.stderr)
            return 2

    # What this peer knows, looked up from the VERIFIED header and not a
    # moment earlier (`admit.judge`'s note).
    snapshot = landing.snapshot_named(a.root, name,
                                      a.snapshot or header["snapshot_id"])
    records = landing.records_for(a.root, name, snapshot, header["shard"],
                                  header["uids"])
    mine = {r["uid"]: r["content_hash"] for r in records}
    cut = landing.cut(codex, records)

    embed = None
    try:
        recipe = _recipe_for(codex, name)
        embed = _checking_embed(a.root, name, codex, recipe)
    except emb.GateDeferred as e:
        if a.require_t3:
            print(f"louise: deferred: {e}", file=sys.stderr)
            return 3

    # 03 §3.5: the T3 sample is drawn with this peer's PRIVATE randomness
    # and never from the bundle — a producer who could predict it would
    # re-roll a nonce until the sample missed its corruption. Fresh per
    # run, like `embedder.run_gate`'s, and printed so the check can be
    # repeated deliberately with `--seed`.
    seed = bytes.fromhex(a.seed) if a.seed else os.urandom(32)

    report = adm.judge(codex, header, rows, possession, bundle_id,
                       envelope=envelope, reference=reference, mine=mine,
                       text_of=lambda cid: (cut.get(cid) or {}).get("text"),
                       embed=embed, seed=seed,
                       strict_allow=set(a.allow) if a.strict else None)
    t3 = report.tier(adm.T3)
    if (a.require_t3 and t3 is not None and t3.verdict == adm.SKIPPED
            and "no compliant encoder" in t3.why):
        # `--require-t3` ASKED for the spot recompute: a T3 that could not run
        # — no encoder, or one that is not a validated run (03 §3.3) — defers
        # the import instead of landing chunks "unverified here"
        print(f"louise: deferred: T3 could not run — {t3.why}; nothing was written",
              file=sys.stderr)
        return 3

    known = adms.reproduced(node, report.producer)
    # 08 §8.1: "no countermeasure may require knowing whom to trust in
    # advance". A first-seen key is reported as such and admitted anyway;
    # the old importer refused it, which is the TOFU this replaces.
    standing = (f"{known['bundles']} bundle(s) accepted here before" if known
                else "FIRST SEEN — and admitted, which is the point (08 §8.1)")
    print(f"{report.bundle_id}")
    print(f"  producer    {report.producer[:31]}…  {standing}")
    print(f"  shard       {header['shard']}  of snapshot {header['snapshot_id']}")
    for t in report.tiers:
        print(f"  {t.tier:<11} {t.verdict.upper():<8} {t.why}")
    for uid, why in report.refused:
        print(f"  refused     {uid}: {why}")
    if not report.ok:
        print(f"  seed        {seed.hex()}")
        print("REFUSED — nothing was written", file=sys.stderr)
        return 1

    out = landing.land(
        codex, report, records=records,
        write=lambda r: embeddings.upsert_chunks(r, collection=local,
                                                 dtype=codex.space.dtype))
    # 08 §8.2's one durable fact, and it is recorded only on a bundle that
    # PASSED: a key is known here by what it produced, never by having
    # sent something. This is what later makes its material not
    # "unverified" and so never queued.
    adms.record_reproduced(node, report.producer, ref=report.bundle_id,
                           chunks=out["written"])
    # 09 §9.10: the coverage map's row — this bundle, accepted here, now
    from app import db as _db
    from app.share import coverage as cov
    with _closing(_db.connect(local)) as mcon:
        cov.record_from_report(mcon, codex, report, header, source=cov.FETCHED, now=_utc_now())
    for cid, why in out["unplaceable"]:
        print(f"  unplaceable {cid}: {why}", file=sys.stderr)
    print(f"  seed        {seed.hex()}")
    print(f"  landed      {out['written']} chunks over {len(mine)} records"
          f"{f', {report.padded} row(s) dropped before the tiers' if report.padded else ''}")
    return 0


def _closing(con):
    from contextlib import closing
    return closing(con)


def env_key_text(key) -> str:
    from app.hold import envelope as env
    return env.key_text(key.verify_key)


def cmd_vectors_show(a) -> int:
    """`hold vectors show` (07 §7.3; 09 §9.10): the coverage map — per
    shard the chunks covered here, the leader and its window among the
    live peers, the producers and their signed work, and the collection's
    vitality (04 §4.7). Inspection, not use: it reads what is held."""
    import time as _time
    from app import copy_state, identity
    from app.share import coverage as cov, peers
    name, codex = col.resolve(a.root, a.collection)
    snapshot = None
    try:
        snapshot = _latest_snapshot(a.root, name, None)
    except CodexError:
        pass
    now = _time.time()
    try:
        own_key = identity.own()
    except Exception:   # noqa: BLE001 — a peer with no key leads nothing
        own_key = None
    with _closing(peers.connect()) as node_con:
        live = cov.live_peers(codex, node_con, now=now, own_key=own_key,
                              own_benchmarks=cov.own_benchmarks(a.root, name, codex, own_key))
    # the manifest opened for READING what the map says, ack or no ack: an
    # inspection, not a use (invariant 9 gates the stages, not `show`)
    with _closing(cov.manifest(a.root, name)) as mcon:
        out = cov.show(codex, snapshot, mcon,
                       attestations=copy_state.attestations(a.root, name, codex),
                       peers=live, now=now)
    _print(out)
    return 0


def cmd_vectors_build(a) -> int:
    """`hold vectors build` — this peer's own chunks, embedded here.

    Exit 0 when it finished (including "nothing to do"), 2 when a rule
    stood in the way — the licence, or no passing calibration check for
    this space — 3 when the encoder is unreachable, 130 when a signal
    arrived and the batches already written stayed written.
    """
    import threading

    from app import config, embeddings, licensing
    from app.hold import collection as col, embedder as emb, land as landing, \
        records as rec, vectors_build as vb

    name, codex = col.resolve(a.root, a.collection)
    _take_write_lock(a.root, name, "hold vectors build")
    local = rec.local_collection(a.root, name, codex)
    # Embedding is a USE of the text (invariant 9): a corpus that may not
    # be read may not be embedded either.
    licensing.require_ack(local, use="this collection's text is embedded")
    from app import copy_state
    copy_state.require_in_use(a.root, name, codex, local, use="embedding for it")
    # 07 §7.3's gate, and the one b10630 cost: an encoder that drifted
    # produces vectors nobody can tell from good ones.
    #
    # FROM CODEX VERSION 3 THE GATE IS PER ANSWER (03 §3.3): every batch's
    # answers must name a run this peer has validated, checked before that
    # batch is written (`before_write` below). A run it has not tested is
    # gated at once and the build goes on under it; an endpoint that states
    # no run writes nothing, exit 2. There is no single statement to require
    # first, and nothing to accept.
    from app.hold import runs as hold_runs
    by_run = hold_runs.applies(codex)
    if not by_run:
        vb.require_statement(a.root, name, codex)

    snapshot = _latest_snapshot(a.root, name, a.snapshot)
    if a.delta:
        previous = _snapshot_before(a.root, name, a.delta)
        shards = vb.delta_shards(previous, _latest_snapshot(a.root, name, a.delta))
    elif a.shard:
        from app.hold import export as exp
        shards = [exp._shard_of(snapshot, a.shard)]
    elif a.slice:
        shards = [s for s in snapshot.shards if s.slice in set(a.slice)]
        if not shards:
            raise CodexError(
                f"{name}: snapshot {snapshot.snapshot_id} has no shard in "
                f"{', '.join(a.slice)}")
    else:
        shards = sorted(snapshot.shards, key=lambda s: (s.slice, s.n))

    embed = None
    if not a.dry_run:
        try:
            recipe = _recipe_for(codex, name)
        except emb.GateDeferred as e:
            print(f"louise: deferred: {e}", file=sys.stderr)
            return 3

        seen: set = set()

        def embed(texts):                            # noqa: F811
            seen.clear()
            try:
                if by_run:
                    return embeddings.embed_texts(texts, recipe=recipe, runs=seen)
                return embeddings.embed_texts(texts, recipe=recipe)
            except Exception as e:                   # noqa: BLE001
                raise emb.GateDeferred(
                    f"the encoder at {recipe.url} could not embed: {e}") from e

    stop = threading.Event()
    before_write = None
    if by_run and not a.dry_run:
        from app import run_check

        def before_write():
            run_check.guard(a.root, name, codex, recipe, set(seen), stop=stop.is_set,
                            log=lambda m: print(m, file=sys.stderr))
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_a: stop.set())
        except (ValueError, OSError):                # not the main thread
            pass

    try:
        out = vb.build(
            codex, shards=shards,
            records_for=lambda s: landing.records_for(
                a.root, name, snapshot, f"{s.slice}/{s.n:05d}", None),
            vectors_for=lambda ids: embeddings.vectors_by_chunk_id(
                ids, collection=local),
            embed=embed or (lambda t: []),
            write=lambda rows: embeddings.upsert_chunks(
                rows, collection=local, dtype=codex.space.dtype),
            batch=config.EMBED_SLICE_CHUNKS, stop=stop,
            log=lambda m: print(m, file=sys.stderr), dry_run=a.dry_run,
            before_write=before_write)
    except emb.GateDeferred as e:
        print(f"louise: deferred: {e}", file=sys.stderr)
        return 3

    what = "would embed" if a.dry_run else "embedded"
    print(f"{out.chunks_written if not a.dry_run else out.chunks_seen} "
          f"chunk(s) {what} over {out.shards} shard(s)")
    print(f"  missing     {out.chunks_seen} of the shards' chunks had no vector here")
    print(f"  tokens      ~{out.tokens} (the chunker's estimate)")
    if out.interrupted:
        print("  interrupted at a batch boundary; what was written stayed "
              "written and a re-run continues (07 §7.7)", file=sys.stderr)
        return 130
    return 0


def _snapshot_before(root, name, snapshot_id):
    """The snapshot before `snapshot_id` in this peer's copy, or None.

    Ids sort chronologically by construction (09 §9.4), so "before" is a
    name comparison and not a chain walk.
    """
    from app.hold import collection as col, snapshot as snap
    d = col.drag_of(Path(root), name) / "snapshots"
    ids = sorted(p.stem for p in d.glob("*.json")
                 if not p.name.endswith(".sig.json")) if d.is_dir() else []
    earlier = [i for i in ids if i < snapshot_id]
    return snap.load(d / f"{earlier[-1]}.json") if earlier else None


def cmd_vectors_check(a) -> int:
    """`hold vectors check [--drop]` — 03 §3.2's `vectors-sample`, locally.

    Exit 0 on a pass, 1 on a fail, 3 when the encoder was unreachable or
    the sample turned up nothing to reproduce: 03 §3.1's third outcome is
    INCONCLUSIVE, "neither a failure nor a pass", and reporting that as a
    pass is the one outcome that would let an empty table certify itself.
    """
    import os

    from app import embeddings
    from app.hold import collection as col, embedder as emb, land as landing, \
        records as rec, vectors_check as vck

    name, codex = col.resolve(a.root, a.collection)
    if getattr(a, "drop", False):          # a Namespace built by hand may lack it
        _take_write_lock(a.root, name, "hold vectors check --drop", exit_code=2)
    local = rec.local_collection(a.root, name, codex)
    # Invariant 9: the sample's text is embedded (and --drop deletes
    # vectors) — use of the corpus, as `bundle resweep` is
    from app import licensing
    licensing.require_ack(local, use="its text is embedded to check its vectors")
    snapshot = _latest_snapshot(a.root, name, a.snapshot)

    embed = None
    try:
        recipe = _recipe_for(codex, name)
        embed = _checking_embed(a.root, name, codex, recipe)
    except emb.GateDeferred:
        pass

    # `check` asks for a shard's chunk ids before it asks for any of its
    # text, so the cut is done once per sampled shard and the text is read
    # out of it. Cutting a shard is the expensive part — a re-cut per
    # sampled chunk would be sixteen passes over the same records.
    cut: dict[str, dict] = {}

    def chunk_ids_for(entry):
        cut.clear()
        cut.update(landing.cut(codex, landing.records_for(
            a.root, name, snapshot, f"{entry.slice}/{entry.n:05d}", None)))
        return sorted(cut)

    def text_of(cid):
        entry = cut.get(cid)
        return entry["text"] if entry else None

    report = vck.check(
        codex, snapshot, seed=bytes.fromhex(a.seed) if a.seed else os.urandom(32),
        chunk_ids_for=chunk_ids_for, text_of=text_of,
        vectors_for=lambda ids: embeddings.vectors_by_chunk_id(ids, collection=local),
        embed=embed, fraction=a.sample,
        drop=(lambda ids: embeddings.delete_chunks(ids, collection=local))
        if a.drop else None)

    print(f"{report.verdict.upper()}  {report.why}")
    print(f"  seed        {report.seed.hex()}")
    print(f"  tolerance   {report.tolerance}  quorum {report.min_pass}/{report.n}")
    for s in report.shards:
        print(f"  {s.verdict:<13} {s.shard}  {s.passed}/{s.checked} of "
              f"{s.sampled} drawn, median {s.median:.4f}, worst {s.worst:.4f}")
    for cid in report.failing:
        print(f"  failing     {cid}", file=sys.stderr)
    if report.verdict == vck.INCONCLUSIVE:
        return 3
    return 1 if report.verdict == vck.FAIL else 0


def _checking_embed(root, name, codex, recipe):
    """The encoder, for a CHECK (03 §3.3): "a check runs under a validated
    run. An answer that names another makes the check inconclusive." From
    Codex version 3; an older collection embeds as it always did
    (`app/run_check.py`: `checking`)."""
    from app import run_check
    return run_check.checking(root, name, codex, recipe,
                              log=lambda m: print(m, file=sys.stderr))


def _latest_snapshot(root, name, snapshot_id=None):
    """The named snapshot, or the latest this peer holds.

    Snapshot ids sort chronologically by construction (09 §9.4), so the
    last name IS the latest — no need to open every file to find out.
    """
    from app.hold import collection as col, drag as drag_mod, snapshot as snap
    d = col.drag_of(Path(root), name) / "snapshots"
    if snapshot_id:
        p = col.drag_of(Path(root), name) / drag_mod.snapshot_path(snapshot_id)
        if not p.is_file():
            raise CodexError(f"no snapshot {snapshot_id!r} in this copy")
        return snap.load(p)
    ids = sorted(p.stem for p in d.glob("*.json")
                 if not p.name.endswith(".sig.json")) if d.is_dir() else []
    if not ids:
        raise CodexError(
            f"{name} holds no snapshot; a sampled check draws over a "
            f"snapshot's shards (03 §3.2) and there is not one yet")
    return snap.load(d / f"{ids[-1]}.json")


def run_check(a):
    """`hold check`'s work (03 §3.2), shared with `share attest`: the plan
    run and every completed check attested. Returns (name, codex, snapshot,
    seed, report) with the attestation ids on the report."""
    import os

    from app import embeddings
    from app.hold import check as chk, collection as col, drag, \
        embedder as emb, records as rec

    name, codex = col.resolve(a.root, a.collection)
    local = rec.local_collection(a.root, name, codex)
    # Invariant 9: the plan embeds sampled text, and its attestations are
    # made to be exchanged — use of the corpus either way
    from app import licensing
    licensing.require_ack(local, use="its text is embedded to check it")
    snapshot = _latest_snapshot(a.root, name, a.snapshot)
    seed = bytes.fromhex(a.seed) if a.seed else os.urandom(32)

    member = a.root / name / "drag" / \
        drag.calibration_paths(codex.space.calibration.set)[0]
    reference = _read_member(member)[0] if member.is_file() else None

    embed = None
    try:
        recipe = _recipe_for(codex, name)
        embed = _checking_embed(a.root, name, codex, recipe)
    except emb.GateDeferred:
        pass

    report = chk.run(
        a.root, name, codex, snapshot, seed=seed,
        checks=a.checks.split(",") if a.checks else None, sample=a.sample,
        embed=embed, reference=reference,
        reparse=_reparse_from(codex, name, getattr(a, "origin_dir", None)),
        origin_unit=_origin_unit_from(codex, name, getattr(a, "origin_dir", None)),
        origin_bytes=(int(a.origin_mib) << 20) if getattr(a, "origin_mib", None) else None,
        vectors_for=lambda ids: embeddings.vectors_by_chunk_id(
            ids, collection=local),
        log=lambda m: print(m, file=sys.stderr))

    written: dict[str, str] = {}
    if any(o.attestable for o in report.outcomes):
        from app.bundles import keys as node_keys
        try:
            key = node_keys.load_signing_key()
        except Exception as e:                          # noqa: BLE001
            # 07 §7.8: "`NODE_KEY_PATH` unset means this peer cannot sign …
            # import, check and search work without it". The report counts
            # on this peer alone, which is what §7.3 says it does.
            print(f"note: unsigned — {e}", file=sys.stderr)
            key = None
        # 09 §9.8: `platform_image` is "the image the verifier ran" — never
        # the Codex's, which it wrote until 2026-09-21 and so turned a new
        # image's equivalence failure into the collection's own (03 §3.4).
        # A process that cannot name its image attests nothing.
        from app.hold import equivalence
        ran_in = equivalence.running_image()
        if key is not None and ran_in is None:
            print("note: unattested — this process does not know the image it "
                  "runs in (LOUISE_PLATFORM_IMAGE, which the deploy sets), and an "
                  "attestation names it (09 §9.8)", file=sys.stderr)
        if key is not None and ran_in is not None:
            written = chk.write_attestations(
                a.root, name, report, codex, snapshot, key,
                platform_image=ran_in, now=_utc_now())
    report = dataclasses.replace(report, attestations=written)
    return name, codex, snapshot, seed, report


def cmd_check(a) -> int:
    """`hold check` — run the plan (03 §3.2) and attest what completed.

    Exit 0 when nothing failed, 1 when a check did. An INCONCLUSIVE check
    is neither (03 §3.1) and does not fail the run: it produces no
    attestation, is reported with its reason, and is retried.
    """
    from app.hold import check as chk
    name, codex, snapshot, seed, report = run_check(a)
    written = report.attestations

    # 03 §3.6: an origin check that COULD NOT RUN yields no attestation, and
    # the operator who is asked to decide is shown "when it was last tried" —
    # so that, and only that, is noted in `state/`. Never sent, never a verdict.
    tried = report.outcome("records-vs-origin")
    if tried is not None and tried.result == chk.INCONCLUSIVE:
        from app import copy_state
        copy_state.note_origin_attempt(a.root, name, codex, why=tried.why, now=_utc_now())

    if a.json:
        _print(report.to_json())
    else:
        print(f"{'PASS' if report.ok else 'FAIL'}  {name}  "
              f"snapshot {snapshot.snapshot_id}  seed {seed.hex()}")
        for o in report.outcomes:
            aid = written.get(o.check)
            print(f"  {o.result.upper():<13} {o.check:<18} {o.why}"
                  f"{f'  [{aid[:20]}…]' if aid else ''}")
    return 0 if report.ok else 1


def _reparse_from(codex, name: str, origin_dir):
    """`(uid, origin locator) -> the record line that origin produces now`.

    03 §3.2 wants the origin "re-fetched … and re-parsed with the Codex's
    toolchain". `origin_dir` is that origin as a local base — a fresh
    mirror, a re-download, or the corpus directory itself. The last is
    weaker and worth naming: re-parsing the copy we kept proves the parser
    is deterministic and that the line matches the bytes on disk, not that
    those bytes are still what the origin serves. It is what an offline
    peer can do, and `origin_hash` is what makes even that meaningful.
    """
    if not origin_dir:
        return None
    from pathlib import Path

    from app import collections as collections_mod, layers as layers_mod, plugin_io
    from app.pack import build as pack_build, records as pack_records

    base = Path(origin_dir)
    # THE CODEX COMES WITH IT. 03 §3.2 asks for the origin "re-parsed with
    # the Codex's toolchain", and two of the things that decides are in the
    # Codex and nowhere else: the layer rules (invariant 1) and
    # `record_line` — a shape-2 collection writes `figures` on every line,
    # so a re-parse that produced shape 1 would differ from every record it
    # was checking and report the whole sample as failing.
    collection = collections_mod.Collection(
        name=name, plugin=codex.plugin, profile=None,
        embed_mode_default=None, corpus_dir=base,
        manifest_path=base / "manifest.sqlite", fts_dir=base,
        lance_dir=base, acquire_enabled=False, license_ack=None,
        layers=layers_mod.layers_for(codex), codex=codex)
    plugin = collection.plugin_instance()

    def reparse(uid: str, locator: str) -> bytes:
        local, path = plugin_io.local_locator(plugin, locator, base)
        if not local.exists():
            raise FileNotFoundError(str(local))
        row = {"uid": uid, "native_id": uid.rsplit(":", 1)[-1], "path": path}
        record, _layer, _blob, _plates = pack_build._record_of(
            collection, plugin, row)
        return pack_records.canonical_line(
            record, record_line=int(getattr(codex, "record_line", 1) or 1))

    return reparse


def _origin_unit_from(codex, name: str, origin_dir):
    """`origin locator -> (the origin file it points into, its size in bytes)`,
    for `records-vs-origin`'s bound (app/hold/check.py: `ORIGIN_BYTES`). With
    the origin as a local base the size is the file's; a file that is not
    there has none, and its records are reported as not there, as before."""
    if not origin_dir:
        return None
    from pathlib import Path

    from app import collections as collections_mod, plugin_io
    base = Path(origin_dir)
    plugin = collections_mod.PLUGIN_REGISTRY[codex.plugin]

    # ONE `stat` PER FILE, NOT PER RECORD. Measured on the node (pubmed,
    # 2026-09-20): 1,998,732 sampled records point into 651 files, and weighing
    # the file again for every record took 51 s of a check that reads the
    # shards themselves in 34. A unit and its size depend on the locator's file
    # part alone — what comes after §9.5's `#` is a member inside it.
    seg = getattr(plugin, "LOCATOR_SEGMENT_SEP", None)
    weighed: dict[str, tuple[str, int | None]] = {}

    def origin_unit(locator: str) -> tuple[str, int | None]:
        key = (locator if seg and seg in locator
               else locator.partition(plugin_io.DEFAULT_MEMBER_SEP)[0])
        if key not in weighed:
            weighed[key] = plugin_io.origin_unit(plugin, locator, base)
        return weighed[key]

    return origin_unit


def cmd_records_check(a) -> int:
    """`hold records check` — 07 §7.3's alias, with `records` included.

    "`records check` is `hold check --checks roots,records[,
    records-vs-origin][,origin-seal]` and nothing else — `records` (03
    §3.2) is the only local check that proves what is inside a shard
    rather than that the file matches the snapshot, and leaving it out
    made `--sample F` on this command affect nothing unless `--origin`
    was given."
    """
    checks = ["roots", "records"]
    if a.origin_dir:
        checks += ["records-vs-origin", "origin-seal"]
    a.checks = ",".join(checks)
    return cmd_check(a)


def _admission_limits():
    """`platform.toml`'s `[admission]`, or 08 §8.2's defaults."""
    from app import platform_config
    from app.hold import admission as adms
    c = platform_config.load().admission
    return adms.Limits(bytes_per_hour=c.bytes_per_hour,
                       objects_per_hour=c.objects_per_hour,
                       queue_bytes=c.queue_bytes)


def _bundle_bytes(src) -> int:
    """What this bundle costs the hourly allowance: its four files (09
    §9.7), not the header's `parquet_bytes` alone."""
    from app.hold import bundle as bd
    return sum((src / n).stat().st_size for n in bd.FILES
               if (src / n).is_file())


def _read_member(member):
    """`(vectors, envelope)` — 09 §9.12's two tensors, read once."""
    from safetensors.numpy import load_file
    tensors = load_file(str(member))
    for want in ("vectors", "envelope"):
        if want not in tensors:
            raise CodexError(f"{member.name}: no `{want}` tensor (09 §9.12)")
    return tensors["vectors"].tolist(), tensors["envelope"]


def cmd_embedder_check(a) -> int:
    """`hold embedder check` â the calibration gate of 03 Â§3.3.

    Exit 0 on a pass, 1 on a fail, 3 when the encoder was unreachable:
    03 Â§3.1's third outcome is INCONCLUSIVE, "neither a failure nor a
    pass", and 07 Â§7.7 reserves 3 for exactly that. A gate that exited 1
    on an unreachable endpoint would flag a collection for a network
    problem.
    """
    from app.hold import collection as col, embedder as emb, runs as hold_runs
    name, codex = col.resolve(a.root, a.collection)
    _take_write_lock(a.root, name, "hold embedder check")     # it writes a statement (07 §7.7)
    if hold_runs.applies(codex):
        return _embedder_check_runs(a, name, codex)
    try:
        run, codex = _gate(a)
    except emb.GateDeferred as e:
        print(f"louise: deferred: {e}", file=sys.stderr)
        return 3
    verdict = "PASS" if run.ok else "FAIL"
    print(f"{verdict}  {run.why()}")
    print(f"  space       {run.space_id[:23]}â¦")
    print(f"  encoder     {codex.space.encoder.label or cx.encoder_field(codex)[1][:23]}")
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


def _embedder_check_runs(a, name, codex) -> int:
    """`hold embedder check` for a version-3 collection: the gate against
    THE RUN THAT ANSWERS NOW, for the document encoder and — in an
    asymmetric space — the query encoder, each a run of its own (03 §3.3).

    The statement is kept either way, one per run and role under
    `state/calibration/` (07 §7.3), so `--accept` has nothing left to do: a
    pass needs nobody's word, and a failure is kept so the run is not tried
    again against the same set. Exit 0 when every role passed, 1 when one
    failed, 2 when the endpoint states no run, 3 when it could not be asked.
    """
    from app import run_check
    from app.hold import embedder as emb, runs as hold_runs

    roles = ["document"] + (["query"] if codex.space.query.encoder is not None else [])
    # THE MEMBER FIRST, before the encoder is even looked for: without it
    # there is nothing to compare against, which is a rule in the way (exit
    # 2) and never a deferral. Asked in the other order, a node with no
    # encoder configured read "deferred" for a drag that had lost its member
    # — found on the reference node, where the test that holds this ran with
    # no collection file beside it (2026-09-19).
    for role in roles:
        run_check.reference(a.root, name, codex, role)
    try:
        recipe = _recipe_for(codex, name)
    except emb.GateDeferred as e:
        print(f"louise: deferred: {e}", file=sys.stderr)
        return 3
    worst = 0
    for role in roles:
        try:
            body = run_check.validate(a.root, name, codex, recipe, role=role)
        except emb.GateDeferred as e:
            print(f"louise: deferred: {role}: {e}", file=sys.stderr)
            return 3
        ok = hold_runs.passing(body, codex)
        print(f"{'PASS' if ok else 'FAIL'}  {role}")
        print(f"  space       {body['space_id'][:23]}…")
        print(f"  run         {body['run']}")
        print(f"  engine      {body['engine'].get('image', '')[:60]}")
        print(f"  driver      {body['driver']}")
        print(f"  tolerance   {codex.space.tolerance}")
        print(f"  sampled     {body['passed']} of {len(body['sampled'])} passed, "
              f"worst {body['worst']}, median {body['median']}")
        print(f"  fingerprint {body['fingerprint']}")
        print(f"  batches     {'as one text at a time' if body['batch_invariant'] else 'DIFFER from one text at a time: validated for single-text work only'}")
        print(f"  benchmark   {body['tokens_per_s']} tokens/s")
        print(f"  statement   {hold_runs.path_for(a.root, name, body['run'], role)}")
        worst = max(worst, 0 if ok else 1)
    return worst


def cmd_embedder_benchmark(a) -> int:
    """`hold embedder benchmark` â the same run, reporting the rate.

    03 Â§3.3 measures it on the same pass as the gate, so this is not a
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
          f"({'compliant' if run.ok else 'NOT COMPLIANT â the rate of a '
             'non-compliant encoder weights nothing (04 Â§4.5)'})")
    return 0 if run.ok else 1


def cmd_embedder_calibrate(a) -> int:
    _not_yet("--against",
             "two live encoder endpoints to compare, which is the one part "
             "of step 3 that wants the node rather than a mock (migration "
             "plan, step 3)")
    return 2


def _gate(a):
    """Run the gate for a collection, resolving its encoder the way 07 Â§7.8
    resolves one: the collection's own endpoint, and nothing else — there
    is no default encoder (operator, 2026-09-18)."""
    from app import embeddings
    from app.hold import collection as col, drag, embedder as emb

    name, codex = col.resolve(a.root, a.collection)
    member = a.root / name / "drag" / \
        drag.calibration_paths(codex.space.calibration.set)[0]
    if not member.is_file():
        raise CodexError(
            f"{name}: no calibration member at {member.name}. It is written "
            f"by `hold collection create` (09 Â§9.12) and is a member of every "
            f"snapshot; without it there is nothing to compare against")
    reference = _read_reference(member, codex)

    recipe = _recipe_for(codex, name)      # raises GateDeferred if unset

    def embed(texts):
        try:
            return embeddings.embed_texts(texts, recipe=recipe)
        except emb.GateDeferred:
            raise
        except Exception as e:                      # noqa: BLE001
            # 03 Â§3.1: an unreachable encoder is INCONCLUSIVE, not a
            # failure â "it produces no attestation and no flag, it is
            # reported with its reason, and it is retried".
            raise emb.GateDeferred(
                f"the encoder for {name} was unreachable or failed: {e}") from e

    return emb.run_gate(codex, reference=reference, embed=embed), codex


def _read_reference(member, codex):
    from safetensors.numpy import load_file
    tensors = load_file(str(member))
    if "vectors" not in tensors:
        raise CodexError(f"{member.name}: no `vectors` tensor (09 Â§9.12)")
    return tensors["vectors"].tolist()


def _recipe_for(codex, name):
    """The endpoint that serves this space's encoder.

    The one the collection's own file names, through
    `collections.recipe_for` — THE resolution its worker embeds with
    (invariant 10). A collection whose file names none, or that has no
    file, is non-compliant: everything that needs its encoder defers (exit
    3) and says why. There were two fallbacks, `[encoder_defaults.<plugin>]`
    and EMBED_URL, until "a default embedder is wrong" (operator,
    2026-09-18).

    It once read `EMBED_URL` alone. On the node that is the Qwen3-4B
    endpoint, so `hold check pubmed` sent MedCPT's texts there ("the encoder
    returned 2560 dimensions, the space is 768", 2026-09-18) and its
    calibration and vectors-sample could never pass. Behind a 768-wide
    endpoint the check would have compared pubmed's vectors with another
    model's.

    The rest of the recipe comes from the Codex and not from any file: the
    dtype, ctx and dim are inside `space_id`, so a file that disagreed with
    them would be describing another space (02 §2.4).
    """
    from dataclasses import replace
    from app import collections as collections_mod
    from app.hold import embedder as emb
    collection = collections_mod.by_name(name)
    if collection is None or getattr(collection, "embed", None) is None:
        # non-compliant: it names no encoder, and there is no default one
        # (operator, 2026-09-18) — everything that needs it defers, exit 3
        raise emb.GateDeferred(f"{name}: {collections_mod.NO_ENCODER}")
    recipe = collections_mod.recipe_for(collection)
    return replace(recipe, dtype=codex.space.dtype, ctx=int(codex.space.ctx),
                   dim=int(codex.space.dim))


def _write_statement(a, run, codex):
    """`--accept`. 09 Â§9.12: the statement is "kept in `state/`"."""
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
    top = argparse.ArgumentParser(prog="louise", description="Louise â knowledge, kept.")
    surfaces = top.add_subparsers(dest="surface", required=True, metavar="hold|share|node")
    node = surfaces.add_parser("node", help="the node's mode, or one collection paused (07 §7.3, §7.7)")
    nverbs = node.add_subparsers(dest="verb", required=True)
    p = nverbs.add_parser("pause", help="the node: nothing, seeding only, or download and seeding only, "
                                       "until resume; or ONE collection: its write lock released at "
                                       "the end of the current unit, nothing run for it until resume")
    p.add_argument("collection", nargs="?", default=None)
    p.add_argument("--acquire-only", action="store_true", dest="acquire_only")
    p.add_argument("--seed-only", action="store_true", dest="seed_only")
    p.set_defaults(fn=cmd_node_pause)
    p = nverbs.add_parser("resume", help="the node, or one collection")
    p.add_argument("collection", nargs="?", default=None)
    p.set_defaults(fn=cmd_node_resume)

    hold = surfaces.add_parser("hold", help="your own copy: complete, current, checked")
    nouns = hold.add_subparsers(dest="noun", required=True)
    coll = nouns.add_parser("collection", help="a Codex and what derives from it")
    verbs = coll.add_subparsers(dest="verb", required=True)

    def root_arg(p):
        p.add_argument("--root", type=Path, default=None,
                       help="where collections live (default: ./collections or $LOUISE_ROOT)")

    p = verbs.add_parser("certify", help="sign a snapshot as this collection's certifier and write the "
                                         "pointer (03 §3.7; 09 §9.11)")
    root_arg(p); p.add_argument("collection"); p.add_argument("snapshot")
    p.set_defaults(fn=cmd_collection_certify)
    p = verbs.add_parser("create", help="write a Codex from a plugin and a recipe; print its id")
    root_arg(p)
    p.add_argument("plugin", nargs="?", default=None,
                   help="the plugin the collection is of; omit when the file is a whole Codex")
    p.add_argument("--recipe", default=None,
                   help="TOML with every Codex field the plugin does not default (06 Â§6.6); "
                        "`create deposit` takes none")
    p.add_argument("--name", default=None,
                   help="the collection's name; `create deposit` requires it (02 §2.9)")
    p.add_argument("--space", default=None, metavar="FILE",
                   help="create deposit: the space block, for the first deposit "
                        "collection on an encoder (later ones copy its space)")
    p.add_argument("--space-from", help="a Codex whose space and layers to copy byte for byte")
    p.add_argument("--platform-image", default=None,
                   help="the digest of the platform image this command runs in "
                        "(required with --recipe; hashed into collection_id)")
    p.add_argument("--encoder", default=None, metavar="URL",
                   help="this node's endpoint for the recipe's encoder: it embeds the "
                        "calibration set, and the collection file names it (a "
                        "collection names its own encoder; there is no default)")
    p.add_argument("--query-encoder", default=None, metavar="URL",
                   help="a bi-encoder's query side (MedCPT): its endpoint. The Codex pins "
                        "its reference vectors too (09 §9.12), and the collection "
                        "file names it as query_url")
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
                   help="creating a sister is an acceptance (02 Â§2.7)")
    p.set_defaults(fn=cmd_derive)

    p = verbs.add_parser("continue", help="a daughter: a new toolchain, the same space")
    root_arg(p); p.add_argument("codex_id")
    p.add_argument("--platform-image", required=True)
    p.add_argument("--continues-through", required=True,
                   help="the mother's LAST snapshot â the boundary, named from "
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

    def args_show(p):
        root_arg(p); p.add_argument("codex_id")
        # 07 §7.3 names `--json` on every show; this one prints 09 §9.3's object
        # in any case, so the flag is accepted and changes nothing (2026-10-06)
        p.add_argument("--json", action="store_true")

    p = verbs.add_parser("show", help="a Codex and the ids derived from it")
    args_show(p); p.set_defaults(fn=cmd_show)

    p = verbs.add_parser(
        "migrate", help="add the §9.3 fields a Codex predates, each as null")
    root_arg(p); p.add_argument("name")
    p.add_argument("--apply", action="store_true",
                   help="write it; without this the change is only printed")
    p.set_defaults(fn=cmd_collection_migrate)

    p = verbs.add_parser("forget",
                         help="stop holding: remove this peer's copy and its state (07 §7.3)")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--kinds", default=None,
                   help="only these members, comma-separated: `vectors`")
    p.add_argument("--yes", action="store_true",
                   help="required when stdin is not a terminal (07 §7.7)")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="name what would go; remove nothing")
    p.add_argument("--here", action="store_true",
                   help="in this process, for a node whose worker is not running; "
                        "otherwise queued for the worker, the one writer")
    p.set_defaults(fn=cmd_collection_forget)

    p = verbs.add_parser("ack",
                         help="record the licence acknowledgement a yellow- "
                              "or red-class collection needs (09 §9.13)")
    root_arg(p); p.add_argument("collection", nargs="?", default=None)
    p.add_argument("--yes", action="store_true",
                   help="record WITHOUT printing the terms (07 §7.7). For a "
                        "script; a person should read them")
    p.add_argument("--web-sources", action="store_true", dest="web_sources",
                   help="accept the web-sources disclaimer, once for the node (04 §4.4)")
    p.add_argument("--origin-unchecked", action="store_true", dest="origin_unchecked",
                   help="record the decision to use a copy whose origin could not "
                        "be checked, every other test having passed (03 §3.6)")
    p.set_defaults(fn=cmd_collection_ack)

    src_ = nouns.add_parser("source", help="a deposit collection's URL sources (06 §6.7)")
    sverbs_ = src_.add_subparsers(dest="verb", required=True)
    p = sverbs_.add_parser("add", help="subscribe a deposit collection to a URL")
    p.add_argument("collection"); p.add_argument("url")
    p.add_argument("--interval", type=int, default=60, help="minutes between fetches")
    p.add_argument("--here", action="store_true",
                   help="write in this process — only where no worker is running")
    p.set_defaults(fn=cmd_source_add)
    p = sverbs_.add_parser("erase", help="end a subscription and erase its deposit")
    p.add_argument("collection"); p.add_argument("url")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--here", action="store_true",
                   help="write in this process — only where no worker is running")
    p.set_defaults(fn=cmd_source_erase)
    p = sverbs_.add_parser("list", help="the subscriptions, each with its last fetch")
    p.add_argument("collection"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_source_list)

    # ---- hold records ---------------------------------------------------
    dep = nouns.add_parser("deposit", help="a deposit collection's documents (02 §2.9)")
    dverbs = dep.add_subparsers(dest="verb", required=True)
    p = dverbs.add_parser("add", help="store a file as a deposit; an existing key is replaced whole")
    p.add_argument("collection"); p.add_argument("file")
    p.add_argument("--key", default=None, help="the deposit's key (default: the file's name)")
    p.add_argument("--here", action="store_true",
                   help="write in this process — only where no worker is running")
    p.set_defaults(fn=cmd_deposit_add)
    p = dverbs.add_parser("erase", help="remove a deposit, leaving no trace (02 §2.9)")
    p.add_argument("collection"); p.add_argument("key")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--here", action="store_true",
                   help="write in this process — only where no worker is running")
    p.set_defaults(fn=cmd_deposit_erase)
    p = dverbs.add_parser("list", help="the collection's deposit keys")
    p.add_argument("collection"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_deposit_list)
    p = dverbs.add_parser("show", help="a deposit's text and its chunk count")
    p.add_argument("collection"); p.add_argument("key")
    p.set_defaults(fn=cmd_deposit_show)

    recs = nouns.add_parser("records", help="the parsed text of documents")
    rverbs = recs.add_subparsers(dest="verb", required=True)

    def args_records_build(p):
        root_arg(p); p.add_argument("collection")
        p.add_argument("--corpus",
                       help="the origin directory to parse (default: the "
                            "collection's own, <root>/<name>/origin, 07 §7.8)")
        p.add_argument("--snapshot", dest="snapshot_id",
                       help="the cutoff to build to")
        p.add_argument("--shard-records", type=int, dest="records_per_shard",
                       help="override the Codex's slicing.shard_records (tests)")

    p = rverbs.add_parser("build", help="parse, slice, shard, hash")
    args_records_build(p); p.set_defaults(fn=cmd_records_build)

    p = rverbs.add_parser("validation", help="write validation.json from the Codex")
    root_arg(p); p.add_argument("collection")
    p.set_defaults(fn=cmd_records_validation)

    p = rverbs.add_parser("check",
                          help="`hold check --checks roots,records"
                               "[,records-vs-origin]` and nothing else")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--origin", dest="origin_dir", metavar="DIR",
                   help="an origin base to re-fetch and re-parse from; "
                        "adds `records-vs-origin`. Without it `--sample` "
                        "affects nothing this command runs (07 §7.3)")
    p.add_argument("--origin-mib", type=int, metavar="N",
                   help="raise what `records-vs-origin` may read of the origin "
                        "for this run (default and floor: 256 MiB)")
    p.add_argument("--sample",
                   help="raise the plan's shard fraction for this run")
    p.add_argument("--seed", help="32 bytes as hex")
    p.add_argument("--snapshot", help="which snapshot (default: the latest)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_records_check)

    # ---- hold snapshot --------------------------------------------------
    snaps = nouns.add_parser("snapshot", help="a collection's records at one cutoff")
    sverbs = snaps.add_subparsers(dest="verb", required=True)

    def args_snapshot_create(p):
        root_arg(p); p.add_argument("collection")
        p.add_argument("--snapshot", dest="snapshot_id",
                       help="the id; by default the period that closed, "
                            "from the Codex's clock")

    def args_collection(p):
        root_arg(p); p.add_argument("collection")

    p = sverbs.add_parser("create",
                          help="freeze the current records as this period's snapshot")
    args_snapshot_create(p); p.set_defaults(fn=cmd_snapshot_create)

    p = sverbs.add_parser("list", help="the chain this peer holds")
    args_collection(p); p.set_defaults(fn=cmd_snapshot_list)

    # ---- hold embedder --------------------------------------------------
    embd = nouns.add_parser("embedder", help="is this peer's encoder the space's?")
    everbs = embd.add_subparsers(dest="verb", required=True)

    p = everbs.add_parser("check", help="the calibration gate (03 Â§3.3)")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--accept", action="store_true",
                   help="record a passing run as this peer's statement (09 Â§9.12)")
    p.set_defaults(fn=cmd_embedder_check)

    p = everbs.add_parser("benchmark", help="tokens per second on the calibration set")
    root_arg(p); p.add_argument("collection"); p.set_defaults(fn=cmd_embedder_benchmark)

    p = everbs.add_parser("calibrate", help="the tolerance a space can bear")
    root_arg(p)
    p.add_argument("--encoder-image"); p.add_argument("--encoder-weights")
    p.add_argument("--against", nargs="*", default=[])
    p.add_argument("--set", dest="set_id")
    p.set_defaults(fn=cmd_embedder_calibrate)

    vecs = nouns.add_parser("vectors", help="this peer's vectors, and the bundles that carry them")
    vverbs = vecs.add_subparsers(dest="verb", required=True)
    p = vverbs.add_parser("build",
                          help="embed this peer's own chunks that have no "
                               "vector yet")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--snapshot",
                   help="which snapshot's shards (default: the latest held)")
    p.add_argument("--delta", metavar="ID",
                   help="embed exactly the shards holding `delta.added ∪ "
                        "delta.changed` of snapshot ID (07 §7.3)")
    p.add_argument("--shard", help="one shard, as \"<slice>/<n>\"")
    p.add_argument("--slice", nargs="*", help="whole slices, by their value")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be embedded and exit 0 without "
                        "writing a byte (07 §7.7)")
    p.set_defaults(fn=cmd_vectors_build)

    p = vverbs.add_parser("export",
                          help="a signed bundle of one shard's vectors (09 §9.7)")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--shard", required=True,
                   help="which shard, as \"<slice>/<n>\" — e.g. lang=it/00007. "
                        "One shard per bundle: §9.7's header has a single "
                        "`shard` field, so a bundle spanning two cannot be "
                        "described by a conformant header")
    p.add_argument("--out", required=True,
                   help="the bundle DIRECTORY to write — §9.7 defines four "
                        "files and fixes no container")
    p.add_argument("--snapshot",
                   help="which snapshot's records (default: the latest held)")
    p.add_argument("--max-chunks", type=int, dest="max_chunks",
                   default=None,
                   help="chunks per bundle; a shard with more becomes "
                        "several, split on record boundaries. Default "
                        "15,000 (operator, 2026-09-10; 09 §9.10's worked "
                        "example is two bundles of 14,800 and 15,200)")
    p.set_defaults(fn=cmd_vectors_export)

    p = vverbs.add_parser("import",
                          help="a peer's bundle through 03 §3.5's T0–T3 ladder")
    root_arg(p); p.add_argument("collection")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--bundle",
                     help="the bundle DIRECTORY to import (09 §9.7's four files)")
    src.add_argument("--archive", metavar="ID",
                     help="a published pre-embedded set from the platform's "
                          "registry, e.g. medcpt-pubmed. Refused unless this "
                          "collection's `credits` names it (06 §6.6)")
    p.add_argument("--corpus", metavar="DIR",
                   help="with --archive: the corpus directory. REQUIRED when "
                        "the archive keeps its vectors in the corpus itself "
                        "(adapter corpus-embedded) — there is nowhere else "
                        "to read them from")
    p.add_argument("--shards", nargs="*",
                   help="with --archive: only these shards (default: all)")
    p.add_argument("--snapshot",
                   help="which snapshot's records to check against "
                        "(default: the one the bundle names)")
    p.add_argument("--strict", action="store_true",
                   help="admit only the producer keys named by --allow. OFF by "
                        "default: an empty allow list admits nothing, and 08 "
                        "§8.1 forbids refusing a key merely for being new")
    p.add_argument("--allow", nargs="*", default=[],
                   help="producer public keys --strict admits")
    p.add_argument("--require-t3", action="store_true",
                   help="exit 3 rather than import when no encoder is "
                        "reachable. Off by default: T0, T1 and T2 are what "
                        "make a bundle usable and T3 is recorded as SKIPPED")
    p.add_argument("--seed",
                   help="hex seed for T3's sample, to repeat a check "
                        "deliberately (default: fresh randomness, printed)")
    p.add_argument("--budget", action="store_true",
                   help="subject this arrival to 08 §8.2's hourly allowance "
                        "for keys whose work this peer has not seen "
                        "reproduced. Off by default: an operator naming a "
                        "path is not material arriving from a stranger")
    p.set_defaults(fn=cmd_vectors_import)

    p = vverbs.add_parser("show", help="the coverage map: covered chunks per shard, leaders, "
                                       "producers, vitality (09 §9.10)")
    root_arg(p); p.add_argument("collection")
    p.set_defaults(fn=cmd_vectors_show)
    p = vverbs.add_parser("check",
                          help="03 §3.2's `vectors-sample` over this peer's "
                               "own vectors")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--snapshot",
                   help="which snapshot's shards to sample (default: the "
                        "latest held)")
    p.add_argument("--sample",
                   help="raise the plan's shard fraction for this run, as a "
                        "decimal string. It cannot lower it (07 §7.3)")
    p.add_argument("--seed",
                   help="hex seed for the draw, to repeat a check (default: "
                        "fresh randomness, printed)")
    p.add_argument("--drop", action="store_true",
                   help="empty a row that no longer reproduces (02 §2.4) so "
                        "the coverage map stops claiming it. Off by default: "
                        "deleting vectors on a cosine is an operator's call")
    p.set_defaults(fn=cmd_vectors_check)

    key = nouns.add_parser("key", help="this peer's key, the trust list, a certifier's anchor")
    kverbs = key.add_subparsers(dest="verb", required=True)
    p = kverbs.add_parser("create", help="this peer's key, at NODE_KEY_PATH, once")
    p.set_defaults(fn=cmd_key_create)
    p = kverbs.add_parser("trust", help="the trust list: allow, deny, forget, lift a burn (09 §9.8)")
    root_arg(p); p.add_argument("key")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--deny", action="store_true", help="ignore this key's attestations, digests and bundles")
    g.add_argument("--remove", action="store_true", help="forget this key: neither allowed nor denied")
    g.add_argument("--restore", action="store_true", help="lift a burn by a proof of forgery (03 §3.5)")
    p.set_defaults(fn=cmd_key_trust)
    p = kverbs.add_parser("anchor", help="check a certifier's key against its anchor now (03 §3.7)")
    root_arg(p); p.add_argument("collection")
    p.set_defaults(fn=cmd_key_anchor)
    p = kverbs.add_parser("show", help="this peer's public key; the trust list with each key's state")
    root_arg(p)
    p.set_defaults(fn=cmd_key_show)

    p = nouns.add_parser("check",
                         help="run the collection's validation plan (03 §3.2) "
                              "and attest what completed")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--checks",
                   help="a comma-separated subset of 03 §3.2's table; the "
                        "default is the collection's own plan")
    p.add_argument("--sample",
                   help="raise the plan's shard fraction for this run, as a "
                        "decimal string. It cannot lower it (07 §7.3)")
    p.add_argument("--seed",
                   help="32 bytes as hex; the default is fresh OS entropy, "
                        "and it is printed and published in the attestation")
    p.add_argument("--snapshot",
                   help="which snapshot to check (default: the latest held)")
    p.add_argument("--origin", dest="origin_dir", metavar="DIR",
                   help="an origin base to re-fetch and re-parse from, for "
                        "`records-vs-origin` (03 §3.2). Without it that "
                        "check is inconclusive, never a pass")
    p.add_argument("--origin-mib", type=int, metavar="N",
                   help="raise what `records-vs-origin` may read of the origin "
                        "for this run: 256 MiB by default, and never less "
                        "(03 §3.2)")
    p.add_argument("--json", action="store_true",
                   help="09 §9.8's report object")
    p.set_defaults(fn=cmd_check)

    share = surfaces.add_parser("share", help="bytes leaving here, and the announcement that they exist (04)")
    shverbs = share.add_subparsers(dest="verb", required=True)
    p = shverbs.add_parser("collection", help="serve what you hold — and announce it; --stop to stop")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--slices", default=None)
    p.add_argument("--kinds", default=None)
    p.add_argument("--stop", action="store_true")
    p.set_defaults(fn=cmd_share)
    p = shverbs.add_parser("fetch", help="take a part: find who holds it, pull and verify, hold it (04 §4.3)")
    root_arg(p); p.add_argument("collection", help="a collection id or a lineage id")
    p.add_argument("--slice", nargs="*", default=None,
                   help="the slices to hold, by value; absent: every slice")
    p.add_argument("--kinds", default=None,
                   help="comma-separated member kinds to hold; default records")
    p.add_argument("--here", action="store_true",
                   help="fetch the members in this process, for a node whose worker is not running")
    p.add_argument("--snapshot", default=None, metavar="ID",
                   help="this snapshot and its chain; default: the latest the peer holds")
    p.add_argument("--from", dest="from_", default=None, metavar="ADDRESS",
                   help="a peer given by hand, http(s)://host[:port]; asked first")
    p.add_argument("--dht", action="store_true",
                   help="probe the DHT under the collection and lineage keys when no peer is known")
    p.add_argument("--encoder", default=None, metavar="URL",
                   help="where this node reaches the Codex's encoder, into the collection's file")
    p.add_argument("--name", default=None, help="the local name; default: the plugin's")
    p.set_defaults(fn=cmd_share_fetch)
    p = shverbs.add_parser("contribute", help="the shards this peer leads: vectors build under rendezvous, "
                                              "bundled, announced (04 §4.5)")
    root_arg(p); p.add_argument("collection", nargs="?", default=None,
                                help="one collection; default: every public collection held")
    p.add_argument("--once", action="store_true", help="one pass, then exit")
    p.add_argument("--interval", default=str(int(900)), metavar="S",
                   help="seconds between passes when looping (default 900)")
    p.set_defaults(fn=cmd_share_contribute)
    p = shverbs.add_parser("attest", help="hold check, then the attestations as events — once each (04 §4.2)")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--checks", help="comma-separated; default: the collection's plan")
    p.add_argument("--seed", help="32 bytes as hex; default: fresh")
    p.add_argument("--sample", help="raise the plan's shard fraction for this run")
    p.add_argument("--snapshot", help="which snapshot (default: the latest)")
    p.add_argument("--origin", dest="origin_dir", metavar="DIR",
                   help="an origin base to re-fetch and re-parse from (records-vs-origin)")
    p.add_argument("--origin-mib", type=int, metavar="N")
    p.set_defaults(fn=cmd_share_attest)
    p = shverbs.add_parser("peers", help="who this peer knows; a peer given by hand")
    pverbs = p.add_subparsers(dest="peers_verb", required=True)
    q = pverbs.add_parser("add", help="a peer given by hand: into platform.toml [peers] (07 §7.8)")
    q.add_argument("address", help="http(s)://host[:port]; a bare host:port is http")
    q.add_argument("--key", default=None,
                   help="pin the address to this key: a digest from it under another key is discarded")
    q.set_defaults(fn=cmd_share_peers)
    q = pverbs.add_parser("show", help="the peer list, every peer heard from, what each holds")
    q.set_defaults(fn=cmd_share_peers)

    ask = surfaces.add_parser("ask", help="search, facets, a fact by key, a traversal; the three tools over MCP (05)")
    akverbs = ask.add_subparsers(dest="verb", required=True)
    p = akverbs.add_parser("search", help="the fused list of 05 §5.1 per space, each result as §5.3 says")
    root_arg(p); p.add_argument("query", nargs="+")
    p.add_argument("--collections", nargs="*", help="ids, prefixes or local names; default: every usable one")
    p.add_argument("--space", help="a space_id: only the collections in it")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--rerank-depth", type=int, default=50)
    p.add_argument("--as-of", help="YYYY-MM-DD: answer from the held snapshot current at that date (05 §5.1)")
    p.add_argument("--freshness", action="store_true", help="ask the origin for the current version (not built yet)")
    p.add_argument("--translate", action="store_true")
    p.add_argument("--all-languages", action="store_true")
    p.add_argument("--filter", action="append", metavar="FACET=VALUE",
                   help="restrict the legs to a facet value (05 §5.2); FACET=!VALUE excludes")
    p.set_defaults(fn=cmd_ask_search)
    p = akverbs.add_parser("chat", help="the chat model with the three tools of 05 §5.4; needs CHAT_URL")
    p.add_argument("question", nargs="+")
    p.add_argument("--collections", nargs="*", help="search only these unless the question says otherwise")
    p.add_argument("--json", action="store_true", help="the answer with every tool call and its result")
    p.set_defaults(fn=cmd_ask_chat)
    p = akverbs.add_parser("facets", help="the fields a collection can be filtered on, or one field's values (05 §5.7)")
    root_arg(p); p.add_argument("collection"); p.add_argument("field", nargs="?", default=None)
    p.add_argument("--under", default=None, help="one subtree of a tree facet")
    p.set_defaults(fn=cmd_ask_facets)
    p = akverbs.add_parser("lookup", help="a fact by key, from the reference source that owns the namespace")
    p.add_argument("id", help="<namespace>:<id>")
    p.set_defaults(fn=cmd_ask_lookup)
    p = akverbs.add_parser("traverse", help="the records reached along declared relations (05 §5.5)")
    root_arg(p); p.add_argument("id", help="a record's uid or a canonical id")
    p.add_argument("--relation", default=None); p.add_argument("--depth", type=int, default=1)
    p.add_argument("--collection", default=None, help="the collection whose edges to follow")
    p.set_defaults(fn=cmd_ask_traverse)
    p = akverbs.add_parser("serve", help="the three tools of 05 §5.4 over MCP, on stdio")
    p.set_defaults(fn=cmd_ask_serve)

    add_deprecated(surfaces, {
        "records_build": args_records_build,
        "snapshot_create": args_snapshot_create,
        "collection": args_collection,
        "show": args_show,
    })
    return top


# --------------------------------------------------------------------------
# 07 Â§7.6's aliases

def deprecated(fn, old: str, new: str):
    """Wrap a command so the old name prints what to use instead.

    07 Â§7.6: "Old names are accepted for one release as hidden aliases â
    `louise <old â¦>` and `python -m app.cli <old â¦>` both â each dispatching
    to the new function and printing `deprecated: use louise <new â¦>` on
    stderr."

    ONE function, two names. The alias calls the SAME `fn` and its parser is
    built from the SAME argument definition, so the two cannot drift â which
    is the whole point of an alias over a second implementation.
    """
    def run(a):
        print(f"deprecated: use louise {new}", file=sys.stderr)
        return fn(a)
    return run


def add_deprecated(top, args_of) -> None:
    """The previous command tree, hidden, for the names whose new command
    EXISTS on this branch (07 Â§7.6's table).

    The rest arrive with the steps that build them: `corpus add`/`grow` need
    `hold records fetch`, `pack verify` needs `hold check`, `pack cat`/`dump`
    need `hold records show`, `corpus add --ack-terms` needs `hold collection
    ack` (`pack adopt`, which needed `dev adopt`, is boxed in attic/). Each
    is owed by its own step;
    none is quietly forgotten here.

    "Hidden" means NO `help=` at all. `argparse.SUPPRESS` does not work on a
    subparser — argparse prints the literal string `==SUPPRESS==` in the
    command list, which is worse than showing the name. A subparser with no
    `help` is simply not listed, while still parsing.
    """
    pack = top.add_parser("pack")
    pv = pack.add_subparsers(dest="verb", required=True)
    p = pv.add_parser("build")
    args_of["records_build"](p)
    p.set_defaults(fn=deprecated(cmd_records_build, "pack build",
                                 "hold records build"))

    corpus = top.add_parser("corpus")
    cv = corpus.add_subparsers(dest="verb", required=True)
    p = cv.add_parser("status")
    args_of["show"](p)
    p.set_defaults(fn=deprecated(cmd_show, "corpus status",
                                 "hold collection show"))

    snap = top.add_parser("snapshot")
    sv = snap.add_subparsers(dest="verb", required=True)
    p = sv.add_parser("record")
    args_of["snapshot_create"](p)
    p.set_defaults(fn=deprecated(cmd_snapshot_create, "snapshot record",
                                 "hold snapshot create"))
    p = sv.add_parser("list")
    args_of["collection"](p)
    p.set_defaults(fn=deprecated(cmd_snapshot_list, "snapshot list",
                                 "hold snapshot list"))


def main(argv=None) -> int:

    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # `| head` is not an error
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 2 and argv[0] == "share" and argv[1] not in _SHARE_VERBS:
        argv.insert(1, "collection")              # `share <collection>` (07 §7.3)

    a = build_parser().parse_args(argv)
    if getattr(a, "root", None) is None and hasattr(a, "root"):
        a.root = _default_root()
    root = getattr(a, "root", None)
    if root is not None and not Path(root).is_dir():
        # A missing root must not be reported as "no Codex collections
        # held": `_held()` swallows a missing directory, so a mistyped or
        # unset root printed an empty list and exited 0 — the same output
        # as a node that genuinely holds none.
        print(f"louise: {root} is not a directory; --root is "
              f"<LOUISE_ROOT>/collections (07 §7.8)", file=sys.stderr)
        return 2
    try:
        return _run_releasing(a)
    except CodexError as e:
        from app.hold.embedder import GateDeferred
        if isinstance(e, GateDeferred):
            # 07 §7.7 keeps 3 for what could not run — neither a pass nor a
            # failure (03 §3.1). Most commands say so themselves; one that
            # let it through must not have it read as a refusal.
            print(f"louise: deferred: {e}", file=sys.stderr)
            return 3
        print(f"louise: {e}", file=sys.stderr)
        return getattr(e, "exit_code", 2)
    except FileNotFoundError as e:
        print(f"louise: {e}", file=sys.stderr)
        return 2
    except Exception as e:                              # noqa: BLE001
        # Invariant 9's refusal is the one that reaches here today, and it
        # is 07 §7.7's exit 2 — "a rule stood in the way before anything
        # was done" — not a crash. `app/licensing.py` is THE resolution
        # and raises its own error type; re-raising it as a CodexError at
        # every call site would be a second implementation of the check.
        from app.acquire.spec import LicenseAckRequiredError
        if not isinstance(e, LicenseAckRequiredError):
            raise
        print(f"louise: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    from app import process_exit
    process_exit.run(main)

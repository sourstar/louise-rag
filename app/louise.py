"""louise — the command line. docs/07-commands.md.

    louise hold collection create <plugin> --recipe FILE [--space-from CODEX]
                                    [--platform-image D] [--root DIR]
    louise hold collection derive <codex> (--space-from CODEX | --space FILE)
                                    [--layers FILE] [--update FILE] [--work FILE]
                                    [--encoder URL [--query-encoder URL] [--api A]
                                     [--embed-model M] [--query-model M]]
                                    [--calibration-set FILE] [--name N] --accept
    louise hold collection continue <codex> --platform-image D --through <snapshot>
                                    [--origin FILE] [--origin-reachable-until <snapshot>]
    louise hold collection replace <codex> --defect FILE [--platform-image D] [--space FILE]
    louise hold collection list
    louise hold collection show <codex>
    louise hold records build <collection> [--corpus DIR] [--snapshot ID]
    louise hold records fetch <collection> [--profile P] [--dry-run]
    louise hold records validation <collection>
    louise hold snapshot create <collection> [--snapshot ID] [--shards-from FROM[:SNAPSHOT]]
    louise hold snapshot list <collection>
    louise hold embedder check|benchmark <collection> [--accept]
    louise hold embedder run --encoder URL [--api A] [--model M] [--query] [--pair]
    louise hold embedder rebaseline <collection> [--yes]
    louise hold vectors build <collection> [--snapshot ID | --delta ID] [--shard S | --slice V...]
                                    [--dry-run]
    louise hold vectors export <collection> --shard S --out DIR [--snapshot ID]
    louise hold vectors import <collection> --bundle DIR [--snapshot ID]
                                    [--strict [--allow KEY...]] [--require-t3] [--seed HEX]
                                    [--budget]
    louise hold vectors check <collection> [--snapshot ID] [--sample F] [--seed HEX] [--drop]
    louise hold check <collection> [--checks A,B] [--sample F] [--seed HEX] [--snapshot ID] [--json]
    louise hold records show <collection> (<record-id> | --shard <slice>/<n>) [--raw]
    louise hold issues list <collection> [--code C] [--limit N] [--json]
    louise hold snapshot delta <collection> <from> <to>
    louise hold index build <collection> --vector | show <collection>
    louise hold config show|apply [<collection>]
    louise node health [<collection>] [--errors]
    louise dev genesis recheck <collection> | recount <collection> [--yes]
    louise dev carry <from> <to> [--copy] [--keep producer:ARCHIVE] [--rebuilt|--reconcile|--resign|--recut] [--yes]

The previous tree (`python -m app.cli`, 07 §7.6) is retired (2026-10-08):
every name it had that is still useful is one of these.

One flag of §7.3's tree is accepted and refused here rather than
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
from starlette.exceptions import HTTPException as _HTTPException
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
    """A recipe is TOML (07 §7.3). JSON is accepted for a file that is
    already a whole Codex — what `create` took before the template existed,
    and what a test fixture is easiest to write."""
    p = Path(path)
    if p.suffix == ".json":
        return _json(path)
    return tomllib.loads(p.read_text(encoding="utf-8"))


def cmd_create(a) -> int:
    if a.plugin == "deposit":
        return cmd_create_deposit(a)
    if a.plugin == "annotations":
        return cmd_create_annotations(a)
    if not a.recipe:
        raise CodexError("--recipe is required: a collection is a plugin and a "
                         "recipe (06 §6.6); only `create deposit` needs none")
    if a.plugin is None:
        # the pre-template form: the file IS a Codex
        spec = _json(a.recipe)
        spec.setdefault("kind", cx.CODEX_KIND)
        if "version" not in spec:
            # 6 for a Codex carrying a backend engine, 5 for every other
            # (09 §9.3, amended 2026-10-08)
            spec["version"] = (cx.written_version(cx.space_from_dict(spec["space"]),
                                                  cx.CODEX_VERSION)
                               if isinstance(spec.get("space"), dict) else cx.CODEX_VERSION)
        keys = {"api": check_api(getattr(a, "api", None)),
                "embed_model": getattr(a, "embed_model", None),
                "query_model": getattr(a, "query_model", None)}
        if a.encoder:                      # refused before anything is created
            model_keys(a.encoder, embed_model=keys["embed_model"],
                       query_model=keys["query_model"])
        codex = col.create(a.root, cx.from_dict(spec))
        print(f"held: {_write_held_file(a.root, codex, a.encoder, **keys)}",
              file=sys.stderr)
        print(codex.collection_id)
        return 0
    codex, held = create_from(a.root, a.plugin, _recipe(a.recipe),
                              platform_image=a.platform_image,
                              space_from=a.space_from, encoder=a.encoder,
                              query_encoder=a.query_encoder, name=a.name,
                              calibration_set=getattr(a, "calibration_set", None),
                              api=getattr(a, "api", None),
                              embed_model=getattr(a, "embed_model", None),
                              query_model=getattr(a, "query_model", None),
                              calibration_from=getattr(a, "calibration_from", None))
    print(f"held: {held}", file=sys.stderr)
    print(codex.collection_id)
    return 0


def create_deposit(root, name: str, *, platform_image: str | None = None,
                   encoder: str | None = None, space_from: str | None = None,
                   space: str | None = None, api: str | None = None,
                   embed_model: str | None = None, query_model: str | None = None):
    """`hold collection create deposit --name N` (07 §7.3; 02 §2.9) — for the
    command line and for the LocalAGI route alike. Whatever the creator does
    not name comes from `platform.toml`'s `[deposit]`; `--api`,
    `--embed-model` and `--query-model` name what `[deposit]` would. Returns
    the Codex and the collection file it wrote."""
    from app import platform_config
    from app.hold import deposit
    from app.bundles import keys as node_keys

    cfg = platform_config.load().deposit
    if encoder and encoder != cfg.encoder:
        # another endpoint than [deposit]'s: its api and models are not this
        # one's to describe — the default api, and the model the URL names
        base = ("openai", None, None)
    else:
        encoder = encoder or cfg.encoder
        base = (cfg.api, cfg.embed_model, cfg.query_model)
    api = check_api(api or base[0])
    embed_model, query_model = embed_model or base[1], query_model or base[2]
    if encoder and (embed_model or query_model):
        # a key that is not the model its URL names: the file would say two things
        model_keys(encoder, embed_model=embed_model, query_model=query_model)
    # the models are settled BEFORE anything is created (07 §7.8, amended
    # 2026-10-09): a URL that names none, with no keys, is refused here
    models = (deposit.encoder_models(encoder, embed_model=embed_model, query_model=query_model)
              if encoder else None)
    space_from = space_from or cfg.by_name.get(name) or cfg.space_from
    source = col.read(root, space_from) if space_from else None
    block = None
    if source is None and deposit.space_source(root, encoder,
                                               models[0] if models else None) is None:
        path = space or (str(cfg.space) if cfg.space else None)
        block = _recipe(path) if path else None
        if block is not None and "space" in block:
            block = block["space"]
        if block is None and encoder and models:
            # RULE 2 (2026-10-25): the node determines the first deposit
            # collection's space itself — a held Codex in the space whose
            # weights the deposit encoder's stated run loads, copied whole
            source = _space_of_served_model(root, encoder, api, models[0])
    codex = deposit.create(
        root, name, platform_image=preflight_create({}, platform_image),
        encoder_url=encoder, embed_model=models[0] if models else None,
        space_from=source, space=block,
        embed=(_embed_for_recipe(block, encoder, models[0] if models else None, api=api)
               if block is not None else None),
        # the key signs the calibration member, which only the first
        # deposit collection on an encoder writes; the others copy it
        key=node_keys.load_signing_key() if block is not None else None,
        now=_utc_now())
    return codex, deposit.write_held_file(
        root, codex, encoder, api=api,
        embed_model=models[0] if models else None,
        query_model=models[1] if models else None)


def _space_of_served_model(root, encoder: str, api: str, model: str):
    """A held text Codex whose space the model at `encoder` runs — the one
    whose `space.encoder.weights` are the weights the model's stated run
    loads (app/encoder_resolve.py: verified against the run id; a name never
    decides) and whose space has no query encoder of its own, since a
    deposit's encoder is symmetric (02 §2.9). The oldest by name, so the
    choice is stable; None where the model states no run or no held space
    is its."""
    from app import encoder_resolve
    weights = encoder_resolve.served_weights(encoder, api, model)
    if not weights:
        return None
    found = sorted((n, c) for n, c in col._held(Path(root))
                   if getattr(c, "space", None) is not None and c.modality == "text"
                   and c.space.encoder.weights == weights and c.space.query.encoder is None)
    return found[0][1] if found else None


def cmd_create_annotations(a) -> int:
    """`hold collection create annotations --annotates C --from-snapshot S
    --claims FILE --name N` (07 §7.3; 02 §2.10): an annotations collection
    from a claims file — its Codex, the record and relations parts, the
    first snapshot (signed with the node key when there is one) and its
    collection file. Prints the new collection's id."""
    from app.hold import annotations as ann
    missing = [f for f, v in (("--claims", a.claims), ("--name", a.name)) if not v]
    if missing:
        raise CodexError(f"create annotations takes {', '.join(missing)}: the claims file and "
                         f"the new collection's name (02 §2.10); the targets and their snapshots "
                         f"are the claims' unless --annotates and --from-snapshot name them")
    if a.recipe:
        raise CodexError("create annotations takes no --recipe: its Codex is fixed by 02 §2.10 "
                         "and named by the flags")
    # named: as named, one --from-snapshot per --annotates; a target alone: its
    # newest held snapshot; none: the collections the claims name (RULE 2)
    targets = ann.resolve_targets(a.root, Path(a.claims).read_bytes(), a.annotates, a.from_snapshot)
    container = getattr(a, "container", None) or "jsonl"
    if getattr(a, "dry_run", False):
        _print(ann.preview(a.root, targets=targets, container=container,
                           claims=Path(a.claims).read_bytes(), annotator=a.annotator))
        return 0
    key = None
    try:
        from app.bundles import keys as node_keys
        key = node_keys.load_signing_key()
    except Exception:   # noqa: BLE001 — no key: the snapshot is held unsigned (09 §9.4)
        print("no node key: the snapshot is written unsigned, and `signature` stays "
              "inconclusive until a key signs it", file=sys.stderr)
    codex, snap = ann.create(
        a.root, a.name, targets=targets, container=container,
        claims=Path(a.claims).read_bytes(), platform_image=preflight_create({}, a.platform_image),
        title=a.title, summary=a.summary, annotator=a.annotator, snapshot_id=a.snapshot,
        key=key, now=_utc_now(), log=lambda m: print(m, file=sys.stderr))
    print(codex.collection_id)
    return 0


def cmd_annotations_update(a) -> int:
    """`hold annotations update <collection> [--claims FILE] [--against
    TARGET=SNAPSHOT]` (02 §2.10; operator, 2026-10-23: "annotations must be
    updated, not replaced"): the next snapshot of the same collection,
    signed with the node key when there is one. Prints what changed."""
    from app.hold import annotations as ann
    against = []
    for v in a.against or ():
        ref, sep, sid = str(v).partition("=")
        if not sep or not ref or not sid:
            raise CodexError(f"--against {v!r}: TARGET=SNAPSHOT, e.g. gutenberg=2026-10")
        against.append((ref, sid))
    claims = Path(a.claims).read_bytes() if a.claims else None
    key = None
    if not a.dry_run:
        try:
            from app.bundles import keys as node_keys
            key = node_keys.load_signing_key()
        except Exception:   # noqa: BLE001 — no key: the snapshot is held unsigned (09 §9.4)
            print("no node key: the snapshot is written unsigned, and `signature` stays "
                  "inconclusive until a key signs it", file=sys.stderr)
    got = ann.update(a.root, a.collection, claims=claims, against=against, snapshot_id=a.snapshot,
                     key=key, now=_utc_now(), dry_run=a.dry_run, log=lambda m: print(m, file=sys.stderr))
    _print(got.to_dict())
    return 0


def annotation_targets(annotates, from_snapshot) -> list[tuple[str, str]]:
    """`--annotates C --from-snapshot S`, repeated once per target (02 §2.10,
    amended 2026-10-20), as (collection, snapshot) pairs in the order given;
    a comma-separated value is the API's spelling of the same list."""
    def items(v) -> list[str]:
        if v is None:
            return []
        parts = v if isinstance(v, (list, tuple)) else [v]
        return [x.strip() for p in parts for x in str(p).split(",") if x.strip()]
    refs, snaps = items(annotates), items(from_snapshot)
    if len(refs) != len(snaps):
        raise CodexError(f"create annotations names {len(refs)} collection(s) with --annotates and "
                         f"{len(snaps)} snapshot(s) with --from-snapshot: one --from-snapshot per "
                         f"--annotates, in the same order (02 §2.10)")
    return list(zip(refs, snaps))


def cmd_create_deposit(a) -> int:
    if not a.name:
        raise CodexError("create deposit takes --name: a deposit collection is "
                         "named by whoever creates it (07 §7.3)")
    codex, held = create_deposit(a.root, a.name, platform_image=a.platform_image,
                                 encoder=a.encoder, space_from=a.space_from,
                                 space=a.space, api=getattr(a, "api", None),
                                 embed_model=getattr(a, "embed_model", None),
                                 query_model=getattr(a, "query_model", None))
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


def cmd_issues_list(a) -> int:
    """`hold issues list <collection> [--code C] [--limit N] [--json]` (06
    §6.1, 07 §7.3, amended 2026-10-25): the origin's inconsistencies the
    last parse of each document met, by uid; the counts per code and each
    code's meaning first (on stderr, or in the JSON). Read-only: no lock."""
    from app import collections as collections_mod, issues
    from app.hold import records as rec
    name, codex = col.resolve(a.root, a.collection)
    plugin = collections_mod.PLUGIN_REGISTRY.get(codex.plugin)
    meaning = issues.declared(plugin) if plugin is not None else dict(issues.PLATFORM)
    if a.code is not None and a.code not in meaning:
        raise CodexError(f"{a.code!r} is no issue code of {name} ({codex.plugin}); its codes: "
                         f"{', '.join(sorted(meaning))}")
    con = issues.open_read(rec._paths(a.root, name)[1] / "manifest.sqlite")
    try:
        summary = issues.summary(con, plugin) if con is not None else {"total": 0, "by_code": {}}
        items = list(issues.listing(con, code=a.code, limit=a.limit)) if con is not None else []
    finally:
        if con is not None:
            con.close()
    if a.json:
        _print({"issues": {"collection": name, "codes": meaning, **summary, "items": items}})
        return 0
    for code, got in sorted(summary["by_code"].items()):
        print(f"{code}\t{got['documents']:,}\t{got['meaning']}", file=sys.stderr)
    for it in items:
        print(f"{it['uid']}\t{it['code']}\t{it['detail']}")
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
                query_encoder: str | None = None, name: str | None = None,
                calibration_set: str | None = None, api: str | None = None,
                embed_model: str | None = None, query_model: str | None = None,
                calibration_from: str | None = None, embed: str = "off"):
    """`hold collection create <plugin> --recipe FILE` — for the command
    line and for its route alike (07 §7.4: the web interface is a client of
    the same function, not a second implementation). Returns the Codex and
    the collection file it wrote (07 §7.8).

    `api`, `embed_model`, `query_model`: the rest of the file's `[encoder]`
    (07 §7.8, amended 2026-10-09) — node-local, so flags beside `--encoder`
    and never the recipe, whose `space` block is hashed into the Codex.
    A model is required where its URL names none (`model_keys`).

    `calibration_set`: the path of a PAIR space's set (09 §9.12, amended
    2026-10-08). With `space_from` naming a pair space it is not needed: the
    source's member carries the set, and the copy embeds the same pairs."""
    pairs = Path(calibration_set).read_bytes() if calibration_set else None
    # `toolchain.platform_image` is hashed into collection_id (09 §9.3), and
    # it is "the digest of the platform the command runs in unless the
    # recipe states it" (07 §7.3): the recipe's own value, else
    # --platform-image, else the digest the deploy hands the container
    # (LOUISE_PLATFORM_IMAGE — the image it started; a container cannot see
    # its own image id). The flag used to default to digest(b""), the
    # SHA-256 of nothing, and the live gutenberg Codex carries exactly that
    # placeholder (found 2026-09-14), so no identity may name no build again.
    platform_image = preflight_create(recipe, platform_image)
    # A SLICING THE PLUGIN DERIVES AT CREATION (wikidata's family map,
    # pack-format.md §2 amended 2026-10-24): the block goes into the Codex,
    # the map into the SHARD. A recipe that states its own slicing keeps it
    slicing_member = None
    source_of = tpl.source_for(plugin)
    if callable(getattr(source_of, "creation_slicing", None)) and "slicing" not in recipe:
        recipe["slicing"], slicing_member = source_of.creation_slicing(name or plugin)
    if space_from:
        source = col.read(root, space_from)
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
        space["calibration"].pop("query_vectors", None)     # computed here too (09 §9.12)
        recipe["space"] = space
        recipe["layers"] = [l.to_dict() for l in source.layers]
        from app.hold import calibration as cal, shard as shard_mod
        if pairs is None and cal.form(source) == "pair":
            # a pair space's set is in its member (09 §9.12, amended
            # 2026-10-08): the copy is embedded over the very same pairs
            member = (col.shard_dir_of(Path(root), col.name_of(root, source.collection_id))
                      / shard_mod.calibration_paths(source.space.calibration.set)[0])
            if not member.is_file():
                raise CodexError(
                    f"--space-from {space_from} is a pair space, whose calibration set "
                    f"travels in its member, and this copy holds no member at "
                    f"{member.name}: pass the set's file, --calibration-set FILE")
            pairs = cal.read_pairs(member, expect=source.space.calibration.set)

    # The file's `api`, `embed_model` / `query_model` (07 §7.8, amended
    # 2026-10-09), decided BEFORE anything is created: an endpoint that names
    # no model, and no key naming it, is refused here, not after the Codex
    # exists — and never answered with the space's label
    api = check_api(api)
    models = (model_keys(encoder, query_encoder, embed_model=embed_model,
                         query_model=query_model)
              if encoder else (None, None))
    credits = tpl.expand_archives(recipe.get("archives") or ())
    # The creator's key: 09 §9.12 makes the calibration member's `key`
    # header required and §9.4 lists its signature among a snapshot's files
    # without allowing null, so there is no unsigned collection. Loaded by
    # PATH (invariant 8) and never printed.
    from app.bundles import keys as node_keys
    codex = col.create_from_recipe(root, plugin, recipe,
                                   key=node_keys.load_signing_key(), now=_utc_now(),
                                   platform_image=platform_image,
                                   # `--name` reached this far and stopped here until 2026-10-07:
                                   # the first certified collection came up as gutenberg-<12 hex>
                                   name=name,
                                   # each side asked on the file's wire with ITS model
                                   embed=_embed_for_recipe(recipe["space"], encoder, models[0],
                                                           api=api),
                                   embed_query=(_embed_for_recipe(recipe["space"], query_encoder,
                                                                  models[1], api=api, query=True)
                                                if query_encoder else None),
                                   space_version=source.version if space_from else None,
                                   credits=credits, pairs=pairs, slicing_member=slicing_member,
                                   # RULE 1: a held twin's document-side reference reused
                                   calibration_from=(col.shard_dir_of(
                                       Path(root), col.resolve(root, calibration_from)[0])
                                       if calibration_from else None))
    if space_from and (codex.space.envelope,
                       codex.space.calibration.vectors) != drifted:
        print(f"warning: --space-from {space_from} recorded reference "
              f"vectors {drifted[1]} and this node's encoder produced "
              f"{codex.space.calibration.vectors}. Both collections are "
              f"in space {codex.space_id} — calibration is not inside "
              f"space_id — but the two builds are not the same build.",
              file=sys.stderr)
    # `embed`: what the file spends (07 §7.8) — off for the command; the
    # node's default for the Library's create from its own recipe (RULE 2)
    return codex, _write_held_file(root, codex, encoder, query_encoder, api=api,
                                   embed_model=models[0], query_model=models[1],
                                   embed=embed)


def _encoder_key_args(p) -> None:
    """The rest of the collection file's `[encoder]`, beside `--encoder`
    (07 §7.8, amended 2026-10-09)."""
    p.add_argument("--api", default=None, choices=("openai", "ollama", "vega"),
                   help="the encoder's wire format, into the file's [encoder] api "
                        "(default openai; vega: LocalAI with VeGa's receipts)")
    p.add_argument("--embed-model", default=None, metavar="MODEL",
                   help="the model documents are embedded with — required when the "
                        "--encoder URL names none (LocalAI); else the one it names")
    p.add_argument("--query-model", default=None, metavar="MODEL",
                   help="the model questions are embedded with — required when a "
                        "--query-encoder URL names none; default: --embed-model's")


def check_api(api: str | None) -> str:
    """An `[encoder]`'s wire format, as `--api` gives it (07 §7.8): one set
    for every encoder block (app/hold/calibrate.py's)."""
    from app.hold.calibrate import APIS
    api = api or "openai"
    if api not in APIS:
        raise CodexError(f"--api is one of {', '.join(APIS)} (07 §7.8), got {api!r}")
    return api


def model_keys(encoder: str, query_encoder: str | None = None, *,
               embed_model: str | None = None,
               query_model: str | None = None) -> tuple[str, str]:
    """(`embed_model`, `query_model`) for a file `create` or `share fetch`
    writes (07 §7.8, amended 2026-10-09): the model each side is given
    (`--embed-model`, `--query-model`), else the model its model-container
    URL names (`…/upstream/<model>`). Settled BEFORE anything is created.

    A URL that names no model (LocalAI's one address for every model,
    api = "vega") with no key is REFUSED — never answered with the space's
    encoder label: the label is a description, not a model any server is
    asked for, and a file naming it can neither embed nor search. A key that
    differs from the model its URL names is refused too: the file would say
    two things, and the collection would be refused at load. The query side
    of a symmetric encoder is the document side's model unless named."""
    from app import collections as collections_mod

    def side(url: str, given: str | None, flag: str, key_flag: str) -> str:
        named = collections_mod.model_named_by(url)
        if given and named and given != named:
            raise CodexError(
                f"{key_flag} {given} and {flag} {url} names {named!r}: on the model "
                f"container the URL says which model, and the key must agree (07 §7.8)")
        model = given or named
        if not model:
            raise CodexError(
                f"{flag} {url} names no model (…/upstream/<model>): name it with "
                f"{key_flag} — the collection file's `{key_flag[2:].replace('-', '_')}` is "
                f"the model the server is asked for, and the space's label is no "
                f"model's name (07 §7.8, amended 2026-10-09). Nothing was created")
        return model

    embed = side(encoder, embed_model, "--encoder", "--embed-model")
    if query_encoder:
        return embed, side(query_encoder, query_model, "--query-encoder", "--query-model")
    # symmetric: one URL; a query key, where given, must agree with it too
    return embed, (side(encoder, query_model, "--encoder", "--query-model")
                   if query_model else embed)


#: 07 §7.8's embed modes as the FILE spells them (`on-demand`, not the
#: runtime's `lazy`; app/collections.py canonicalises on read)
HELD_EMBED_MODES = ("off", "on-demand", "all", "p2p")


def check_embed_mode(embed: str | None) -> str:
    """`--embed MODE` / the Library's choice, as the collection file writes
    it; absent is `off` — nothing spent until the operator says."""
    mode = (embed or "off").strip()
    if mode == "lazy":
        mode = "on-demand"
    if mode not in HELD_EMBED_MODES:
        raise CodexError(f"embed is one of {', '.join(HELD_EMBED_MODES)} (07 §7.8), got {embed!r}")
    return mode


def _write_held_file(root, codex, encoder: str | None = None,
                     query_encoder: str | None = None, *, source: str | None = None,
                     api: str = "openai", embed_model: str | None = None,
                     query_model: str | None = None, share: bool = False,
                     embed: str = "off") -> Path:
    """07 §7.8: `hold collection create` writes `collections/<name>.toml`,
    and the file existing is the collection being held.

    It says what a new collection holds before anything is fetched: no
    slice yet (`records fetch` records the ones it takes, 07 §7.3), the
    default kinds, nothing spent on embedding, nothing shared unless
    `share` says so — `share fetch` says so for a public collection, since
    sharing is automatic after a completed fetch (04 §4.3; until 2026-10-19
    it wrote `share = false`, and a peer's bundles and records were offered
    to nobody) — and the
    encoder it was created with, in `[encoder]`: a collection names its
    own, and a file that names none is refused (no default encoder,
    operator 2026-09-18).

    Where: `<root>/<name>.toml`, the spec's path. On a node whose files
    live in CONFIG_DIR rather than under LOUISE_ROOT (the reference node:
    /config beside /data), a create in the node's own root writes where the
    node reads, `collections.config_toml_path(name)`.

    A name holds one collection for its life: a file already naming this
    Codex is left alone, and one naming another is refused.

    `embed` (07 §7.8; `share fetch --embed`, the Library's take, 2026-10-20):
    what this node's encoder spends on the collection — `off` unless said.
    `p2p` publishes, so it needs sharing: asked for on a collection that is
    not shared, it is written as `all`, which is how the worker would run it
    anyway (07 §7.8: "a file … p2p with share = false runs as all").
    """
    embed = check_embed_mode(embed)
    if embed == "p2p" and not share:
        embed = "all"
    path = _held_file(root, col.name_of(root, codex.collection_id))
    if path.exists():
        with open(path, "rb") as f:
            held = str(tomllib.load(f).get("codex") or "")
        if held.removeprefix("sha256:") == codex.collection_id.removeprefix("sha256:"):
            return path
        raise CodexError(
            f"{path} holds {held or 'no codex'} and this is "
            f"{codex.collection_id}: a name holds one collection for its "
            f"life, and another collection is another name (07 §7.8)")
    if encoder:
        api = check_api(api)
        embed_model, query_model = model_keys(encoder, query_encoder,
                                              embed_model=embed_model,
                                              query_model=query_model)
    q = json.dumps                 # a TOML basic string, quotes and backslashes escaped
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(f'codex = "{codex.collection_id}"\n'
                   "slices = []\n"
                   # an annotations collection has relations and no vectors (02 §2.10)
                   + ('kinds = ["records", "relations"]\n' if cx.is_annotations(codex)
                      else 'kinds = ["records", "vectors"]\n')
                   + f'embed = "{embed}"\n'
                   + f"share = {'true' if share else 'false'}\n"
                   # 07 §7.8 `source`: a fetched collection reads its documents
                   # from the shards it holds (app/plugins/shard.py)
                   + (f'source = "{source}"\n' if source else "")
                   + (f'\n[encoder]\nurl = {q(encoder)}\napi = {q(api)}\n'
                      # a bi-encoder's query side, where the collection has one
                      + (f'query_url = {q(query_encoder)}\n' if query_encoder else "")
                      + f'embed_model = {q(embed_model)}\nquery_model = {q(query_model)}\n'
                      if encoder else ""),
                   encoding="utf-8")
    tmp.replace(path)
    return path


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


NO_ENCODER = (
    "name the encoder: --encoder URL. There is no default encoder "
    "(operator, 2026-09-18), and the new collection's file names the one "
    "it was created with. `hold collection create` embeds the "
    "512-text calibration set with the encoder the recipe names "
    "and writes the reference vectors' hash into the Codex "
    "(06 §6.6); there is no offline path that produces a valid "
    "Codex, because a placeholder there is a collection no peer "
    "can ever join")


def _expected_run(space: dict, query: bool) -> tuple[str | None, str | None]:
    """(`engine_id`, weights) the recipe says its reference is computed with
    — the query side's where it has its own encoder; a query encoder that
    names no engine runs on the document side's (09 §9.3). `engine_id` is
    None for a space that names an image, not an engine."""
    enc = space.get("encoder") or {}
    q = (space.get("query") or {}).get("encoder") if query else None
    side = q if isinstance(q, dict) else enc
    raw = side.get("engine") or enc.get("engine")
    engine_id = None
    if isinstance(raw, dict):
        engine_id = cx.engine_from_dict(tpl._engine_from_recipe(raw, "space.encoder.engine"),
                                        "space.encoder.engine").engine_id
    return engine_id, side.get("weights")


def check_reference_runs(space: dict, recipe, runs: set, *, query: bool = False) -> None:
    """G5 (docs/notes/v5-migration.md): the runs that embedded a new
    reference ARE the engine and the weights the recipe writes into the Codex,
    or nothing is created. A reference computed by another engine than the
    one the Codex names is a Codex whose `space.encoder.engine` is false — the
    one field written so anyone can reproduce those vectors (02 §2.4)."""
    from app import run_check
    want_engine, want_weights = _expected_run(space, query)
    if not runs or None in runs:
        raise CodexError(f"the encoder at {recipe.url} stated no run for the calibration set "
                         f"(no receipt names one): nothing ties the reference to the engine "
                         f"the recipe names. Nothing was created")
    for run_id in sorted(runs):
        try:
            stated = run_check.describe_run(recipe, run_id)
        except CodexError:
            raise
        except Exception as e:                          # noqa: BLE001
            raise CodexError(f"the run {run_id[:23]}… that embedded the calibration set "
                             f"could not be described: {e}. Nothing was created") from e
        got_engine = cx.engine_from_dict(stated.engine, "engine").engine_id
        bad = []
        if want_engine is not None and got_engine != want_engine:
            bad.append(f"engine {got_engine[:23]}… (the recipe's is {want_engine[:23]}…)")
        if want_weights and stated.weights != want_weights:
            bad.append(f"weights {stated.weights[:23]}… (the recipe's are {want_weights[:23]}…)")
        if bad:
            raise CodexError(
                f"the run {run_id[:23]}… that embedded the calibration set has "
                f"{' and '.join(bad)}. The Codex names the engine and the weights its "
                f"reference was computed with (02 §2.4, 09 §9.3): write the recipe from "
                f"`louise hold embedder run`, or point --encoder at the run it names. "
                f"Nothing was created")


def _embed_for_recipe(space: dict, url: str | None, model: str | None = None, *,
                      api: str = "openai", query: bool = False):
    """The encoder `create` embeds the calibration set with (06 §6.6).

    There is no Codex yet — the two hashes this produces are Codex
    fields — so the endpoint is resolved from the RECIPE's space block.
    The endpoint is `--encoder URL`, the one the new file will name. It
    was EMBED_URL; there is no default encoder now (operator, 2026-09-18).

    An unreachable encoder here is not `GateDeferred`: there is no
    collection to defer a check on. It is a plain refusal to create.
    """
    from app import collections as collections_mod, embeddings

    if not url:
        raise CodexError(NO_ENCODER)
    # `model`: the side's model as the file will name it (`model_keys`); a
    # caller that gives none asks with the URL's, and never with the space's
    # label, which names no model (amended 2026-10-09). `api`: the wire the
    # new file will name
    model = model or collections_mod.model_named_by(url)
    if not model:
        raise CodexError(f"the encoder at {url} names no model (…/upstream/<model>) and "
                         f"none was given: name it (--embed-model, 07 §7.8). Nothing "
                         f"was created")
    # THE CUT IS THE SPACE'S (09 §9.3, Codex version 8): the reference vectors
    # are made through the cut the recipe declares, or they name other inputs
    recipe = collections_mod.EmbedRecipe(
        url=url, api=api, model=model,
        dtype=space["dtype"], ctx=space["ctx"], dim=space["dim"],
        cut=space.get("cut") or "characters")
    # G5: an endpoint that states its runs — VeGa's receipts — is held to
    # the engine and weights the recipe names, checked before the Codex exists
    vouched = api == embeddings.VEGA

    def embed(texts):
        runs: set = set()
        try:
            # the calibration set is no collection's chunks: text, by design
            out = (embeddings.embed_calibration(recipe, texts, runs=runs) if vouched
                   else embeddings.embed_calibration(recipe, texts))
        except Exception as e:                      # noqa: BLE001
            raise CodexError(
                f"the encoder at {url} could not embed the calibration "
                f"set: {e}. Nothing was created") from e
        if vouched:
            check_reference_runs(space, recipe, runs, query=query)
        return out

    return embed


def cmd_derive(a) -> int:
    mother = col.read(a.root, a.codex_id)
    space_from = col.read(a.root, a.space_from) if a.space_from else None
    space = cx.space_from_dict(_json(a.space)) if a.space else None
    layers = (tuple(cx.layer_from_dict(l, i) for i, l in enumerate(_json(a.layers)))
              if a.layers else None)
    update = Update(**_json(a.update)) if a.update else None
    work = Work(**_json(a.work)) if a.work else None
    pairs = Path(a.calibration_set).read_bytes() if getattr(a, "calibration_set", None) else None

    # Her encoder, as `create`'s (G4, docs/notes/v5-migration.md): a sister in
    # a NEW space embeds her own reference at this endpoint, signed with the
    # node's key at the one clock; one in a space a source holds inherits its
    # member and needs neither. The file's [encoder] is settled BEFORE
    # anything is written, as `create` settles it (07 §7.8).
    encoder = getattr(a, "encoder", None)
    query_encoder = getattr(a, "query_encoder", None)
    api = check_api(getattr(a, "api", None))
    models = (model_keys(encoder, query_encoder, embed_model=getattr(a, "embed_model", None),
                         query_model=getattr(a, "query_model", None))
              if encoder else (None, None))
    block = (space or (space_from.space if space_from else mother.space)).to_dict()
    key = None
    if encoder:
        from app.bundles import keys as node_keys
        key = node_keys.load_signing_key()
    sister = col.derive(a.root, a.codex_id, space=space, space_from=space_from,
                        layers=layers, update=update, work=work,
                        credits=tpl.expand_archives(a.archives) if a.archives else None,
                        embed=_embed_for_recipe(block, encoder, models[0], api=api)
                        if encoder else None,
                        embed_query=_embed_for_recipe(block, query_encoder, models[1],
                                                      api=api, query=True)
                        if query_encoder else None,
                        key=key, now=_utc_now(), pairs=pairs,
                        name=getattr(a, "name", None))
    _acceptance(mother, sister)
    if encoder:
        held = _write_held_file(a.root, sister, encoder, query_encoder, api=api,
                                embed_model=models[0], query_model=models[1])
    else:
        held = _write_held_file(a.root, sister, **_inherited_encoder(a.root, sister, mother,
                                                                     space_from))
    print(f"held: {held}", file=sys.stderr)
    print(sister.collection_id)
    return 0


def _held_file(root, name: str) -> Path:
    """Where `name`'s collection file is (07 §7.8): `<root>/<name>.toml`, or
    the node's CONFIG_DIR for a collection in the node's own root."""
    from app import collections as collections_mod
    node_root = collections_mod.louise_root() / "collections"
    return (collections_mod.config_toml_path(name)
            if Path(root).resolve() == node_root.resolve()
            else Path(root) / f"{name}.toml")


def _inherited_encoder(root, sister: cx.Codex, mother: cx.Codex,
                       space_from: cx.Codex | None) -> dict:
    """A sister derived with no --encoder: the [encoder] of the collection
    whose SPACE she is in — her mother's, or the `--space-from` source's —
    from its file; that endpoint already serves her space. `{}` when she is
    in neither's (the file then names none, and says so)."""
    for source in (mother, space_from):
        if source is None or source.space_id != sister.space_id:
            continue
        path = _held_file(root, col.name_of(root, source.collection_id))
        if not path.is_file():
            continue
        enc = tomllib.loads(path.read_text(encoding="utf-8")).get("encoder") or {}
        if enc.get("url"):
            return {"encoder": enc["url"], "query_encoder": enc.get("query_url"),
                    "api": enc.get("api") or "openai", "embed_model": enc.get("embed_model"),
                    "query_model": enc.get("query_model")}
    print(f"note: {sister.collection_id[:23]}… is in a space no collection held here "
          f"serves, and no --encoder was given: its file names no [encoder] and is "
          f"refused until one is added (07 §7.8)", file=sys.stderr)
    return {}


def _acceptance(mother: cx.Codex, sister: cx.Codex) -> None:
    """Creating a sister is an acceptance, of the space and of what follows
    from it (02 §2.7). What can be said without the SHARD on disk is said
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
        # every credit names itself: an archive by its id, the corpus and the
        # community entries (whose `archive` is null, 02 §2.7) by their kind
        print("  credits inherited: " + ", ".join(c.archive or f"{c.kind} ({c.producer})"
                                                  for c in sister.credits),
              file=sys.stderr)
    else:
        print("  credits: none — she owes nothing and starts from zero",
              file=sys.stderr)


def cmd_continue(a) -> int:
    origin = Origin(**_json(a.origin)) if a.origin else None
    slicing = member = None
    if getattr(a, "reslice", False):
        # the slicing the plugin gives a collection created NOW (wikidata: the
        # map of the code's family list over the pack's class tree), so the
        # daughter is what a creation from scratch would be (02 §2.7, A1g)
        mother_name, mother = col.resolve(a.root, a.codex_id)
        source = tpl.source_for(mother.plugin)
        if not callable(getattr(source, "creation_slicing", None)):
            raise CodexError(f"--reslice: the {mother.plugin} plugin derives no slicing at creation")
        slicing, member = source.creation_slicing(mother_name)
    d = col.continue_(a.root, a.codex_id, platform_image=a.platform_image,
                      continues_through=a.continues_through, origin=origin,
                      origin_reachable_until=a.origin_reachable_until,
                      slicing=slicing, slicing_member=member)
    print(d.collection_id)
    return 0


def cmd_replace(a) -> int:
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


def cmd_collection_rename(a) -> int:
    """`hold collection rename <collection> <new-name> [--dry-run]` (07 §7.3):
    the same collection under another local name — its directory, its
    shared folders, the manifest's stored paths, its pause and its held
    file (app/hold/rename.py). Takes the collection's write lock: on a
    running node, `node pause <collection>` first. Exit 2 while it is held."""
    from app.hold import rename as rename_mod
    name = col.name_of(a.root, a.collection)
    root = Path(a.root) if a.root else _default_root()
    if not a.dry_run:
        _take_write_lock(a.root, name, "hold collection rename", exit_code=2)
    _print(rename_mod.rename(root, name, a.new_name, dry_run=a.dry_run))
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
    node_state.set_worker_state(state, by=f"louise node pause{' --acquire-only' if a.acquire_only else ' --seed-only' if a.seed_only else ''}")
    _print({"node": state, "store": str(node_state.path())})
    return 0


#: `louise dev`'s instruments (07 §7.3, §7.6): verb -> the tools module it
#: runs. Wrapped, not rewritten — the module keeps its own flags, and
#: `python -m tools.<module>` keeps working until step 12 retires it.
DEV_TOOLS = {"schedule-report": "sched_report", "embedder-profile": "profile_embed",
             "refs-sync": "sync_refs", "wikidata-classes": "wikidata_classes"}


def cmd_dev_tool(a) -> int:
    """`louise dev <instrument> [its flags]`: the tools module, given the
    rest of the line as its own arguments."""
    import importlib
    import inspect
    module = importlib.import_module(f"tools.{DEV_TOOLS[a.verb]}")
    argv = [x for x in (a.rest or []) if x != "--"]
    if inspect.signature(module.main).parameters:
        rc = module.main(argv)
    else:
        # a module whose main reads sys.argv: given the line it would have had
        saved = sys.argv
        sys.argv = [f"louise dev {a.verb}", *argv]
        try:
            rc = module.main()
        finally:
            sys.argv = saved
    return int(rc or 0)


def cmd_dev_mark_embedded(a) -> int:  # noqa: C901
    """`louise dev mark-embedded <collection>`: the one-off reconcile for the
    documents `share contribute`'s build embedded and never marked (372 on
    the node, 2026-10-19). ONE scan of the vector table's `id` column,
    keeping the ids of the candidate documents (`embed_done = 0`), then per
    shard of the snapshot the candidates whose every layer-derived chunk id
    is among them, marked as the embed stage marks them. The first form
    looked the chunk ids up per shard — 1,600 lookups over a 10.5 M-row
    table, 54 minutes and no end in sight (München) — a cost of shards ×
    table; this one is the table once plus the shards once. Takes the
    collection's lock unless `--dry-run`; progress on stderr."""
    import time
    from app import db as _db, embed_marks, embeddings, layers as layers_mod
    from app.hold import land as landing, records as rec
    from app.share import seeder
    name, codex = col.resolve(a.root, a.collection)
    snapshot = seeder.current_snapshot(col.shard_dir_of(a.root, name))
    if snapshot is None:
        raise CodexError(f"{name}: no snapshot in the SHARD")
    local = rec.local_collection(a.root, name, codex)
    if not a.dry_run:
        _take_write_lock(a.root, name, "dev mark-embedded")
    out = {"collection": name, "candidates": 0, "rows_scanned": 0, "shards": 0, "complete_unmarked": 0,
           "marked": 0, "dry_run": bool(a.dry_run)}
    t0 = time.time()
    with _closing(_db.connect(local)) as con:
        candidates = {r["uid"] for r in con.execute(
            "SELECT uid FROM documents WHERE in_scope=1 AND embed_done=0 AND error IS NULL").fetchall()}
        out["candidates"] = len(candidates)
        if not candidates:
            _print(out)
            return 0
        held: dict[str, set[str]] = {}
        for batch in embeddings.scan_columns(local, ("id",)):
            ids = batch.column("id").to_pylist()
            out["rows_scanned"] += len(ids)
            for cid in ids:
                uid = cid.rsplit(":", 2)[0] if isinstance(cid, str) else None
                if uid in candidates:
                    held.setdefault(uid, set()).add(cid)
            if out["rows_scanned"] % (65_536 * 16) < 65_536:
                print(f"  scanned {out['rows_scanned']:,} rows, {len(held):,} candidate(s) with vectors, "
                      f"{time.time() - t0:.0f} s", file=sys.stderr)
        print(f"  table scanned: {out['rows_scanned']:,} rows in {time.time() - t0:.0f} s; "
              f"{len(held):,} of {len(candidates):,} candidate(s) have vectors", file=sys.stderr)
        for shard in snapshot.shards:
            sid = f"{shard.slice}/{shard.n:05d}"
            out["shards"] += 1
            records = [r for r in landing.records_for(a.root, name, snapshot, sid, None) if r["uid"] in held]
            complete = []
            for r in records:
                row = {"uid": r["uid"], "native_id": r.get("native_id", 0)}
                sections = [tuple(x) for x in r.get("sections") or []]
                wanted = [c["id"] for c in layers_mod.chunks_for_document(codex, row, sections)]
                if wanted and all(c in held[r["uid"]] for c in wanted):
                    complete.append(r["uid"])
            if not complete:
                continue
            out["complete_unmarked"] += len(complete)
            if not a.dry_run:
                out["marked"] += embed_marks.mark_embedded(con, local, complete, synced=False)
            if out["shards"] % 200 == 0:
                print(f"  {out['shards']} shard(s): {out['complete_unmarked']} complete and unmarked so far",
                      file=sys.stderr)
    out["seconds"] = round(time.time() - t0, 1)
    _print(out)
    return 0


def cmd_dev_deprecate_duplicates(a) -> int:
    """`louise dev deprecate-duplicates <collection>` (03 §3.6; 09 §9.8;
    operator, 2026-10-19): this peer's accepted bundles that share a
    `content_digest` with another of its own for the same part — the holder
    pass bundled 418 partially held parts again after every restart — the
    later of each pair DEPRECATED: one attestation pairing each with the
    kept copy, published for peers; nothing removed here — the copies are
    served one more day (`deprecate.KEEP_S`) for fetches in flight, never
    offered for a new one, and go at the beat after the window. Takes the
    collection's lock."""
    from app import identity
    from app.bundles import keys as node_keys
    from app.hold import equivalence, records as rec
    from app.share import coverage as cov, deprecate as deprecate_mod, peers, seeder, withdraw as withdraw_mod
    name, codex = col.resolve(a.root, a.collection)
    try:
        key = node_keys.load_signing_key()
        own_key = identity.own()
    except Exception as e:   # noqa: BLE001
        raise CodexError(f"a deprecation is signed and this peer has no key: {e}") from None
    image = equivalence.running_image()
    if not image:
        raise CodexError("this process does not know the image it runs (LOUISE_PLATFORM_IMAGE): an attestation "
                         "names it (09 §9.8), so nothing is deprecated")
    snapshot = seeder.current_snapshot(col.shard_dir_of(a.root, name))
    if snapshot is None:
        raise CodexError(f"{name}: no snapshot in the SHARD")
    local = rec.local_collection(a.root, name, codex)
    from app import db as _db
    with _closing(_db.connect(local)) as con:
        already = deprecate_mod.deprecated(a.root, name, codex)
        groups = [dict(d, withdraw=[b for b in d["withdraw"] if not deprecate_mod.by_producer(already, b, own_key)])
                  for d in withdraw_mod.duplicates(a.root, name, codex, con, own_key=own_key)]
        groups = [d for d in groups if d["withdraw"]]        # stated already: not stated twice
        for d in groups:
            print(f"  {d['shard'].rsplit('/', 2)[1]}/{d['shard'].rsplit('/', 1)[1]}  current {d['keep'][:23]}…  "
                  f"deprecate {', '.join(b[:23] + '…' for b in d['withdraw'])}  ({d['chunks']} chunks)",
                  file=sys.stderr)
        pairs = {b: d["keep"] for d in groups for b in d["withdraw"]}
        # and this peer's copies of ANOTHER producer's earlier bundle — the
        # same chunk rows re-signed (Perugia, 2026-10-19: 44 of the node's)
        foreign = deprecate_mod.foreign_duplicates(a.root, name, codex, con, own_key=own_key,
                                                   log=lambda m: print(m, file=sys.stderr))
        foreign = {b: c for b, c in foreign.items() if not deprecate_mod.by_producer(already, b, own_key)}
        shards = {d["shard"] for d in groups}
        chunks = sum(d["chunks"] for d in groups)
        for b, c in foreign.items():
            r = cov.row(con, b)
            shards.add(r["shard"])
            chunks += int(r["chunks"] or 0)
            print(f"  {r['shard'].rsplit('/', 2)[1]}/{r['shard'].rsplit('/', 1)[1]}  current {c[:23]}… (another "
                  f"producer's)  deprecate {b[:23]}…  ({r['chunks']} chunks)", file=sys.stderr)
            pairs[b] = c
        # `parts` are distinct parts; a part held as two bundles, both
        # duplicated, is one part and two groups (en/00013 on the node)
        summary = {"collection": name, "parts": len(shards), "groups": len(groups), "foreign": len(foreign),
                   "bundles": len(pairs), "chunks": chunks}
        if not pairs or a.dry_run:
            _print({**summary, "deprecated": False})
            return 0
        if not a.yes:
            reply = _ask(f"Deprecate {len(pairs)} duplicate copy(ies) over {len(groups)} part(s) of {name}? They stay "
                         f"served for a day and are never offered for a new fetch. [y/N] ")
            if reply not in ("y", "yes"):
                print("not deprecated; nothing was written", file=sys.stderr)
                return 2
        _take_write_lock(a.root, name, "dev deprecate-duplicates")
        with _closing(peers.connect()) as node_con:
            out = deprecate_mod.publish(a.root, name, codex, snapshot, pairs, key=key, own_key=own_key, con=con,
                                        node_con=node_con, platform_image=image,
                                        log=lambda m: print(m, file=sys.stderr))
        _print({**summary, "deprecated": True, **out,
                "note": "the worker forwards the attestation at its next beat; the digest lists the copies under "
                        "deprecated_members from its next send; they are removed at the beat after keep_until"})
    return 0


def cmd_dev_withdraw_covered(a) -> int:
    """`louise dev withdraw-covered <collection> [--dry-run] [--yes]` (03 §3.6;
    09 §9.8): this peer's accepted bundles of the collection whose every
    record is already held, with the same content hash, by bundles that are
    not among them — another producer's or a same-space predecessor's
    (app/share/possession.py). WITHDRAWN: one attestation, published for
    peers; their directories and coverage rows go here, their vectors stay
    (the other bundles cover them). Not a deprecation: there is no single
    current copy of the same part to name (the predecessor's bundles span the
    re-cut parts). First use: gutenberg 6f030405's 117 bundles of 2026-10-22,
    made by the held path after the carry cleared the predecessor's rows.
    Takes the collection's lock."""
    from app import db as _db, identity
    from app.bundles import keys as node_keys
    from app.hold import equivalence, records as rec
    from app.share import peers, possession, seeder, withdraw as withdraw_mod
    name, codex = col.resolve(a.root, a.collection)
    try:
        key = node_keys.load_signing_key()
        own_key = identity.own()
    except Exception as e:   # noqa: BLE001
        raise CodexError(f"a withdrawal is signed and this peer has no key: {e}") from None
    image = equivalence.running_image()
    if not image:
        raise CodexError("this process does not know the image it runs (LOUISE_PLATFORM_IMAGE): an attestation "
                         "names it (09 §9.8), so nothing is withdrawn")
    snapshot = seeder.current_snapshot(col.shard_dir_of(a.root, name))
    if snapshot is None:
        raise CodexError(f"{name}: no snapshot in the SHARD")
    local = rec.local_collection(a.root, name, codex)
    with _closing(_db.connect(local)) as con:
        found = possession.covered_by_others(a.root, name, codex, con, own_key=own_key)
        summary = {"collection": name, "bundles": len(found), "uids": sum(f["uids"] for f in found),
                   "parts": len({f["shard"] for f in found})}
        if not found or a.dry_run:
            _print({**summary, "withdrawn": False})
            return 0
        if not a.yes:
            reply = _ask(f"Withdraw {len(found)} bundle(s) of {name} whose every record another bundle held here "
                         f"covers? [y/N] ")
            if reply not in ("y", "yes"):
                print("not withdrawn; nothing was written", file=sys.stderr)
                return 2
        _take_write_lock(a.root, name, "dev withdraw-covered")
        with _closing(peers.connect()) as node_con:
            out = withdraw_mod.publish(a.root, name, codex, snapshot, [f["bundle_id"] for f in found], key=key,
                                       own_key=own_key, con=con, node_con=node_con, platform_image=image,
                                       log=lambda m: print(m, file=sys.stderr))
        _print({**summary, "withdrawn": True, **out})
    return 0


def cmd_dev_adopt_bundles(a) -> int:
    """`louise dev adopt-bundles <collection>` (07 §7.8, 2026-10-19): the
    collection's `state/bundles` copied under `<BUNDLES_ROOT>/<name>`,
    verified, set aside, and linked the way `create` links a new one."""
    name, _codex = col.resolve(a.root, a.collection)
    _print(col.adopt_bundles_root(a.root, name, log=lambda m: print(m, file=sys.stderr)))
    return 0


def cmd_dev_migrate_schema(a) -> int:
    """`louise dev migrate schema [<collection>]`: pending schema changes
    applied to a collection's state database (tools/migrate_manifest.py),
    every collection's when none is named. Writes; never runs on its own."""
    from app import collections as collections_mod
    from tools import migrate_manifest
    if a.collection:
        c = collections_mod.by_name(a.collection)
        if c is None:
            raise CodexError(f"{a.collection}: no such collection on this node")
        targets = [c]
    else:
        targets = collections_mod.load_all()
    out = {}
    for c in targets:
        out[c.name] = migrate_manifest.run(Path(c.manifest_path), c.plugin, a.dry_run, not a.no_backup)
    _print(out)
    return 0


def cmd_dev_drop_rows(a) -> int:
    """`louise dev drop-rows <collection> --ids FILE [--yes]` (02 §2.4's remedy,
    07 §7.3): the rows FILE lists (one chunk id a line) dropped and their
    documents embedded again — for rows that are not the space's function,
    found by a check or a scan. The collection's write lock held. A dry run
    without --yes. Writes; never runs on its own."""
    from app.hold import carry as carry_mod
    name, _ = col.resolve(a.root, a.collection)
    ids = sorted({line.strip() for line in Path(a.ids).read_text(encoding="utf-8").splitlines()
                  if line.strip()})
    if not a.yes:
        print(f"[drop] would drop {len(ids)} row(s) of {name} and embed their documents "
              f"again (--yes)", file=sys.stderr)
        return 0
    _take_write_lock(a.root, name, "louise dev drop-rows", exit_code=2)
    _print(carry_mod.drop_rows(a.root, name, ids,
                               log=lambda m: print(m, file=sys.stderr)))
    return 0


def cmd_dev_reindex(a) -> int:
    """`louise dev reindex <collection> --ids FILE [--yes]`: the documents
    FILE lists (one uid a line) queued for the text-index stage again —
    `fts_done = 0`, nothing else — so the worker re-cuts them from the live
    parse, rewrites their text-index entries (delete-then-insert) and their
    `documents.chunks`. Embedding is untouched: their vectors are the
    records' (2026-10-25: gutenberg rows indexed under an earlier parse kept
    stale chunk counts and text, `tools/measure/chunks_drift.py` lists them).
    A document out of scope or not yet extracted is left alone and counted.
    The collection's write lock held. A dry run without --yes."""
    import time
    from app import db
    from app.hold import records as rec
    name, codex = col.resolve(a.root, a.collection)
    uids = sorted({line.strip() for line in Path(a.ids).read_text(encoding="utf-8").splitlines()
                   if line.strip()})
    coll = rec.local_collection(a.root, name, codex)
    if a.yes:
        _take_write_lock(a.root, name, "louise dev reindex", exit_code=2)
    con = db.connect(coll)
    try:
        eligible = 0
        for i in range(0, len(uids), 900):
            part = uids[i:i + 900]
            eligible += con.execute(
                f"SELECT COUNT(*) FROM documents WHERE uid IN ({','.join('?' * len(part))}) "
                f"AND in_scope=1 AND rdf_done=1", part).fetchone()[0]
        if not a.yes:
            print(f"[reindex] would queue {eligible} of {len(uids)} listed document(s) of {name} for the "
                  f"text-index stage (fts_done = 0; embedding untouched) — --yes", file=sys.stderr)
            return 0
        con.execute("BEGIN IMMEDIATE")
        queued = 0
        for i in range(0, len(uids), 900):
            part = uids[i:i + 900]
            queued += con.execute(
                f"UPDATE documents SET fts_done=0, updated_at=? WHERE uid IN ({','.join('?' * len(part))}) "
                f"AND in_scope=1 AND rdf_done=1", [int(time.time()), *part]).rowcount
        con.execute("COMMIT")
    finally:
        con.close()
    _print({"collection": name, "listed": len(uids), "queued": queued, "left_alone": len(uids) - queued,
            "note": "the worker's text-index stage re-cuts them; embedding untouched"})
    return 0


def cmd_dev_unindexable(a) -> int:
    """`louise dev unindexable <collection> <uid> [--yes]`: a document this
    image's plugin now refuses (`UnindexableDocument`, e.g. gutenberg's parser
    10 on pg673) withdrawn from every index at once and its error written, as
    the worker does when a stage meets it (app/unindexable.py) — not left to
    the next scan. Refused for a document the plugin still parses. The
    collection's write lock held. A dry run without --yes."""
    import time
    from app import db, plugin_io, unindexable
    from app.hold import records as rec
    from app.plugins.base import UnindexableDocument
    name, codex = col.resolve(a.root, a.collection)
    coll = rec.local_collection(a.root, name, codex)
    if a.yes:
        _take_write_lock(a.root, name, "louise dev unindexable", exit_code=2)
    con = db.connect(coll)
    try:
        row = con.execute("SELECT rowid, uid, native_id, path, lang FROM documents WHERE uid=?",
                          (a.uid,)).fetchone()
        if row is None:
            raise CodexError(f"{name}: no document {a.uid}")
        try:
            plugin_io.canonical_doc(coll.plugin_instance(), row)
        except UnindexableDocument as e:
            reason = str(e)[:500]
        else:
            raise CodexError(f"{name}: {a.uid} parses under this image — it is not unindexable")
        if not a.yes:
            print(f"[unindexable] would withdraw {a.uid} from {name}'s indexes and record: "
                  f"{reason} (--yes)", file=sys.stderr)
            return 0
        out = unindexable.withdraw(con, coll, a.uid)
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE documents SET error=?, embed_done=0, updated_at=? WHERE uid=?",
                    (reason, int(time.time()), a.uid))
        con.execute("COMMIT")
        db.log_event(con, "info", "unindexable", f"not indexable: {reason}", a.uid)
        _print({**out, "collection": name, "error": reason})
    finally:
        con.close()
    return 0


def cmd_dev_carry(a) -> int:
    """`louise dev carry <from> <to> [--copy] [--keep producer:<archive>] --yes`
    (07 §7.3; docs/notes/v5-migration.md G1, G2; amendment A1): the source's
    manifest, text index and vector table into a new collection with the same
    records, with both collections' write locks held. A dry run without --yes.
    Writes; never runs on its own."""
    from app.hold import carry as carry_mod
    from_name, _ = col.resolve(a.root, a.source)
    to_name, _ = col.resolve(a.root, a.target)
    producer = carry_mod.producer_of(a.keep) if a.keep else None
    if a.yes:
        # one writer per collection (07 §7.7), both of them, in name order
        for name in sorted({from_name, to_name}):
            _take_write_lock(a.root, name, "louise dev carry", exit_code=2)
    say = lambda m: print(m, file=sys.stderr)              # noqa: E731
    if getattr(a, "predecessor_rows", False):
        _print({"predecessor_rows": carry_mod.keep_predecessor_rows(a.root, from_name, to_name,
                                                                    apply=a.yes, log=say)})
        return 0
    if getattr(a, "reconcile", False):
        if not a.yes:
            say(f"[carry] would reconcile {to_name} against {from_name} (--yes)")
            return 0
        _print({"reconcile": carry_mod.reconcile(a.root, from_name, to_name, log=say)})
        return 0
    if getattr(a, "resign", False):
        if not a.yes:
            say(f"[carry] would re-sign {to_name}'s documents by its own discovery and prove "
                f"their chunks unchanged (A1d; --yes)")
            return 0
        _print({"resign": carry_mod.resign(a.root, from_name, to_name, log=say)})
        return 0
    recut = getattr(a, "recut", False)
    # --recut WITH --keep (2026-10-21, wikipedia's parser-5 switch: the cut, the
    # weights and the parser move at once): keeping only an archive's rows drops
    # every row that is not the archive's — a superset of what the recut drops
    # (rows NOT an archive's that the character cut shortened) — so the cut is
    # crossed and the recut pass has nothing left to do. Without --keep, --recut
    # stands alone, as before.
    if recut and getattr(a, "rebuilt", False) and not producer:
        raise CodexError("--recut with --rebuilt needs --keep producer:<archive>: on its own "
                         "it drops the rows the character cut shortened and keeps every other (A1e)")
    out = {"carry": carry_mod.carry(a.root, from_name, to_name, copy=a.copy,
                                    apply=a.yes, keep=producer,
                                    rebuilt=getattr(a, "rebuilt", False), recut=recut,
                                    resliced=getattr(a, "resliced", False), log=say)}
    if recut and producer:
        say(f"[carry] the cut is crossed with --keep {a.keep}: only that archive's rows stay, "
            f"so no row the character cut shortened is left to recut (A1e)")
    elif recut:
        # the SOURCE's budget: the character cut its rows went through
        _, src_codex = col.resolve(a.root, from_name)
        out["recut"] = carry_mod.recut(a.root, to_name, budget=src_codex.space.ctx * 4,
                                       apply=a.yes, log=say)
    if producer and a.yes:
        keep = carry_mod.keep_producer_scan if getattr(a, "scan", False) else carry_mod.keep_producer
        out["keep"] = keep(a.root, to_name, producer, log=say)
    elif producer:
        say(f"[carry] then: keep only {producer}'s vectors in {to_name} (--yes)")
    _print(out)
    return 0


def cmd_embedder_run(a) -> int:
    """`hold embedder run --encoder URL [--api A] [--model M] [--query] [--pair]`
    (07 §7.3; docs/notes/v5-migration.md G5): the run the endpoint states now
    — its id and what it stands for (engine, weights, driver), verified
    against the id — as JSON, for writing a recipe's `space.encoder`."""
    import httpx

    from app import collections as collections_mod, run_check
    model = a.model or collections_mod.model_named_by(a.encoder)
    if not model:
        raise CodexError(f"--encoder {a.encoder} names no model (…/upstream/<model>): name "
                         f"it with --model (07 §7.8)")
    if a.query and a.pair:
        raise CodexError("--query and --pair: a query is one text in every space; a text "
                         "pair is a document's form (09 §9.3)")
    recipe = collections_mod.EmbedRecipe(url=a.encoder, api=check_api(a.api), model=model,
                                         dtype="f32", ctx=8192, query_model=model)
    try:
        stated = run_check.stated_run(recipe, query=a.query,
                                      probe_item=run_check.PAIR_PROBE if a.pair
                                      else run_check.PROBE)
    except CodexError:
        raise
    except (ConnectionError, httpx.TransportError) as e:
        print(f"louise: deferred: {a.encoder}: {e}", file=sys.stderr)
        return 3
    except Exception as e:                              # noqa: BLE001
        raise CodexError(f"{a.encoder}: {type(e).__name__}: {e}") from e
    _print({"run": stated.run_id, "engine": stated.engine, "weights": stated.weights,
            "driver": stated.driver})
    return 0


def cmd_embedder_tree_diff(a) -> int:
    """`hold embedder tree-diff BEFORE AFTER [--json]` (vega.md §12): the
    per-file lists two engine images are the digests of — read from VeGa
    (`<VEGA_URL>/vega/trees/<image>`) for a digest, from a JSON file
    otherwise — and what differs between them, path by path. Exit 0 when the
    two are the same list, 1 when they differ, 2 when one cannot be read."""
    from app import config
    from app.vega import trees
    try:
        before = trees.read(a.before, vega_url=config.VEGA_URL)
        after = trees.read(a.after, vega_url=config.VEGA_URL)
    except (OSError, ValueError) as e:
        print(f"louise: {e}", file=sys.stderr)
        return 2
    d = trees.diff(before, after)
    if a.as_json:
        _print(d)
    else:
        for line in trees.lines(d):
            print(line)
    return 0 if not (d["added"] or d["removed"] or d["changed"]) else 1


def cmd_node_show(a) -> int:
    """`node show` (07 §7.3): state per collection, the current stage, the
    last round's cost, the overrides in force, the verbs running under the
    node. Read-only; creates nothing."""
    from app import node_show
    _print(node_show.show())
    return 0


#: The web process `node start` runs beside the worker: the image's own
#: command (Dockerfile), so one node started either way serves the same app.
WEB_COMMAND = ("-m", "uvicorn", "app.api:app", "--host", "0.0.0.0")
WEB_STOP_WAIT_S = 30.0


def cmd_node_start(a) -> int:
    """`node start [--acquire-only | --seed-only] [--only worker|web]` (07
    §7.3): the daemon — the worker's rounds and the web interface — until
    stopped. A mode flag sets the node's mode as `node pause` does (it
    survives a restart, §7.8). `--only` runs one of the two, which is how a
    deployment with one container each runs them. Exit: the worker's —
    0 after `node stop`, 130 on a signal (§7.7)."""
    import subprocess
    from app import config, node_state
    if a.acquire_only or a.seed_only:
        state = "acquire-only" if a.acquire_only else "seed-only"
        node_state.set_worker_state(state, by=f"louise node start --{state}")
    web_argv = [sys.executable, *WEB_COMMAND, "--port", str(config.PORT)]
    if a.only == "web":
        os.execv(sys.executable, web_argv)
    web = subprocess.Popen(web_argv) if a.only is None else None
    try:
        from app import worker
        return worker.main()
    finally:
        if web is not None and web.poll() is None:
            web.terminate()
            try:
                web.wait(timeout=WEB_STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                web.kill()
                web.wait()


def cmd_node_stop(a) -> int:
    """`node stop` (07 §7.3): asks the worker to finish its current units,
    commit and exit 0 — through the node's state, which every container of
    the node shares; it does not wait for the exit."""
    from app import node_state
    node_state.request_stop()
    _print({"node": "stopping", "store": str(node_state.path()),
            "says": "the worker finishes its current units, commits and exits 0"})
    return 0


def cmd_node_resume(a) -> int:
    from app import node_state
    if a.collection:
        names = node_state.set_paused(a.collection, False)
        _print({"paused": sorted(names)})
        return 0
    node_state.set_worker_state("running", by="louise node resume")
    # the store written, said: the worker obeys the one it reads (its log
    # names it: "node mode … (read from …)"), and a command run with another
    # LOUISE_ROOT writes another (2026-10-23)
    _print({"node": "running", "store": str(node_state.path())})
    return 0


#: 07 §7.3 writes `share <collection>`: the collection stands where a verb would
_SHARE_VERBS = ("collection", "serve", "fetch", "contribute", "attest", "peers", "-h", "--help", "offer")


def cmd_share(a) -> int:
    """`share <collection> [--stop]` (07 §7.3; 04 §4.3): the desired state —
    `share = true` (or false) in the collection's file — which the worker
    serves at its next beat (app/share/seeder.py). A `hold` command never
    opens a port (07 §7.4), and neither does this one. `--slices` and
    `--kinds` narrow what is served: `share_slices` and `share_kinds` in the
    same file (07 §7.8), comma-separated; `all` lifts a narrowing. A
    whole-collection member — terms, validation, calibration, seals — is
    always served (app/share/seeder.py `serves`)."""
    import re
    from app.hold import deposit
    name, codex = col.resolve(a.root, a.collection)
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
    from app import collections as collections_mod
    narrowed = {}
    for flag, key in ((a.slices, "share_slices"), (a.kinds, "share_kinds")):
        if flag is None:
            continue
        values = [v.strip() for v in flag.split(",") if v.strip()]
        if key == "share_kinds":
            bad = [v for v in values if v not in collections_mod.SHARE_KINDS and v != "all"]
            if bad:
                raise CodexError(f"--kinds names {bad}; the member kinds are "
                                 f"{', '.join(collections_mod.SHARE_KINDS)} (02 §2.6)")
        elif values != ["all"]:
            # A SLICE BY ITS NAME, as the snapshot lists it: `--slices la`
            # matched no member (`records-lang=la`) and served no part at all,
            # silently (2026-10-12)
            from app.share import seeder as seeder_mod
            snap = seeder_mod.current_snapshot(col.shard_dir_of(a.root, name))
            known = sorted({s.slice for s in snap.shards}) if snap is not None else None
            bad = [v for v in values if known is not None and v not in known]
            if bad:
                hint = [k for k in known if any(k.endswith("=" + b) for b in bad)]
                raise CodexError(f"--slices names {bad}, which the snapshot does not list; a slice "
                                 f"is named as it is listed, e.g. {hint or known[:3]}")
        line = None if values == ["all"] else f"{key} = [" + ", ".join(json.dumps(v) for v in values) + "]"
        narrowed[key] = None if line is None else values
        if re.search(rf"^{key}\s*=", text, flags=re.M):
            text = re.sub(rf"^{key}\s*=.*\n?", "" if line is None else line + "\n", text,
                          count=1, flags=re.M)
        elif line is not None:
            text = re.sub(r"^share\s*=.*$", lambda m: m.group(0) + "\n" + line, text,
                          count=1, flags=re.M)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    _print({"collection": name, "share": not a.stop, "file": str(path),
            **narrowed,
            "note": "the worker serves and announces it at its next beat; "
                    "`hold collection show` names the state it publishes"})
    return 0


def _check_slices(out: dict, slices) -> None:
    """A SLICE BY ITS NAME, as the snapshot just pulled lists it (07 §7.8):
    `--slice la` matched no member and fetched no part (2026-10-12). Shared
    by `share fetch` and the Library's take (app/jobs.py)."""
    if slices == "all":
        return
    from app.share import seeder as seeder_mod
    snap = seeder_mod.current_snapshot(Path(out["shard"])) if out.get("shard") else None
    known = sorted({s.slice for s in snap.shards}) if snap is not None else None
    bad = [v for v in slices if known is not None and v not in known]
    if bad:
        hint = [k for k in known if any(k.endswith("=" + b) for b in bad)]
        raise CodexError(f"--slice names {bad}, which snapshot {snap.snapshot_id} does not "
                         f"list; a slice is named as it is listed, e.g. {hint or known[:3]}")


def _set_held_selection(path, slices, kinds) -> dict:
    """07 §7.8: the file's `slices` and `kinds` — what `share fetch` was asked
    for, joined with what the file already said (`"all"` wins); the worker
    fetches what the SHARD lacks of it. Returns what the file says now."""
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
    encoder_keys = {"api": check_api(getattr(a, "api", None)),
                    "embed_model": getattr(a, "embed_model", None),
                    "query_model": getattr(a, "query_model", None)}
    if a.encoder:          # refused before anything is pulled (07 §7.8, amended 2026-10-09)
        model_keys(a.encoder, embed_model=encoder_keys["embed_model"],
                   query_model=encoder_keys["query_model"])
    out = fetch_mod.fetch(a.root, ref, snapshot_id=a.snapshot, from_address=getattr(a, "from_", None),
                          encoder=a.encoder, encoder_keys=encoder_keys, name=a.name, session=session,
                          embed=check_embed_mode(getattr(a, "embed", None)),
                          log=lambda m: print(m, file=sys.stderr))
    _check_slices(out, slices)
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
    republished. Exit 0 when nothing failed, 1 when a check did.
    `--republish`: no check; this key's held attestations that no event of
    this peer ever carried are published now (added 2026-10-19: a peer that
    joined after the node's checks could not contribute until the node's
    operator published them by hand)."""
    from app import identity
    from app.bundles import keys as node_keys
    from app.share import attest as attest_mod, peers
    try:
        key = node_keys.load_signing_key()
        own_key = identity.own()
    except Exception as e:   # noqa: BLE001 — a peer with no key attests nothing (07 §7.8)
        raise CodexError(f"share attest publishes signed events and this peer has no key: "
                         f"{e}") from None
    if getattr(a, "republish", False):
        name, codex = col.resolve(a.root, a.collection)
        with _closing(peers.connect()) as con:
            ids = attest_mod.own_unpublished(a.root, name, codex, own_key=own_key, con=con)
            out = attest_mod.publish(a.root, name, codex, ids, key=key, own_key=own_key, con=con,
                                     log=lambda m: print(m, file=sys.stderr))
        _print({"republish": True, "held_by_this_key_unpublished": len(ids), **out,
                "note": "the worker forwards the events to every neighbour at its next beat"})
        return 0
    name, codex, snapshot, seed, report = run_check(a)
    with _closing(peers.connect()) as con:
        out = attest_mod.publish(a.root, name, codex, list(report.attestations.values()),
                                 key=key, own_key=own_key, con=con,
                                 log=lambda m: print(m, file=sys.stderr))
    _print({"check": report.to_json(), "seed": seed.hex(), **out,
            "note": "the worker forwards the events to every neighbour at its next beat"})
    return 0 if report.ok else 1


def _serves_here(root, name: str, node_con) -> bool:
    """Whether a bundle this peer accepts can be fetched from it (04 §4.2:
    announcing what you do not serve would be a lie): `share = true` in the
    collection's file, and a node mode that seeds (07 §7.8). The Perugia
    peer announced bundles under `share = false`, and the node waited two
    minutes on each (2026-10-19)."""
    from app import node_state
    try:
        with open(_held_file(root, name), "rb") as f:
            share = bool(tomllib.load(f).get("share", False))
    except (OSError, ValueError, CodexError):
        share = False
    try:
        seeds = node_state.runs(node_state.worker_state(node_con), node_state.SEED)
    except Exception:   # noqa: BLE001 — no state: the mode is unknown, and an unknown mode runs nothing
        seeds = False
    return share and seeds


def _lease_holder(command: str) -> str:
    from app import encoder_use
    return encoder_use.holder(command)


def _leasing(embed, recipe, *, holder: str):
    from app import encoder_use
    return encoder_use.leasing(embed, recipe, holder=holder)


def _release_lease(holder: str) -> None:
    from app import encoder_use
    encoder_use.release(holder)


def _embedding_callables(root, name: str, codex, *, stop, holder: str | None = None):
    """What `share contribute` embeds with (03 §3.3; app/encoder_use.py):
    the collection's own encoder through its recipe and, for a collection
    gated per answer, the guard that stands between a batch's answers and
    its write. Raises `embedder.GateDeferred` when the encoder is not there
    to ask. `holder` names the encoder lease the batches renew."""
    from app import encoder_use
    recipe = _recipe_for(codex, name)
    return encoder_use.callables(codex, recipe, root=root, name=name, stop=stop,
                                 holder=holder or _lease_holder("embed"),
                                 log=lambda m: print(m, file=sys.stderr))


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
    from app import collections as collections_mod, config, copy_state, embeddings, identity, \
        licensing
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
    holder = _lease_holder("share contribute")

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
            local = rec.local_collection(a.root, name, codex)
            # invariant 9: embedding is a use, and a bundle is an exchange.
            # `vectors build` asks; this wrapper of it did not, which left
            # the gate to quarantine — and quarantine only reports on a node
            # that does not enforce it (found retiring app/cli.py, whose
            # `contribute` asked: 2026-10-08)
            licensing.require_ack(local, use="its text is embedded and shared as bundles")
            copy_state.require_in_use(a.root, name, codex, local, use="contributing vectors for it")
            embed, before_write = _embedding_callables(a.root, name, codex, stop=stop, holder=holder)
            now = __import__("time").time()
            with _closing(peers.connect()) as node_con:
                live = cov.live_peers(codex, node_con, now=now, own_key=own_key,
                                      own_benchmarks=cov.own_benchmarks(a.root, name, codex, own_key))
                from app import db as _db
                with _closing(_db.connect(local)) as mcon:
                    results[name] = contrib.once(
                        a.root, name, codex, key=key, own_key=own_key, embed=embed,
                        vectors_for=lambda ids: embeddings.vectors_by_chunk_id(ids, collection=local),
                        held_books=lambda ids: embeddings.books_with_vectors(ids, collection=local),
                        write=lambda rows: embeddings.upsert_chunks(rows, collection=local,
                                                                    dtype=codex.space.dtype),
                        live=live, con=mcon, node_con=node_con, now=now, stop=stop,
                        log=lambda m: print(m, file=sys.stderr), batch=config.EMBED_SLICE_CHUNKS,
                        before_write=before_write, serving=_serves_here(a.root, name, node_con))
            if stop.is_set():
                break
        return results

    try:
        if a.once:
            _print(one_pass())
            # 07 §7.7: a signal finishes the unit and exits 130 — `--once`
            # returned 0 after SIGTERM (Perugia, 2026-10-19)
            return 130 if stop.is_set() else 0
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
    finally:
        _release_lease(holder)


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
    _print(traverse.traverse(a.id, relation=a.relation, depth=a.depth, collection=a.collection, root=a.root,
                             resolve_in=a.resolve_in))
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


def cmd_share_offer(a) -> int:
    """`share offer` (07 §7.3; 04 §4.2): the collections neighbours announce
    that this node does not hold — the catalogue minus what is held — with
    who serves each; `share fetch` takes one. The Library's "Offered by
    peers" reads the same function (app/share/offer.py)."""
    from app.share import offer
    _print({"offered": offer.offered_for_node(),
            "note": "`share fetch <collection id> [--slice …] [--kinds …] [--encoder URL]` takes one; "
                    "nothing is fetched that was not chosen"})
    return 0


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
    out.update(_show_shard(a.root, name))
    # 06 §6.1 (amended 2026-10-25): the origin's inconsistencies the last
    # parse of each document met, counted per code (`hold issues list`)
    out["issues"] = _issues_summary(a.root, name, codex)
    _print(out)
    return 0


def _issues_summary(root, name: str, codex) -> dict:
    from app import collections as collections_mod, issues
    from app.hold import records as rec
    return issues.summary_at(rec._paths(root, name)[1] / "manifest.sqlite",
                             collections_mod.PLUGIN_REGISTRY.get(codex.plugin))


def _show_shard(root, name: str) -> dict:
    """09 §9.3's `snapshots` and `holdings`, read off the SHARD: every
    snapshot in the chain with its verified signatures and whether its
    record members are all held; what the SHARD holds of the current one."""
    import json
    from app.hold import snapshot as sn
    from app.share import holdings, members as mem, seeder
    shard_dir = col.shard_dir_of(Path(root), name)
    sdir = shard_dir / "snapshots"
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
                      "held": bool(records) and all(holdings.held(shard_dir, m) for m in records)})
    current = seeder.current_snapshot(shard_dir)
    from app.hold import anchor
    codex = col.read(Path(root), name)
    return {"snapshots": snaps, "holdings": holdings.summary(shard_dir, current),
            **_trust_level(root, name, current),
            # 05 §5.3, 03 §3.8: certification beside the trust level — two axes
            "certification": anchor.certification(Path(root), name, codex, current),
            "anchor": anchor.status(Path(root), name, codex),
            # 03 §3.8: the record shards no certifier-signed snapshot lists —
            # non-empty, the copy is integrity violated on this peer
            "integrity_violated": list(_integrity_violations(Path(root), name, codex)),
            # 03 §3.2: the last plan the worker ran in the background
            "scheduled_check": _scheduled_check(Path(root), name)}


def _scheduled_check(root, name):
    from app import scheduled_checks
    got = scheduled_checks.last(root, name)
    if got is None:
        return None
    return {k: got.get(k) for k in ("time", "snapshot_id", "outcome", "exit", "secs")}


def _integrity_violations(root, name, codex):
    from app.hold import integrity
    return integrity.violations(root, name, codex)


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
    # the parse pool takes the whole quota here (config.build_parse_processes)
    from app import config
    if config.BUILD_WHOLE_QUOTA:
        config.PARSE_PROCESSES = config.build_parse_processes()
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
                       records_per_shard=a.records_per_shard, stop=stop,
                       records_from=getattr(a, "records_from", None))
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
    seeding = bool(getattr(a, "shards_from", None))
    if not seeding:
        equivalence.require_reference(a.root, ref_name, ref_codex,
                                      use="create its snapshots")
    # SEEDING BUILDS NO RECORD (03 §3.4, amended 2026-10-09): the parts are
    # the source's own, byte for byte (pieces roots checked), and the source
    # names the SAME reference image (`refuse_unless_same_records`), so they
    # are that image's work, not this one's. What this image does compute —
    # each part's records_root and chunks_root — must then equal the source
    # snapshot's, or the snapshot is withdrawn: an image that cuts the chunks
    # differently is caught here, where the equivalence proof would have
    # caught it. (Live: gutenberg6, whose check needs a snapshot first.)
    snapshot_id = a.snapshot_id
    if seeding:
        # G3 (docs/notes/v5-migration.md): the parts are another collection's
        # byte-identical ones, linked and checked, its staging written as a
        # build would leave it; the snapshot below is the ordinary one, of the
        # source snapshot's period unless --snapshot names another
        from app.hold import seed
        source, source_snapshot = seed.split_ref(a.shards_from)
        src_snap = seed.latest_snapshot(a.root, c.name_of(a.root, source), source_snapshot)
        if seed.holds_the_snapshot(a.root, ref_name, src_snap):
            # a HOLDER (2026-10-21): the snapshot is held already, fetched and
            # signed by its creator — the parts this peer has are linked, the
            # rest are its selection's to fetch; nothing is computed or signed
            held = seed.seed_holder(a.root, ref_name, source, from_snapshot=source_snapshot,
                                    log=lambda m: print(m, file=sys.stderr))
            print(held["snapshot"])
            return 0
        seeded = seed.seed_shards(a.root, ref_name, source, from_snapshot=source_snapshot,
                                  log=lambda m: print(m, file=sys.stderr))
        snapshot_id = snapshot_id or seeded["snapshot_id"]
    snap = sn.create(a.root, a.collection, snapshot_id=snapshot_id)
    if seeding:
        bad = seed.same_parts_or_withdraw(a.root, ref_name, snap, source, source_snapshot)
        if bad:
            print(f"louise: {ref_name}: the seeded snapshot does not reproduce "
                  f"{source}'s parts under this image, so it is withdrawn: "
                  + "; ".join(bad[:5]), file=sys.stderr)
            return 1
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
        reply = _ask(f"Use {name} although its origin could not be checked? The "
                     f"check still runs when it can. [y/N] ")
        if reply not in ("y", "yes"):
            print("not acknowledged; nothing was written. `louise hold collection "
                  f"forget {name}` rejects the copy", file=sys.stderr)
            return 2
    out = copy_state.record_origin_ack(a.root, name, codex, now=_utc_now())
    print(f"acknowledged  origin unchecked  {out}")
    return 0


def _ask(question: str) -> str:
    """A yes/no a person answers — never one read from a pipe: without a
    terminal it is a refusal naming `--yes` (07 §7.7), as `forget` says it.
    The acknowledgements read stdin regardless and ended in a traceback
    (EOFError) when run with `docker exec` (second peer, 2026-10-07)."""
    if not sys.stdin.isatty():
        raise CodexError("--yes is required when stdin is not a terminal (07 §7.7)")
    return input(question).strip().lower()


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

    from app.hold import collection as col, shard as shard_mod

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

    terms = a.root / name / "shard" / "LICENSE-TERMS"
    if not terms.is_file():
        raise CodexError(
            f"{name}: the Codex pins terms {want[:23]}… and this copy has no "
            f"shard/LICENSE-TERMS. It is one of the three members fetchable "
            f"BEFORE an acknowledgement (04 §4.4), so a copy without it "
            f"cannot be acknowledged — fetch it first")

    import hashlib
    got = "sha256:" + hashlib.sha256(terms.read_bytes()).hexdigest()
    if got != want:
        raise CodexError(
            f"{name}: shard/LICENSE-TERMS hashes to {got[:23]}… and the Codex "
            f"pins {want[:23]}…. Terms are byte-exact and never converted — a "
            f"CRLF checkout silently invalidates every recorded ack (09 §9.13). "
            f"Nothing is recorded")

    if not a.yes:
        print(terms.read_text(encoding="utf-8", errors="replace"))
        print("-" * 72)
        reply = _ask(f"Acknowledge these terms for {name}? [y/N] ")
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
        shard as shard_mod, embedder as emb, land as landing, records as rec, vectors as vec

    if getattr(a, "retry", False):
        if not getattr(a, "archive", None):
            raise CodexError("--retry puts an ARCHIVE's rejected shards back in line; "
                             "it needs --archive ID")
        return cmd_vectors_archive_retry(a)
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

    member = a.root / name / "shard" / \
        shard_mod.calibration_paths(codex.space.calibration.set)[0]
    if not member.is_file():
        raise CodexError(
            f"{name}: no calibration member at {member.name}. T0's envelope "
            f"and T2's challenge are both read from it (09 §9.12), so a peer "
            f"without one cannot judge a bundle at all")
    reference, envelope = _read_member(member)

    node = adms.connect(a.root)
    header, rows, possession, bundle_id = vec.read(Path(a.bundle))

    # 03 §3.5: a key this peer denied, or one a proof of forgery burned, is
    # one it accepts no traffic from at all, bundles included — the same
    # peer-wide list the legacy inbox reads (app/bundles/trust.py)
    from app.hold import trust_file
    muted = trust_file.refusal(header["producer_pubkey"], Path(a.root).parent)
    if muted is not None:
        print(f"louise: {muted}; nothing was written", file=sys.stderr)
        return 2

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
                       # T3 re-embeds what the record gives back: a pair
                       # layer's chunk with its `fields` (09 §9.3)
                       text_of=lambda cid: landing.input_of(cut, cid),
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
        from app.share import counting
        out = cov.show(codex, snapshot, mcon,
                       attestations=copy_state.attestations(a.root, name, codex),
                       peers=live, now=now, **counting.view_kwargs(a.root, name, codex, mcon))
    _print(out)
    return 0


def cmd_vectors_validity_columns(a) -> int:
    """`hold vectors validity-columns <collection>`: the repair for a
    versioned collection whose vector table was written without its validity
    columns — dense search as of a date withheld, the embed stage deferring
    every document (Normattiva, 2026-10-13: the batched path passed none).
    The table is rebuilt with the three columns read from the manifest's
    version table, EVERY VECTOR KEPT (RULE 1: nothing is re-embedded). Its
    vector index is not carried: `hold index build <collection> --vector`
    afterwards. Exit 1 for a collection that declares no validity."""
    from app import db, embeddings, validity
    from app.hold import collection as col, records as rec

    name, codex = col.resolve(a.root, a.collection)
    _take_write_lock(a.root, name, "hold vectors validity-columns")
    local = rec.local_collection(a.root, name, codex)
    if validity.declared(local) is None:
        print(f"louise: {name} declares no validity: its vector table carries no columns "
              f"by design", file=sys.stderr)
        return 1
    con = db.connect(local)
    log = lambda m: print(m, file=sys.stderr)       # noqa: E731
    out = embeddings.add_validity_columns(local, con, lock_con=con, log=log)
    _print({"collection": name, **out,
            "next": None if out.get("unchanged") else f"hold index build {name} --vector"})
    return 0


def cmd_vectors_reembed(a) -> int:
    """`hold vectors reembed <collection>`: every vector of the collection
    made again by the encoder that serves it now — for vectors a run made
    that does not give back its own bytes (CLAUDE.md invariant 12;
    Normattiva's DAR-legal-it, 2026-10-21: 1 in 4 repeats bit-identical).
    Every embedded document is marked `embeddings.REPLACED_VERSION`; the
    embed stage's version walk selects it and keeps nothing of its own
    (`may_reuse`), and in a versioned collection a text another document
    has already re-embedded is carried from that one, never from a vector
    being replaced (06 §6.3, amended 2026-10-21). The old vectors serve
    search until each document's are replaced. Needs no lock: windows of
    rowids, one transaction each. The worker's version walk finds them
    within an hour, at once when it next starts."""
    from app import db, embeddings
    from app.hold import collection as col, records as rec

    name, codex = col.resolve(a.root, a.collection)
    local = rec.local_collection(a.root, name, codex)
    con = db.connect(local)
    n = embeddings.mark_for_reembed(con)
    _print({"collection": name, "marked": n,
            "next": "the embed stage re-embeds them: within the hour, at once on the "
                    "worker's next start"})
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
    holder = _lease_holder("hold vectors build")
    if not a.dry_run:
        try:
            recipe = _recipe_for(codex, name)
        except emb.GateDeferred as e:
            print(f"louise: deferred: {e}", file=sys.stderr)
            return 3

        seen: set = set()

        def embed(chunks):                           # noqa: F811
            seen.clear()
            try:
                # each chunk in its layer's form (input forms, 09 §9.3)
                if by_run:
                    return embeddings.embed_chunks(codex, chunks, recipe=recipe, runs=seen)
                return embeddings.embed_chunks(codex, chunks, recipe=recipe)
            except Exception as e:                   # noqa: BLE001
                raise emb.GateDeferred(
                    f"the encoder at {recipe.url} could not embed: {e}") from e
        embed = _leasing(embed, recipe, holder=holder)

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
    finally:
        _release_lease(holder)

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
    d = col.shard_dir_of(Path(root), name) / "snapshots"
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
    # vectors) — use of the corpus, as the retired `bundle resweep` was
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
        # the text, or a pair layer's chunk with its `fields` (09 §9.3)
        return landing.input_of(cut, cid)

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
    from app.hold import collection as col, shard as shard_mod, snapshot as snap
    d = col.shard_dir_of(Path(root), name) / "snapshots"
    if snapshot_id:
        p = col.shard_dir_of(Path(root), name) / shard_mod.snapshot_path(snapshot_id)
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
    from app.hold import check as chk, collection as col, shard as shard_mod, \
        embedder as emb, records as rec

    name, codex = col.resolve(a.root, a.collection)
    local = rec.local_collection(a.root, name, codex)
    # Invariant 9: the plan embeds sampled text, and its attestations are
    # made to be exchanged — use of the corpus either way
    from app import licensing
    licensing.require_ack(local, use="its text is embedded to check it")
    snapshot = _latest_snapshot(a.root, name, a.snapshot)
    seed = bytes.fromhex(a.seed) if a.seed else os.urandom(32)

    reference = embed = None
    if codex.space is not None:
        # an annotations collection (02 §2.10) has no space: no reference,
        # nothing to embed, and its plan names no check that would
        member = a.root / name / "shard" / \
            shard_mod.calibration_paths(codex.space.calibration.set)[0]
        reference = _read_member(member)[0] if member.is_file() else None
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
        weigh_unit=_weigh_from(codex, getattr(a, "origin_dir", None)),
        origin_bytes=(int(a.origin_mib) << 20) if getattr(a, "origin_mib", None) else None,
        against=_json(a.against) if getattr(a, "against", None) else None,
        only_shards=([s for s in a.shards.split(",") if s] if getattr(a, "shards", None) else None),
        live=getattr(a, "live", None),
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
            # by identity at the end, not by the name the check started under
            # (a scheduled check runs for up to half an hour; the gutenberg
            # re-pin renamed its collection meanwhile, 2026-10-20)
            name_now = col.where_now(a.root, name, codex.collection_id)
            if name_now is None:
                print(f"note: unattested — {name} is held under no name now (forgotten while it was "
                      f"checked); nothing written", file=sys.stderr)
            else:
                if name_now != name:
                    print(f"note: {name} was renamed {name_now} while it was checked; the attestations "
                          f"are kept there", file=sys.stderr)
                written = chk.write_attestations(
                    a.root, name_now, report, codex, snapshot, key,
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
    from pathlib import Path

    from app import collections as collections_mod, layers as layers_mod, plugin_io
    from app.pack import build as pack_build, records as pack_records

    prepare = None
    if origin_dir:
        base = Path(origin_dir)
    else:
        # no local origin given: a Codex whose `origin.unit` is `index-page`
        # re-fetches each sampled record's MEMBER by Range into a temporary
        # base (app/acquire/member_origin.py; 02 §2.3; review §3.10); one of
        # a directory per document, its files by the plugin's own transport
        # (app/acquire/file_origin.py: a copy fetched as a SHARD)
        mo = _member_origin(codex)
        if mo is None:
            return None
        base, prepare = mo.base, mo.materialise
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
        if prepare is not None:
            prepare(locator)
        local, path = plugin_io.local_locator(plugin, locator, base)
        if not local.exists():
            raise FileNotFoundError(str(local))
        row = {"uid": uid, "native_id": uid.rsplit(":", 1)[-1], "path": path}
        record, _layer, _blob, _plates = pack_build._record_of(
            collection, plugin, row)
        return pack_records.canonical_line(
            record, record_line=int(getattr(codex, "record_line", 1) or 1))

    return reparse


_member_origins: dict = {}


def _member_origin(codex):
    """One network origin per Codex for a run — its members by Range
    (app/acquire/member_origin.py) or its documents' files
    (app/acquire/file_origin.py): `_reparse_from`, `_origin_unit_from` and
    `_weigh_from` share its listings and its temporary base."""
    from app.acquire import file_origin, member_origin
    key = getattr(codex, "collection_id", id(codex))
    if key not in _member_origins:
        _member_origins[key] = member_origin.for_codex(codex) or file_origin.for_codex(codex)
    return _member_origins[key]


def _weigh_from(codex, origin_dir):
    """`origin unit -> its size`, for a network origin that weighs a unit
    only when the check's bound reaches it (a listing per unit); None for a
    local origin, which `_origin_unit_from` weighs by `stat`."""
    if origin_dir:
        return None
    mo = _member_origin(codex)
    return getattr(mo, "weigh", None)


def _origin_unit_from(codex, name: str, origin_dir):
    """`origin locator -> (the origin file it points into, its size in bytes)`,
    for `records-vs-origin`'s bound (app/hold/check.py: `ORIGIN_BYTES`). With
    the origin as a local base the size is the file's; a file that is not
    there has none, and its records are reported as not there, as before."""
    if not origin_dir:
        mo = _member_origin(codex)
        return mo.unit if mo is not None else None
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
        # the FILES of every piece: wikipedia's page_props segment (parser 5)
        # puts a piece in nearly every locator, and a key with the members in
        # it would weigh each record again
        pieces = locator.split(seg) if seg and seg in locator else [locator]
        key = (seg or "").join(p.partition(plugin_io.DEFAULT_MEMBER_SEP)[0] for p in pieces)
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
        checks.append("records-vs-origin")
    if getattr(a, "seal", False):
        checks.append("origin-seal")
    a.checks = ",".join(checks)
    return cmd_check(a)


def cmd_records_seal(a) -> int:
    """`hold records seal <collection> <snapshot> --files DIR --trusted-list
    FILE --lotl FILE --lotl-signers FILE` (07 §7.3; 03 §3.7; 09 §9.15): the
    certifier's capture of the seals' evidence. The lists first: the LOTL
    verified by a certificate the Official Journal announced, the national
    list by one the LOTL's pointer for its territory names — a list is
    trusted for who signed it, not for its hash — both kept by their hash.
    Then one entry per sealed file under DIR, its revocation responses
    embedded, given (`--revocation`) or asked of each certificate's
    responder now (`--fetch-revocation`, on the budget), each entry
    validated in archival mode BEFORE the member is written — hard-fail on
    revocation when it carries any: a seal that does not validate is
    refused, never recorded."""
    import time
    from app.hold import revocation as rev, seals, tsl
    root = _collections_root(a)
    name, codex = col.resolve(root, a.collection)
    if codex.publication != "certified":
        raise CodexError(f"{name}: an open collection carries no seals (09 §9.15)")
    tl_bytes = Path(a.trusted_list).read_bytes()
    lotl_bytes = Path(a.lotl).read_bytes()
    try:
        lotl_said = tsl.verify_lotl(lotl_bytes, tsl.load_certificates(Path(a.lotl_signers).read_bytes()))
        tl_said = tsl.verify_against_lotl(tl_bytes, lotl_bytes)
    except tsl.XmlSigError as e:
        raise CodexError(f"seals not written — {e}") from None
    tl_digest, _ = seals.keep_trusted_list(root, name, tl_bytes)
    lotl_digest, _ = seals.keep_trusted_list(root, name, lotl_bytes)
    anchors = seals.anchors_from_trusted_list(tl_bytes)
    if not anchors:
        raise CodexError(f"{a.trusted_list}: the trusted list grants no CA; nothing could validate against it")
    given = []
    for f in a.revocation or []:
        raw = Path(f).read_bytes()
        e = rev.entry("ocsp", raw) or rev.entry("crl", raw)
        if e is None:
            raise CodexError(f"{f}: neither an OCSP response nor a CRL")
        given.append(e)
    files = sorted(p for p in Path(a.files).rglob("*") if p.is_file())
    if not files:
        raise CodexError(f"no sealed file under {a.files}")
    entries, refused = [], []
    fetched: dict[tuple, tuple] = {}
    now = time.time()
    for p in files:
        try:
            e = seals.capture(p, format=a.format, now=now, revocation=given,
                              trusted_list={"lotl_sha256": lotl_digest, "tl_sha256": tl_digest, "url": a.url or ""})
            if a.fetch_revocation:
                key = tuple(e["chain"])
                if key not in fetched:
                    fetched[key] = rev.fetch([seals._unb64(c, "chain") for c in e["chain"]], anchors)
                got, unanswered = fetched[key]
                if unanswered:
                    raise seals.SealError(f"no revocation responder answered for {', '.join(unanswered)[:300]}")
                e["revocation"] = rev.merge(e["revocation"], got)
            e["revocation"] = rev.relevant(e["revocation"], [seals._unb64(c, "chain") for c in e["chain"]])
            got = seals.validate(e, anchors=anchors)
        except seals.SealError as ex:
            refused.append(f"{p.name}: {ex}")
            continue
        if not got["ok"]:
            refused.append(f"{p.name}: {got['why']}")
            continue
        entries.append(e)
    if refused:
        raise CodexError("seals not written — " + "; ".join(refused)[:1500])
    path = seals.write(col.shard_dir_of(root, name), codex, a.snapshot, entries)
    _print({"collection_id": codex.collection_id, "snapshot_id": a.snapshot, "entries": len(entries),
            "trusted_list": tl_digest, "lotl": lotl_digest, "trusted_list_signed_by": tl_said["signer"],
            "lotl_signed_by": lotl_said["signer"],
            "revocation": sum(len(e["revocation"]) for e in entries), "path": str(path)})
    return 0


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
    """`hold embedder check` — the calibration gate of 03 §3.3.

    Exit 0 on a pass, 1 on a fail, 3 when the encoder was unreachable:
    03 §3.1's third outcome is INCONCLUSIVE, "neither a failure nor a
    pass", and 07 §7.7 reserves 3 for exactly that. A gate that exited 1
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
    print(f"  space       {run.space_id[:23]}…")
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
    # encoder configured read "deferred" for a SHARD that had lost its member
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
        passes: list = []
        try:
            body = run_check.validate(a.root, name, codex, recipe, role=role,
                                      on_run=passes.append)
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
        for run in passes:                  # all 512 against the reference: the
            print(f"  whole set   worst {min(run.all_cosines):.6f}, "      # cross-vendor table's row
                  f"median {__import__('statistics').median(run.all_cosines):.6f}, "
                  f"below 0.995: {sum(c < 0.995 for c in run.all_cosines)} of {len(run.all_cosines)}")
        print(f"  fingerprint {body['fingerprint']}")
        print(f"  batches     {'as one text at a time' if body['batch_invariant'] else 'DIFFER from one text at a time: validated for single-text work only'}")
        print(f"  benchmark   {body['tokens_per_s']} tokens/s")
        print(f"  statement   {hold_runs.path_for(a.root, name, body['run'], role)}")
        worst = max(worst, 0 if ok else 1)
    return worst


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
    """`hold embedder calibrate` (07 §7.3, 03 §3.3): the tolerance a space
    can bear, before `collection create`. Prints JSON; its `tolerance` is
    the recipe's `space.tolerance`. Exit 3 when an endpoint is unreachable
    (raised as CalibrateUnreachable), 2 for a file or set refused."""
    from app.hold import calibrate as calib
    if a.ctx <= 0:
        raise CodexError(f"--ctx is the space's input window in tokens, got {a.ctx}")
    reference = calib.read_endpoint(a.engine, weights=a.encoder_weights)
    against = [calib.read_endpoint(f) for f in a.against]
    if getattr(a, "pairs", None):
        if a.set_id:
            raise CodexError("--pairs names the set by its file; --set is the "
                             "platform's text set: one or the other")
        set_id, texts = calib.load_pairs(a.pairs)
    else:
        set_id, texts = calib.load_set(a.set_id)
    _print(calib.measure(reference, against, set_id=set_id, texts=texts,
                         embed=calib.http_embed(a.ctx)))
    return 0


def _gate(a):
    """Run the gate for a collection, resolving its encoder the way 07 §7.8
    resolves one: the collection's own endpoint, and nothing else — there
    is no default encoder (operator, 2026-09-18)."""
    from app import embeddings
    from app.hold import collection as col, shard as shard_mod, embedder as emb

    name, codex = col.resolve(a.root, a.collection)
    member = a.root / name / "shard" / \
        shard_mod.calibration_paths(codex.space.calibration.set)[0]
    if not member.is_file():
        raise CodexError(
            f"{name}: no calibration member at {member.name}. It is written "
            f"by `hold collection create` (09 §9.12) and is a member of every "
            f"snapshot; without it there is nothing to compare against")
    reference = _read_reference(member, codex)

    recipe = _recipe_for(codex, name)      # raises GateDeferred if unset
    from app.hold import calibration as cal
    # the set as the space's encoder is fed it: a pair space's pairs, from
    # its member (09 §9.12, amended 2026-10-08)
    items = cal.items(codex, member)

    def embed(texts):
        try:
            # the gate embeds the calibration set, in the space's form
            return embeddings.embed_calibration(recipe, texts)
        except emb.GateDeferred:
            raise
        except Exception as e:                      # noqa: BLE001
            # 03 §3.1: an unreachable encoder is INCONCLUSIVE, not a
            # failure — "it produces no attestation and no flag, it is
            # reported with its reason, and it is retried".
            raise emb.GateDeferred(
                f"the encoder for {name} was unreachable or failed: {e}") from e

    return emb.run_gate(codex, reference=reference, embed=embed, items=items), codex


def _read_reference(member, codex):
    from safetensors.numpy import load_file
    tensors = load_file(str(member))
    if "vectors" not in tensors:
        raise CodexError(f"{member.name}: no `vectors` tensor (09 §9.12)")
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


def cmd_embedder_rebaseline(a) -> int:
    """`hold embedder rebaseline <collection> [--yes]` (07 §7.3): for a
    Codex version 1 or 2 collection, re-record the backend gate's probe
    baseline (app/verify/backend_gate.py) — the operator's explicit
    acceptance of a CHANGED backend. Vectors embedded under the old one
    belong to another function, so accepting means planning their re-embed.
    What `backend rebaseline` did before app/cli.py went (2026-10-08).

    Exit 0 recorded, 1 a baseline exists and `--yes` was not given, 2 for a
    version-3 collection (held to a validated run, 03 §3.3 — `hold embedder
    check`; no baseline is taken for it) or one with no encoder, 3 when the
    encoder could not be reached (07 §7.7)."""
    from app import collections as collections_mod, run_check
    from app.hold import collection as col
    from app.verify import backend_gate
    name, codex = col.resolve(a.root, a.collection)
    collection = collections_mod.by_name(name)
    if collection is None:
        print(f"louise: {name}: no collection of that name on this node", file=sys.stderr)
        return 2
    if run_check.applies(collection):
        print(f"louise: {name} is a Codex version {codex.version} collection: its "
              f"writes are held to a validated run (03 §3.3), not to a probe "
              f"baseline — `louise hold embedder check {name}`", file=sys.stderr)
        return 2
    if getattr(collection, "embed", None) is None:
        print(f"louise: {name}: {collections_mod.NO_ENCODER}", file=sys.stderr)
        return 2
    path = backend_gate.baseline_path(collection)
    existed = path.exists()
    if existed and not a.yes:
        print("louise: a baseline already exists. Re-recording declares the CURRENT "
              "endpoint authoritative; vectors embedded under the old backend "
              "become foreign to every future query. Re-run with --yes if that "
              "is intended (and plan a re-embed).", file=sys.stderr)
        return 1
    try:
        baseline = backend_gate.record_baseline(collection)
    except backend_gate.BackendUnavailable as e:
        print(f"louise: deferred: {e}", file=sys.stderr)
        return 3
    backend_gate.reset_cache()
    print(f"{'re-recorded' if existed else 'recorded'} baseline "
          f"(dim {baseline['dim']}, {len(baseline['probes'])} probes) at {path}")
    return 0


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
    d = a.root / name / "shard" / "snapshots"
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        if f.name.endswith(".sig.json"):
            continue
        s = sn.load(f)
        print(f"{s.snapshot_id}  {s.hash}  {s.records:>9} records  "
              f"{len(s.shards):>5} shards")
    return 0


# ---- the old `python -m app.cli` commands, given their homes (step 12) -------
# Carried over from app/cli.py, never imported from it; that module is gone.

def cmd_snapshot_delta(a) -> int:
    """`hold snapshot delta <collection> <from> <to>` (07 §7.3): what changed
    between two held snapshots — their own deltas composed along the chain
    (app/hold/chain.py). A gap in the chain is exit 2. Read-only: it writes
    no delta file, since every snapshot already carries its own."""
    from app.hold import chain
    name, _ = col.resolve(a.root, a.collection)
    _print(chain.delta(a.root, name, a.from_snapshot, a.to_snapshot))
    return 0


def cmd_records_show(a) -> int:
    """`hold records show <collection> (<record-id> | --shard <slice>/<n>)
    [--raw]` (07 §7.3): one record of the current snapshot with the layer
    and chunker derived for it and its chunks (02 §2.3), or `--shard` the
    whole shard as stored, as JSON Lines (09 §9.5). Was `pack cat` / `pack
    dump`."""
    from app import layers as layers_mod
    from app.ask import figures
    from app.hold import shard as shard_mod, export as exp
    from app.pack import records as pack_records
    name, codex = col.resolve(a.root, a.collection)
    snapshot = _latest_snapshot(a.root, name)
    shard_dir = col.shard_dir_of(Path(a.root), name)
    if a.shard:
        entry = exp._shard_of(snapshot, a.shard)
        path = shard_dir / entry.path
        if not path.is_file():
            raise CodexError(f"{entry.path} is in snapshot {snapshot.snapshot_id} "
                             f"but not in this copy")
        sys.stdout.flush()
        n = pack_records.dump_jsonl(path, sys.stdout.buffer)
        sys.stdout.buffer.flush()
        print(f"{n} records", file=sys.stderr)
        return 0
    if not a.record_id:
        raise CodexError("name a record id (e.g. pubmed:38112034) or --shard <slice>/<n>")
    order = (getattr(codex, "slicing", None) or {}).get("order", "bytes")
    got = figures.record_of(shard_dir, snapshot, a.record_id, order)
    if got is None:
        print(f"louise: {a.record_id}: in no shard of snapshot "
              f"{snapshot.snapshot_id} this copy holds", file=sys.stderr)
        return 1
    record, shard = got
    if a.raw:
        _print(record)
        return 0
    row = {"uid": record["uid"], "native_id": record.get("native_id", 0)}
    i = layers_mod.layer_index(codex, row["native_id"], row)
    layer = layers_mod.layers_for(codex)[i]
    sections = [tuple(s) for s in record.get("sections") or []]
    _print({"snapshot": snapshot.snapshot_id,
            "shard": shard_mod.shard_path(shard.slice, shard.n),
            "layer": {"index": i, **layer.as_manifest()},
            "record": record,
            "chunks": layers_mod.chunks_for_document(codex, row, sections)})
    return 0


def cmd_index_build(a) -> int:
    """`hold index build <collection> [--text | --vector]` (07 §7.3): the
    vector table's indexes now — the book_id index every delete-then-insert
    write filters on, then the vector index queries probe (app/vector_index.py,
    as `index vector build` built them). The text index is the worker's fts
    stage's, shard by shard as documents land; nothing rebuilds it on
    demand, so `--text` says so (exit 2) rather than pretending."""
    from app import vector_index
    from app.hold import records as rec
    if not a.vector:
        raise CodexError("the text index is built by the node's fts stage as documents "
                         "land; there is no on-demand build. `hold index build "
                         f"{a.collection} --vector` builds the vector table's indexes")
    name, codex = col.resolve(a.root, a.collection)
    _take_write_lock(a.root, name, "hold index build")
    local = rec.local_collection(a.root, name, codex)
    if a.dry_run:
        _print({"collection": name, "would_build": ["book_id", "vector"],
                "stats": vector_index.stats(local)})
        return 0
    log = lambda *x: print(*x, file=sys.stderr)       # noqa: E731
    out = vector_index.build_scalar(local, log=log)
    if not out.get("built"):
        print(f"louise: book_id index not built: {out.get('reason')}", file=sys.stderr)
        return 1
    out = vector_index.build(local, index_type=a.type, partitions=a.partitions, log=log)
    if not out.get("built"):
        print(f"louise: vector index not built: {out.get('reason')}", file=sys.stderr)
        return 1
    _print({"collection": name, "vector_index": out})
    return 0


def cmd_index_show(a) -> int:
    """`hold index show <collection>`: the vector table's rows, its vector
    index and the rows not yet folded in, and whether the book_id index
    exists. Read-only; takes no lock (07 §7.7)."""
    from app import config, vector_index
    from app.hold import records as rec
    name, codex = col.resolve(a.root, a.collection)
    local = rec._local_collection(a.root, name, codex)
    st = vector_index.stats(local)
    out: dict = {"collection": name, "vector_table": st is not None}
    if st is not None:
        out.update(rows=st["rows"], vector_index=(
            {"name": st["name"], "type": st["type"], "unindexed_rows": st["unindexed_rows"]}
            if st["indexed"] else None),
            book_id_index=bool(st.get("book_id_indexed")),
            auto_build_at_rows=config.VECTOR_INDEX_MIN_ROWS)
        from app import embeddings, validity
        if validity.declared(local) is not None:
            # the first check of a versioned collection's table (2026-10-13:
            # a table written without them is withheld from every as-of search)
            names = embeddings._conn(local).open_table(embeddings.TABLE).schema.names
            out["validity_columns"] = set(embeddings.VALIDITY_COLUMNS) <= set(names)
    _print(out)
    return 0


def cmd_config_show(a) -> int:
    """`hold config show [<collection>]` (07 §7.8): the effective
    configuration, each value with its tier, from the resolution the node
    itself uses (app/effective_config.py)."""
    from app import effective_config
    _print(effective_config.show(a.collection))
    return 0


def cmd_config_apply(a) -> int:
    """`hold config apply [<collection>]` (07 §7.8): the files applied now,
    as the node applies them at start — a runtime embed-mode override gives
    way to the file's value. Exit 1 when a named file is refused (an
    `[encoder]` it does not name: what `embed materialize` reported)."""
    from app import effective_config
    out = effective_config.apply(a.collection)
    _print(out)
    return 1 if out["refused"] else 0


def _node_collections(name: str | None) -> list:
    from app import collections as collections_mod
    if name:
        c = collections_mod.by_name(name)
        if c is None:
            raise CodexError(f"{name}: no such collection on this node")
        return [c]
    got = collections_mod.load_all(skip_unreadable=True)
    return got[0] if isinstance(got, tuple) else got


def cmd_records_fetch(a) -> int:
    """`hold records fetch <collection> [--profile P] [--dry-run]` (07 §7.3):
    the acquisition spec run for a profile — the file's, unless one is
    named (app/hold/records_fetch.py). Was `corpus add` / `corpus grow`."""
    from app import db
    from app.hold import records_fetch
    collection = _node_collections(a.collection)[0]
    profile = a.profile or collection.profile or "demo"
    with _closing(db.connect(collection)) as con:
        return records_fetch.fetch(collection, con, profile, dry_run=a.dry_run)


def cmd_node_health(a) -> int:
    """`node health [<collection>] [--errors]`: every stage's state, and
    exit 1 when a stage has work it cannot start — a CHECK, not only a page
    (app/health.py). A stage with nothing to do exits 0 whether or not it is
    switched off: "off and finished" is not a problem. Was `health`."""
    from app import health
    reports, blocking = health.check(_node_collections(a.collection), errors=a.errors)
    print(health.render(reports), end="")
    if not blocking:
        return 0
    print("\nBLOCKED:", file=sys.stderr)
    for line in blocking:
        print(f"  {line}", file=sys.stderr)
    return 1


def cmd_dev_genesis_recheck(a) -> int:
    """`dev genesis recheck <collection>`: reopen FINAL archive shards that
    still have documents the archive could serve (genesis.reopen_finals_
    with_unserved). Writes; one indexed range count per final shard, which
    is why it is a command and not a round."""
    from app import db
    from app.bundles import genesis
    collection = _node_collections(a.collection)[0]
    with _closing(db.connect(collection)) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            found = genesis.reopen_finals_with_unserved(con, collection)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    if not found:
        print("every final shard has served every document in its range")
        return 0
    for shard, n in sorted(found.items(), key=lambda kv: -kv[1]):
        print(f"shard {shard}: {n:,} unserved -> reopened")
    print(f"{len(found)} shard(s) reopened, {sum(found.values()):,} documents; "
          f"the fetcher re-fetches them and the worker imports their vectors")
    return 0


def cmd_dev_genesis_recount(a) -> int:
    """`dev genesis recount <collection> [--yes]`: the archive ledger's
    docs/chunks recomputed from chunk_verification (genesis.recount_ledger).
    A dry run that measures the walk unless --yes; with --yes, stop the
    worker first. Exit 2 when rows differ and nothing was written."""
    import time
    from app import db
    from app.bundles import genesis
    collection = _node_collections(a.collection)[0]
    with _closing(db.connect(collection)) as con:
        t0 = time.monotonic()
        out = genesis.recount_ledger(con, write=a.yes)
    print(f"walked chunk_verification in {time.monotonic() - t0:.1f} s")
    changed = [rc for rc in out if rc.changed]
    if not changed:
        print(f"{len(out)} ledger row(s) already match chunk_verification")
        return 0
    for rc in changed:
        print(f"{rc.archive}/{rc.shard}: docs {rc.docs:,} -> {rc.held_docs:,}, "
              f"chunks {rc.chunks:,} -> {rc.held_chunks:,}")
    if not a.yes:
        print(f"{len(changed)} row(s) differ; nothing written. Re-run with --yes "
              f"(worker stopped) to set them.")
        return 2
    print(f"{len(changed)} row(s) set from chunk_verification")
    return 0


def cmd_vectors_archive_retry(a) -> int:
    """`hold vectors import <collection> --archive ID --retry` (07 §7.3):
    the archive's REJECTED shards put back in line — state `fetched`, the
    verdict cleared — for the worker to retry on real evidence
    (genesis.retry_rejected). Was `genesis retry`."""
    from app import db
    from app.bundles import genesis
    from app.hold import records as rec
    name, codex = col.resolve(a.root, a.collection)
    _take_write_lock(a.root, name, "hold vectors import --retry")
    local = rec.local_collection(a.root, name, codex)
    reset: list = []
    with _closing(db.connect(local)) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            for shard in (a.shards or [None]):
                reset += genesis.retry_rejected(con, archive_id=a.archive, shard=shard)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    if not reset:
        print(f"{a.archive}: nothing rejected"
              + (f" among shards {', '.join(a.shards)}" if a.shards else ""))
        return 0
    for archive_id, shard in reset:
        print(f"{archive_id}/{shard}: rejected -> fetched (the worker retries it)")
    return 0


def cmd_user(a) -> int:
    """`louise user add | list | role | expire | reissue | revoke | remove`
    (07 §7.3, amended 2026-10-22): the users of app/users.py. A token is
    printed by `add` and `reissue` only, once; nothing else ever shows it."""
    from app import users
    try:
        if a.verb == "add":
            u, token = users.add(a.name, role=a.role, expires=a.expires)
            print(f"{u.name} ({u.role}), expires {u.expires or 'never'}. Token — shown once, give it to them:")
            print(token)
        elif a.verb == "reissue":
            u, token = users.reissue(a.name)
            print(f"{u.name}: a new token; the old one no longer works. Shown once:")
            print(token)
        elif a.verb == "list":
            rows = users.listing()
            if getattr(a, "json", False):
                print(json.dumps(rows, indent=2))
            elif not rows:
                print("no users: Ask is open to anyone on the network (`louise user add <name>` closes it)")
            else:
                for r in rows:
                    print(f"{r['name']:<20} {r['role']:<6} {r['state']:<8} expires {r['expires'] or 'never':<21} "
                          f"last seen {r['last_seen'] or '—'}")
        elif a.verb == "role":
            u = users.set_role(a.name, a.role)
            print(f"{u.name}: role {u.role}")
        elif a.verb == "expire":
            u = users.set_expiry(a.name, a.expires)
            print(f"{u.name}: expires {u.expires or 'never'}")
        elif a.verb == "revoke":
            u = users.revoke(a.name)
            print(f"{u.name}: revoked at {u.revoked}; `louise user reissue {u.name}` restores it")
        elif a.verb == "remove":
            users.remove(a.name)
            print(f"{a.name}: removed")
    except users.UserError as e:
        print(f"louise user {a.verb}: {e}", file=sys.stderr)
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="louise", description="Louise — knowledge, kept.")
    surfaces = top.add_subparsers(dest="surface", required=True, metavar="hold|share|node|user")
    node = surfaces.add_parser("node", help="the node's mode, or one collection paused (07 §7.3, §7.7)")
    nverbs = node.add_subparsers(dest="verb", required=True)
    p = nverbs.add_parser("start", help="the daemon: the worker's rounds and the web interface, until stopped")
    p.add_argument("--acquire-only", action="store_true", dest="acquire_only")
    p.add_argument("--seed-only", action="store_true", dest="seed_only")
    p.add_argument("--only", choices=("worker", "web"), default=None,
                   help="run one of the two (a deployment with a container each)")
    p.set_defaults(fn=cmd_node_start)
    p = nverbs.add_parser("stop", help="the worker finishes its current units, commits and exits 0")
    p.set_defaults(fn=cmd_node_stop)
    p = nverbs.add_parser("pause", help="the node: nothing, seeding only, or download and seeding only, "
                                       "until resume; or ONE collection: its write lock released at "
                                       "the end of the current unit, nothing run for it until resume")
    p.add_argument("collection", nargs="?", default=None)
    p.add_argument("--acquire-only", action="store_true", dest="acquire_only")
    p.add_argument("--seed-only", action="store_true", dest="seed_only")
    p.set_defaults(fn=cmd_node_pause)
    p = nverbs.add_parser("show", help="state per collection, the current stage, the last round's cost, "
                                      "the overrides in force, the verbs running under the node")
    p.set_defaults(fn=cmd_node_show)
    p = nverbs.add_parser("resume", help="the node, or one collection")
    p.add_argument("collection", nargs="?", default=None)
    p.set_defaults(fn=cmd_node_resume)
    p = nverbs.add_parser("health", help="every stage's state per collection; exit 1 when a stage has "
                                        "work it cannot start")
    p.add_argument("collection", nargs="?", default=None)
    p.add_argument("--errors", action="store_true", help="also count errored documents (43 s on a 40M manifest: `error` is in "
                        "no index, so proving there are none walks the whole scope range)")
    p.set_defaults(fn=cmd_node_health)

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
                   help="TOML with every Codex field the plugin does not default (06 §6.6); "
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
    _encoder_key_args(p)
    p.add_argument("--calibration-set", default=None, metavar="FILE",
                   help="a PAIR space's calibration set: the 512 title-and-abstract pairs "
                        "tools/build_pair_calibration_set.py drew from the collection's "
                        "records, whose sha256 the recipe names as space.calibration.set "
                        "(09 §9.12); the member carries it")
    p.add_argument("--calibration-from", default=None, metavar="NAME",
                   help="a held collection whose document-side reference vectors and "
                        "envelope the new Codex reuses (RULE 1): its space must be the "
                        "recipe's but for the query side, the document engine included; "
                        "only the query side is embedded")
    # `create annotations` (02 §2.10): claims about another collection's records
    p.add_argument("--annotates", default=None, metavar="COLLECTION", action="append",
                   help="create annotations: the collection the claims are about (id, prefix or name); "
                        "repeat for several targets (02 §2.10, amended 2026-10-20), each with its "
                        "own --from-snapshot in the same order")
    p.add_argument("--from-snapshot", default=None, dest="from_snapshot", metavar="ID", action="append",
                   help="create annotations: the target snapshot the claims are checked against "
                        "and hold from (the Codex's annotates.from_snapshot); one per --annotates")
    p.add_argument("--container", default=None, choices=("jsonl", "parquet"),
                   help="create annotations: the record parts' container (09 §9.5, 2026-10-20); "
                        "jsonl unless said, and parquet is declared but not yet written")
    p.add_argument("--claims", default=None, metavar="FILE",
                   help="create annotations: JSON Lines, one claim per line (09 §9.5, amended "
                        "2026-10-09)")
    p.add_argument("--title", default=None, help="create annotations: the Codex's title")
    p.add_argument("--summary", default=None, help="create annotations: the Codex's summary")
    p.add_argument("--annotator", default=None,
                   help="create annotations: whom the Codex credits (default: the claims' one "
                        "contributor)")
    p.add_argument("--snapshot", default=None, metavar="ID",
                   help="create annotations: this collection's first snapshot id (default: the "
                        "period that closed last, by its monthly cadence)")
    p.add_argument("--dry-run", action="store_true",
                   help="create annotations: check the claims against the target snapshot and print the "
                        "counts; write nothing")
    p.set_defaults(fn=cmd_create)

    p = verbs.add_parser("derive", help="a sister: her mother's corpus, a new space")
    root_arg(p); p.add_argument("codex_id")
    p.add_argument("--space-from", help="a Codex whose space and layers to copy byte for byte")
    p.add_argument("--space", help="JSON file with the space block")
    p.add_argument("--layers", help="JSON file with the layers list")
    p.add_argument("--update", help="JSON file: cadence, day, grace_days")
    p.add_argument("--work", help="JSON file: tokens, per")
    p.add_argument("--archives", nargs="*", default=None,
                   help="archive ids a sister in a new space draws on")
    p.add_argument("--encoder", default=None, metavar="URL",
                   help="this node's endpoint for her encoder: a sister in a new space embeds "
                        "her own reference there, and her file names it (default: the "
                        "[encoder] of the collection whose space she is in)")
    p.add_argument("--query-encoder", default=None, metavar="URL",
                   help="a bi-encoder's query side: its endpoint (09 §9.12)")
    _encoder_key_args(p)
    p.add_argument("--calibration-set", default=None, metavar="FILE",
                   help="a PAIR space's calibration set, when she embeds a reference of her "
                        "own in one (09 §9.12)")
    p.add_argument("--name", default=None, help="her local name (default: the plugin's, 07 §7.8)")
    p.add_argument("--accept", action="store_true", required=True,
                   help="creating a sister is an acceptance (02 §2.7)")
    p.set_defaults(fn=cmd_derive)

    p = verbs.add_parser("continue", help="a daughter: a new toolchain, the same space")
    root_arg(p); p.add_argument("codex_id")
    p.add_argument("--platform-image", required=True)
    p.add_argument("--through", "--continues-through", dest="continues_through", required=True,
                   help="the mother's LAST snapshot — the boundary, named from "
                        "the side that already exists")
    p.add_argument("--origin", help="JSON file with the new origin block")
    p.add_argument("--origin-reachable-until")
    p.add_argument("--reslice", action="store_true",
                   help="the slicing a creation would get now (wikidata: the family map of the "
                        "code's families) — every record and vector reused (A1g)")
    p.set_defaults(fn=cmd_continue)

    p = verbs.add_parser("replace", help="a corrective sister, with its reproducible defect")
    root_arg(p); p.add_argument("codex_id")
    p.add_argument("--defect", required=True,
                   help="JSON file: check, toolchain, witnesses, expected, observed, rationale")
    p.add_argument("--platform-image")
    p.add_argument("--space", help="JSON file with the space block")
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

    p = verbs.add_parser("rename",
                         help="the same collection under another local name: its directory, "
                              "shared folders, stored paths, pause and held file (07 §7.3)")
    root_arg(p); p.add_argument("collection"); p.add_argument("new_name", metavar="new-name")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="name what would move; move nothing")
    p.set_defaults(fn=cmd_collection_rename)

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

    anns = nouns.add_parser("annotations", help="an annotations collection's claims (02 §2.10)")
    averbs = anns.add_subparsers(dest="verb", required=True)
    p = averbs.add_parser("update", help="the next snapshot of the SAME collection: the claims re-checked "
                                         "against the target's newer snapshot, or replaced by a new file")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--claims", metavar="FILE", default=None,
                   help="the whole claim set from now on (JSON Lines): a claim made before keeps its uid, a new "
                        "one takes the next, one left out is tombstoned. Without it the claims held are re-checked")
    p.add_argument("--against", action="append", metavar="TARGET=SNAPSHOT", default=None,
                   help="the target snapshot the claims are checked against (default: the newest held); "
                        "once per target")
    p.add_argument("--snapshot", default=None,
                   help="this snapshot's id (default: the period after the previous one, written during it)")
    p.add_argument("--dry-run", action="store_true",
                   help="check and count what would change; write nothing")
    p.set_defaults(fn=cmd_annotations_update)

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
        p.add_argument("--records-from", dest="records_from", metavar="COLLECTION",
                       help="take the record lines of this held collection's latest snapshot "
                            "instead of parsing — a daughter or re-creation that changes only "
                            "the slicing (RULE 1)")

    p = rverbs.add_parser("build", help="parse, slice, shard, hash")
    args_records_build(p); p.set_defaults(fn=cmd_records_build)
    p = rverbs.add_parser("fetch", help="run the acquisition spec for a profile (06 §6.2): "
                                        "the origin's files, on the node's budget")
    p.add_argument("collection")
    p.add_argument("--profile", default=None,
                   help="a profile the acquisition spec declares (default: the "
                        "collection file's)")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="print the plan — files, bytes, the licence state — and fetch nothing")
    p.set_defaults(fn=cmd_records_fetch)

    p = rverbs.add_parser("validation", help="write validation.json from the Codex")
    root_arg(p); p.add_argument("collection")
    p.set_defaults(fn=cmd_records_validation)

    p = rverbs.add_parser("seal", help="the certifier captures the seals' evidence into seals/<snapshot>.json "
                                       "(03 §3.7; 09 §9.15), each seal validated offline first")
    root_arg(p); p.add_argument("collection"); p.add_argument("snapshot")
    p.add_argument("--files", required=True, metavar="DIR", help="the sealed origin files (.p7m): every file under it")
    p.add_argument("--trusted-list", required=True, dest="trusted_list", metavar="FILE",
                   help="the national trusted list (ETSI TS 119 612 XML) the chains validate against; kept by its hash")
    p.add_argument("--lotl", required=True, metavar="FILE",
                   help="the EU list of trusted lists: the national list must be signed by a key its pointer names")
    p.add_argument("--lotl-signers", required=True, dest="lotl_signers", metavar="FILE",
                   help="the certificates the Official Journal announces for signing the LOTL (PEM or DER)")
    p.add_argument("--url", help="where the trusted list is published")
    p.add_argument("--format", default="cades", choices=["cades", "pades", "xades"],
                   help="the envelope: a CMS .p7m, the signature inside a PDF, or an XML signature")
    p.add_argument("--revocation", action="append", metavar="FILE",
                   help="an OCSP response or CRL (DER) obtained for the chain; repeatable")
    p.add_argument("--fetch-revocation", action="store_true", dest="fetch_revocation",
                   help="ask each certificate's OCSP responder, else its CRL point, now (on the budget)")
    p.set_defaults(fn=cmd_records_seal)

    p = rverbs.add_parser("check",
                          help="`hold check --checks roots,records"
                               "[,records-vs-origin][,origin-seal]` and nothing else")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--seal", action="store_true",
                   help="adds `origin-seal`: the seals member validated offline against the trusted list held")
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
    p.add_argument("--live", metavar="DIR",
                   help="with --seal: live mode — the sealed files under DIR re-validated now (03 §3.7)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_records_check)

    p = rverbs.add_parser("show", help="one record of the current snapshot with its layer and chunks, "
                                       "or a whole shard as JSON Lines")
    root_arg(p); p.add_argument("collection")
    p.add_argument("record_id", nargs="?", default=None, help="a uid, e.g. pubmed:38112034")
    p.add_argument("--shard", default=None, metavar="SLICE/N", help="the whole shard, as stored (09 §9.5)")
    p.add_argument("--raw", action="store_true", help="the record without its chunks")
    p.set_defaults(fn=cmd_records_show)

    # ---- hold config ----------------------------------------------------
    iss = nouns.add_parser("issues", help="the origin's inconsistencies the parses met (06 §6.1)")
    isverbs = iss.add_subparsers(dest="verb", required=True)
    p = isverbs.add_parser("list", help="each document's flagged origin inconsistencies, by uid, "
                                        "with the counts per code")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--code", default=None, help="only this code's documents")
    p.add_argument("--limit", type=int, default=None, help="at most N documents' issues")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_issues_list)

    cfg = nouns.add_parser("config", help="the effective configuration, each value with its tier (07 §7.8)")
    cverbs = cfg.add_subparsers(dest="verb", required=True)
    p = cverbs.add_parser("show", help="each collection's effective values and the tier each comes from")
    p.add_argument("collection", nargs="?", default=None)
    p.set_defaults(fn=cmd_config_show)
    p = cverbs.add_parser("apply", help="apply the files now, as the node does at start: a runtime "
                                        "override gives way to the file's value")
    p.add_argument("collection", nargs="?", default=None)
    p.set_defaults(fn=cmd_config_apply)

    # ---- hold snapshot --------------------------------------------------
    snaps = nouns.add_parser("snapshot", help="a collection's records at one cutoff")
    sverbs = snaps.add_subparsers(dest="verb", required=True)

    def args_snapshot_create(p):
        root_arg(p); p.add_argument("collection")
        p.add_argument("--snapshot", dest="snapshot_id",
                       help="the id; by default the period that closed, "
                            "from the Codex's clock")
        p.add_argument("--shards-from", dest="shards_from", default=None,
                       metavar="FROM[:SNAPSHOT]",
                       help="seed the parts from another collection's byte-identical ones "
                            "(its latest snapshot, or the one named), linked and checked; "
                            "refused unless every record-deciding field agrees. The "
                            "snapshot is then the source's period unless --snapshot names one")

    def args_collection(p):
        root_arg(p); p.add_argument("collection")

    p = sverbs.add_parser("create",
                          help="freeze the current records as this period's snapshot")
    args_snapshot_create(p); p.set_defaults(fn=cmd_snapshot_create)

    p = sverbs.add_parser("list", help="the chain this peer holds")
    args_collection(p); p.set_defaults(fn=cmd_snapshot_list)

    p = sverbs.add_parser("delta", help="what changed between two held snapshots of the chain")
    args_collection(p)
    p.add_argument("from_snapshot", metavar="from"); p.add_argument("to_snapshot", metavar="to")
    p.set_defaults(fn=cmd_snapshot_delta)

    # ---- hold index -----------------------------------------------------
    idx = nouns.add_parser("index", help="the vector table's indexes")
    iverbs = idx.add_subparsers(dest="verb", required=True)
    p = iverbs.add_parser("build", help="the book_id and vector indexes of the vector table, now")
    args_collection(p)
    which = p.add_mutually_exclusive_group()
    which.add_argument("--text", action="store_true",
                       help="refused: the node's fts stage builds the text index")
    which.add_argument("--vector", action="store_true", help="the vector table's indexes")
    p.add_argument("--type", default=None, help="the vector index type (default: VECTOR_INDEX_TYPE)")
    p.add_argument("--partitions", type=int, default=None,
                   help="IVF partitions (default: from the row count)")
    p.add_argument("--dry-run", action="store_true", dest="dry_run")
    p.set_defaults(fn=cmd_index_build)
    p = iverbs.add_parser("show", help="rows, the vector index and what it has not folded in, "
                                       "the book_id index")
    args_collection(p); p.set_defaults(fn=cmd_index_show)

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
    # no --root: it runs before any collection exists (03 §3.3)
    p.add_argument("--encoder-weights", required=True,
                   help="the reference encoder's weights digest (09 §9.3)")
    p.add_argument("--engine", required=True, metavar="FILE",
                   help="the reference engine: url, [engine] (09 §9.3)")
    p.add_argument("--against", nargs="+", default=[], metavar="FILE",
                   help="another build: url, weights, [engine]")
    p.add_argument("--ctx", type=int, required=True,
                   help="the space's input window in tokens; texts are cut as the gate cuts them")
    p.add_argument("--set", dest="set_id", metavar="SHA256",
                   help="the calibration set (default: the platform's text set)")
    p.add_argument("--pairs", default=None, metavar="FILE",
                   help="a PAIR space: its calibration set, the pairs "
                        "tools/build_pair_calibration_set.py drew, embedded as pairs "
                        "(09 §9.12); not with --set")
    p.set_defaults(fn=cmd_embedder_calibrate)

    p = everbs.add_parser("run", help="the run an endpoint states now — engine, weights, driver, "
                                      "run id — as JSON, for writing a recipe")
    p.add_argument("--encoder", required=True, metavar="URL")
    p.add_argument("--api", default=None, choices=("openai", "ollama", "vega"))
    p.add_argument("--model", default=None, help="the model asked (required where the URL names none)")
    p.add_argument("--query", action="store_true", help="the query side's run")
    p.add_argument("--pair", action="store_true", help="probe in the pair form (09 §9.3)")
    p.set_defaults(fn=cmd_embedder_run)

    p = everbs.add_parser("tree-diff",
                          help="what differs between two backend trees — the per-file lists "
                               "two engine images are the digests of, read from VeGa or from "
                               "files — path by path (vega.md §12)")
    p.add_argument("before", help="an engine image digest (read from VEGA_URL) or a JSON file")
    p.add_argument("after", help="the other, the same way")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="the whole diff as JSON, entries included, instead of one line a path")
    p.set_defaults(fn=cmd_embedder_tree_diff)

    p = everbs.add_parser("rebaseline",
                          help="accept a changed backend: re-record the probe baseline "
                               "(Codex version 1 or 2 only)")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--yes", action="store_true",
                   help="replace an existing baseline (plan a re-embed)")
    p.set_defaults(fn=cmd_embedder_rebaseline)

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
    p.add_argument("--retry", action="store_true",
                   help="with --archive: put the archive's rejected shards (or "
                        "those of --shards) back in line for the worker; "
                        "imports nothing now")
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
    p = vverbs.add_parser("validity-columns",
                          help="add the validity columns a versioned collection's "
                               "vector table was written without: rebuilt in place, "
                               "every vector kept; then `hold index build --vector`")
    root_arg(p); p.add_argument("collection")
    p.set_defaults(fn=cmd_vectors_validity_columns)
    p = vverbs.add_parser("reembed",
                          help="every vector made again by the encoder that serves the "
                               "collection now: documents marked, the embed stage replaces "
                               "them; a versioned collection's shared texts once each")
    root_arg(p); p.add_argument("collection")
    p.set_defaults(fn=cmd_vectors_reembed)

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
    p.add_argument("--against", metavar="FILE",
                   help="`image-equivalence` (03 §3.4): the reference image's re-parse of a "
                        "sample, written by tools/reparse_lines.py under the Codex's own image")
    p.add_argument("--shards", metavar="SLICE/NNNNN,…",
                   help="run every check on these shards alone, in place of its sample — how a "
                        "received claim is reproduced here (03 §3.6)")
    p.add_argument("--live", metavar="DIR",
                   help="`origin-seal` in live mode (03 §3.7): the sealed files under DIR re-validated now "
                        "against the current trusted list and fresh revocation responses")
    p.add_argument("--json", action="store_true",
                   help="09 §9.8's report object")
    p.set_defaults(fn=cmd_check)

    user = surfaces.add_parser("user", help="who may search: tokens, roles, expiry (07 §7.4; app/users.py)")
    uverbs = user.add_subparsers(dest="verb", required=True)
    p = uverbs.add_parser("add", help="a new user; its token is printed once")
    p.add_argument("name")
    p.add_argument("--role", default="user", choices=("user", "admin"))
    p.add_argument("--expires", default=None, help="never (default), 90d, 2027-03-31 or an ISO time")
    p.set_defaults(fn=cmd_user)
    p = uverbs.add_parser("list", help="name, role, state, expiry, last seen; never a token")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_user)
    p = uverbs.add_parser("role", help="change a user's role")
    p.add_argument("name")
    p.add_argument("role", choices=("user", "admin"))
    p.set_defaults(fn=cmd_user)
    p = uverbs.add_parser("expire", help="set or clear the expiry: a date, 90d, or never")
    p.add_argument("name")
    p.add_argument("expires")
    p.set_defaults(fn=cmd_user)
    for verb, what in (("reissue", "a new token, printed once; the old one stops working now"),
                       ("revoke", "the token stops working; the entry stays (reissue restores it)"),
                       ("remove", "the entry deleted")):
        p = uverbs.add_parser(verb, help=what)
        p.add_argument("name")
        p.set_defaults(fn=cmd_user)

    dev = surfaces.add_parser("dev", help="operator instruments; read-only unless the line says otherwise (07 §7.3)")
    dverbs = dev.add_subparsers(dest="verb", required=True)
    for verb, what in (("schedule-report", "what the worker spent its rounds on (tools/sched_report.py)"),
                       ("embedder-profile", "the concurrency an endpoint sustains (tools/profile_embed.py)"),
                       ("refs-sync", "reference tables from their origins; writes (tools/sync_refs.py)"),
                       ("wikidata-classes", "why the family map gives a class its family, and what candidate "
                                            "roots would move (tools/wikidata_classes.py)")):
        p = dverbs.add_parser(verb, help=what, add_help=False)
        p.add_argument("rest", nargs=argparse.REMAINDER, help="the instrument's own flags")
        p.set_defaults(fn=cmd_dev_tool)
    p = dverbs.add_parser("adopt-bundles", help="an existing collection's state/bundles moved under BUNDLES_ROOT "
                                                "and linked, as a new collection's is; writes (07 §7.8)")
    root_arg(p); p.add_argument("collection")
    p.set_defaults(fn=cmd_dev_adopt_bundles)
    p = dverbs.add_parser("mark-embedded", help="documents whose every chunk has a vector here and whose "
                                                 "manifest row says embed_done = 0, marked (writes)")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--dry-run", action="store_true", help="count them and mark nothing")
    p.set_defaults(fn=cmd_dev_mark_embedded)
    p = dverbs.add_parser("deprecate-duplicates", help="this peer's bundles that duplicate another of its own for a "
                                                       "part, deprecated (03 §3.6; 09 §9.8): served one more day, "
                                                       "never offered for a new fetch; writes, asks first")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--yes", action="store_true", help="no question (07 §7.7: required without a terminal)")
    p.add_argument("--dry-run", action="store_true", help="list the pairs and deprecate nothing")
    p.set_defaults(fn=cmd_dev_deprecate_duplicates)
    p = dverbs.add_parser("withdraw-covered", help="this peer's bundles whose every record another bundle held "
                                                   "here covers (a predecessor's, another producer's), withdrawn "
                                                   "(03 §3.6; 09 §9.8); writes, asks first")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--yes", action="store_true", help="no question (07 §7.7: required without a terminal)")
    p.add_argument("--dry-run", action="store_true", help="count them and withdraw nothing")
    p.set_defaults(fn=cmd_dev_withdraw_covered)
    p = dverbs.add_parser("migrate", help="schema changes to a state database; writes; never runs on its own")
    mverbs = p.add_subparsers(dest="what", required=True)
    p = mverbs.add_parser("schema", help="apply pending schema changes to a collection's state database")
    p.add_argument("collection", nargs="?", default=None)
    p.add_argument("--dry-run", action="store_true", dest="dry_run", help="report the changes, write nothing")
    p.add_argument("--no-backup", action="store_true", dest="no_backup", help="no .bak copy of the file first")
    p.set_defaults(fn=cmd_dev_migrate_schema)
    p = dverbs.add_parser("drop-rows", help="the rows a file lists dropped and their documents "
                                            "embedded again (02 §2.4's remedy); writes only with "
                                            "--yes (the collection's lock)")
    root_arg(p)
    p.add_argument("collection")
    p.add_argument("--ids", required=True, metavar="FILE", help="one chunk id a line")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_dev_drop_rows)
    p = dverbs.add_parser("reindex", help="the documents a file lists queued for the text-index stage "
                                          "again (fts_done = 0), embedding untouched; writes only with "
                                          "--yes (the collection's lock)")
    root_arg(p)
    p.add_argument("collection")
    p.add_argument("--ids", required=True, metavar="FILE", help="one uid a line")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_dev_reindex)
    p = dverbs.add_parser("unindexable", help="a document the plugin now refuses: out of every index "
                                              "and its error written at once; writes only with --yes")
    root_arg(p); p.add_argument("collection"); p.add_argument("uid")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_dev_unindexable)
    p = dverbs.add_parser("carry", help="a collection's manifest, text index and vector table into a new "
                                        "collection with the same records (amendment A1); writes "
                                        "only with --yes (both collections' locks)")
    root_arg(p)
    p.add_argument("source", metavar="from"); p.add_argument("target", metavar="to")
    p.add_argument("--copy", action="store_true",
                   help="copy (a reflink where the filesystem has one) instead of moving")
    p.add_argument("--keep", default=None, metavar="producer:ARCHIVE",
                   help="then keep only the vectors that archive produced; the rest are "
                        "dropped and their documents embedded again")
    p.add_argument("--rebuilt", action="store_true",
                   help="the target's records are built, not seeded: only its parser and image "
                        "differ (A1c); then `--reconcile` after its build and snapshot")
    p.add_argument("--recut", action="store_true",
                   help="into a space that differs only by cutting by tokens (A1e): then drop "
                        "the rows the character cut shortened, not an archive's, and embed "
                        "their documents again")
    p.add_argument("--resliced", action="store_true",
                   help="into a collection that differs only by its slicing, its parts cut by "
                        "content (A1g: a family map added): every vector carried; then "
                        "`hold records build <to> --records-from <from>`")
    p.add_argument("--predecessor-rows", action="store_true", dest="predecessor_rows",
                   help="after a same-space carry made before provenance was kept: the "
                        "source's coverage rows copied unkeyed and its bundle tree linked "
                        "(contribute.claimed_uids reads them; never this collection's coverage)")
    p.add_argument("--reconcile", action="store_true",
                   help="after `--rebuilt`, the build and the snapshot: keep the vectors of "
                        "every document whose content_hash did not move, embed the rest again")
    p.add_argument("--resign", action="store_true",
                   help="after `--rebuilt`, where the source holds no records: re-sign every "
                        "document by the target's discovery and prove a sample's chunks "
                        "unchanged (A1d)")
    p.add_argument("--scan", action="store_true",
                   help="with --keep: one pass over the vector table instead of a walk by "
                        "document — for a table too large to visit document by document")
    p.add_argument("--yes", action="store_true", help="do it; without it, the checks and the plan")
    p.set_defaults(fn=cmd_dev_carry)
    p = dverbs.add_parser("genesis", help="one-shot repairs of an archive's ledger; writes")
    gverbs = p.add_subparsers(dest="what", required=True)
    p = gverbs.add_parser("recheck", help="reopen final shards that still have documents the archive "
                                          "could serve")
    p.add_argument("collection")
    p.set_defaults(fn=cmd_dev_genesis_recheck)
    p = gverbs.add_parser("recount", help="the ledger's docs and chunks from chunk_verification; a dry "
                                          "run unless --yes (worker stopped)")
    p.add_argument("collection")
    p.add_argument("--yes", action="store_true", help="write the recounted rows")
    p.set_defaults(fn=cmd_dev_genesis_recount)

    share = surfaces.add_parser("share", help="bytes leaving here, and the announcement that they exist (04)")
    shverbs = share.add_subparsers(dest="verb", required=True)
    p = shverbs.add_parser("collection", aliases=["serve"],
                           help="serve what you hold — and announce it; --stop to stop")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--slices", default=None)
    p.add_argument("--kinds", default=None)
    p.add_argument("--stop", action="store_true")
    p.set_defaults(fn=cmd_share)
    p = shverbs.add_parser("fetch", help="take a part: find who holds it, pull and verify, hold it (04 §4.3)")
    root_arg(p); p.add_argument("collection", help="a collection id or a lineage id")
    p.add_argument("--slice", nargs="*", default=None,
                   help="the slices to hold, as the snapshot names them (lang=la); absent: every slice")
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
    p.add_argument("--embed", default=None, choices=("off", "on-demand", "all", "p2p"),
                   help="what this node's encoder spends on it (07 §7.8), into the file: off (default; "
                        "take peers' vectors only), on-demand, all, p2p (its share of the parts, "
                        "published; needs sharing, which a public collection has)")
    _encoder_key_args(p)
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
    p.add_argument("--republish", action="store_true",
                   help="no check: publish this key's held attestations that were never published (04 §4.2)")
    p.set_defaults(fn=cmd_share_attest)
    p = shverbs.add_parser("offer", help="the collections neighbours announce that this node does not hold, "
                                         "and who serves each (04 §4.2); `fetch` takes one")
    p.set_defaults(fn=cmd_share_offer)
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
    p.add_argument("--freshness", action="store_true", help="ask the origin for the current version of the top five of each list (05 §5.3)")
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
    p.add_argument("--resolve-in", dest="resolve_in", action="append", metavar="COLLECTION",
                   help="resolve edges into another collection against this one (05 §5.5); repeatable")
    p.set_defaults(fn=cmd_ask_traverse)
    p = akverbs.add_parser("serve", help="the three tools of 05 §5.4 over MCP, on stdio")
    p.set_defaults(fn=cmd_ask_serve)
    return top


def main(argv=None) -> int:

    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # `| head` is not an error
    if hasattr(signal, "SIGUSR1"):
        # a command that cannot say where it is: SIGUSR1 prints every
        # thread's stack to stderr and goes on (a SIGTERM'd contribute spent
        # 15 minutes somewhere nobody could name, no py-spy in the image —
        # Perugia, 2026-10-19)
        try:
            import faulthandler
            # chain=False: chained, the default action for SIGUSR1 ran after
            # the dump and TERMINATED the pass (exit 138; Perugia, 2026-10-19)
            faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
        except (ValueError, OSError, AttributeError, RuntimeError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 2 and argv[0] == "share" and argv[1] not in _SHARE_VERBS:
        argv.insert(1, "collection")              # `share <collection>` (07 §7.3)
    if len(argv) >= 2 and argv[0] == "dev" and argv[1] in DEV_TOOLS:
        # an instrument's flags are its own: handed over whole, never parsed
        # here (argparse cannot pass `--all` through a positional remainder)
        return cmd_dev_tool(argparse.Namespace(verb=argv[1], rest=argv[2:]))

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
    except _HTTPException as e:
        # the ask surface (05) is one service under the CLI and the API, and
        # its refusals are HTTP ones — a withheld collection named is 403,
        # an unknown one 400. Found live 2026-10-07: the first certified
        # collection, still in quarantine, answered `ask search` with a
        # traceback. A refusal is exit 2 and says why (07 §7.3).
        print(f"louise: {e.detail}", file=sys.stderr)
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

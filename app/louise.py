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
        raise CodexError(
            "name the encoder: --encoder URL. There is no default encoder "
            "(operator, 2026-09-18), and the new collection's file names the one "
            "it was created with. `hold collection create` embeds the "
            "512-text calibration set with the encoder the recipe names "
            "and writes the reference vectors' hash into the Codex "
            "(06 Â§6.6); there is no offline path that produces a valid "
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


def cmd_show(a) -> int:
    _print(col.show(a.root, a.codex_id))
    return 0


def cmd_collection_migrate(a) -> int:
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
    from app.hold import records as rec
    # 07 §7.3's `build <collection> [--snapshot ID]`: the origin is the
    # collection's own, `<root>/<name>/origin` (07 §7.8); --corpus names
    # another, for a node whose origins are not there yet
    corpus = a.corpus or (Path(a.root) / col.name_of(a.root, a.collection) / "origin")
    rec.build(a.root, a.collection, corpus_dir=corpus,
              snapshot_id=a.snapshot_id, records_per_shard=a.records_per_shard)
    return 0


def cmd_records_validation(a) -> int:
    from app.hold import records as rec
    n = rec.write_validation(a.root, a.collection)
    print(f"validation.json: {n} bytes")
    return 0


def cmd_snapshot_create(a) -> int:
    from app.hold import collection as c, snapshot as sn
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
    try:
        recipe = _recipe_for(codex, name)
    except emb.GateDeferred as e:
        print(f"louise: deferred: {e}", file=sys.stderr)
        return 3

    def embed(texts):
        try:
            return embeddings.embed_texts(texts, recipe=recipe)
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

    for b in out["bundles"]:
        print(f"{b['bundle_id']}")
        print(f"  {b['path']}  —  {b['chunks']} chunks over {b['uids']} records")
    print(f"  shard       {out['shard']}  of snapshot {out['snapshot_id']}")
    print(f"  carries     {out['chunks']} chunks over {out['uids']} of "
          f"{out['records_in_shard']} records in "
          f"{len(out['bundles'])} bundle(s)")
    print(f"  space       {out['dim']} x {out['dtype']}")
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

    name, codex = col.resolve(a.root, a.collection)
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
        embed_fn = lambda texts: embeddings.embed_texts(texts, recipe=recipe)  # noqa: E731
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
    local = rec.local_collection(a.root, name, codex)
    # Invariant 9, the IMPORTER half (migration plan step 4f). However the
    # bundle arrived — a peer copy, a hand-populated folder — a yellow- or
    # red-class collection without a recorded acknowledgement is not
    # imported. The archive half already sits at `genesis.import_shard`.
    licensing.require_ack(
        local, use="a peer's vectors are imported into this collection")

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
        embed = lambda texts: embeddings.embed_texts(texts, recipe=recipe)  # noqa: E731
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
    for cid, why in out["unplaceable"]:
        print(f"  unplaceable {cid}: {why}", file=sys.stderr)
    print(f"  seed        {seed.hex()}")
    print(f"  landed      {out['written']} chunks over {len(mine)} records"
          f"{f', {report.padded} row(s) dropped before the tiers' if report.padded else ''}")
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
    local = rec.local_collection(a.root, name, codex)
    # Embedding is a USE of the text (invariant 9): a corpus that may not
    # be read may not be embedded either.
    licensing.require_ack(local, use="this collection's text is embedded")
    # 07 §7.3's gate, and the one b10630 cost: an encoder that drifted
    # produces vectors nobody can tell from good ones.
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

        def embed(texts):                            # noqa: F811
            try:
                return embeddings.embed_texts(texts, recipe=recipe)
            except Exception as e:                   # noqa: BLE001
                raise emb.GateDeferred(
                    f"the encoder at {recipe.url} could not embed: {e}") from e

    stop = threading.Event()
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
            log=lambda m: print(m, file=sys.stderr), dry_run=a.dry_run)
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
    local = rec.local_collection(a.root, name, codex)
    # Invariant 9: the sample's text is embedded (and --drop deletes
    # vectors) — use of the corpus, as `bundle resweep` is
    from app import licensing
    licensing.require_ack(local, use="its text is embedded to check its vectors")
    snapshot = _latest_snapshot(a.root, name, a.snapshot)

    embed = None
    try:
        recipe = _recipe_for(codex, name)
        embed = lambda texts: embeddings.embed_texts(texts, recipe=recipe)  # noqa: E731
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


def cmd_check(a) -> int:
    """`hold check` — run the plan (03 §3.2) and attest what completed.

    Exit 0 when nothing failed, 1 when a check did. An INCONCLUSIVE check
    is neither (03 §3.1) and does not fail the run: it produces no
    attestation, is reported with its reason, and is retried.
    """
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
        embed = lambda texts: embeddings.embed_texts(texts, recipe=recipe)  # noqa: E731
    except emb.GateDeferred:
        pass

    report = chk.run(
        a.root, name, codex, snapshot, seed=seed,
        checks=a.checks.split(",") if a.checks else None, sample=a.sample,
        embed=embed, reference=reference,
        reparse=_reparse_from(codex, name, getattr(a, "origin_dir", None)),
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
        if key is not None:
            written = chk.write_attestations(
                a.root, name, report, codex, snapshot, key,
                platform_image=codex.toolchain.platform_image, now=_utc_now())
    report = dataclasses.replace(report, attestations=written)

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
    from app.hold import embedder as emb
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
    surfaces = top.add_subparsers(dest="surface", required=True, metavar="hold|share|ask")

    hold = surfaces.add_parser("hold", help="your own copy: complete, current, checked")
    nouns = hold.add_subparsers(dest="noun", required=True)
    coll = nouns.add_parser("collection", help="a Codex and what derives from it")
    verbs = coll.add_subparsers(dest="verb", required=True)

    def root_arg(p):
        p.add_argument("--root", type=Path, default=None,
                       help="where collections live (default: ./collections or $LOUISE_ROOT)")

    p = verbs.add_parser("create", help="write a Codex from a plugin and a recipe; print its id")
    root_arg(p)
    p.add_argument("plugin", nargs="?", default=None,
                   help="the plugin the collection is of; omit when the file is a whole Codex")
    p.add_argument("--recipe", required=True,
                   help="TOML with every Codex field the plugin does not default (06 Â§6.6)")
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

    p = verbs.add_parser("show", help="a Codex and the ids derived from it")
    args_show(p); p.set_defaults(fn=cmd_show)

    p = verbs.add_parser(
        "migrate", help="add the §9.3 fields a Codex predates, each as null")
    root_arg(p); p.add_argument("name")
    p.add_argument("--apply", action="store_true",
                   help="write it; without this the change is only printed")
    p.set_defaults(fn=cmd_collection_migrate)

    p = verbs.add_parser("ack",
                         help="record the licence acknowledgement a yellow- "
                              "or red-class collection needs (09 §9.13)")
    root_arg(p); p.add_argument("collection")
    p.add_argument("--yes", action="store_true",
                   help="record WITHOUT printing the terms (07 §7.7). For a "
                        "script; a person should read them")
    p.set_defaults(fn=cmd_collection_ack)

    # ---- hold records ---------------------------------------------------
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
    p.add_argument("--json", action="store_true",
                   help="09 §9.8's report object")
    p.set_defaults(fn=cmd_check)

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
        return a.fn(a)
    except CodexError as e:
        print(f"louise: {e}", file=sys.stderr)
        return 2
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

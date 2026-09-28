"""The collection template — what a plugin defaults, and what a recipe must
supply. docs/06-plugins.md §6.1 and §6.6; docs/09-formats.md §9.3.

`hold collection create <plugin> --recipe FILE` is the two halves meeting.
The plugin supplies `plugin`, `origin`, `parser`, `layers`, `slicing`, the
classes and the cadence; the recipe supplies what only a creator knows —
the `space` block, `work`, `descriptive`, `validation`, the publication
kind, and the archives to draw on. The platform supplies the toolchain
digests. Nothing is silently defaulted: `create` refuses (exit 2) a recipe
without `space` or `work`, and every default a recipe tightens is tightened
explicitly.

This module is the only place in `app/hold/` that knows a plugin exists.
It imports the registry lazily so `codex`, `collection`, `drag` and
`snapshot` stay importable on their own — they are pure functions over
bytes, and a peer checking a Codex needs none of the corpus machinery.
"""
from __future__ import annotations

from typing import Any

from . import codex as codex_mod
from .codex import (Chunker, Codex, CodexError, Credit, Descriptive, Facet,
                    Layer, License, Origin, Parser, Toolchain, Update,
                    Validation, Work, default_per, space_from_dict)
from .drag import check_slice

#: Fields a recipe must carry: the plugin cannot know them and the platform
#: must not guess them (06 §6.6).
REQUIRED = ("descriptive", "space", "work")

#: Fields a recipe MAY carry. Anything else is refused, so a typo in a
#: recipe is an error rather than a silently ignored line.
OPTIONAL = ("validation", "publication", "certifier", "embedding",
            "succession", "content", "archives", "layers", "slicing",
            "facets", "update", "license", "privacy", "erasure", "modality",
            "profile", "origin", "parser", "cleaning_version",
            "toolchain", "protocol_version", "record_line")


class TemplateError(CodexError):
    """The recipe and the plugin do not make a Codex between them."""


def _registry() -> dict:
    from app.collections import PLUGIN_REGISTRY      # lazy: see the docstring
    return PLUGIN_REGISTRY


def source_for(plugin: str):
    reg = _registry()
    if plugin not in reg:
        raise TemplateError(f"no plugin {plugin!r}; the platform has "
                            f"{', '.join(sorted(reg))}")
    return reg[plugin]


#: 02 §2.7's community credit. ONE collective, not a roster — a roster is
#: unbounded and changing and belongs in the coverage map (02 §2.5), where
#: it does not have to be knowable at creation. This is a constant, so it
#: costs one entry and forks nothing.
COMMUNITY_PRODUCER = "the Project Louise community"
COMMUNITY_ATTRIBUTION = ("Vectors contributed by the Project Louise "
                         "community")


def expand_archives(ids) -> tuple[Credit, ...]:
    """The archive ids a recipe names, expanded into `kind: "archive"`
    credits from the platform's registry (06 §6.6). The entry is copied
    WHOLE: an importer neither composes the acknowledgement nor is free to
    omit it, and because `credits` is hashed into `collection_id`, no later
    build can alter the licence, the attribution or the coverage.

    Archive entries only — `build_credits` assembles the whole list.
    """
    from app.bundles.genesis import ARCHIVES
    out = []
    for aid in ids:
        entry = ARCHIVES.get(aid)
        if entry is None:
            raise TemplateError(
                f"no archive {aid!r} in the platform's registry; it has "
                f"{', '.join(sorted(ARCHIVES)) or 'none'}")
        out.append(Credit(**entry.credit()))
    return tuple(sorted(out, key=codex_mod.credit_order))


def corpus_credit(source) -> Credit:
    """The `kind: "corpus"` entry: whom the TEXT is from (02 §2.7).

    Declared by the plugin (06 §6.1) and copied here — never taken from a
    recipe. A creator free to phrase an attribution is a creator free to
    phrase it away, and this is the one credit no collection can be
    without: every corpus came from someone.
    """
    provider = getattr(source, "corpus_provider", None)
    attribution = getattr(source, "corpus_attribution", None)
    if not provider or not attribution:
        raise TemplateError(
            f"plugin {source.source_id!r} declares no corpus_provider / "
            f"corpus_attribution; 06 §6.1 requires both of every plugin, "
            f"green ones included — a provider that asks for nothing is "
            f"still the provider")
    licence = getattr(source, "corpus_license", None)
    return Credit(kind=codex_mod.CORPUS, archive=None, producer=provider,
                  license=licence, attribution=attribution, coverage="all")


def build_credits(source, archives, embedding: str) -> tuple[Credit, ...]:
    """The whole of 09 §9.3's `credits`, derived and never composed.

    Three sources, one per kind, and a creator writes none of them: the
    corpus entry from the plugin, the archive entries from the recipe's
    `archives` through the registry, and the community entry from
    `embedding` — present exactly when contributed vectors are admissible.
    Sorted by `(kind, archive, producer)`, so two creators building the
    same collection derive the same list and the same `collection_id`.
    """
    out = [corpus_credit(source), *archives]
    if embedding in ("open", "endorsed"):
        out.append(Credit(kind=codex_mod.COMMUNITY, archive=None,
                          producer=COMMUNITY_PRODUCER, license=None,
                          attribution=COMMUNITY_ATTRIBUTION, coverage="all"))
    return tuple(sorted(out, key=codex_mod.credit_order))


def archive_of_key(producer_pubkey: str) -> str | None:
    """Which archive, if any, signs with this key (03 §3.5).

    A bundle carries a `producer_pubkey` and no archive id (09 §9.7), so
    this is the first of the refusal's two steps: resolve the key to an
    archive through the registry, then refuse the bundle if the importing
    collection's `credits` does not name that archive. A key the registry
    does not know belongs to no archive, and the bundle is an ordinary
    producer's — admitted or not by 03 §3.5's ladder like any other.
    """
    from app.bundles.genesis import ARCHIVES
    for aid, entry in sorted(ARCHIVES.items()):
        if producer_pubkey in entry.keys:
            return aid
    return None


def credits_admit(codex, producer_pubkey: str) -> bool:
    """03 §3.5's archive refusal, whole: may a bundle signed with this key
    be imported into this collection?"""
    archive = archive_of_key(producer_pubkey)
    if archive is None:
        return True                       # not an archive's key
    # Only the archive entries answer this. The corpus and community entries
    # name no archive and gate nothing — the community entry is precisely the
    # standing acknowledgement of the peers whose keys the registry does NOT
    # know, which is the branch above (03 §3.5).
    return any(c.archive == archive for c in codex_mod.archive_credits(codex))


# --------------------------------------------------------------------------
# what the plugin declares

def default_layers(source) -> tuple[Layer, ...]:
    """06 §6.3. A plugin that declares `default_layers` is taken at its word;
    otherwise its cut is derived from `chunking_profile` and `mutability`,
    which is where it already lives — a plugin declares its cut once, not
    twice, and the two could otherwise disagree."""
    declared = getattr(source, "default_layers", None)
    if declared:
        return tuple(_layer(d, i) for i, d in enumerate(declared))
    from app import chunking
    name = chunking.chunker_name(getattr(source, "chunking_profile", None),
                                 getattr(source, "mutability", None))
    return (Layer(Chunker(name, chunking.chunk_version_for(name), {}), "default"),)


def default_slicing(source) -> dict[str, Any]:
    """02 §2.2, 09 §9.3. Derived from the plugin's slice scheme where it does
    not declare a whole block: `describe` is the scheme's own description,
    and `unknown`, `shard_records` and `order` complete it to §9.3's shape."""
    declared = getattr(source, "default_slicing", None)
    if declared:
        return _check_slicing(dict(declared))
    from app.pack import slicing as sl
    scheme = sl.SCHEMES.get(source.source_id, sl.DEFAULT_SCHEME)
    block = dict(scheme.describe)
    keys = list(block.get("keys") or [])
    block["keys"] = keys
    block.setdefault("unknown", f"{keys[0]}=unknown" if keys else "all")
    block["shard_records"] = scheme.records_per_shard
    block["order"] = source.native_id_order
    return _check_slicing(block)


def default_facets(source) -> tuple[Facet, ...]:
    """05 §5.7. Sorted by field, as 09 §9.3 requires."""
    declared = getattr(source, "default_facets", None) or []
    facets = tuple(_facet(d) for d in declared)
    return tuple(sorted(facets, key=lambda f: f.field))


#: What a recipe's layer and its chunker may carry (09 §9.3).
_LAYER_KEYS = ("chunker", "rule")
_CHUNKER_KEYS = ("id", "version", "params")


def _layer(d: dict[str, Any], i: int) -> Layer:
    """UNKNOWN KEYS ARE REFUSED, as 09 §9.18 refuses them on every hashed
    object — and here the reason is TOML rather than typos.

    TOML binds a bare key to the table that precedes it, so a recipe
    written as

        [layers.chunker]
        id = "abstract"

        archives = ["medcpt-pubmed"]

    puts `archives` inside the CHUNKER. `from_recipe` validates top-level
    keys only, so it passed silently and the collection was created with
    no archive credited — a permanent `collection_id` standing on work it
    never declared, which `hold vectors import --archive` then correctly
    refuses. Cost one `create` on 2026-09-10 taking pubmed through step 8.
    """
    d = _only(d, _LAYER_KEYS, f"layers[{i}]", optional=("rule",))
    ch = _only(d["chunker"], _CHUNKER_KEYS, f"layers[{i}].chunker",
               optional=("params",))
    return Layer(Chunker(ch["id"], ch["version"], ch.get("params") or {}),
                 d.get("rule", "default" if i == 0 else ""))


def _only(d: dict[str, Any], allowed: tuple[str, ...], where: str,
          *, optional: tuple[str, ...] = ()) -> dict[str, Any]:
    """Like `codex._only`, but a recipe MAY omit what has a default."""
    unknown = sorted(set(d) - set(allowed))
    if unknown:
        raise TemplateError(
            f"{where}: unknown key(s) {', '.join(unknown)}. A recipe's layer "
            f"carries {', '.join(allowed)} and nothing else — and in TOML a "
            f"bare key belongs to the table above it, so a top-level setting "
            f"written after a [table] header lands inside that table")
    missing = sorted(set(allowed) - set(optional) - set(d))
    if missing:
        raise TemplateError(f"{where}: missing key(s) {', '.join(missing)}")
    return d


def _facet(d: dict[str, Any]) -> Facet:
    return Facet(d["field"], d["kind"], d.get("separator"), d.get("authority"))


def _check_slicing(block: dict[str, Any]) -> dict[str, Any]:
    """A slice name that a `<space>` segment could also be would give two
    members one name (09 §9.16), and the `unknown` slice is a slice."""
    if "unknown" in block:
        check_slice(str(block["unknown"]))
    return block


# --------------------------------------------------------------------------
# a recipe omits what a Codex nulls

# A Codex carries every field and writes `null` for one not in use (09 §9.3).
# A recipe is TOML (07 §7.3) and TOML has no null, so a recipe OMITS those
# fields and the template fills them. Anything not listed here a recipe must
# still supply: the rule is "absent means not in use", never "absent means
# guess", so only fields whose unused value is genuinely `null` are here.
_SPACE_NULLABLE = {"evaluation": None}
_ENCODER_NULLABLE = {"label": ""}
_QUERY_NULLABLE = {"encoder": None, "instruction": None}
_ORIGIN_NULLABLE = {"jurisdiction": None}


def _engine_from_recipe(raw: Any, where: str) -> dict[str, Any]:
    """The engine table of a recipe as 09 §9.3's object. TOML has no null and
    an empty table is easy to omit, so `variant` absent is null and `env`
    absent is `{}`; everything else is the recipe's to state — an engine is
    never guessed, because it is what a stranger runs to reproduce the
    reference."""
    if not isinstance(raw, dict):
        raise TemplateError(f"recipe: {where} is a table: name, build, image, args, "
                            f"device, and variant and env where there are any")
    engine = dict(raw)
    engine.setdefault("variant", None)
    engine.setdefault("env", {})
    if engine["variant"] == "":
        engine["variant"] = None
    return engine


def _space_from_recipe(block: dict[str, Any],
                       calibration: tuple[str, str], *, image_shaped: bool = False) -> Any:
    """The space block, with the two hashes `create` computed spliced in.

    `calibration.vectors` and `envelope` are OUTPUTS of `hold collection
    create` (it embeds the calibration set with the named encoder, 06 §6.6),
    never inputs. A recipe carrying them would assert the result of a
    computation its author never ran, and the two would be believed: nothing
    downstream recomputes them, because they ARE the reference.
    """
    b = dict(block)
    cal_block = dict(b.get("calibration") or {})
    if "vectors" in cal_block or "envelope" in b:
        raise TemplateError(
            "recipe: space.calibration.vectors and space.envelope are written "
            "by `hold collection create`, which embeds the calibration set "
            "with the encoder the recipe names (06 §6.6). A recipe cannot "
            "carry them: they are the hashes of vectors it never computed")
    if "set" not in cal_block:
        raise TemplateError(
            "recipe: space.calibration.set is required — it names which "
            "512-text set the reference vectors were produced over, and a "
            "peer holding another set compares nothing (09 §9.12)")
    if "query_vectors" in cal_block:
        raise TemplateError(
            "recipe: space.calibration.query_vectors is written by `hold "
            "collection create`, like `vectors`: it is the hash of what the query "
            "encoder answers for the calibration set (09 §9.12)")
    cal_block["vectors"] = calibration[0]
    b["calibration"] = cal_block
    b["envelope"] = calibration[1]
    for k, v in _SPACE_NULLABLE.items():
        b.setdefault(k, v)
    if isinstance(b.get("encoder"), dict):
        enc = dict(b["encoder"])
        for k, v in _ENCODER_NULLABLE.items():
            enc.setdefault(k, v)
        # A NEW CODEX NAMES ITS ENGINE (09 §9.3, the engine amendment): the
        # run the reference vectors come from, written out whole so that
        # anyone can run it again — never the image alone, which is what a
        # version-2 Codex carried and what put the image inside `space_id`.
        # EXCEPT a space copied whole from a version-2 Codex (`--space-from`,
        # `image_shaped`): 02 §2.7's copy lands two collections in ONE space
        # by construction, and a version-2 space moved to the version-3
        # shape is another `space_id` — every vector of the first becomes
        # inadmissible to the second (operator, 2026-09-21).
        if image_shaped:
            if "image" not in enc or "engine" in enc:
                raise TemplateError(
                    "recipe: a space copied from a version-2 Codex keeps its "
                    "encoder `{image, weights, label}` byte for byte (09 §9.3)")
        elif "image" in enc or "engine" not in enc:
            raise TemplateError(
                "recipe: space.encoder names the ENGINE the reference vectors are "
                "computed with — [space.encoder.engine] with name, build, image, "
                "args, device (and variant, env where there are any) — beside "
                "`weights` and `label`. `image` alone is a version-2 Codex, which "
                "is read for ever and written only as a copy of a version-2 "
                "space (`--space-from`, 09 §9.3)")
        else:
            enc["engine"] = _engine_from_recipe(enc["engine"], "space.encoder.engine")
        b["encoder"] = enc
    q = dict(b.get("query") or {})
    for k, v in _QUERY_NULLABLE.items():
        q.setdefault(k, v)
    # TOML cannot write an empty table as null, so an empty one reads as
    # "there is no query encoder" rather than as a malformed one.
    if q.get("encoder") == {} or q.get("encoder") == []:
        q["encoder"] = None
    if q.get("instruction") == "":
        q["instruction"] = None
    if isinstance(q.get("encoder"), dict) and image_shaped:
        q["encoder"] = dict(q["encoder"])        # the version-2 shape, as copied
    elif isinstance(q.get("encoder"), dict):
        qe = dict(q["encoder"])
        if "image" in qe:
            raise TemplateError(
                "recipe: space.query.encoder names `weights` and, where it differs "
                "from the document side's, an `engine` — not an image (09 §9.3)")
        # "the same, its own weights" (09 §9.3's worked example) is the
        # common case — one engine, two weights files — so a query encoder
        # that names no engine runs on the document side's.
        qe["engine"] = (_engine_from_recipe(qe["engine"], "space.query.encoder.engine")
                        if "engine" in qe
                        else (b.get("encoder") or {}).get("engine"))
        q["encoder"] = qe
    b["query"] = q
    if image_shaped:
        # a version-2 calibration is {set, vectors, quorum}: `query_vectors`
        # came with the engine amendment (09 §9.12), so the copy has none
        if b.get("evaluation") in ([], {}):
            b["evaluation"] = None
        return space_from_dict(b)
    # present and null in a symmetric space: every field is present (§9.3)
    cal_block["query_vectors"] = (calibration[2] if len(calibration) > 2 else None) \
        if q.get("encoder") is not None else None
    if q.get("encoder") is not None and cal_block["query_vectors"] is None:
        raise TemplateError(
            "recipe: this space has a query encoder, and `hold collection create` "
            "was given no reference vectors for it (09 §9.12, query_vectors)")
    if b.get("evaluation") in ([], {}):
        b["evaluation"] = None
    return space_from_dict(b)


def _origin_from_recipe(block: dict[str, Any]) -> Origin:
    b = dict(block)
    for k, v in _ORIGIN_NULLABLE.items():
        b.setdefault(k, v)
    if b.get("jurisdiction") == "":
        b["jurisdiction"] = None
    spec = dict(b.get("spec") or {})
    spec.setdefault("files", None)
    if spec.get("files") in ([], {}):
        spec["files"] = None
    if spec["files"] is not None:
        spec["files"] = [_file_entry(f, i) for i, f in enumerate(spec["files"])]
    b["spec"] = spec
    return Origin(**b)


_FILE_KEYS = ("rel_path", "size", "checksum")


def _file_entry(f, i: int) -> dict[str, Any]:
    """09 §9.17's `{rel_path, size or null, checksum or null}` — all three
    keys, always. TOML has no null, so an ABSENT size or checksum is the
    null the table allows; without this the Codex carried entries missing
    the key, a shape §9.17 does not define and a verifier would read
    differently. An unknown key is refused, as §9.18 refuses them on
    every hashed object."""
    if not isinstance(f, dict) or not f.get("rel_path"):
        raise TemplateError(f"origin.spec.files[{i}] needs a rel_path (09 §9.17)")
    extra = sorted(set(f) - set(_FILE_KEYS))
    if extra:
        raise TemplateError(f"origin.spec.files[{i}]: unknown key(s) {', '.join(extra)} "
                            f"(09 §9.17: rel_path, size, checksum)")
    size, checksum = f.get("size"), f.get("checksum")
    return {"rel_path": str(f["rel_path"]),
            "size": int(size) if size is not None else None,
            "checksum": str(checksum) if checksum else None}


# --------------------------------------------------------------------------
# the recipe

def from_recipe(plugin: str, recipe: dict[str, Any], *,
                platform_image: str,
                calibration: tuple[str, str],
                credits: tuple = (), space_version: int | None = None) -> Codex:
    """Build a Codex from a plugin's defaults and a creator's recipe.

    `calibration` is `(vectors_hash, envelope_hash)` — what the caller got
    by embedding the calibration set with the recipe's encoder (06 §6.6).
    It is required and has no default: a Codex whose reference vectors are
    a placeholder passes every check made here and fails every peer's gate.

    `credits` is the ARCHIVE entries the caller expanded from the platform's
    registry for the recipe's `archives` (06 §6.6); this function never
    composes one, and refuses a recipe naming archives when none were
    expanded, so an unreachable registry is an error rather than an
    uncredited collection. The corpus and community entries are added here
    from the plugin's declaration and from `embedding` — neither is the
    caller's to supply and neither is a recipe field (02 §2.7).
    """
    unknown = sorted(set(recipe) - set(REQUIRED) - set(OPTIONAL))
    if unknown:
        raise TemplateError(f"recipe: unknown key(s) {', '.join(unknown)}; a "
                            f"recipe carries a Codex field or tightens a default")
    missing = [f for f in REQUIRED if f not in recipe]
    if missing:
        raise TemplateError(
            f"recipe: {', '.join(missing)} is required — the plugin never "
            f"defaults it and the platform must not guess it (06 §6.6)")
    if recipe.get("archives") and not credits:
        raise TemplateError(
            "recipe names archives but none were expanded from the platform's "
            "registry; an archive a collection draws on is credited at "
            "creation or not at all (06 §6.6)")

    source = source_for(plugin)
    # `space_version`: the version of the Codex a `--space-from` space was
    # copied from. Below the engine amendment that space names an image, and
    # it is written back as it was — in a Codex of that version, whose
    # space_id is computed the way the copied one's was (02 §2.7: one space
    # by construction). Only a copy: a recipe cannot state that shape itself.
    image_shaped = (space_version is not None
                    and space_version < codex_mod.ENGINE_VERSION
                    and "image" in ((recipe.get("space") or {}).get("encoder") or {}))
    # Read once: it is written into the Codex AND it decides whether the
    # community is credited (02 §2.7), and the two must not disagree.
    _embedding = recipe.get("embedding", "open")
    update = Update(**recipe["update"]) if "update" in recipe else Update()
    work = dict(recipe["work"])
    work.setdefault("per", default_per(update.cadence))

    space = _space_from_recipe(recipe["space"], calibration, image_shaped=image_shaped)
    codex = Codex(
        descriptive=Descriptive(**recipe["descriptive"]),
        plugin=plugin,
        origin=_origin(source, recipe),
        toolchain=Toolchain(recipe.get("toolchain", {}).get("platform_image")
                            or platform_image, None),
        parser=(Parser(**recipe["parser"]) if "parser" in recipe
                else Parser(source.source_id, source.PARSER_VERSION)),
        cleaning_version=recipe.get("cleaning_version", 1),
        protocol_version=recipe.get("protocol_version", source.protocol_version),
        layers=(tuple(_layer(d, i) for i, d in enumerate(recipe["layers"]))
                if "layers" in recipe else default_layers(source)),
        slicing=(_check_slicing(dict(recipe["slicing"])) if "slicing" in recipe
                 else default_slicing(source)),
        space=space,
        work=Work(**work),
        modality=recipe.get("modality", source.modality.value),
        erasure=recipe.get("erasure", source.erasure.value),
        validation=_validation(recipe),
        license=_license(source, recipe),
        privacy=recipe.get("privacy", source.privacy_class.value),
        update=update,
        publication=recipe.get("publication", "open"),
        certifier=None, embedding=_embedding,
        succession=recipe.get("succession", "frozen"),
        content=recipe.get("content"),
        facets=(tuple(sorted((_facet(d) for d in recipe["facets"]),
                             key=lambda f: f.field))
                if "facets" in recipe else default_facets(source)),
        credits=build_credits(source, credits, _embedding),
        profile=recipe.get("profile"),
        # 09 §9.3: version 4 exactly when an engine carries `load`
        version=(space_version if image_shaped
                 else codex_mod.LOAD_VERSION if space.has_load
                 else codex_mod.CODEX_VERSION),
        record_line=_record_line(recipe),
    )
    _refuse_a_loosened_class(source, codex)
    return codex


def _record_line(recipe: dict[str, Any]) -> int:
    """09 §9.5's record shape, declared once and for the collection's life.

    Defaults to 1. 2 became writable on 2026-09-12, when the figures
    containers, the `figures` member kind, the snapshot's `figures` list
    and the plugin side of the amendment all landed
    (docs/notes/figures-amendment-plan.md); `RECORD_LINES_WRITABLE` is the
    one place that says so. Writing 2 into a Codex and then producing
    record lines without a `figures` key would be a collection whose own
    identity says what its shards are not — and `record_line` is inside
    `collection_id`, so it could never be corrected, only replaced.
    """
    want = int(recipe.get("record_line", codex_mod.RECORD_LINE_BUILT))
    if want not in codex_mod.RECORD_LINES:
        raise TemplateError(f"record_line is 1 or 2 (09 §9.3), got {want!r}")
    if want not in codex_mod.RECORD_LINES_WRITABLE:
        raise TemplateError(
            f"record_line {want} is not writable by this build. "
            f"`record_line` is inside `collection_id`, so a collection "
            f"created claiming plates it cannot write could never be "
            f"corrected, only replaced")
    return want


def _origin(source, recipe: dict[str, Any]) -> Origin:
    """The origin block is the plugin's — it names the source, the form
    taken and why, and the acquisition spec (06 §6.2, §6.4; 09 §9.17). Until
    the plugin's `AcquisitionSpec` is serialised into it, which is migration
    step 2's work, the recipe supplies it and `create` refuses without it
    rather than inventing an origin the collection cannot be verified
    against (invariant 11)."""
    if "origin" in recipe:
        return _origin_from_recipe(recipe["origin"])
    raise TemplateError(
        f"recipe: origin is required. {source.source_id}'s acquisition spec "
        f"is not yet serialised into the Codex (06 §6.2, 09 §9.17), so the "
        f"recipe carries the origin block until it is")


def _validation(recipe: dict[str, Any]) -> Validation:
    if "validation" not in recipe:
        raise TemplateError("recipe: validation is required — a collection that "
                            "declares no plan cannot be checked (03 §3.2)")
    v = recipe["validation"]
    checks = tuple(v["checks"])
    # A COLLECTION THAT CARRIES PLATES CHECKS THEM. `record_line` 2 is the
    # whole of what makes a collection a plates collection (09 §9.5), and a
    # plan that does not name `figures` publishes tens of gigabytes of
    # images that nothing ever re-hashes — the figure hash on the record
    # line being the only thing that can.
    #
    # HERE, ON THE CREATE PATH, and deliberately not in `codex.validate`:
    # `validation` is inside `collection_id`, so before the id exists is
    # the only moment this can be fixed — and a Codex that already exists
    # with this shape, anywhere, must still PARSE. Refusing it at `loads`
    # would make a peer's collection unreadable rather than unserved, and
    # would have made this node's own previous attempt unloadable too,
    # which is how the placement was found.
    if _record_line(recipe) == 2 and "figures" not in checks:
        raise TemplateError(
            "recipe: record_line = 2 carries plates, so validation.checks "
            "must name `figures` (09 §9.8). Nothing else re-hashes a plate "
            "against the record that names it, and validation is inside "
            "collection_id — this is the only moment it can be added")
    return Validation(checks, v.get("sample", {}), tuple(v.get("required", ())))


def _license(source, recipe: dict[str, Any]) -> License:
    if "license" in recipe:
        return License(recipe["license"]["class"],
                       recipe["license"].get("terms_sha256"))
    cls = source.license_class.value
    if cls == "green":
        return License("green", None)
    raise TemplateError(
        f"recipe: license.terms_sha256 is required — {source.source_id} is "
        f"{cls}-class and a yellow or red collection names its terms by hash "
        f"(04 §4.4)")


def _refuse_a_loosened_class(source, codex: Codex) -> None:
    """The plugin declares the default and a collection may TIGHTEN it,
    never loosen it (04 §4.4). Tightening is towards the more restrictive
    end of each ladder."""
    for name, ladder, got, declared in (
        ("license", ("green", "yellow", "red"),
         codex.license.cls, source.license_class.value),
        ("privacy", ("public", "restricted", "never-share"),
         codex.privacy, source.privacy_class.value),
        # `erasure` is deliberately not on this ladder: 04 §4.4 states the
        # tighten-never-loosen rule for the privacy class, and erasure's two
        # ends are restrictive about different things — `append-only` about
        # removal, `erasable` about sharing, which is why it requires
        # `never-share`. A direction nobody has stated is not one to enforce.
    ):
        if ladder.index(got) < ladder.index(declared):
            raise TemplateError(
                f"recipe: {name} {got!r} loosens the plugin's {declared!r}; a "
                f"collection may tighten a class and never loosen it (04 §4.4)")

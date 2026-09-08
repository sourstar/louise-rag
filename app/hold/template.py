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
            "toolchain", "protocol_version")


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


def expand_archives(ids) -> tuple[Credit, ...]:
    """The archive ids a recipe names, expanded into `credits` from the
    platform's registry (06 §6.6). The entry is copied WHOLE: an importer
    neither composes the acknowledgement nor is free to omit it, and because
    `credits` is hashed into `collection_id`, no later build can alter the
    licence, the attribution or the coverage.

    Sorted by archive, as 09 §9.3 requires.
    """
    from app.bundles.genesis import ARCHIVES
    out = []
    for aid in ids:
        entry = ARCHIVES.get(aid)
        if entry is None:
            raise TemplateError(
                f"no archive {aid!r} in the platform's registry; it has "
                f"{', '.join(sorted(ARCHIVES)) or 'none'}")
        try:
            out.append(Credit(**entry.credit()))
        except ValueError as e:
            raise TemplateError(str(e)) from e
    return tuple(sorted(out, key=lambda c: c.archive))


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


def _layer(d: dict[str, Any], i: int) -> Layer:
    ch = d["chunker"]
    return Layer(Chunker(ch["id"], ch["version"], ch.get("params") or {}),
                 d.get("rule", "default" if i == 0 else ""))


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


def _space_from_recipe(block: dict[str, Any]) -> Any:
    b = dict(block)
    for k, v in _SPACE_NULLABLE.items():
        b.setdefault(k, v)
    if isinstance(b.get("encoder"), dict):
        enc = dict(b["encoder"])
        for k, v in _ENCODER_NULLABLE.items():
            enc.setdefault(k, v)
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
    b["query"] = q
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
    b["spec"] = spec
    return Origin(**b)


# --------------------------------------------------------------------------
# the recipe

def from_recipe(plugin: str, recipe: dict[str, Any], *,
                platform_image: str,
                credits: tuple = ()) -> Codex:
    """Build a Codex from a plugin's defaults and a creator's recipe.

    `credits` is what the caller expanded from the platform's registry for
    the recipe's `archives` (06 §6.6); this function never composes one,
    and refuses a recipe naming archives when none were expanded, so an
    unreachable registry is an error rather than an uncredited collection.
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
    update = Update(**recipe["update"]) if "update" in recipe else Update()
    work = dict(recipe["work"])
    work.setdefault("per", default_per(update.cadence))

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
        space=_space_from_recipe(recipe["space"]),
        work=Work(**work),
        modality=recipe.get("modality", source.modality.value),
        erasure=recipe.get("erasure", source.erasure.value),
        validation=_validation(recipe),
        license=_license(source, recipe),
        privacy=recipe.get("privacy", source.privacy_class.value),
        update=update,
        publication=recipe.get("publication", "open"),
        certifier=None, embedding=recipe.get("embedding", "open"),
        succession=recipe.get("succession", "frozen"),
        content=recipe.get("content"),
        facets=(tuple(sorted((_facet(d) for d in recipe["facets"]),
                             key=lambda f: f.field))
                if "facets" in recipe else default_facets(source)),
        credits=tuple(credits),
        profile=recipe.get("profile"),
    )
    _refuse_a_loosened_class(source, codex)
    return codex


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
    if "validation" in recipe:
        v = recipe["validation"]
        return Validation(tuple(v["checks"]), v.get("sample", {}),
                          tuple(v.get("required", ())))
    raise TemplateError("recipe: validation is required — a collection that "
                        "declares no plan cannot be checked (03 §3.2)")


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

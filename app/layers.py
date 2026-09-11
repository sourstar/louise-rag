"""Chunker LAYERS — architecture-v2.md amendment 41, invariant 1 as amended.

A collection declares an ordered list of layers, each with a chunker;
every document belongs to exactly one layer by a rule computable from the
record and the collection's declaration alone; one encoder per collection.
The common case is ONE layer, and then the layer simply IS the plugin's
chunker — nothing pinned before this module existed changes.

TOML:

    [[layers]]
    chunker = "verbatim"
    rule = "archive:upstash-wikipedia-2024-06"   # covered by that archive

    [[layers]]
    chunker = "cdc"
    rule = "default"                              # everything else (last)

Rules:
    default            every document (must be the last layer)
    archive:<id>       documents the named genesis archive declares it can
                       cover (a remote archive with declared id ranges); a
                       local archive (the corpus IS the archive) covers all
    field:<name>=<value>  documents whose record field — a top-level field,
                       or `extra.<key>` — has that value (06 §6.3)
"""
from dataclasses import dataclass, field

from . import chunking


@dataclass(frozen=True)
class Layer:
    chunker: str                 # chunking.BOOK | FIXED | VERBATIM | ABSTRACT | CDC
    version: int
    rule: str
    #: What the chunker cuts with. 06 §6.3: "a chunker reads no node
    #: configuration" — so a chunker that takes parameters takes them from
    #: HERE, and 09 §9.3 hashes them into `space_id`. Empty for the
    #: chunkers that take none, and empty for `book`, which is
    #: grandfathered and reads the environment (chunking.chunk_book).
    params: dict = field(default_factory=dict)

    def as_manifest(self) -> dict:
        c = {"id": self.chunker, "version": self.version}
        # Only when non-empty: every collection on this node predates
        # params, and a manifest that gained an empty key would differ
        # from the bytes already on disk for no reason.
        if self.params:
            c["params"] = self.params
        return {"chunker": c, "rule": self.rule}


class LayerError(ValueError):
    """A declaration the platform cannot honour."""


#: 06 §6.3's rule vocabulary, in full.
RULE_FORMS = ("default", "archive:<id>", "field:<name>=<value>")


def check_rule(rule: str, *, where: str) -> None:
    """Refuse a rule this platform cannot evaluate.

    A rule that is merely UNRECOGNISED is the dangerous case, not an
    obviously broken one: `_rule_matches` returns False for anything it
    does not know and `layer_index` then falls through to the last layer,
    so a Codex carrying a typo — or a rule from a newer platform — chunks
    the whole collection by the default layer and says nothing. The layer
    list is inside `space_id`, so that silence produces vectors under an
    id that promises a different cut.
    """
    if rule == "default" or rule.startswith("archive:"):
        return
    if rule.startswith("field:"):
        spec = rule[len("field:"):]
        if "=" not in spec or not spec.split("=", 1)[0]:
            raise LayerError(
                f"{where}: rule {rule!r} must be field:<name>=<value> (06 §6.3)")
        return
    raise LayerError(f"{where}: unknown rule {rule!r} "
                     f"(known: {', '.join(RULE_FORMS)})")


def _field_value(row, name: str):
    """A record field by 06 §6.3's name: a top-level field, or
    `extra.<key>`. Returns None when the row cannot answer."""
    if row is None:
        return None
    if name.startswith("extra."):
        key = name[len("extra."):]
        extra = _get(row, "extra")
        if isinstance(extra, dict):
            return extra.get(key)
        return None
    return _get(row, name)


def _get(row, name: str):
    """Rows reach here as sqlite3.Row, plain dicts and record dicts, and
    only the dict answers `.get`."""
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return None


def parse_layers(raw, *, where: str) -> tuple[Layer, ...] | None:
    """The TOML `[[layers]]` array -> layers, validated. None when absent."""
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise LayerError(f"{where}: layers must be a non-empty array of tables")
    out = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or "chunker" not in entry:
            raise LayerError(f"{where}: layers[{i}] needs a chunker")
        name = str(entry["chunker"])
        if name not in chunking._CHUNKERS:
            raise LayerError(f"{where}: layers[{i}]: unknown chunker {name!r} "
                             f"(known: {', '.join(sorted(chunking._CHUNKERS))})")
        rule = str(entry.get("rule", "default"))
        check_rule(rule, where=f"{where}: layers[{i}]")
        params = entry.get("params", {})
        if not isinstance(params, dict):
            raise LayerError(f"{where}: layers[{i}]: params is a table")
        out.append(Layer(chunker=name, version=chunking.chunk_version_for(name),
                         rule=rule, params=params))
    if out[-1].rule != "default":
        raise LayerError(f"{where}: the last layer must have rule = \"default\"")
    if any(l.rule == "default" for l in out[:-1]):
        raise LayerError(f"{where}: only the last layer may have rule = \"default\"")
    return tuple(out)


def layers_for(collection) -> tuple[Layer, ...]:
    """The collection's layers: its declaration, or the one implicit layer
    every collection had before layers existed (the plugin's chunker).

    A CODEX declares the same thing in a different shape — 09 §9.3 nests
    the chunker as `{id, version, params}` where this module keeps it
    flat — and it is normalised here rather than at every call site.
    Invariant 1 asks that every stage cut a document through
    `chunks_for_document`, and a stage holding a Codex (an export, a
    check, a bundle import) is a stage; making each of them convert would
    be four chances to convert it differently.
    """
    declared = getattr(collection, "layers", None)
    if declared:
        return tuple(_normalise(l) for l in declared)
    name, version = chunking.chunker_version_for_collection(collection)
    return (Layer(chunker=name, version=version, rule="default"),)


def _normalise(layer) -> Layer:
    """This module's Layer, from either shape."""
    if isinstance(getattr(layer, "chunker", None), str):
        return layer
    c = layer.chunker                      # 09 §9.3's {id, version, params}
    return Layer(chunker=c.id, version=c.version, rule=layer.rule,
                 params=dict(c.params or {}))


def _codex_coverage(collection, archive_id: str):
    """The archive's `coverage` as the COLLECTION declares it, or None.

    06 §6.3 is explicit that this is where it comes from: the documents an
    archive covers are "read from that archive's `coverage` in the Codex's
    `credits` (02 §2.7), so the rule is computable from the record and the
    Codex alone, and A PLATFORM BUILD CANNOT MOVE A DOCUMENT BETWEEN
    LAYERS".

    That last clause is the whole reason. `layers` is inside `space_id`, so
    two peers agree on the space; if they then read coverage from their own
    platform builds, they can put the same document in different layers,
    cut it with different chunkers, and produce different vectors under one
    space id. Nothing detects it — both computed the id the document
    prescribes, and both are honest. It is invariant 1's failure mode.
    """
    for credit in _archive_credits(collection):
        archive = (getattr(credit, "archive", None)
                   or (credit.get("archive") if isinstance(credit, dict) else None))
        if archive == archive_id:
            return (getattr(credit, "coverage", None)
                    or (credit.get("coverage") if isinstance(credit, dict) else None))
    return None


def _archive_credits(collection) -> list:
    """`credits` entries of `kind: "archive"` — the ONLY kind a layer rule
    reads (06 §6.3).

    The corpus and community entries carry `coverage: "all"` because a
    provider and the community are owed for every document; a rule that
    read them would match everything and put the whole corpus in the first
    layer. An entry with no `kind` at all predates the three kinds and is
    an archive entry, which is what every one of them was.
    """
    out = []
    for credit in getattr(collection, "credits", None) or ():
        kind = (getattr(credit, "kind", None)
                or (credit.get("kind") if isinstance(credit, dict) else None))
        if kind in (None, "archive"):
            out.append(credit)
    return out


def _rule_matches(rule: str, collection, native_id: int, row=None) -> bool:
    if rule == "default":
        return True
    if rule.startswith("archive:"):
        archive_id = rule[len("archive:"):]
        declared = getattr(collection, "credits", None)
        if declared:
            # A collection that declares credits AT ALL is authoritative
            # about every archive, including the ones it does not name: an
            # archive absent from `credits` covers nothing here. Falling
            # through to the platform registry for those would reintroduce
            # the bug — the platform would be deciding coverage again, just
            # for a narrower set of archives.
            coverage = _codex_coverage(collection, archive_id)
            if coverage is None:
                return False
            if coverage == "all":
                return True
            if not isinstance(coverage, dict):
                return False
            per = coverage.get("ids_per_shard")
            shards = coverage.get("shards")
            if not per:
                return True         # the corpus IS the archive: it covers all
            # The Codex writes `shards` as a COUNT, so the covered ids are
            # shards 0 … shards-1. The platform registry below instead holds
            # shard NAMES and can express a gap; the Codex form cannot, and
            # an archive with a hole in it would need §2.1 to say so.
            return 0 <= int(native_id) // int(per) < int(shards)

        # A collection with no `credits` predates them: fall back to the
        # platform's registry, which is what every collection on this node
        # uses today. Grandfathered, exactly as `book` is beside `fixed` —
        # a Codex collection reads its own Codex and never comes here.
        from .bundles.genesis import ARCHIVES
        archive = ARCHIVES.get(archive_id)
        if archive is None:
            return False
        if archive.local or not archive.ids_per_shard:
            return True             # the corpus IS the archive: it covers all
        return archive.shard_of_id(int(native_id)) is not None
    if rule.startswith("field:"):
        # 06 §6.3: "the documents whose record field (a top-level field, or
        # `extra.<key>`) has that value". Computable from the record and the
        # Codex alone, which is what stops a platform build from moving a
        # document between layers.
        name, _, want = rule[len("field:"):].partition("=")
        if row is None:
            raise LayerError(
                f"layer rule {rule!r} needs the document's record and none "
                f"was supplied — the caller must pass the row, or the layer "
                f"cannot be decided without guessing")
        got = _field_value(row, name)
        return got is not None and str(got) == want
    return False


def layer_index(collection, native_id, row=None) -> int:
    layers = layers_for(collection)
    if len(layers) == 1:
        return 0
    for i, layer in enumerate(layers):
        if _rule_matches(layer.rule, collection, int(native_id), row):
            return i
    return len(layers) - 1


def layer_of(collection, native_id, row=None) -> Layer:
    return layers_for(collection)[layer_index(collection, native_id, row)]


def chunker_for_document(collection, native_id, row=None):
    """The chunker bound to this document's layer — the ONE way a stage
    should cut a document (worker, importer, genesis, contribute)."""
    layer = layer_of(collection, native_id, row)
    fn = chunking._CHUNKERS[layer.chunker]
    version = layer.version
    params = layer.params

    def _chunk(uid, nid, sections):
        return fn(uid, nid, sections, version, params)
    return _chunk


def chunks_for_document(collection, row, sections) -> list[dict]:
    native_id = int(row["native_id"])
    return list(chunker_for_document(collection, native_id, row)(
        row["uid"], native_id, sections))


def space_payload(collection) -> dict:
    """What the space id records about chunking. ONE layer: exactly the
    pre-layers payload (the chunker, grandfathered names omitted), so every
    pinned space id is byte-identical. MORE: the ordered layer chunkers."""
    layers = layers_for(collection)
    if len(layers) == 1:
        return {}
    return {"layers": [[l.chunker, l.version] for l in layers]}

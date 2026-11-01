"""Collection = (plugin, corpus snapshot, pipeline versions, embedding space).

Each collection gets its own manifest.db / fts/ / lancedb/ paths. Config lives
in `${CONFIG_DIR:-.}/collections/*.toml` (architecture-v2.md §3, §14 Tier 2).
With no collections/ directory present, a single default Gutenberg collection
is synthesized whose paths equal today's top-level DATA_DIR/CORPUS_DIR layout
exactly — an existing deployment upgrades with zero data movement and no new
required config or volume mount.
"""
import math
import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import config
from .plugins import deposit, embedded, gutenberg, normattiva, pubmed, wikidata, wikipedia
from .plugins.base import CorpusSource

# `genome` and `packed` are boxed in attic/ (operator, 2026-09-14): kept for
# reference until converted to the final format or dropped.
PLUGIN_REGISTRY: dict[str, CorpusSource] = {
    "deposit": deposit.SOURCE,
    "embedded": embedded.SOURCE,
    "gutenberg": gutenberg.SOURCE,
    "normattiva": normattiva.SOURCE,
    "pubmed": pubmed.SOURCE,
    "wikidata": wikidata.SOURCE,
    "wikipedia": wikipedia.SOURCE,
}


def model_named_by(url: str) -> str | None:
    """The model a URL names, where the URL is the model container's —
    `http://<node>:<port>/upstream/<model>` (07 §7.8: "an `[encoder]` URL
    names a model and never a device"); None for any other URL."""
    _, sep, tail = url.partition("/upstream/")
    model = tail.split("/", 1)[0] if sep else ""
    return model or None


@dataclass(frozen=True)
class EmbedRecipe:
    """A collection's embedding recipe — architecture-v2.md §14 amendment 9.

    Declared per collection in the TOML's [embed] block; a collection
    without one uses the node's Tier-1 env recipe (fallback, not override).
    Immutability is NOT this dataclass's job: the pinned space_id and boot
    validation decide whether the vectors on disk agree with the declared
    recipe (invariant 1)."""
    url: str
    api: str                     # openai | ollama
    model: str                   # the file's `embed_model`: the DOCUMENT side's model
    dtype: str                   # f32 | f16
    ctx: int
    dim: int | None = None       # declared width; checked against the table
    query_url: str | None = None  # bi-encoder pairs (MedCPT); None = symmetric
    # The file's `query_model` (07 §7.8, amended 2026-10-08): the model a
    # QUESTION is embedded with — sent as the body's `model` on the query
    # side (`embeddings._side`), never the document model. None only on a
    # recipe built by hand; one read from a file always names it.
    query_model: str | None = None
    # The Codex's `space.query.instruction` (02 §2.4, 09 §9.3), prepended to
    # QUERIES only, never to documents: one model whose query side expects a
    # lead-in ("query: " for DAR-legal-it, "Instruct: …\nQuery: " for
    # Qwen3-Embedding) — worth +4.2 MRR@100 on unchanged weights
    # (gazzetta-plugin.md §7.1b). It IS part of the space: "the query side is
    # part of the function". So it is never a node's to state — only
    # `_codex_encoder` fills it, from the Codex, and a collection file that
    # names one is refused. A node-local prefix (25b30f3) sat outside
    # `space_id` while calibration and the gate prefixed with the Codex's:
    # the gate would have passed one function and search run another.
    query_instruction: str | None = None
    # HOW AN INPUT LONGER THAN `ctx` IS CUT, which is part of the function a
    # space's vectors are of (docs/notes/char-cut-truncation.md, 2026-10-18).
    # `characters`: the platform cuts to ~ctx x 4 characters, as every live
    # Codex's calibration reference was made; `tokens`: the input goes to a
    # VeGa encoder whole and is cut there to exactly `ctx` tokens, as 09 §9.3
    # says. Only `_codex_encoder` sets it, from the Codex's space, and a space
    # that does not declare one is `characters` — changing the cut under a
    # Codex changes its function, and its references no longer reproduce
    # (rollout of 2026-10-18: gutenberg's re-check failed on the first try).
    cut: str = "characters"
    # Documents embedded concurrently by the worker (thread pool of whole
    # documents, each a single-text request). NOT part of the space identity
    # — it changes no vector, like EMBED_BATCH. >1 belongs to CPU encoders
    # (TEI/MedCPT); GPU llama-server stays serial — its multi-sequence
    # handling is the defect the batch gate exists for.
    concurrency: int = 1
    # Chunks a request carries (2026-10-31, München's production profile:
    # wikipedia's embedding was bound by one chunk a request, the card ~10 %
    # busy). 0: the node's EMBED_BATCH. Per collection because whether a
    # batch answers as one text at a time does is the ENGINE's property: the
    # gate measures it at this batching before a batched answer is written
    # (`hold.runs`, `run_check.contributing`). Not part of the space, like
    # `concurrency`.
    batch: int = 0
    # HOW EXACT A RUN OF THIS ENCODER MUST BE (docs/notes/near-exact-runs.md).
    # "near", THE DEFAULT since the operator's choice B (2026-11-01: "it makes
    # not much sense to enforce local bit exactness" — peers already accept
    # each other at the space's tolerance): a run is admitted when it
    # reproduces itself within NEAR_VARIATION across fresh loads, orders and
    # batches, and reaches the stored CPU reference at CPU_NEAR_LINE; a run
    # that gives back its own bytes is recorded exact either way. "exact"
    # stays as a collection's stricter choice: bytes or nothing. Never in the
    # space: the vectors are the same function within tolerance.
    exactness: str = "near"
    # Which processor this endpoint spends: "gpu" | "cpu". DESCRIPTIVE
    # ONLY — it never enters the space id, because the same model on a
    # different device computes the same function (that is precisely what
    # the live cross-backend calibration measured). It exists so the UI can
    # tell an operator whether flipping embed mode spends the scarce GPU or
    # spare CPU. Absent, it is inferred from `concurrency`, which in this
    # codebase IS the split: GPU stays serial, CPU parallelizes.
    device: str | None = None

    @property
    def spends(self) -> str:
        if self.device in ("gpu", "cpu"):
            return self.device
        return "cpu" if self.concurrency > 1 else "gpu"


#: why a collection that names no encoder is not served
NO_ENCODER = (
    "non-compliant: its file names no [encoder]. A collection names its own "
    "embedder and there is no default one — no EMBED_URL, no per-plugin "
    "default (operator, 2026-09-18). Add [encoder] url = \"…\" to the file "
    "(07 §7.8)")


class NoEncoder(ValueError):
    """A collection with no encoder of its own: non-compliant, never served."""


class ModelKeys(ValueError):
    """A collection file whose `[encoder]` or `[reranker]` does not name its
    models as 07 §7.8 (amended 2026-10-08) says — the old `model` key, a
    missing `embed_model`/`query_model`/`rerank_model`, or a key that is
    not the model its model-container URL names. Refused for THAT
    collection (`load_all` lists it in `refusals()`), never fatal."""


#: how to fix a file still in the old shape
MIGRATE_HINT = ("tools/migrate_model_keys.py rewrites a file in place")


def _named_model(raw: dict, key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ModelKeys(f"{where}: `{key}` is required and names a model (07 §7.8, "
                        f"amended 2026-10-08), got {value!r}; {MIGRATE_HINT}")
    return value


def _agrees(url: str, key: str, value: str, url_key: str, where: str) -> None:
    """A model-container URL names its model (`…/upstream/<model>`): the
    file's key must be that model, or the file says two things."""
    named = model_named_by(url)
    if named is not None and named != value:
        raise ModelKeys(f"{where}: `{key}` = {value!r} and {url_key} {url} names "
                        f"{named!r}. On the model container the URL says which model; "
                        f"the key must agree with it (07 §7.8, amended 2026-10-08)")


def _exactness(raw: dict, where: str) -> str:
    value = str(raw.get("exactness", "near"))
    if value not in ("exact", "near"):
        raise ValueError(f"{where}: `exactness` is \"exact\" or \"near\" "
                         f"(docs/notes/near-exact-runs.md), got {value!r}")
    return value


def _batch(raw: dict, where: str) -> int:
    value = raw.get("batch", 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{where}: `batch` is the chunks a request carries, a whole "
                         f"number (0 or absent: the node's EMBED_BATCH), got {value!r}")
    return value


def parse_recipe(raw: Any, *, where: str) -> EmbedRecipe | None:
    """An [embed]-shaped TOML table -> EmbedRecipe. None for absent/not-a-
    table, so callers can say "declared or not".

    Shared by the per-collection [embed] block and platform.toml's per-plugin
    defaults: one parser means one set of field names, defaults and error
    messages, whichever file the operator is editing."""
    if not isinstance(raw, dict):
        return None
    for key in ("query_prefix", "query_instruction", "instruction"):
        if key in raw:
            # 25b30f3 read `query_prefix` here, and the operator was told to
            # write one into a collection file. Refused rather than ignored:
            # ignored, it would look set while search ran without it.
            raise ValueError(
                f"{where}: `{key}` is not a node's to state. A query's "
                f"instruction is the space's — `space.query.instruction` in "
                f"the Codex (02 §2.4, 09 §9.3), inside `space_id` — and "
                f"`hold collection create` is where it is set")
    if "model" in raw:
        # No alias (operator, 2026-10-08): one `model` named the document
        # side and the query side guessed — the guess is what sent the
        # article encoder's name to a query endpoint (`embeddings._side`).
        raise ModelKeys(
            f"{where}: `model` is not a key of [encoder] since 2026-10-08 (07 §7.8): "
            f"name `embed_model` (documents) and `query_model` (questions) — both "
            f"the same model when one does both; {MIGRATE_HINT}")
    if not raw.get("url"):
        raise ValueError(f"{where}: the block requires url (missing 'url')")
    url = str(raw["url"]).rstrip("/")
    query_url = str(raw["query_url"]).rstrip("/") if raw.get("query_url") else None
    embed_model = _named_model(raw, "embed_model", where)
    query_model = _named_model(raw, "query_model", where)
    _agrees(url, "embed_model", embed_model, "url", where)
    _agrees(query_url or url, "query_model", query_model,
            "query_url" if query_url else "url", where)
    return EmbedRecipe(
        url=url,
        api=str(raw.get("api", "openai")),
        model=embed_model,
        dtype=str(raw.get("dtype", "f16")),
        ctx=int(raw.get("ctx", 512)),
        dim=int(raw["dim"]) if raw.get("dim") is not None else None,
        query_url=query_url,
        query_model=query_model,
        concurrency=max(1, int(raw.get("concurrency", 1))),
        batch=_batch(raw, where),
        exactness=_exactness(raw, where),
        device=(str(raw["device"]).lower()
                if raw.get("device") in ("gpu", "cpu", "GPU", "CPU")
                else None),
    )


@dataclass(frozen=True)
class RerankRecipe:
    """Where this node reaches a reranker for a collection (05 §5.6): a
    query-time choice of the asking peer, node-local, never named by a
    Codex. `url` is the full endpoint. `api` is its wire format:
    "tei" — {"query", "texts"} -> [{"index", "score"}], RERANK_URL's
    format — or "jina" — {"query", "documents"} -> {"results": [{"index",
    "relevance_score"}]}, what llama.cpp's /v1/rerank serves."""
    url: str
    api: str = "tei"
    # the file's `rerank_model` (07 §7.8, amended 2026-10-08), sent as the
    # body's `model`; None only for RERANK_URL, which names no model
    model: str | None = None
    # the longest candidate it reads, in ITS tokens (05 §5.6, amended
    # 2026-10-21): a longer one is cut before it is sent, so the model's
    # context is the window and not the longest chunk a corpus holds.
    # Measured on live searches: gutenberg p99 1,170 tokens (max 3,335),
    # wikipedia p99 584 — 2,048 reads 99 % of candidates whole. 0: uncut.
    window: int = 2048
    # 05 §5.6 (amended 2026-10-31, operator's B): this reranker's score as
    # a probability of relevance, fitted on judged candidates of THIS
    # collection; what lets its list be merged with other collections' on
    # relevance instead of rank. None: merged by rank.
    calibration: "Calibration | None" = None


@dataclass(frozen=True)
class Calibration:
    """PLATT SCALING (Platt 1999): P(relevant | score) = 1 / (1 + e^-(a·score + b)),
    fitted on a collection's own judged candidates by
    tools/measure/fusion_calibration.py (docs/notes/federated-merge.md).
    It names the reranker it was fitted for: under another model it is
    not used, and the search says so (`merge_withheld`). Isotonic regression
    (Zadrozny & Elkan 2002) is the non-parametric alternative, kept for
    when the judged set is large enough to beat two parameters."""
    a: float
    b: float
    rerank_model: str
    judged: int = 0          # candidates it was fitted on
    set: str = ""            # the judged set's name

    def probability(self, score: float) -> float:
        z = self.a * float(score) + self.b
        # the two branches keep exp() from overflowing at either end
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-z))
        e = math.exp(z)
        return e / (1.0 + e)


def parse_calibration(raw: Any, *, where: str) -> Calibration | None:
    """`[reranker] calibration = {a, b, rerank_model, judged?, set?}` ->
    Calibration; None when the table names none."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: [reranker] calibration is a table {{a, b, rerank_model}}")
    try:
        a, b = float(raw["a"]), float(raw["b"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{where}: [reranker] calibration needs numbers a and b") from None
    if not (math.isfinite(a) and math.isfinite(b)) or a <= 0:
        # a ≤ 0 would rank a higher score as LESS relevant: not a calibration
        raise ValueError(f"{where}: [reranker] calibration needs a finite a > 0 and a finite b, "
                         f"got a={raw['a']!r}, b={raw['b']!r}")
    model = raw.get("rerank_model")
    if not isinstance(model, str) or not model:
        raise ValueError(f"{where}: [reranker] calibration names the rerank_model it was fitted for")
    judged = raw.get("judged", 0)
    if not isinstance(judged, int) or isinstance(judged, bool) or judged < 0:
        raise ValueError(f"{where}: [reranker] calibration judged is a count, got {judged!r}")
    return Calibration(a=a, b=b, rerank_model=model, judged=judged, set=str(raw.get("set", "")))


RERANK_APIS = ("tei", "jina")


def parse_reranker(raw: Any, *, where: str) -> RerankRecipe | None:
    """A collection file's `[reranker]` table -> RerankRecipe; None when
    the file declares none."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw.get("url"):
        raise ValueError(f"{where}: [reranker] needs a url")
    api = str(raw.get("api", "tei")).lower()
    if api not in RERANK_APIS:
        raise ValueError(f"{where}: [reranker] api must be one of "
                         f"{', '.join(RERANK_APIS)}, got {api!r}")
    if "model" in raw:
        raise ModelKeys(f"{where}: [reranker] `model` is now `rerank_model` (07 §7.8, "
                        f"amended 2026-10-08); {MIGRATE_HINT}")
    model = _named_model(raw, "rerank_model", f"{where} [reranker]")
    _agrees(str(raw["url"]), "rerank_model", model, "url", f"{where} [reranker]")
    window = raw.get("window", RerankRecipe.window)
    if not isinstance(window, int) or isinstance(window, bool) or window < 0:
        raise ValueError(f"{where}: [reranker] window is a number of tokens (0: uncut), got {window!r}")
    return RerankRecipe(url=str(raw["url"]), api=api, model=model, window=window,
                        calibration=parse_calibration(raw.get("calibration"), where=where))


def reranker_for(collection) -> RerankRecipe | None:
    """THE way to resolve a collection's reranker: its own `[reranker]`,
    then the node's RERANK_URL (TEI format), else none — and a list with
    no reranker keeps its fused order (05 §5.6)."""
    own = getattr(collection, "reranker", None) if collection is not None else None
    if own is not None:
        return own
    return RerankRecipe(url=config.RERANK_URL) if config.RERANK_URL else None


def recipe_for(collection) -> EmbedRecipe:
    """THE way to resolve a collection's embedding recipe: its own file's
    `[encoder]` (a Codex file's carries the Codex's space — `dtype`, `ctx`
    and `dim` — already, `_codex_encoder`), and nothing else.

    There used to be two fallbacks behind it: the node's per-plugin
    `[encoder_defaults]`, then EMBED_URL. "A default embedder is wrong …
    A collection that does not specify an embedder is a non compliant
    collection" (operator, 2026-09-18): `load_all` refuses such a file,
    and anything else that reaches here without an encoder is told so."""
    if collection is None:
        # the single-collection call sites that predate federation name no
        # collection and mean the default one — never "no encoder"
        collection = default_collection()
    embed = getattr(collection, "embed", None)
    if embed is None:
        raise NoEncoder(f"{getattr(collection, 'name', 'this collection')}: {NO_ENCODER}")
    return embed


# What the worker may SPEND on a collection's local encoder (never what
# retrieval uses — amendment 6). `embed_mode` in the collection TOML is the
# Tier-2 desired state; the Library's override is Tier 3 and lasts until the
# worker next applies the TOML (restart, or a TOML edit) — invariant 10.
EMBED_MODES = ("off", "lazy", "all", "p2p")
DEFAULT_EMBED_MODE = "lazy"
#: the modes under which this node's encoder embeds at all, and the two
#: under which it backfills: `all` every part itself ("all local"), `p2p`
#: the parts rendezvous gives it, publishing as it goes ("all p2p"; 04 §4.5,
#: 07 §7.8, operator 2026-10-19). The Library shows the display names; the
#: file and the API carry these tokens, as `lazy` for on-demand.
EMBEDDING_MODES = ("lazy", "all", "p2p")
BACKFILL_MODES = ("all", "p2p")
EMBED_MODE_LABELS = {"off": "off", "lazy": "on-demand", "all": "all local", "p2p": "all p2p"}


@dataclass(frozen=True)
class Collection:
    name: str
    plugin: str
    profile: str | None
    embed_mode_default: str | None      # TOML `embed_mode`; None = DEFAULT_EMBED_MODE
    corpus_dir: Path
    manifest_path: Path
    fts_dir: Path
    lance_dir: Path
    # app/acquire/ — both default off/unset so an existing hand-populated
    # deployment (no collections/*.toml, or a TOML predating P0.4) sees zero
    # behavior change and no surprise outbound network access on restart.
    acquire_enabled: bool = False
    license_ack: str | None = None   # "<iso8601>/<sha256-of-terms-text>", Tier-2 only (invariant 10)
    embed: EmbedRecipe | None = None  # [embed] block; None = env fallback
    # [reranker] block (05 §5.6); None = RERANK_URL, else no reranking
    reranker: RerankRecipe | None = None
    # Declarative reuse of published embeddings (§8 genesis): the worker
    # drains this archive automatically before spending GPU on backfill.
    genesis_archive: str | None = None
    # Opted-in EXTRA formats from the acquisition source (spec.extras) —
    # a LOCAL convenience (gutenberg: epubs beside the text), never part
    # of the p2p exchange. TOML: extras = ["epub"].
    extras: tuple[str, ...] = ()
    # PEER PLANE: a magnet URI or .torrent path. Set, acquisition fetches
    # this collection's corpus from a peer's torrent instead of the origin
    # — same plugin, profiles, license gate; the torrent's piece hashes
    # stand in for origin checksums (engine.effective_spec). TOML:
    # peer_source = "magnet:?xt=...&x.pe=host:port".
    peer_source: str | None = None
    # WHERE THE DOCUMENTS ARE READ FROM (07 §7.8 `source`, amended
    # 2026-10-07): "origin" — the plugin's corpus, as always; "shard" — the
    # record shards this peer holds and did not build (`share fetch`), read
    # by app/plugins/shard.py through the same discover/fetch/parse.
    source: str = "origin"
    # WHICH dated snapshot of the origin this collection holds, for sources
    # that publish them (Wikipedia dumps, a PubMed baseline year).
    #
    # None means "whatever the origin calls latest", which is what every
    # collection did before this existed and is deliberately still the
    # default — but it is NOT reproducible: the same URL serves different
    # bytes over time, so a corpus fetched under it cannot be torrented,
    # cached, adopted, or compared with a peer's, and the collection cannot
    # answer the one question a freshness feature has to ask, which is "how
    # old am I?". Pinning a date fixes all of that at the cost of an
    # explicit decision to move.
    snapshot: str | None = None
    # Chunker LAYERS (amendment 41): None = the one implicit layer, the
    # plugin's chunker — every collection that predates layers. TOML:
    # [[layers]] chunker = "verbatim" rule = "archive:<id>" ... last "default".
    layers: tuple | None = None
    # THE CODEX, when this collection is run from one (07 §7.8's `codex`
    # key). None for every legacy TOML, which is every collection that
    # predates step 8 — so nothing that does not name a Codex changes.
    # Where it is set it is the IDENTITY: `space_id`, layers, plugin and
    # licence all come from it, and `spaces.validate_boot` pins the vector
    # table against `codex.space_id` rather than the legacy preimage.
    codex: Any = None
    # 07 §7.8's collection file. Set from a Codex TOML; a legacy file
    # leaves the defaults, which are what a legacy collection always was.
    title: str | None = None            # this peer's own listing; never published
    slices: Any = None                  # "all" | () | None when the file does not say
    kinds: tuple[str, ...] = ("records", "vectors")   # 07 §7.7's default
    share: bool = False
    # `share <collection> --slices … --kinds …` (07 §7.8 `share_slices`,
    # `share_kinds`; 04 §4.3): what serving is narrowed to; None = all held
    share_slices: tuple[str, ...] | None = None
    share_kinds: tuple[str, ...] | None = None
    # 07 §7.8 `embed_slices` (operator, 2026-10-31: wikidata "hold all 14 and
    # embed 9"): the slices the node's encoder spends on, out of those held;
    # None = every slice held. A document outside them is marked "not
    # embedded here" by app/slice_scope.py and never offered to the encoder
    embed_slices: tuple[str, ...] | None = None
    #: why `embed_mode_default` is not what the file says: `embed = "p2p"`
    #: with `share = false` runs as `all` — p2p publishes what it embeds, and
    #: a node that does not share can publish nothing (07 §7.8, 2026-10-19)
    embed_mode_note: str | None = None
    #: set on a PART of a split vector store only (app/vector_parts.py
    #: `view`): the value of the table key whose table `lance_dir` holds.
    #: Never read from a file.
    vector_part: str | None = None

    def plugin_instance(self) -> CorpusSource:
        inner = PLUGIN_REGISTRY[self.plugin]
        if self.source == "shard":
            from .plugins.shard import ShardSource
            from .hold import integrity
            shard_tree = Path(self.corpus_dir)
            skip = integrity.violations(shard_tree.parent.parent, shard_tree.parent.name, self.codex)
            return ShardSource(inner, self.corpus_dir, frozenset(skip))
        return inner


def _control_fallback() -> Collection:
    """Where `db.connect()` writes when the node holds NO collection — never
    listed, never run, never served. Not a collection: the legacy
    single-collection default it was, gutenberg over CORPUS_DIR/DATA_DIR,
    left with the pre-Codex shape on 2026-10-08 (operator)."""
    return Collection(
        name="control",
        plugin="gutenberg",
        profile=None,
        embed_mode_default=None,
        corpus_dir=config.CORPUS_DIR,
        manifest_path=config.DATA_DIR / "manifest.db",
        fts_dir=config.DATA_DIR / "fts",
        lance_dir=config.DATA_DIR / "lancedb",
        acquire_enabled=False,
        license_ack=None,
    )


def _config_dir() -> Path:
    return Path(os.getenv("CONFIG_DIR", "."))


def louise_root() -> Path:
    """`<LOUISE_ROOT>` — the one directory of 07 §7.8, whose
    `collections/<name>/` holds a Codex collection's SHARD, state, index
    and vector table. Defaults to `DATA_DIR` so a node that never sets it
    behaves exactly as before."""
    import os
    value = os.getenv("LOUISE_ROOT")
    return Path(value) if value else config.DATA_DIR


def _codex_collection(path: Path, raw: dict[str, Any], name: str) -> Collection:
    """A collection the worker runs FROM ITS CODEX (07 §7.8).

    The bridge between the `hold` tree and the running node, and the one
    piece that stood between "migrated" and "in use": step 8 produced
    complete, verified Codex collections under
    `<LOUISE_ROOT>/collections/<name>/` that nothing in the worker could
    see, because `Collection` was built entirely from a legacy TOML's
    `[paths]` block.

    A TOML that names `codex` gets §7.8's tree instead — `state/`,
    `index/`, `lance/` beside the SHARD — and its plugin, layers and
    corpus come from the Codex rather than from the file, because the
    Codex is the identity and a file that could disagree with it would be
    a second answer to the same question.

    THE HASH IS CHECKED, not trusted. §7.8: a collection whose
    `shard/codex.json` has an inner hash other than its file's `codex` "is
    not served, not shared and not searched". That is a per-collection
    refusal, so it is raised here and the caller keeps its other
    collections.
    """
    from .hold import codex as cx

    want = str(raw["codex"])
    want = want if want.startswith("sha256:") else "sha256:" + want
    d = louise_root() / "collections" / name
    p = d / "shard" / "codex.json"
    if not p.is_file():
        raise ValueError(
            f"{path}: names codex {want[:23]}… and there is no {p}. A "
            f"collection's Codex is the first line of its SHARD (07 §7.8); "
            f"without it there is nothing to run")
    codex = cx.loads(p.read_bytes())
    if codex.collection_id != want:
        raise ValueError(
            f"{path}: names codex {want[:23]}… and {p} is "
            f"{codex.collection_id[:23]}…. A name holds one collection for "
            f"its life (07 §7.8); this one is not served, shared or searched "
            f"until they agree")

    if cx.is_annotations(codex):
        return _annotations_collection(path, raw, name, codex, d)

    # 07 §7.8's file is `codex`, `title`, `slices`, `kinds`, `embed`,
    # `share` and `[encoder]`. Everything else a Codex file used to say is
    # an answer the Codex or the tree already gives — the archive is in
    # `credits`, the dump in `origin.spec`, the space in `space`, the origin
    # beside the SHARD — so those keys are still READ (the files on disk
    # predate the shape) but derived when absent (`codex_defaults`).
    #
    # A file in the OLD shape keeps the old defaults for what it leaves
    # out: a file that never said `slices`, `kinds`, `share` or `title` is
    # read exactly as it was before this existed, so no deployed collection
    # starts fetching, importing or moving on upgrade.
    where = str(path)
    _warn_deprecated(path, raw)
    new_shape = any(k in raw for k in _SPEC_FILE_ONLY_KEYS)
    kinds = _parse_kinds(raw.get("kinds"), where)
    title = raw.get("title")
    if title is not None and not isinstance(title, str):
        raise ValueError(f"{where}: title is a string (07 §7.8), got {title!r}")
    share = raw.get("share", False)
    if not isinstance(share, bool):
        raise ValueError(f"{where}: share is true or false (07 §7.8), got {share!r}")
    share_slices = _parse_share_list(raw.get("share_slices"), "share_slices", where)
    share_kinds = _parse_share_list(raw.get("share_kinds"), "share_kinds", where)
    embed_slices = _parse_share_list(raw.get("embed_slices"), "embed_slices", where)
    if share_kinds is not None:
        bad = [k for k in share_kinds if k not in SHARE_KINDS]
        if bad:
            raise ValueError(f"{where}: share_kinds names {bad}; the member kinds are "
                             f"{', '.join(SHARE_KINDS)} (02 §2.6)")
    slices = None
    if "slices" in raw:
        if "profile" in raw:
            raise ValueError(
                f"{where}: slices and profile are both given. `slices` is 07 "
                f"§7.8's key and `profile` the older spelling it replaces; "
                f"keep one")
        slices = _parse_slices(raw["slices"], where)
    source = str(raw.get("source") or "origin")
    if source not in ("origin", "shard"):
        raise ValueError(f"{where}: source is \"origin\" or \"shard\" (07 §7.8), got {raw.get('source')!r}")
    defaults = codex_defaults(codex, name, slices, kinds, source)
    if slices is None and raw.get("profile") and raw["profile"] == defaults["everything"]:
        # the profile that selects every slice IS `slices = "all"`
        slices = "all"
        defaults = codex_defaults(codex, name, slices, kinds, source)
    profile = raw["profile"] if raw.get("profile") else defaults["profile"]

    if "acquire_enabled" in raw:
        acquire = bool(raw["acquire_enabled"])
    else:
        acquire = defaults["acquire_enabled"] if new_shape else False
    if acquire and source == "shard":
        raise ValueError(
            f"{where}: acquire_enabled = true with source = \"shard\": the SHARD IS this "
            f"collection's corpus, and acquiring from the origin would write the "
            f"origin's files into it. A fetched collection is held, not acquired "
            f"(07 §7.8)")

    credited = _credited_archives(codex)
    if "genesis_archive" in raw:
        genesis = raw["genesis_archive"] or None
        if genesis and genesis not in credited:
            raise ValueError(
                f"{where}: genesis_archive = {genesis!r} and the Codex credits "
                f"{', '.join(credited) or 'no archive'}. An archive's vectors "
                f"are admitted only when `credits` names it (03 §3.5), so "
                f"this collection would import what it must refuse")
    else:
        genesis = defaults["genesis_archive"] if new_shape else None

    snapshot = defaults["snapshot"]
    if raw.get("snapshot"):
        stated = str(raw["snapshot"])
        if snapshot and stated != snapshot:
            raise ValueError(
                f"{where}: snapshot = {stated!r} and the Codex pins "
                f"{snapshot!r} (09 §9.17: a dated-dump origin's `files` are "
                f"the dump pinned). A name holds one collection for its life "
                f"(07 §7.8)")
        snapshot = stated

    if source == "shard":
        # the documents are the record shards held: the SHARD is the corpus
        # (app/plugins/shard.py), and the scan cursor keys on it
        corpus = d / "shard"
    elif raw.get("corpus_dir"):
        corpus = Path(raw["corpus_dir"])
    else:
        corpus = defaults["corpus_dir"] if new_shape else config.CORPUS_DIR

    embed_mode, embed_note = effective_embed_mode(
        _embed_mode(raw.get("embed", raw.get("embed_mode")), path), share, where)
    from . import layers as layers_mod
    return Collection(
        name=name,
        # FROM THE CODEX, not the file: plugin and layers are inside
        # `collection_id`, so a TOML that set them could name a collection
        # and then chunk it differently — invariant 1's failure mode, and
        # undetectable, because both peers computed the id honestly.
        plugin=codex.plugin,
        profile=profile,
        embed_mode_default=embed_mode,
        embed_mode_note=embed_note,
        corpus_dir=corpus,
        manifest_path=d / "state" / "manifest.sqlite",
        fts_dir=d / "index",
        lance_dir=d / "lance",
        acquire_enabled=acquire,
        license_ack=_codex_ack(codex, d / "state"),
        embed=_codex_encoder(raw.get("embed_recipe") or raw.get("encoder"),
                             codex, where),
        reranker=parse_reranker(raw.get("reranker"), where=where),
        genesis_archive=genesis,
        extras=tuple(raw.get("extras") or ()),
        snapshot=snapshot,
        layers=layers_mod.layers_for(codex),
        codex=codex,
        title=title,
        slices=slices,
        kinds=kinds,
        share=share,
        share_slices=share_slices,
        share_kinds=share_kinds,
        embed_slices=embed_slices,
        source=source,
    )


def _annotations_collection(path: Path, raw: dict[str, Any], name: str, codex,
                            d: Path) -> Collection:
    """An ANNOTATIONS collection (02 §2.10) as the node holds it: a SHARD and
    a collection file like any other, and NOTHING for the worker to run —
    no origin to acquire, no text to index, no space to embed into, so no
    `[encoder]`. `load_all` (what the worker ingests and a search ranks)
    leaves it out; `load_annotations` lists it, and sharing, `hold
    collection show` and the facts a search attaches read it from there."""
    where = str(path)
    for key in ("encoder", "embed_recipe", "reranker"):
        if raw.get(key):
            raise ValueError(f"{where}: [{key}] in an annotations collection's file: it has no "
                             f"space and nothing is embedded or ranked for it (02 §2.10)")
    title = raw.get("title")
    if title is not None and not isinstance(title, str):
        raise ValueError(f"{where}: title is a string (07 §7.8), got {title!r}")
    share = raw.get("share", False)
    if not isinstance(share, bool):
        raise ValueError(f"{where}: share is true or false (07 §7.8), got {share!r}")
    share_slices = _parse_share_list(raw.get("share_slices"), "share_slices", where)
    share_kinds = _parse_share_list(raw.get("share_kinds"), "share_kinds", where)
    kinds = _parse_kinds(raw.get("kinds", ["records", "relations"]), where)
    slices = _parse_slices(raw["slices"], where) if "slices" in raw else "all"
    return Collection(
        name=name, plugin=codex.plugin, profile=None, embed_mode_default="off",
        corpus_dir=d / "shard", manifest_path=d / "state" / "manifest.sqlite",
        fts_dir=d / "index", lance_dir=d / "lance", acquire_enabled=False,
        license_ack=_codex_ack(codex, d / "state"), embed=None, codex=codex,
        title=title, slices=slices, kinds=kinds, share=share,
        share_slices=share_slices, share_kinds=share_kinds,
        source="shard")


def is_annotations(collection) -> bool:
    """An annotations collection (02 §2.10): held, checked and shared like any
    collection, and never ingested, embedded or ranked."""
    from .hold import codex as cx
    return cx.is_annotations(getattr(collection, "codex", None))


def load_annotations() -> list:
    """The annotations collections this node holds: the files in
    `collections/` whose Codex annotates another collection. One that
    cannot load is left out here and named by `load_all`'s caller the way
    any unreadable file is — this listing never raises, because it runs
    beside every search and every seeding beat."""
    cfg_dir = collections_dir()
    out: list[Collection] = []
    if not cfg_dir.is_dir():
        return out
    for p in sorted(cfg_dir.glob("*.toml")):
        try:
            c = _load_one(p)
        except Exception:                               # noqa: BLE001
            continue
        if is_annotations(c):
            out.append(c)
    return out


#: 07 §7.8 spells the middle mode `on-demand`; the code has always called
#: it `lazy`, and `EMBED_MODES` is stored in the runtime-override table and
#: read by the worker and the UI. The documents fix the vocabulary
#: (CLAUDE.md), so a Codex TOML is written in THEIR word and canonicalised
#: here rather than the rename being done under a running node mid-
#: migration. Retiring `lazy` belongs with the other old names, step 12.
_EMBED_MODE_ALIASES = {"on-demand": "lazy", "all local": "all", "all-local": "all",
                       "all p2p": "p2p", "all-p2p": "p2p"}


def _embed_mode(value, path: Path) -> str | None:
    if value is None:
        return None
    mode = _EMBED_MODE_ALIASES.get(str(value), str(value))
    if mode not in EMBED_MODES:
        raise ValueError(
            f"{path}: embed must be one of off | on-demand | all | p2p "
            f"(07 §7.8), got {value!r}")
    return mode


def effective_embed_mode(mode: str | None, share: bool, where: str) -> tuple[str | None, str | None]:
    """`(the mode the node runs, why it is not the file's)`: `p2p` with
    sharing off runs as `all` — the Library asks before writing either
    pair, so this is for a file edited by hand (07 §7.8, 2026-10-19)."""
    if mode == "p2p" and not share:
        return "all", (f"{where}: embed = \"p2p\" with share = false — p2p embedding publishes what it "
                       f"embeds and sharing is off, so the node embeds as all local until one of the "
                       f"two changes")
    return mode, None


#: 02 §2.6: "The kinds are closed, and `--kinds` (07 §7.3) takes their names."
MEMBER_KINDS = ("records", "vectors", "buckets", "index", "relations", "figures",
                "bundle", "centres", "calibration", "terms", "seals", "origin")
#: 07 §7.7: "the default is records and vectors".
DEFAULT_KINDS = ("records", "vectors")
#: Keys a Codex file carried before 07 §7.8 spelled the file out. Each is
#: still read; `collection_file._write_codex_toml` writes one back only where the Codex
#: and the tree do not give the same answer.
DEPRECATED_CODEX_KEYS = ("profile", "acquire_enabled", "genesis_archive",
                         "snapshot", "corpus_dir", "extras")
#: The keys only 07 §7.8's shape has: a file with any of them is read by
#: that shape's rules.
_SPEC_FILE_ONLY_KEYS = ("slices", "kinds", "share", "title")
#: the member kinds `share --kinds` may narrow to (02 §2.6; 09 §9.16)
SHARE_KINDS = ("records", "relations", "figures", "vectors", "bundle", "index")


def _parse_share_list(value, key: str, where) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"{where}: {key} is a list of names (07 §7.8), got {value!r}")
    return tuple(value)
_WARNED: set[tuple[str, str]] = set()


def origin_dir(name: str) -> Path:
    """`<LOUISE_ROOT>/collections/<name>/origin/` — the origin files a
    collection's records are parsed from: what acquisition fetched (06
    §6.2's corpus directory), kept after the records are written so they
    can be re-fetched and checked (invariant 11). Beside `shard/`, `state/`,
    `index/` and `lance/`, so one root holds everything about a collection
    (operator, 2026-09-15). The bytes may live anywhere: on the reference
    node this path is a mount of the dataset that holds them."""
    return louise_root() / "collections" / name / "origin"


def codex_at(name: str):
    """The Codex a held name's SHARD carries."""
    from .hold import codex as cx
    return cx.loads((louise_root() / "collections" / name / "shard"
                     / "codex.json").read_bytes())


def _credited_archives(codex) -> list[str]:
    return [c.archive for c in codex.credits if c.kind == "archive" and c.archive]


def codex_defaults(codex, name: str, slices, kinds, source: str = "origin") -> dict[str, Any]:
    """What a Codex file's older keys come to when the file does not state
    them — each an answer the Codex or the tree already gives.

    ONE derivation for the loader and the writer, so a rewrite drops a key
    exactly when loading the file back without it gives the same value.

    - `profile`: the acquisition spec's spelling of `slices = "all"` — the
      profile that selects everything. Profiles are local (09 §9.17); the
      file names the selection. A LIST of slices the origin delivers whole
      (`spec.slice_profiles`) is spelt by their profiles, combined: what
      `records fetch --slice` recorded, so the monthly cycle continues it
      (07 §7.3, amended 2026-10-25). A list it does not deliver whole — a
      `share fetch` selection of parts — derives none, as before.
    - `acquire_enabled`: the node fetches what the file holds — every
      slice, or the slices the origin delivers whole, records among the
      kinds — from an origin the plugin can fetch.
      Never when `source = "shard"`: the SHARD is the corpus, and a fetched
      collection acquiring from the origin would write the origin's files
      into it — the second peer's worker started an rsync of the whole
      Gutenberg mirror for three fetched books (2026-10-07).
    - `genesis_archive`: the archive `credits` names (03 §3.5 admits no
      other), when this peer holds vectors.
    - `snapshot`: the dump the Codex pins in `origin.spec.files`.
    - `corpus_dir`: `origin_dir(name)`; the SHARD for `source = "shard"`.
    """
    from .acquire import spec as acquire_spec
    from .acquire.specs import ACQUIRE_REGISTRY
    spec = ACQUIRE_REGISTRY.get(codex.plugin)
    everything = acquire_spec.everything_profile(spec) if spec is not None else None
    credited = _credited_archives(codex)
    if slices == "all":
        profile = everything
    elif slices and spec is not None and source != "shard":
        # a fetched copy's list is parts taken from peers, never an origin fetch
        profile = acquire_spec.profile_for_slices(spec, slices)
    else:
        profile = None
    return {
        "everything": everything,
        "profile": profile,
        "acquire_enabled": bool(profile and "records" in kinds and source != "shard"),
        "genesis_archive": credited[0] if credited and "vectors" in kinds else None,
        "snapshot": (acquire_spec.pinned_snapshot(spec, codex.origin.spec)
                     if spec is not None else None),
        "corpus_dir": origin_dir(name).parent / "shard" if source == "shard" else origin_dir(name),
    }


def _parse_slices(value: Any, where: str):
    if value == "all":
        return "all"
    if isinstance(value, list) and all(isinstance(v, str) and v for v in value):
        # a list is PART of the collection, held by `share fetch --slice`
        # (07 §7.3, migration step 9d); each value a slice name of 09 §9.3
        from .hold import shard as shard_mod
        out: list[str] = []
        for v in value:
            try:
                shard_mod.check_slice(v)
            except Exception as e:   # noqa: BLE001 — the loader's errors are ValueErrors
                raise ValueError(f"{where}: slices = {value!r}: {e}") from None
            if v not in out:
                out.append(v)
        return tuple(out)
    raise ValueError(f'{where}: slices is "all" or a list of slice values '
                     f"(07 §7.8), got {value!r}")


def _parse_kinds(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_KINDS
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where}: kinds is a list of member kinds (07 §7.8), "
                         f"got {value!r}")
    unknown = [k for k in value if k not in MEMBER_KINDS]
    if unknown:
        raise ValueError(f"{where}: kinds {unknown!r} — 02 §2.6's kinds are "
                         f"closed: {', '.join(MEMBER_KINDS)}")
    return tuple(value)


def _codex_encoder(raw: Any, codex, where: str) -> EmbedRecipe | None:
    """`[encoder]` on a Codex file: WHERE this node reaches the Codex's
    encoder (07 §7.8), never what the space is. `dtype`, `ctx` and `dim`
    are the space's (02 §2.4): a file that still states one must state the
    Codex's, or it is a second answer and the collection is refused."""
    if not isinstance(raw, dict) or codex.space is None:
        # no space (an annotations Codex, 02 §2.10): nothing to reach
        return None
    space = codex.space
    for key, have in (("dtype", space.dtype), ("ctx", space.ctx), ("dim", space.dim)):
        if key in raw and str(raw[key]) != str(have):
            raise ValueError(
                f"{where}: [encoder] {key} = {raw[key]!r} and the Codex's space "
                f"says {have!r}. The space is the Codex's (02 §2.4); this "
                f"table names only where this node reaches it (07 §7.8)")
    # `embed_model` and `query_model` are the FILE's (07 §7.8, amended
    # 2026-10-08): the Codex's `space.encoder.label` used to stand in for a
    # missing `model`, and it named the document side only
    recipe = parse_recipe({**raw, "dtype": space.dtype, "ctx": space.ctx, "dim": space.dim},
                          where=f"{where} [encoder]")
    # The query side of the space, from the Codex and only from it: the same
    # instruction calibration and the gate prefix with (hold/calibration.py,
    # hold/embedder.py), so search runs the function the gate passed.
    # and the cut, the space's when it declares one (no live Codex does)
    return replace(recipe, query_instruction=space.query.instruction or None,
                   cut=getattr(space, "cut", None) or "characters")


def _warn_deprecated(path: Path, raw: dict) -> None:
    import sys
    for key in DEPRECATED_CODEX_KEYS:
        if key in raw and (str(path), key) not in _WARNED:
            _WARNED.add((str(path), key))
            print(f"[warn:config] {path}: `{key}` is not a key of 07 §7.8's "
                  f"collection file — still read; a rewrite keeps it only "
                  f"where the Codex and the tree do not give the same answer",
                  file=sys.stderr, flush=True)


def _codex_ack(codex, state_dir: Path) -> str | None:
    """The acknowledgement `hold collection ack` recorded (09 §9.13),
    in the shape `licensing.state_for` compares — read, never decided
    here; `app/licensing.py` is THE resolution."""
    if codex.license.cls == "green":
        return None
    import json
    p = state_dir / "ack.json"
    if not p.is_file():
        return None
    try:
        ack = json.loads(p.read_text(encoding="utf-8"))
        return f"{ack['time']}/{ack['terms_sha256'].replace('sha256:', '')}"
    except (OSError, ValueError, KeyError):
        return None


NOT_CONVERTED = ("not converted: the file declares no `codex`, and a node serves only Codex "
                 "collections (07 §7.8; the pre-Codex shape left on 2026-10-08). It is served "
                 "once migrated and its file names its collection_id")


class NotConverted(ValueError):
    """A collection file that names no Codex: refused, never fatal."""


def _load_one(path: Path) -> Collection:
    with open(path, "rb") as f:
        raw: dict[str, Any] = tomllib.load(f)
    name = raw.get("name") or path.stem
    if not raw.get("codex"):
        raise NotConverted(NOT_CONVERTED)
    return _codex_collection(path, raw, name)


def config_toml_path(name: str) -> Path:
    """Where a collection's Tier-2 definition file lives (whether or not it
    exists yet). The file existing IS the collection existing — creation and
    removal act on this path, never on a database row."""
    return _config_dir() / "collections" / f"{name}.toml"


def recipe_path(plugin: str) -> Path:
    """Where this node keeps its recipe for a plugin (06 §6.6; 07 §7.8,
    amended 2026-10-25): `$CONFIG_DIR/recipes/<plugin>.toml`, desired state
    an operator writes once. The Library's *new collection* names a plugin
    and a local name, and the node creates it from this file (RULE 2): the
    space, the work budget and the description are the creator's choices,
    made on the command line or in this file, never in a dialog."""
    return _config_dir() / "recipes" / f"{plugin}.toml"


def collections_dir() -> Path:
    """`$CONFIG_DIR/collections` — the TOML files.

    NOT `louise_root()/"collections"`, which is a DIFFERENT directory
    holding each Codex collection's `shard/state/index/lance` tree. Both
    are called "collections" and on this deployment they are `/config/
    collections` and `/data/collections`. Retiring a file means moving it
    out of THIS one.
    """
    return _config_dir() / "collections"


@dataclass(frozen=True)
class Refusal:
    """A collection whose file is present and which is NOT served.

    A refusal is never silent and never fatal: the node keeps its other
    collections, and this record is what the log line and the System page
    name so the operator can see WHY something they can see on disk is
    not in the Library.
    """
    name: str
    path: Path
    reason: str


_REFUSALS: tuple[Refusal, ...] = ()


def refusals() -> tuple[Refusal, ...]:
    """What the last `load_all()` refused to serve. Recomputed by every
    `load_all()`, so it is as fresh as the listing beside it."""
    return _REFUSALS


def load_all(*, skip_unreadable: bool = False):
    """The collections this node has: exactly the Codex collections whose
    files are in `collections/`, possibly none — and none when the
    directory is absent. (Until 2026-10-08 an absent directory meant one
    synthesized legacy gutenberg; the pre-Codex shape is gone.) A file that
    names no Codex is refused, said in `refusals()`, never served.

    `skip_unreadable` is THE WORKER'S round loop and nothing else: it
    returns `(served, [(name, reason)…])` instead of raising, so one bad
    TOML refuses its own collection rather than every collection's round
    (invariant 1's rule, applied to the other way a collection can be
    unrunnable). Every other caller wants the raise — see below.
    """
    cfg_dir = collections_dir()
    global _REFUSALS
    if not cfg_dir.is_dir():
        _REFUSALS = ()                  # as fresh as the listing: nothing there, nothing refused
        return ([], []) if skip_unreadable else []
    served: list[Collection] = []
    refused: list[Refusal] = []
    unreadable: list[tuple[str, str]] = []
    for p in sorted(cfg_dir.glob("*.toml")):
        name = p.stem
        # A MALFORMED file still raises, and that is deliberate. "A typo
        # must not silently become the default"
        # (tests/test_config_tiers_embed_mode.py) — an operator who wrote
        # `embed_mode = "always"` must be told, not quietly served a node
        # missing a collection. Refusal below is a POLICY decision the
        # operator asked for; a parse error is their mistake, and the two
        # must not be reported the same way.
        try:
            c = _load_one(p)
        except (NotConverted, ModelKeys) as e:
            # a POLICY refusal, like NO_ENCODER below: a file whose
            # [encoder]/[reranker] does not name its models as 07 §7.8 says
            # (2026-10-08) is not served, and the node keeps its others
            refused.append(Refusal(name, p, str(e)))
            continue
        except Exception as e:                          # noqa: BLE001
            if not skip_unreadable:
                raise
            unreadable.append((name, f"{type(e).__name__}: {e}"))
            continue
        if is_annotations(c):
            # 02 §2.10: held, and nothing for the worker or a ranked list —
            # `load_annotations` is where it is listed
            continue
        if c.embed is None:
            # NON-COMPLIANT, and said so where the operator looks (the
            # System page's refusals, `hold collection show`), never a
            # default quietly standing in for the encoder it did not name
            refused.append(Refusal(name, p, NO_ENCODER))
            continue
        served.append(c)
    _REFUSALS = tuple(refused)
    return (served, unreadable) if skip_unreadable else served


def is_private(collection) -> bool:
    """A deposit collection (02 §2.9): never returned by an open route —
    never-share is not only about peers, and a guest on the LAN is not the
    depositor. Searched only with the deposit token or the admin token
    (07 §7.4)."""
    codex = getattr(collection, "codex", None)
    return codex is not None and codex.plugin == "deposit"


def load_public() -> list:
    """Every collection an open route may name, list or search."""
    return [c for c in load_all() if not is_private(c)]


def default_collection() -> Collection:
    """The first collection load_all() returns — used wherever no explicit
    collection is given (single-collection call sites predating P0.5's
    federation), and as the CONTROL manifest (worker state, seeding table).
    With zero collections the control manifest falls back to
    DATA_DIR/manifest.db (`_control_fallback`) so the node keeps running;
    it is NOT listed anywhere and no pipeline runs on it."""
    collections = load_all()
    if collections:
        return collections[0]
    if _REFUSALS:
        # The fallback's paths are DATA_DIR/manifest.db, DATA_DIR/fts and
        # DATA_DIR/lancedb — on this deployment the LEGACY gutenberg tree
        # that docs/notes/legacy-to-delete.md lists for deletion. Handing
        # that back because every file was refused would put the node's
        # control state, and anything else `db.connect()` writes without a
        # collection, into a tree nothing serves and someone is about to
        # remove. A node holding only refusals is misconfigured, and
        # saying so is the only safe answer.
        raise ValueError(
            "every collection file was refused, so this node has nothing to "
            "run and no collection to hold its control state: "
            + "; ".join(f"{r.name} ({r.reason})" for r in _REFUSALS))
    return _control_fallback()


def by_name(name: str) -> Collection | None:
    """A collection is available for query federation simply by existing in
    load_all() — there's no separate "enabled" flag (unlike acquire_enabled,
    which guards a real side effect: outbound network, disk, license gates).
    Querying an already-ingested collection has no equivalent hazard, so an
    operator opts a collection out of federation the same way they opt it
    out of existing at all today: by not shipping its collections/*.toml.

    ANOTHER collection's fault is not this one's. A lookup by name used to
    raise when any other file could not load — so on 2026-09-21 `hold check
    pubmed` died on gutenberg's missing SHARD before it checked anything.
    The named collection's own fault still raises, with its own reason: a
    typo in THIS file must not read as "no such collection"."""
    served, unreadable = load_all(skip_unreadable=True)
    for c in served:
        if c.name == name:
            return c
    for other, why in unreadable:
        if other == name:
            raise ValueError(why)
    return None

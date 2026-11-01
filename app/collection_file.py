"""A collection's file — 07 §7.8's `collections/<name>.toml` — and its ONE
writer.

The writer was the Library's (`app/web.py`), and it moved here unchanged on
2026-10-25 so that `louise hold config apply --set` and `hold records fetch
--slice` write the file through it rather than through a second one: 07 §7.6
maps the Library's embed-mode control to `hold config apply --set embed=…`,
and two writers of one file is how a key one of them did not know about
disappears (tests/test_toml_roundtrip.py). `app.web` re-exports the old
names.

`set_values` is the one way to change a value: the collection is loaded as
the node loads it, the change is applied to its fields, the whole file is
rendered by the writer, the rendering is LOADED BACK by the node's own
loader before a byte is written — a value the node would refuse is refused
here, the file untouched — and only then replaced, atomically. Desired state
goes into the file and nowhere else (invariant 10); the caller applies it
(`effective_config.apply`, the node at its next round).

Standardised alternative, not taken: `tomlkit` edits a TOML document in
place and keeps its comments. Adopting it is a new dependency and a second
model of the file beside `Collection`; it would pay if operators come to
annotate their files by hand, since a rewrite drops comments today.
"""
from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path
from typing import Any

from . import collections as collections_mod


class SetRefused(ValueError):
    """A value the file cannot take — an unknown key, a malformed value, or a
    file the node would refuse. Nothing was written (07 §7.7: exit 2)."""


# --------------------------------------------------------------------------
# the writer (moved from app/web.py, 2026-10-25)

def _codex_id(collection) -> str | None:
    """The `collection_id` a rewrite must carry through, or None for a
    collection that predates the migration. One helper so no call site
    has to remember the `.codex and .codex.collection_id` dance."""
    cx = getattr(collection, "codex", None)
    return None if cx is None else cx.collection_id


def _toml_scalar(v) -> str:
    """A layer param as TOML. Ints and floats bare, bools lowercase,
    everything else quoted — the set `Chunker.params` actually holds. A
    backslash is escaped before the quote (2026-10-25): unescaped, a value
    holding one wrote a file TOML reads otherwise, or not at all."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return '"%s"' % str(v).replace("\\", "\\\\").replace('"', '\\"')


#: 07 §7.8 spells the middle mode `on-demand`; the stored value is `lazy`
#: (app/collections.py's `_EMBED_MODE_ALIASES`). A Codex file is written in
#: the DOCUMENT's word — CLAUDE.md: the documents fix the vocabulary.
_MODE_ON_DISK = {"lazy": "on-demand"}


def _write_codex_toml(path: Path, **kw) -> None:
    """`_codex_toml_text`, written in one rename."""
    _replace(path, _codex_toml_text(path, **kw))


def _replace(path: Path, text: str) -> None:
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _codex_toml_text(path: Path, *, codex: str, profile: str | None,
                     acquire_enabled: bool | None, corpus_dir: Path,
                     genesis_archive: str | None, encoder,
                     embed_mode: str | None, snapshot: str | None,
                     extras: tuple[str, ...], title: str | None = None,
                     slices=None, kinds=None, share: bool = False,
                     reranker=None, source: str = "origin",
                     share_slices=None, share_kinds=None, embed_slices=None) -> str:
    """07 §7.8's file: `codex`, `title`, `slices`, `kinds`, `embed`,
    `share`, `[encoder]` naming where this node reaches the encoder, and
    `[reranker]` where it reaches the collection's reranker (05 §5.6).

    What is NOT written is the point. `plugin`, the chunker `[[layers]]`
    and the `[paths]` tree are the CODEX's answers — all three are inside
    `collection_id` or derived from `<LOUISE_ROOT>/collections/<name>/` —
    and a file that repeated them could disagree with them; so are the
    space's `dtype`, `ctx` and `dim`, which `[encoder]` no longer states,
    and its `query.instruction` — which a file that stated it would have
    refused on the next load (collections.parse_recipe).
    07 §7.8: "A file's `codex` is never edited: a name holds one collection
    for its life, and another collection is another name." So this function
    carries the id through every rewrite and never invents one.

    The keys a Codex file carried before §7.8 spelled the file out —
    `profile`, `acquire_enabled`, `genesis_archive`, `snapshot`,
    `corpus_dir`, `extras` — are written only where loading the file back
    without them would give a different value (`collections.codex_defaults`
    is the one derivation both sides use), so a rewrite never changes what
    a collection does. `acquire_enabled=None` is the derivation itself: a
    change of `slices` or `kinds` then changes whether the node fetches, as
    an edit of the file by hand does.

    A deposit collection's `[[sources]]` (06 §6.7) are carried from the file
    as it stands, last, as `app/hold/sources.py` keeps them (2026-10-25: the
    writer dropped them).

    `license_ack` is absent for the same reason as `plugin`: a Codex
    collection's acknowledgement is `state/ack.json` (09 §9.13), read by
    `collections._codex_ack`, not a line in this file.
    """
    name = path.stem
    cx = collections_mod.codex_at(name)
    kinds = tuple(kinds or collections_mod.DEFAULT_KINDS)
    if slices is None and profile and \
            profile == collections_mod.codex_defaults(cx, name, None, kinds)["everything"]:
        slices = "all"
    derived = collections_mod.codex_defaults(cx, name, slices, kinds, source or "origin")
    if acquire_enabled is None:
        acquire_enabled = derived["acquire_enabled"]
    lines = [f'codex = "{codex}"']
    if source and source != "origin":
        # 07 §7.8 `source` (2026-10-07): a fetched collection reads the
        # shards it holds; a rewrite that dropped this would turn it back
        # into a collection scanning an origin it does not have
        lines.append(f'source = "{source}"')
    if title:
        lines.append(f"title = {_toml_scalar(title)}")
    if slices == "all":
        lines.append('slices = "all"')
    elif slices is not None:
        lines.append("slices = [%s]" % ", ".join(_toml_scalar(s) for s in slices))
    elif profile:
        # a profile no list of slices spells (gutenberg's `starter`, a
        # capped selection): kept, not translated. `records fetch --slice`
        # translates one it can when it records what it fetched
        lines.append(f"profile = {_toml_scalar(profile)}")
    lines.append("kinds = [%s]" % ", ".join(_toml_scalar(k) for k in kinds))
    if embed_mode:
        lines.append(f'embed = "{_MODE_ON_DISK.get(embed_mode, embed_mode)}"')
    # 07 §7.8 `embed_slices` (2026-10-31): what the encoder spends on, out of
    # what is held; carried through every rewrite
    if embed_slices is not None:
        lines.append("embed_slices = [%s]" % ", ".join(_toml_scalar(s) for s in embed_slices))
    lines.append(f"share = {'true' if share else 'false'}")
    # 07 §7.8 `share_slices` / `share_kinds` (2026-10-07): what serving is
    # narrowed to, carried through every rewrite like `source`
    if share_slices is not None:
        lines.append("share_slices = [%s]" % ", ".join(_toml_scalar(s) for s in share_slices))
    if share_kinds is not None:
        lines.append("share_kinds = [%s]" % ", ".join(_toml_scalar(k) for k in share_kinds))
    kept = []
    if bool(acquire_enabled) != derived["acquire_enabled"]:
        kept.append(f"acquire_enabled = {'true' if acquire_enabled else 'false'}")
    if (genesis_archive or None) != derived["genesis_archive"]:
        kept.append(f"genesis_archive = {_toml_scalar(genesis_archive or '')}")
    if snapshot and snapshot != derived["snapshot"]:
        kept.append(f"snapshot = {_toml_scalar(snapshot)}")
    if Path(corpus_dir) != derived["corpus_dir"]:
        kept.append(f"corpus_dir = {_toml_scalar(Path(corpus_dir).as_posix())}")
    if extras:
        kept.append("extras = [%s]" % ", ".join(_toml_scalar(e) for e in extras))
    if kept:
        lines += ["", "# Not keys of 07 §7.8's file: each is kept because the Codex and",
                  "# <LOUISE_ROOT>/collections/<name>/ do not give the same answer.",
                  *kept]
    if encoder is not None:
        # `[encoder]`, not `[embed]`: on a Codex file the bare `embed` key
        # above is the MODE (07 §7.8), so the recipe cannot share its name.
        lines += ["", "[encoder]", f"url = {_toml_scalar(encoder.url)}"]
        if encoder.query_url:
            lines.append(f"query_url = {_toml_scalar(encoder.query_url)}")
        # No query instruction: it is the Codex's (`space.query.instruction`),
        # and a file that states one is refused (collections.parse_recipe).
        if encoder.api != "openai":
            lines.append(f"api = {_toml_scalar(encoder.api)}")
        # both, always (07 §7.8, amended 2026-10-08): the file names the
        # model of each side, and one model doing both is named twice
        lines += [f"embed_model = {_toml_scalar(encoder.model)}",
                  f"query_model = {_toml_scalar(_query_model_of(encoder))}"]
        # The engine's settings, carried through every rewrite: a key the
        # file named stays named — even at its default, since naming it is
        # what keeps a recommendation out of it (09 §9.19) — and a value
        # off its default is written. `batch` and `exactness` were dropped
        # by every rewrite until 2026-11-01 (a share toggle in the Library
        # sent wikidata back from 16 texts a request to EMBED_BATCH).
        stated = getattr(encoder, "stated", frozenset())
        if "batch" in stated or getattr(encoder, "batch", 0):
            lines.append(f"batch = {int(getattr(encoder, 'batch', 0) or 0)}")
        if "concurrency" in stated or getattr(encoder, "concurrency", 1) > 1:
            lines.append(f"concurrency = {encoder.concurrency}")
        if getattr(encoder, "exactness", "near") != "near":
            lines.append(f"exactness = {_toml_scalar(encoder.exactness)}")
        if getattr(encoder, "device", None):
            lines.append(f"device = {_toml_scalar(encoder.device)}")
    lines += _reranker_lines(reranker)
    lines += _sources_lines(path)
    lines.append("")
    return "\n".join(lines)


def _reranker_lines(reranker) -> list[str]:
    """A collection's `[reranker]` (05 §5.6), carried through every rewrite
    like `[encoder]`: node-local and query-time, never the Codex's. The
    writers used to drop it. On 2026-09-15 pubmed's file was rewritten when
    its embedding was switched back on, came out without the table, and its
    searches went unreranked with nothing saying so. The round-trip guard
    passed throughout: its fixtures declared no reranker, so None == None.
    Its `window` (amended 2026-10-21) was dropped the same way until
    2026-10-25: written where it is not the default."""
    if reranker is None:
        return []
    lines = ["", "[reranker]", f"url = {_toml_scalar(reranker.url)}",
             f"api = {_toml_scalar(reranker.api)}"]
    if reranker.model:
        lines.append(f"rerank_model = {_toml_scalar(reranker.model)}")
    window = getattr(reranker, "window", collections_mod.RerankRecipe.window)
    if window != collections_mod.RerankRecipe.window:
        lines.append(f"window = {int(window)}")
    return lines


def _sources_lines(path: Path) -> list[str]:
    """The file's `[[sources]]` (a deposit collection's subscriptions, 06
    §6.7) as it states them now, every key of each table kept."""
    try:
        with open(path, "rb") as f:
            subs = tomllib.load(f).get("sources") or []
    except (OSError, ValueError):
        return []
    lines: list[str] = []
    for s in subs:
        if isinstance(s, dict):
            lines += ["", "[[sources]]"] + [f"{k} = {_toml_scalar(v)}" for k, v in s.items()]
    return lines


def _query_model_of(encoder) -> str:
    """The query side's model as the file states it: the recipe's own (every
    recipe read from a file has one), else — a recipe built by hand — the
    query URL's model, else the document model when the encoder is
    symmetric. Never the document model for a separate query endpoint."""
    if encoder.query_model:
        return encoder.query_model
    if encoder.query_url:
        return collections_mod.model_named_by(encoder.query_url) or ""
    return encoder.model


def _write_collection_toml(path: Path, *, codex: str | None,
                            plugin: str, profile: str | None,
                            acquire_enabled: bool, corpus_dir: Path,
                            manifest: Path, fts: Path, lancedb: Path,
                            license_ack: str | None = None,
                            genesis_archive: str | None = None,
                            embed=None, embed_mode: str | None = None,
                            snapshot: str | None = None,
                            extras: tuple[str, ...] = (),
                            peer_source: str | None = None,
                            layers=None, title: str | None = None,
                            slices=None, kinds=None, share: bool = False,
                            reranker=None, source: str = "origin",
                            share_slices=None, share_kinds=None, embed_slices=None) -> None:
    """Rewrite a collection's Tier-2 file. `codex` has NO DEFAULT on
    purpose: a call site that forgets it must fail loudly here, because
    the failure it would otherwise cause is silent and unrepairable.

    Observed live 2026-09-11 on the only peer in existence. gutenberg had
    been migrated to a Codex; the operator changed its embed mode in the
    Library; this writer — which had never heard of a Codex — re-emitted
    the file in the legacy shape. That dropped nothing visible and did two
    invisible things: it un-migrated the collection back to the legacy
    load path, and it materialised the plugin's implicit layer as an
    explicit `[[layers]] chunker = "fixed"` with no `params`, which
    `chunk_fixed` refuses. 10,212 documents errored at the embed stage and
    left every selection. Both halves stayed internally consistent, so
    nothing detected it (invariant 1's failure mode); only the chunker's
    refusal to guess a window made it visible at all.
    """
    if codex:
        _write_codex_toml(
            path, codex=codex, profile=profile,
            acquire_enabled=acquire_enabled, corpus_dir=corpus_dir,
            genesis_archive=genesis_archive, encoder=embed,
            embed_mode=embed_mode, snapshot=snapshot, extras=extras,
            title=title, slices=slices, kinds=kinds, share=share,
            reranker=reranker, source=source,
            share_slices=share_slices, share_kinds=share_kinds, embed_slices=embed_slices)
        return
    lines = [f'plugin = "{plugin}"']
    if peer_source:
        # peer plane: fetch from this magnet/.torrent instead of the origin
        # (engine.effective_spec). Carried on every rewrite like the rest.
        # Several (one per language of a pre-embedded corpus, STATUS 5h)
        # are written as a TOML array, in fetch order.
        from .acquire.engine import peer_sources as _peer_sources
        sources = _peer_sources(type("_P", (), {"peer_source": peer_source})())
        if len(sources) > 1:
            lines.append("peer_source = [")
            lines.extend(f'  "{s}",' for s in sources)
            lines.append("]")
        else:
            lines.append(f'peer_source = "{sources[0] if sources else peer_source}"')
    if embed_mode:
        lines.append(f'embed_mode = "{embed_mode}"')
    if snapshot:
        # carried like the [embed] block and embed_mode: dropping a pinned
        # snapshot on a rewrite would silently move the collection back to
        # "whatever the origin calls latest"
        lines.append(f'snapshot = "{snapshot}"')
    if profile:
        lines.append(f'profile = "{profile}"')
    if acquire_enabled:
        lines.append("acquire_enabled = true")
    if license_ack:
        lines.append(f'license_ack = "{license_ack}"')
    if genesis_archive:
        lines.append(f'genesis_archive = "{genesis_archive}"')
    if extras:
        # opted-in EXTRA formats (spec.extras) — local-only downloads,
        # never p2p. Carried on every rewrite like embed_mode: dropping it
        # would silently stop the extras at the next profile touch.
        quoted = ", ".join(f'"{e}"' for e in extras)
        lines.append(f"extras = [{quoted}]")
    lines.append(f'corpus_dir = "{corpus_dir.as_posix()}"')
    if embed is not None:
        # any rewrite must carry the [embed] block forward — dropping it
        # would silently flip the collection back to the env recipe, which
        # the space pin would then refuse at the next boot
        lines += ["", "[embed]", f'url = "{embed.url}"',
                  f'api = "{embed.api}"', f'model = "{embed.model}"',
                  f'dtype = "{embed.dtype}"', f"ctx = {embed.ctx}"]
        if embed.dim is not None:
            lines.append(f"dim = {embed.dim}")
        if embed.query_url:
            lines.append(f'query_url = "{embed.query_url}"')
        if getattr(embed, "concurrency", 1) > 1:
            # not space identity, but VERY MUCH desired state: dropping it
            # on a rewrite silently serialized a CPU collection's embeds
            lines.append(f"concurrency = {embed.concurrency}")
        if getattr(embed, "device", None):
            lines.append(f'device = "{embed.device}"')
    if layers:
        # CHUNKER LAYERS (amendment 41). These are part of space_id
        # (invariant 1 as amended), so dropping them on a rewrite does not
        # merely lose a setting — it silently redefines the collection's
        # SPACE, and the next boot either refuses or, worse, accepts a
        # collection whose stored vectors belong to a different function.
        # `version` is deliberately not written: parse_layers derives it
        # from the chunker, so writing it back would let a stale number
        # outlive a chunker bump.
        for layer in layers:
            lines += ["", "[[layers]]", f'chunker = "{layer.chunker}"',
                      f'rule = "{layer.rule}"']
            # `params` ARE the chunker's window and are inside space_id
            # exactly as the chunker id is (09 §9.3). Omitting them was the
            # same bug as omitting the layer, one level down: `fixed`
            # refuses a params-less layer outright, and a chunker that
            # defaulted instead would have cut a whole collection by a
            # window its own id does not describe.
            if layer.params:
                lines.append("params = { %s }" % ", ".join(
                    f"{k} = {_toml_scalar(v)}"
                    for k, v in sorted(layer.params.items())))
    lines += _reranker_lines(reranker)
    lines += [
        "",
        "[paths]",
        f'manifest = "{manifest.as_posix()}"',
        f'fts = "{fts.as_posix()}"',
        f'lancedb = "{lancedb.as_posix()}"',
        "",
    ]
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# one collection, as loaded, back into its file

def _raw(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _file_embed(raw: dict[str, Any]) -> str | None:
    """The mode the file STATES, canonical — not the one the node runs
    (`p2p` with sharing off runs as `all`; the file still says `p2p`)."""
    value = raw.get("embed", raw.get("embed_mode"))
    if value is None:
        return None
    return collections_mod._EMBED_MODE_ALIASES.get(str(value), str(value))


def _fields(c, raw: dict[str, Any]) -> dict[str, Any]:
    """The writer's arguments for a loaded collection: everything it holds,
    unchanged. One list, so no call site forgets a field.

    `acquire_enabled` is the file's own where it states one or is in the
    shape before 07 §7.8 (whose default is off); otherwise it is left to the
    derivation, so that it follows `slices` and `kinds` as a hand edit does."""
    annotations = collections_mod.is_annotations(c)
    new_shape = any(k in raw for k in collections_mod._SPEC_FILE_ONLY_KEYS)
    return dict(
        codex=_codex_id(c), profile=c.profile,
        acquire_enabled=(c.acquire_enabled if "acquire_enabled" in raw or not new_shape else None),
        corpus_dir=c.corpus_dir, genesis_archive=c.genesis_archive,
        encoder=None if annotations else c.embed,
        embed_mode=None if annotations else _file_embed(raw),
        snapshot=c.snapshot, extras=c.extras, title=c.title, slices=c.slices,
        kinds=c.kinds, share=c.share, reranker=c.reranker, source=c.source,
        share_slices=c.share_slices, share_kinds=c.share_kinds,
        embed_slices=getattr(c, "embed_slices", None))


def rewrite(collection, **changes) -> Path:
    """The collection's file written again from its loaded fields, with
    `changes` — the writer's argument names — in place of theirs."""
    path = collections_mod.config_toml_path(collection.name)
    _write_codex_toml(path, **{**_fields(collection, _raw(path)), **changes})
    return path


def load(name: str):
    """`(collection, raw, path)` for a held name, as the node loads it — a
    collection the node refuses for want of an encoder still loads here,
    since naming one is a thing this file is edited for."""
    path = collections_mod.config_toml_path(name)
    if not path.is_file():
        raise SetRefused(f"{name}: no such collection on this node (its file would be {path})")
    try:
        return collections_mod._load_one(path), _raw(path), path
    except (ValueError, OSError) as e:
        raise SetRefused(f"{name}: its file does not load, so no value of it is set "
                         f"until it is repaired by hand: {e}") from None


# --------------------------------------------------------------------------
# one value at a time: `hold config apply --set`, the Library's controls

#: 07 §7.8's editable desired state, as `hold config show` names it: the
#: file's keys, and `[encoder]` / `[reranker]` by their dotted names.
SETTABLE: dict[str, str] = {
    "embed": "off | on-demand | all | p2p",
    "share": "true | false",
    "title": "this peer's own name for it; empty removes it",
    "slices": '"all", or slice names, comma-separated',
    "kinds": "member kinds, comma-separated (02 §2.6)",
    "share_slices": 'slice names, comma-separated; "all" serves every slice held',
    "embed_slices": 'slice names, comma-separated; "all" embeds every slice held',
    "share_kinds": 'member kinds, comma-separated; "all" serves every kind held',
    "encoder.url": "the encoder's server (07 §7.8)",
    "encoder.query_url": "the query side's server; empty when symmetric",
    "encoder.api": "openai | ollama | vega",
    "encoder.embed_model": "the model documents are embedded with",
    "encoder.query_model": "the model questions are embedded with",
    "reranker.url": "the reranker's endpoint; empty removes [reranker]",
    "reranker.api": "jina | tei",
    "reranker.rerank_model": "the reranker's model",
    "reranker.window": "the longest candidate read, in tokens; 0 uncut",
}
#: `hold config show` prints the mode as `embed_mode`
ALIASES = {"embed_mode": "embed"}
_ENCODER_APIS = ("openai", "ollama", "vega")


def accepted() -> str:
    return ", ".join(SETTABLE)


def parse_assignments(items) -> dict[str, str]:
    """`KEY=VALUE` strings to `{key: value}`, keys canonical. An unknown key,
    a missing `=` or a key given twice is refused, naming the keys."""
    out: dict[str, str] = {}
    for item in items or ():
        key, sep, value = str(item).partition("=")
        key = ALIASES.get(key.strip(), key.strip())
        if not sep:
            raise SetRefused(f"--set {item!r}: KEY=VALUE; the keys are {accepted()}")
        if key not in SETTABLE:
            raise SetRefused(f"--set {key!r} is not a value of a collection's file; "
                             f"the keys are {accepted()} (07 §7.8)")
        if key in out:
            raise SetRefused(f"--set {key} is given twice")
        out[key] = value.strip()
    return out


def _bool(key: str, text: str) -> bool:
    low = text.lower()
    if low not in ("true", "false"):
        raise SetRefused(f"{key} is true or false, got {text!r}")
    return low == "true"


def _names(text: str) -> list[str]:
    return [v.strip() for v in text.split(",") if v.strip()]


def _parse(key: str, text: str, where: str) -> Any:
    """A `--set` value to the field it sets, checked as the loader checks it."""
    try:
        if key == "embed":
            mode = collections_mod._embed_mode(text, where)
            if mode is None:
                raise ValueError("embed needs a value")
            return mode
        if key == "share":
            return _bool(key, text)
        if key == "title":
            return text or None
        if key == "slices":
            return "all" if text == "all" else collections_mod._parse_slices(_names(text), where)
        if key == "kinds":
            return collections_mod._parse_kinds(_names(text), where)
        if key in ("share_slices", "embed_slices"):
            if text in ("", "all"):
                return None
            got = collections_mod._parse_slices(_names(text), where)
            return got if isinstance(got, tuple) else None
        if key == "share_kinds":
            if text in ("", "all"):
                return None
            bad = [k for k in _names(text) if k not in collections_mod.SHARE_KINDS]
            if bad:
                raise ValueError(f"share_kinds names {bad}; the member kinds served are "
                                 f"{', '.join(collections_mod.SHARE_KINDS)} (02 §2.6)")
            return tuple(_names(text))
        if key == "encoder.api":
            if text not in _ENCODER_APIS:
                raise ValueError(f"encoder.api is one of {', '.join(_ENCODER_APIS)}, got {text!r}")
            return text
        if key == "reranker.api":
            if text.lower() not in collections_mod.RERANK_APIS:
                raise ValueError(f"reranker.api is one of {', '.join(collections_mod.RERANK_APIS)}, "
                                 f"got {text!r}")
            return text.lower()
        if key == "reranker.window":
            if not text.isdigit():
                raise ValueError(f"reranker.window is a number of tokens (0: uncut), got {text!r}")
            return int(text)
        return text.rstrip("/") if key.endswith("url") else text
    except ValueError as e:
        raise SetRefused(str(e)) from None


def _encoder(c, sets: dict[str, Any]):
    """The `[encoder]` with the dotted values set: the file's, or a new one
    built from the space when the file names none. Whether the models agree
    with the URL is the loader's to say, below."""
    if not sets:
        return c.embed
    have = c.embed
    if have is None:
        space = c.codex.space
        have = collections_mod.EmbedRecipe(url="", api="openai", model="", dtype=space.dtype,
                                           ctx=space.ctx, dim=space.dim)
    fields = {"url": "url", "query_url": "query_url", "api": "api",
              "embed_model": "model", "query_model": "query_model"}
    return dataclasses.replace(have, **{fields[k]: (v or None) if k == "query_url" else v
                                        for k, v in sets.items()})


def _reranker(c, sets: dict[str, Any]):
    if not sets:
        return c.reranker
    if sets.get("url") == "":
        if len(sets) > 1:
            raise SetRefused("reranker.url is empty, which removes [reranker]: set nothing else of it")
        return None
    have = c.reranker or collections_mod.RerankRecipe(url="", api="jina")
    fields = {"url": "url", "api": "api", "rerank_model": "model", "window": "window"}
    return dataclasses.replace(have, **{fields[k]: v for k, v in sets.items()})


def set_values(name: str, values: dict[str, str]) -> dict[str, Any]:
    """`hold config apply --set KEY=VALUE …` (07 §7.8 "Pickup"): the values
    written into `collections/<name>.toml`, together, in one rename — or
    nothing written and `SetRefused`. Returns what the file says now."""
    c, raw, path = load(name)
    where = str(path)
    parsed = {k: _parse(k, v, where) for k, v in values.items()}
    return _apply(c, raw, path, parsed)


def _apply(c, raw: dict[str, Any], path: Path, parsed: dict[str, Any], *,
           follow_acquisition: bool = False) -> dict[str, Any]:
    annotations = collections_mod.is_annotations(c)
    kw = _fields(c, raw)
    if follow_acquisition and "acquire_enabled" not in raw:
        kw["acquire_enabled"] = None
    also: dict[str, Any] = {}
    for key, value in parsed.items():
        if annotations and (key == "embed" or key.startswith(("encoder.", "reranker."))):
            raise SetRefused(f"{c.name} is an annotations collection: it has no space and nothing "
                             f"is embedded or ranked for it, so it has no {key} (02 §2.10)")
        if key == "share" and value and c.codex.privacy == "never-share":
            raise SetRefused(f"{c.name} is never-share: it is shared with nobody (04 §4.4)")
    plain = {"title": "title", "slices": "slices", "kinds": "kinds",
             "share_slices": "share_slices", "share_kinds": "share_kinds",
             "embed_slices": "embed_slices"}
    for key, field in plain.items():
        if key in parsed:
            kw[field] = parsed[key]
    if "share" in parsed:
        kw["share"] = parsed["share"]
    if "embed" in parsed:
        kw["embed_mode"] = parsed["embed"]
    if not annotations:
        kw["encoder"] = _encoder(c, {k.split(".", 1)[1]: v for k, v in parsed.items()
                                     if k.startswith("encoder.")})
        kw["reranker"] = _reranker(c, {k.split(".", 1)[1]: v for k, v in parsed.items()
                                       if k.startswith("reranker.")})
        # THE MEETING POINT of the two axes (07 §7.8, 2026-10-19): `p2p`
        # publishes what it embeds, so it needs sharing. Asking for p2p turns
        # sharing on; turning sharing off under p2p takes the mode to `all`
        # (all local) — what the Library asks before it sends. Asking for both
        # at once is a contradiction, refused.
        if kw["embed_mode"] == "p2p" and not kw["share"]:
            if "embed" in parsed and "share" in parsed:
                raise SetRefused("embed = p2p publishes what it embeds, and share = false shares "
                                 "nothing: set embed=all, or share=true (07 §7.8)")
            if "embed" in parsed:
                kw["share"] = also["share"] = True
            elif "share" in parsed:
                kw["embed_mode"] = also["embed"] = "all"
    text = _codex_toml_text(path, **kw)
    # LOADED BACK before a byte is written: what the node would refuse is
    # refused here, and the file stays as it was
    try:
        back = collections_mod._codex_collection(path, tomllib.loads(text), raw.get("name") or c.name)
    except (ValueError, OSError) as e:
        raise SetRefused(f"{c.name}: the file would not load with this value, so nothing was "
                         f"written: {e}") from None
    _read_back(back, kw, parsed)
    _replace(path, text)
    written = tomllib.loads(text)
    return {"collection": c.name, "file": str(path),
            "set": {k: _shown(k, v) for k, v in parsed.items()},
            "also": also,
            "embed": None if annotations else _file_embed(written),
            "share": bool(written.get("share", False)),
            "acquire_enabled": back.acquire_enabled, "profile": back.profile}


def _shown(key: str, v: Any) -> Any:
    """A value as the file spells it: lists for tuples, `on-demand` for the
    stored `lazy` (07 §7.8's word)."""
    if isinstance(v, tuple):
        return list(v)
    return _MODE_ON_DISK.get(v, v) if key == "embed" else v


def _read_back(back, kw: dict[str, Any], parsed: dict[str, Any]) -> None:
    """Every value set reads back as set: a rendering that changed one (an
    escape, a character TOML takes otherwise) is a refusal, never a file
    that says something else."""
    got = {"title": back.title, "slices": back.slices, "kinds": back.kinds,
           "share_slices": back.share_slices, "share_kinds": back.share_kinds,
           "embed_slices": back.embed_slices,
           "share": back.share}
    enc, rr = back.embed, back.reranker
    if enc is not None:
        got.update({"encoder.url": enc.url, "encoder.query_url": enc.query_url or "",
                    "encoder.api": enc.api, "encoder.embed_model": enc.model,
                    "encoder.query_model": enc.query_model})
    if rr is not None:
        got.update({"reranker.url": rr.url, "reranker.api": rr.api,
                    "reranker.rerank_model": rr.model, "reranker.window": rr.window})
    for key, want in parsed.items():
        if key == "embed" or key not in got:
            continue
        if key == "encoder.query_url":
            want = want or ""
        if got[key] != want:
            raise SetRefused(f"{key} = {want!r} would read back as {got[key]!r}; nothing was written")


# --------------------------------------------------------------------------
# what `records fetch --slice` fetched, recorded (07 §7.3)

def selection_after_fetch(c, raw: dict[str, Any], spec, slices) -> Any:
    """The file's `slices` once these are recorded: joined with what it says
    — `"all"` wins — and, where it still names a `profile`, with the slices
    that profile spells. Refused (before anything is fetched) when that
    profile is one no list of slices spells: recording would drop it."""
    from .acquire import spec as acquire_spec
    if slices == "all":
        return "all"
    had = c.slices
    if had is None and raw.get("profile"):
        had = acquire_spec.slices_for_profile(spec, raw["profile"])
        if had is None:
            raise SetRefused(
                f"{c.name}: its file names profile {raw['profile']!r}, which no list of slices "
                f"spells; recording {', '.join(slices)} would drop it. Fetch it with --profile, "
                f"or name what the profile holds with --slice")
    if had == "all":
        return "all"
    out = list(had or ())
    out += [s for s in slices if s not in out]
    return tuple(out)


def record_fetched(name: str, selection) -> dict[str, Any]:
    """The selection written as the file's `slices`: the node's acquisition
    continues it every cycle (07 §7.3) — `acquire_enabled` follows it unless
    the file states one."""
    c, raw, path = load(name)
    return _apply(c, raw, path, {"slices": selection}, follow_acquisition=True)

"""tests/hold/test_template.py — a plugin plus a recipe makes a Codex.

docs/06-plugins.md §6.1 (what a source declares) and §6.6 (from a plugin to
a collection); docs/09-formats.md §9.3. The template is the half a plugin
supplies: a new collection is a plugin, a `space` block and a `work` block.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from app.hold import codex as cx, collection as col, template
from app.hold.codex import Codex, CodexError
from app.hold.template import TemplateError
from app.plugins.gutenberg import SOURCE
from tests.hold.conftest import (CAL_SET, CAL_VECS, ENCODER, ENVELOPE, KEY, NOW,
                                 PLATFORM, PUBMED_SAMPLE, Q_ENCODER,
                                 Q_WEIGHTS, TERMS, WEIGHTS, a_credit,
                                 fake_embed, pubmed)


def from_recipe(plugin, r, **kw):
    """`template.from_recipe` with the two hashes `create` would have
    computed. They are required and have no default (06 §6.6), so every
    call supplies them; the tests that care about that are below."""
    kw.setdefault("calibration", (CAL_VECS, ENVELOPE))
    return template.from_recipe(plugin, r, **kw)

SPACE = {
    "encoder": {"image": ENCODER, "weights": WEIGHTS, "label": "MedCPT"},
    "dim": 768, "dtype": "f16", "ctx": 512, "pooling": "cls",
    "normalise": True,
    "query": {"encoder": {"image": Q_ENCODER, "weights": Q_WEIGHTS},
              "instruction": None},
    "tolerance": "0.95",
    # No `vectors` and no `envelope`: `create` computes both by embedding
    # the set, and a recipe that carried them would be asserting the
    # result of a computation its author never ran (06 §6.6).
    "calibration": {"set": CAL_SET, "quorum": {"n": 16, "min_pass": 12}},
    "epoch": "0", "evaluation": None,
}

ORIGIN = {
    "spec": {"source_id": "gutenberg", "transport": "https",
             "url": "https://www.gutenberg.org/", "layout": "one-dir-per-doc",
             "listing": "directory", "files": None, "filters": ["*.txt"],
             "checksums": {"kind": "none", "suffix": "", "algo": ""},
             "delta": {"mode": "full-resync", "subdir": None}},
    "form": "the plain-text edition, because the epub is a derivative of it "
            "and the plates it carries are not in the text we keep",
    "unit": "file", "clock": "UTC", "cutoff_rule": "record-date",
    "jurisdiction": None,
}

VALIDATION = {"checks": ["roots", "records", "chunks", "license", "signature"],
              "sample": {"records": {"shards": "0.05"},
                         "chunks": {"shards": "0.05"}},
              "required": []}


def recipe(**over) -> dict:
    base = {
        "descriptive": {"title": "Project Gutenberg",
                        "summary": "Public-domain books, sliced by language."},
        "space": dict(SPACE),
        "work": {"tokens": 5_000_000},
        "origin": dict(ORIGIN),
        "validation": dict(VALIDATION),
    }
    base.update(over)
    return base


# ---- what the plugin declares ---------------------------------------------

def test_every_plugin_declares_what_6_1_requires():
    for name in ("gutenberg", "pubmed", "wikipedia", "embedded", "normattiva"):
        s = template.source_for(name)
        assert s.protocol_version == 1
        assert s.PARSER_VERSION >= 1
        assert s.modality.value in ("text", "dna")
        assert s.erasure.value in ("append-only", "retractable", "erasable")
        assert s.native_id_order in ("bytes", "numeric")
        assert isinstance(s.default_facets, list)
        # 06 §6.1's corpus credit, of EVERY plugin — green ones included.
        # `create` cannot assemble `credits` without them, and a collection
        # without a corpus entry is one the specification refuses, so a
        # plugin that forgot these is a plugin no collection can be made
        # from. That failure belongs here and not at someone's `create`.
        assert s.corpus_provider and isinstance(s.corpus_provider, str)
        assert s.corpus_attribution and isinstance(s.corpus_attribution, str)
        assert template.corpus_credit(s).kind == "corpus"


def test_an_unknown_plugin_names_the_ones_there_are():
    with pytest.raises(TemplateError, match="no plugin 'nope'.*gutenberg"):
        template.source_for("nope")


def test_layers_come_from_the_plugins_own_cut():
    """A plugin declares its cut once — in `chunking_profile`/`mutability` —
    and the template reads it there rather than asking for it twice."""
    layers = template.default_layers(template.source_for("pubmed"))
    assert len(layers) == 1
    assert layers[0].rule == "default"
    assert layers[0].chunker.id == "abstract"
    # a plugin that DECLARES its layers is taken at its word: wikipedia's two
    # (the archive's verbatim cut, then cdc for the dump's pages)
    assert [l.chunker.id for l in template.default_layers(template.source_for("wikipedia"))] \
        == ["verbatim", "cdc"]
    assert template.default_layers(template.source_for("gutenberg"))[0].chunker.id == "book"


def test_slicing_completes_the_schemes_description_to_9_3s_shape():
    block = template.default_slicing(template.source_for("pubmed"))
    assert block["keys"] == ["year"]
    assert block["shard_records"] == 30_000
    assert block["order"] == "numeric"
    assert block["unknown"] == "year=unknown"
    assert block["year"]["field"] == "date"


def test_a_plugin_with_no_slice_key_gets_one_slice_called_all():
    """A plugin with no slicing scheme (genome was the worked case; boxed in
    attic/) gets one slice called all, ordered as it declares."""
    from types import SimpleNamespace
    unsliced = SimpleNamespace(source_id="unsliced", native_id_order="bytes",
                               default_slicing=None)
    block = template.default_slicing(unsliced)
    assert block["keys"] == []
    assert block["unknown"] == "all"
    assert block["order"] == "bytes", "ordered as the plugin declares"


def test_facets_arrive_sorted_by_field():
    facets = template.default_facets(template.source_for("gutenberg"))
    assert [f.field for f in facets] == ["lang", "subjects"]
    assert facets[1].authority == {"name": "LCSH"}
    assert facets[0].separator is None


def test_a_tree_facet_declares_its_separator():
    subjects = template.default_facets(template.source_for("pubmed"))[0]
    assert (subjects.field, subjects.kind, subjects.separator) == \
        ("subjects", "tree", "/")
    assert subjects.authority == {"name": "MeSH"}


# ---- the recipe -----------------------------------------------------------

def test_a_plugin_and_two_blocks_make_a_collection():
    """The whole point of the template: a new collection is a plugin plus a
    `space` block plus a `work` block (07 §7.3)."""
    codex = from_recipe("gutenberg", recipe(), platform_image=PLATFORM)
    assert isinstance(codex, Codex)
    assert codex.plugin == "gutenberg"
    assert codex.parser.id == "gutenberg"
    # READ FROM THE PLUGIN, never pinned here: what this asserts is that
    # the template takes the parser version from the source, and a literal
    # would turn every legitimate parser bump into a test edit.
    assert codex.parser.version == SOURCE.PARSER_VERSION
    assert codex.modality == "text"
    assert codex.erasure == "retractable"
    assert codex.privacy == "public"
    assert codex.license.cls == "green" and codex.license.terms_sha256 is None
    assert codex.slicing["order"] == "numeric"
    assert [f.field for f in codex.facets] == ["lang", "subjects"]
    assert codex.toolchain.platform_image == PLATFORM
    assert codex.relation == "root" and codex.space.epoch == "0"
    assert codex.collection_id.startswith("sha256:")


def test_work_per_defaults_to_the_cadence_and_is_written_out():
    monthly = from_recipe("gutenberg", recipe(), platform_image=PLATFORM)
    assert monthly.work.per == "P1M"
    weekly = from_recipe(
        "gutenberg", recipe(update={"cadence": "weekly", "day": 1, "grace_days": 0}),
        platform_image=PLATFORM)
    assert weekly.work.per == "P1W"
    named = from_recipe(
        "gutenberg", recipe(work={"tokens": 1, "per": "P7D"}),
        platform_image=PLATFORM)
    assert named.work.per == "P7D"


def test_space_and_work_are_required_and_never_guessed():
    for missing in ("space", "work", "descriptive"):
        r = recipe()
        del r[missing]
        with pytest.raises(TemplateError, match=f"{missing} is required"):
            from_recipe("gutenberg", r, platform_image=PLATFORM)


def test_a_typo_in_a_recipe_is_an_error_not_a_silent_default():
    with pytest.raises(TemplateError, match="unknown key.*spce"):
        from_recipe("gutenberg", recipe(spce={}), platform_image=PLATFORM)


def test_a_yellow_plugin_needs_its_terms_named_by_hash():
    r = recipe(origin=dict(ORIGIN, spec=dict(ORIGIN["spec"], source_id="pubmed")))
    with pytest.raises(TemplateError, match="terms_sha256 is required"):
        from_recipe("pubmed", r, platform_image=PLATFORM)
    ok = from_recipe(
        "pubmed", dict(r, license={"class": "yellow", "terms_sha256": TERMS}),
        platform_image=PLATFORM)
    assert ok.license.terms_sha256 == TERMS


def test_a_recipe_may_tighten_a_class_and_never_loosen_it():
    """04 §4.4: the plugin declares the default and a collection may tighten
    it, never loosen it."""
    tighter = from_recipe(
        "gutenberg", recipe(privacy="restricted",
                            license={"class": "yellow", "terms_sha256": TERMS}),
        platform_image=PLATFORM)
    assert tighter.privacy == "restricted" and tighter.license.cls == "yellow"

    r = recipe(origin=dict(ORIGIN, spec=dict(ORIGIN["spec"], source_id="pubmed")),
               license={"class": "green", "terms_sha256": None})
    with pytest.raises(TemplateError, match="loosens the plugin's 'yellow'"):
        from_recipe("pubmed", r, platform_image=PLATFORM)


def test_a_validation_plan_is_required():
    r = recipe()
    del r["validation"]
    with pytest.raises(TemplateError, match="validation is required"):
        from_recipe("gutenberg", r, platform_image=PLATFORM)


def test_an_origin_is_required_until_the_spec_is_serialised():
    r = recipe()
    del r["origin"]
    with pytest.raises(TemplateError, match="origin is required"):
        from_recipe("gutenberg", r, platform_image=PLATFORM)


def test_archives_without_an_expanded_registry_entry_are_refused():
    """06 §6.6: an archive a collection draws on is credited at creation or
    not at all — the Codex is written once and never edited."""
    with pytest.raises(TemplateError, match="names archives but none were expanded"):
        from_recipe("gutenberg", recipe(archives=["wikipedia-bge-m3"]),
                             platform_image=PLATFORM)
    ok = from_recipe("gutenberg", recipe(archives=["wikipedia-bge-m3"]),
                              platform_image=PLATFORM, credits=(a_credit(),))
    assert [c.archive for c in cx.archive_credits(ok)] == ["wikipedia-bge-m3"]
    # And the two the caller did NOT supply, assembled by `build` from the
    # plugin and from `embedding` (02 §2.7). Gutenberg is GREEN and is
    # credited anyway: a provider that asks for nothing is still the provider.
    assert [c.kind for c in ok.credits] == ["archive", "community", "corpus"]
    corpus = next(c for c in ok.credits if c.kind == "corpus")
    assert corpus.producer == "Project Gutenberg"


def test_a_recipe_may_override_any_default_it_tightens():
    codex = from_recipe(
        "gutenberg",
        recipe(slicing={"keys": [], "unknown": "all", "shard_records": 25,
                        "order": "numeric"},
               facets=[{"field": "lang", "kind": "flat"}],
               cleaning_version=2),
        platform_image=PLATFORM)
    assert codex.slicing["shard_records"] == 25
    assert [f.field for f in codex.facets] == ["lang"]
    assert codex.cleaning_version == 2


def test_a_slicing_block_may_not_name_a_space_shaped_slice():
    with pytest.raises(CodexError, match="collide with a vector index"):
        from_recipe(
            "gutenberg",
            recipe(slicing={"keys": [], "unknown": "03ae64488c899305",
                            "shard_records": 25, "order": "numeric"}),
            platform_image=PLATFORM)


# ---- create ---------------------------------------------------------------

def test_create_from_recipe_writes_the_drag_and_is_idempotent(tmp_path: Path):
    a = col.create_from_recipe(tmp_path, "gutenberg", recipe(),
                               platform_image=PLATFORM, embed=fake_embed, key=KEY, now=NOW)
    b = col.create_from_recipe(tmp_path, "gutenberg", recipe(),
                               platform_image=PLATFORM, embed=fake_embed, key=KEY, now=NOW)
    assert a.collection_id == b.collection_id
    assert (tmp_path / "gutenberg" / "drag" / "codex.json").read_bytes() == a.canonical()
    assert [c.collection_id for c in col.list_all(tmp_path)] == [a.collection_id]


def test_two_plugins_make_two_collections(tmp_path: Path):
    g = col.create_from_recipe(tmp_path, "gutenberg", recipe(),
                               platform_image=PLATFORM, embed=fake_embed, key=KEY, now=NOW)
    p = col.create_from_recipe(
        tmp_path, "pubmed",
        recipe(origin=dict(ORIGIN, spec=dict(ORIGIN["spec"], source_id="pubmed")),
               license={"class": "yellow", "terms_sha256": TERMS}),
        platform_image=PLATFORM, embed=fake_embed, key=KEY, now=NOW)
    assert g.collection_id != p.collection_id
    assert g.space_id != p.space_id, "different layers, different space"


# ---- the platform's archive registry --------------------------------------

def test_the_registry_declares_what_a_credit_needs():
    """06 §6.6: the platform's registry is where an archive is declared —
    its id, coverage, producer, the licence its vectors carry and the
    attribution that licence requires — and `create` copies it whole."""
    from app.bundles.genesis import ARCHIVES
    for aid, entry in ARCHIVES.items():
        assert entry.archive_id == aid
        assert entry.producer, f"{aid} names no producer"
        assert entry.license, f"{aid} names no licence for its vectors"
        assert entry.attribution, f"{aid} names no attribution"
        cov = entry.coverage()
        assert cov == "all" or set(cov) == {"ids_per_shard", "shards"}


def test_medcpts_coverage_is_its_declared_id_ranges():
    from app.bundles.genesis import ARCHIVES
    assert ARCHIVES["medcpt-pubmed"].coverage() == \
        {"ids_per_shard": 1_000_000, "shards": 37}
    # its language blocks since wikipedia's two layers: "all" would have put
    # every document of the collection in layer 1
    assert ARCHIVES["upstash-wikipedia-2024-06"].coverage() == \
        {"ids_per_shard": 10 ** 10, "shards": 11}


def test_an_archive_that_signs_nothing_is_credited_like_any_other():
    """The ordinary case: MedCPT is a directory of .npy files and signs
    nothing, so its key set is empty. That is not a defect — 03 §3.5's
    refusal simply never fires for it (02 §2.7, 06 §6.6)."""
    from app.bundles.genesis import ARCHIVES
    assert ARCHIVES["medcpt-pubmed"].keys == ()
    credits = template.expand_archives(["medcpt-pubmed"])
    assert [c.archive for c in credits] == ["medcpt-pubmed"]
    assert credits[0].license == "public-domain"
    assert "key" not in credits[0].to_dict()


def test_an_unknown_archive_names_the_ones_there_are():
    with pytest.raises(TemplateError, match="no archive 'nope'.*medcpt-pubmed"):
        template.expand_archives(["nope"])


def test_expanded_credits_are_sorted_and_copied_whole():
    credits = template.expand_archives(
        ["upstash-wikipedia-2024-06", "medcpt-pubmed"])
    assert [c.archive for c in credits] == \
        ["medcpt-pubmed", "upstash-wikipedia-2024-06"], "sorted by archive"
    assert credits[0].producer == "NCBI (National Library of Medicine)"
    assert credits[0].license == "public-domain"
    assert credits[1].license == "CC-BY-SA-4.0"


# ---- 03 §3.5's archive refusal, in two steps ------------------------------

SIGNED_KEY = "ed25519:" + "3e" * 32


def _registry_where_medcpt_signs(monkeypatch):
    from app.bundles import genesis
    from dataclasses import replace as dc_replace
    signed = dict(genesis.ARCHIVES)
    signed["medcpt-pubmed"] = dc_replace(signed["medcpt-pubmed"],
                                         keys=(SIGNED_KEY,))
    monkeypatch.setattr(genesis, "ARCHIVES", signed)


def test_a_key_the_registry_does_not_know_belongs_to_no_archive(codex):
    """A bundle carries a producer key and no archive id (09 §9.7), so the
    refusal resolves the key through the registry first. An unknown key is
    an ordinary producer's and the ladder decides it."""
    assert template.archive_of_key("ed25519:" + "ff" * 32) is None
    assert template.credits_admit(codex, "ed25519:" + "ff" * 32)


def test_a_registered_archives_bundle_needs_that_archive_credited(
        monkeypatch, codex):
    _registry_where_medcpt_signs(monkeypatch)
    assert template.archive_of_key(SIGNED_KEY) == "medcpt-pubmed"
    assert not template.credits_admit(codex, SIGNED_KEY), \
        "this collection credits no archive, so it may not stand on one"
    crediting = replace(codex, credits=template.expand_archives(["medcpt-pubmed"]))
    assert template.credits_admit(crediting, SIGNED_KEY)


def test_an_archive_may_start_signing_without_forking_any_identity(
        monkeypatch, codex):
    """The point of moving the keys out: a collection created before the
    archive signed can still adopt its bundles, because what is hashed is
    the archive's NAME and not a key it did not have."""
    before = replace(codex, credits=template.expand_archives(["medcpt-pubmed"]))
    _registry_where_medcpt_signs(monkeypatch)
    after = replace(codex, credits=template.expand_archives(["medcpt-pubmed"]))
    assert before.collection_id == after.collection_id, \
        "a key appearing in the registry moves no collection_id"
    assert template.credits_admit(after, SIGNED_KEY)


# ---- a recipe omits what a Codex nulls ------------------------------------

def test_a_recipe_may_omit_what_a_codex_writes_as_null():
    """A Codex carries every field and nulls what is not in use (09 §9.3);
    a recipe is TOML (07 §7.3) and TOML has no null. So a recipe omits
    those fields and the template fills them — absent means not in use,
    never absent means guess."""
    space = {k: v for k, v in SPACE.items() if k != "evaluation"}
    space["encoder"] = {"image": ENCODER, "weights": WEIGHTS}   # no label
    space["query"] = {}                                          # no query side
    origin = {k: v for k, v in ORIGIN.items() if k != "jurisdiction"}
    origin["spec"] = {k: v for k, v in ORIGIN["spec"].items() if k != "files"}

    codex = from_recipe("gutenberg",
                                 recipe(space=space, origin=origin),
                                 platform_image=PLATFORM)
    assert codex.space.evaluation is None
    assert codex.space.encoder.label == ""
    assert codex.space.query.encoder is None
    assert codex.space.query.instruction is None
    assert codex.origin.jurisdiction is None
    assert codex.origin.spec["files"] is None
    # and the result is a whole Codex: every field present, round-trippable
    assert cx_loads_roundtrips(codex)


def cx_loads_roundtrips(codex) -> bool:
    from app.hold import codex as cx
    return cx.loads(cx.dumps(codex)) == codex


def test_an_empty_toml_table_reads_as_null_not_as_malformed():
    """TOML cannot write `null`, so `[space.query]` with nothing in it is
    how a symmetric space says it has no query encoder."""
    space = dict(SPACE, query={"instruction": ""}, evaluation=[])
    codex = from_recipe("gutenberg", recipe(space=space),
                                 platform_image=PLATFORM)
    assert codex.space.query.encoder is None
    assert codex.space.query.instruction is None
    assert codex.space.evaluation is None


# ---- a recipe's layer refuses what it does not know ----------------------

def test_a_stray_key_in_the_chunker_table_is_refused():
    """TOML binds a bare key to the table above it, so a top-level setting
    written after `[layers.chunker]` lands INSIDE the chunker. `from_recipe`
    validates top-level keys only, so it passed silently — and on
    2026-09-10 that created a permanent pubmed `collection_id` with no
    archive credited, which `hold vectors import --archive` then correctly
    refused. One `create` wasted; 09 §9.18 refuses unknown keys on every
    hashed object and a recipe's layer should too.
    """
    r = recipe(layers=[{"chunker": {"id": "abstract", "version": 1,
                                    "params": {},
                                    "archives": ["medcpt-pubmed"]},
                        "rule": "default"}])
    with pytest.raises(TemplateError, match="unknown key.*archives"):
        from_recipe("pubmed", r, platform_image=PLATFORM)


def test_a_stray_key_beside_the_chunker_is_refused_too():
    r = recipe(layers=[{"chunker": {"id": "abstract", "version": 1},
                        "rule": "default", "archives": ["medcpt-pubmed"]}])
    with pytest.raises(TemplateError, match="unknown key.*archives"):
        from_recipe("pubmed", r, platform_image=PLATFORM)


def test_a_layer_missing_its_chunker_says_so():
    with pytest.raises(TemplateError, match="missing key.*chunker"):
        from_recipe("pubmed", recipe(layers=[{"rule": "default"}]),
                    platform_image=PLATFORM)


def test_a_well_formed_layer_still_works():
    """`params` and `rule` are optional; the rest is required."""
    c = from_recipe("gutenberg", recipe(layers=[
        {"chunker": {"id": "abstract", "version": 1}}]),
        platform_image=PLATFORM)
    assert [(l.chunker.id, l.chunker.version, l.rule) for l in c.layers] == \
        [("abstract", 1, "default")]

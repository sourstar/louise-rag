"""The options amendment (operator, 2026-10-03; 09 §9.3, §9.12, §9.18;
migration-plan step 15a).

From Codex version 4 an engine MAY carry `options`: what a model runner — a
LocalAI backend's `LoadModel` — sent the engine when it loaded the weights,
which no argument vector shows. ABSENT, never null, for an engine started by
`args` alone, so that every engine in use today keeps its bytes, its
`engine_id` and every run and statement built on it (CLAUDE.md RULE 1: no
space re-gated for nothing). The peer's calibration statement moves to
version 3 with it, and version 2 is read beside it for ever.
"""
import json
from dataclasses import replace

import pytest

from app.hold import codex as cx, collection as col, embedder as emb
from app.hold.codex import CodexError, Engine, canonical_json, digest

from .conftest import ENGINE, KEY, NOW, a_root, fake_embed, fake_query_embed, pubmed
from .test_embedder import a_key, a_stated_run, codex_768, gate, reference

#: What LocalAI's `GET /system` lists for an encoder (deploy/localai/patches
#: P1), made canonical by 09 §9.3: proto field names, the host's paths as
#: placeholders, every non-integer number as its decimal string.
OPTIONS = {"Model": "bge-m3.gguf", "ModelFile": "${WEIGHTS}", "ModelPath": "${MODELS}",
           "ContextSize": 8192, "NBatch": 512, "NGPULayers": 99, "Threads": 8,
           "Embeddings": True, "RopeFreqBase": "10000.5",
           "Options": ["parallel:1"], "FlashAttention": "auto"}

LOCALAI = Engine(name="localai/llama-cpp", build="v3.9.0+b10630", variant="intel-sycl-f16",
                 image=ENGINE.image, args=("/backends/llama-cpp/run.sh", "--addr",
                                           "127.0.0.1:${PORT}"),
                 env={}, device=ENGINE.device, options=OPTIONS)


def _with(engine: Engine, **over):
    """pubmed's Codex at version 4 with `engine` on both sides of its space."""
    c = pubmed(**over)
    sp = c.space
    return replace(c, space=replace(
        sp, encoder=replace(sp.encoder, engine=engine),
        query=replace(sp.query, encoder=replace(sp.query.encoder, engine=engine))))


# ---- an engine started by `args` keeps every byte ------------------------------------

def test_an_argv_engine_is_the_version_3_object_byte_for_byte():
    """No key added, not even as null: the bytes, and so `engine_id`, are the
    ones every bundle, attestation, run and statement already names."""
    as_written_at_3 = {"name": "llama.cpp", "build": "b10630", "variant": "sycl",
                       "image": ENGINE.image, "args": list(ENGINE.args), "env": {},
                       "device": ENGINE.device}
    assert ENGINE.options is None
    assert ENGINE.to_dict() == as_written_at_3
    assert ENGINE.engine_id == digest(canonical_json(as_written_at_3))


def test_a_version_3_codex_reads_and_writes_back_unchanged():
    v3 = pubmed(version=3)
    raw = cx.dumps(v3)
    again = cx.loads(raw)
    assert again.version == 3 and cx.dumps(again) == raw
    assert again.collection_id == v3.collection_id
    assert again.space.encoder.engine.engine_id == ENGINE.engine_id


def test_a_version_4_codex_and_a_version_3_one_share_a_space():
    """`version` is inside `collection_id`, so the same recipe at 4 is
    another collection; the space — the function — is the same one."""
    assert cx.OPTIONS_VERSION == 4
    v4, v3 = pubmed(), pubmed(version=3)
    assert v4.version == 4
    assert v4.space_id == v3.space_id
    assert v4.collection_id != v3.collection_id


# ---- an engine with options ----------------------------------------------------------

def test_options_round_trip_and_enter_the_collection_but_not_the_space():
    c = _with(LOCALAI)
    again = cx.loads(cx.dumps(c))
    assert again.space.encoder.engine.options == OPTIONS
    assert again.space.query.encoder.engine.options == OPTIONS
    one_more = dict(OPTIONS, NBatch=1024)
    other = _with(replace(LOCALAI, options=one_more))
    assert other.space_id == c.space_id == pubmed().space_id, "the engine is outside space_id"
    assert other.collection_id != c.collection_id, "the engine is inside collection_id"
    assert replace(LOCALAI, options=one_more).engine_id != LOCALAI.engine_id


def test_options_are_refused_in_a_version_3_codex():
    """Below version 4 `options` is an unknown key (09 §9.18): refused on
    write and on read, never stripped."""
    with pytest.raises(CodexError, match="version 4 field"):
        cx.dumps(_with(LOCALAI, version=3))
    d = _with(LOCALAI).to_dict()
    d["version"] = 3
    with pytest.raises(CodexError, match="version 4 field"):
        cx.loads(canonical_json(d))


@pytest.mark.parametrize("value", [
    "/models/bge-m3.gguf",                  # the weights, not as ${WEIGHTS}
    "mmproj:/models/projector.gguf",        # a second file no digest covers
    "--lora=/srv/adapter.safetensors",
    "https://api.example.invalid/v1",       # a service a stranger cannot run
])
def test_an_option_naming_another_absolute_path_is_refused(value):
    bad = replace(LOCALAI, options=dict(OPTIONS, Extra=value))
    with pytest.raises(CodexError, match="absolute path"):
        cx.dumps(_with(bad))
    nested = replace(LOCALAI, options=dict(OPTIONS, Options=["parallel:1", value]))
    with pytest.raises(CodexError, match="absolute path"):
        cx.dumps(_with(nested))


def test_the_placeholders_and_relative_names_are_accepted():
    ok = replace(LOCALAI, options=dict(OPTIONS, MMProj="${MODELS}/proj.gguf",
                                       Tokenizer="tokenizers/bge"))
    cx.dumps(_with(ok))


def test_a_non_integer_number_is_refused_and_its_decimal_string_accepted():
    with pytest.raises(CodexError, match="decimal string"):
        cx.dumps(_with(replace(LOCALAI, options=dict(OPTIONS, RopeFreqBase=10000.5))))
    d = _with(LOCALAI).to_dict()
    raw = json.dumps(d).replace('"10000.5"', "10000.5").encode()
    with pytest.raises(CodexError):
        cx.loads(raw)


@pytest.mark.parametrize("value,rule", [(None, "never null"), ({}, "never empty")])
def test_options_are_absent_when_there_are_none_never_null_or_empty(value, rule):
    d = _with(LOCALAI).to_dict()
    d["space"]["encoder"]["engine"]["options"] = value
    with pytest.raises(CodexError, match=rule if value is not None else "ABSENT"):
        cx.loads(canonical_json(d))


# ---- the sister ----------------------------------------------------------------------

def test_a_sister_naming_an_engine_with_options_is_version_4_in_her_mothers_space(tmp_path):
    """The engine is outside `space_id`, so a sister may name a new one in
    her mother's space — and an engine with options needs version 4 there
    too. One without keeps her mother's version, as before."""
    mother = a_root(tmp_path, pubmed(version=3))
    sp = mother.space
    localai = replace(sp, encoder=replace(sp.encoder, engine=LOCALAI),
                      query=replace(sp.query, encoder=replace(sp.query.encoder, engine=LOCALAI)))
    sister = col.derive(tmp_path, mother.collection_id, space=localai,
                        embed=fake_embed, embed_query=fake_query_embed, key=KEY, now=NOW)
    assert sister.version == 4 and sister.space_id == mother.space_id
    weekly = col.derive(tmp_path, mother.collection_id, update=cx.Update("weekly", 1, 0))
    assert weekly.version == 3 and weekly.space_id == mother.space_id


# ---- the statement -------------------------------------------------------------------

def _localai_run(codex):
    run = {"driver": "kernel 6.18.0; xe 9F2C1A; libze-intel-gpu1 26.05.1",
           "engine": LOCALAI.to_dict(), "weights": codex.space.encoder.weights}
    return emb.StatedRun(run_id=digest(canonical_json(run)), engine=run["engine"],
                         weights=run["weights"], driver=run["driver"])


def test_a_run_with_options_is_signed_at_version_3_and_its_engine_id_counts_them():
    codex = codex_768()
    ref = reference()
    run = gate(codex, ref, ref=ref, stated=_localai_run(codex))
    body = emb.read_statement(emb.statement(run, codex, a_key(), now=NOW))
    assert body["version"] == emb.STATEMENT_VERSION == 3
    assert body["engine"]["options"] == OPTIONS
    assert emb.benchmarks_entry(body)["encoder_engine"] == LOCALAI.engine_id


def test_a_run_whose_options_name_another_file_is_not_signed():
    """A statement is published and adopted; one naming a file no weights
    digest covers could never be repaired by those who adopted it."""
    codex = codex_768()
    ref = reference()
    bad = replace(LOCALAI, options=dict(OPTIONS, MMProj="/models/projector.gguf"))
    stated = _localai_run(codex)
    stated = replace(stated, engine=bad.to_dict())
    run = gate(codex, ref, ref=ref, stated=stated)
    with pytest.raises(emb.GateError, match="absolute path"):
        emb.statement(run, codex, a_key(), now=NOW)


def _resigned(body: dict) -> dict:
    from app.hold import envelope as env
    return env.sign(env.CALIBRATION, body, a_key())


def test_a_version_2_statement_is_still_read_and_unknown_versions_are_not():
    """09 §9.12: "a version-2 statement stays valid as written and is read
    beside version 3" — its run is unchanged, and gating it again would
    recompute a verdict already held."""
    codex = codex_768()
    ref = reference()
    run = gate(codex, ref, ref=ref, stated=a_stated_run(codex))
    body = emb.read_statement(emb.statement(run, codex, a_key(), now=NOW))
    as_v2 = dict(body, version=2)
    assert emb.read_statement(_resigned(as_v2))["version"] == 2
    with pytest.raises(emb.GateError, match="version 4"):
        emb.read_statement(_resigned(dict(body, version=4)))
    with_options = dict(as_v2, engine=LOCALAI.to_dict())
    with pytest.raises(emb.GateError, match="version-3 body"):
        emb.read_statement(_resigned(with_options))

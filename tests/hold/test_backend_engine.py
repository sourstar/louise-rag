"""The VeGa amendment (2026-10-08; 09 §9.3 *a backend engine*, §9.12,
§9.18; 02 §2.7; docs/notes/vega.md §4).

A backend engine is one a model runner installed from its own gallery and
VeGa's wrapper launches. It is told by its bytes: its program, `args[0]`,
begins with `${BACKEND}/`, "which no other engine may carry", and its
`image` is the tree digest of the backend's installed files. It is admitted
from Codex version 6 and calibration statement version 4, and those
versions are written for an object carrying one ALONE: every other Codex is
written at 5 and every other statement at 3, so no byte already written
moves (CLAUDE.md RULE 1) — pinned below, byte for byte.
"""
from dataclasses import replace

import pytest

from app.hold import codex as cx, collection as col, embedder as emb
from app.hold.codex import CodexError, Engine, canonical_json, digest

from .conftest import (ENGINE, KEY, NOW, PLATFORM, a_root, fake_embed, fake_query_embed,
                       pubmed, pubmed_v2)
from .test_embedder import a_key, a_stated_run, codex_768, gate, reference
from .test_engine_options import OPTIONS
from .test_template import ENGINE_TABLE, from_recipe, recipe

TREE = "sha256:" + "5d" * 32

#: 09 §9.3's table, *a backend engine*: what VeGa's wrapper writes.
BACKEND = Engine(name="localai/llama-cpp", build=TREE[7:19], variant=None, image=TREE,
                 args=("${BACKEND}/run.sh", "--addr", "127.0.0.1:${PORT}"),
                 env={}, device=ENGINE.device, options=OPTIONS)


def _with(engine: Engine, query_engine: Engine | None = None, **over):
    """pubmed's Codex with `engine` on the document side and `query_engine`
    (default: the same) on the query side."""
    c = pubmed(**over)
    sp = c.space
    q = engine if query_engine is None else query_engine
    return replace(c, space=replace(
        sp, encoder=replace(sp.encoder, engine=engine),
        query=replace(sp.query, encoder=replace(sp.query.encoder, engine=q))))


# ---- byte stability: nothing already written moves -----------------------------------

#: Taken BEFORE the amendment was implemented (2026-10-08), from the same
#: fixtures: (collection_id, space_id, codex_hash).
PINNED = {
    "v4": ("sha256:d40fedd065b711feeb98d7ec75f8f2c3c31e65e83b803c7b9eed082fe8a8de47",
           "sha256:96aee088cbb9e42c113f53f5f333c3ca864a96370ef835c91fe21aea2857878a",
           "sha256:99f6becdff2fa05fd879fe0a8831ca51d35ccbb74805c1c7f4afd0df29a37332"),
    "v5": ("sha256:db6c4ddff77e469f223a367bce3bf982420b381f01a1fb643f0993b74adab552",
           "sha256:a9ebf9dd85fc09fbb93cb1c5e8925480258c85db45509e661bdbcbf4d32690e5",
           "sha256:64b67adfa8cd391c0becc7c144e3794c63d7907310fb37a54c556c15a6921a03"),
    "v3": ("sha256:f394a576f5d094ae9c3602405a07fa6404778f5535bd07b47f2ac34256229071",
           "sha256:96aee088cbb9e42c113f53f5f333c3ca864a96370ef835c91fe21aea2857878a",
           "sha256:34e6722823e3b73ef157769cee2ef54dc552d711af666eb82eb7e07d8256f914"),
    "v2": ("sha256:b22aed2bb481c1da2a3a75fd0bd1a704320c246d88d2ec955136daea52952810",
           "sha256:03ae64488c899305491dcc1fe8c9aad369cc79d219829d5ff31a9d5fcc663e0c",
           "sha256:a5070b29cb79b78d7eedc183d5c3d184e5385e8c19cc5b14068ab7ae0fc50e96"),
}
#: The digest of the body of a statement of an argv (RAGE) run, as written
#: before the amendment, `tokens_per_s` zeroed: it is a timing, measured anew
#: by every pass, and the only field not a function of the inputs.
PINNED_STATEMENT = "sha256:31824734ce189f20207adecb2811fb877325932aca72f6ab119e894d2b09352e"


@pytest.mark.parametrize("name,codex", [
    ("v4", pubmed()), ("v5", pubmed(version=5)), ("v3", pubmed(version=3)),
    ("v2", pubmed_v2())])
def test_no_existing_codex_moves(name, codex):
    raw = cx.dumps(codex)
    again = cx.loads(raw)
    assert (again.collection_id, again.space_id, again.codex_hash) == PINNED[name]
    assert cx.dumps(again) == raw


def test_an_argv_run_is_still_signed_at_3_byte_for_byte():
    codex = codex_768(version=5)
    ref = reference()
    run = gate(codex, ref, ref=ref, stated=a_stated_run(codex))
    body = emb.statement(run, codex, a_key(), now=NOW)["body"]
    assert body["version"] == 3
    assert digest(canonical_json(dict(body, tokens_per_s=0))) == PINNED_STATEMENT


@pytest.mark.parametrize("version", [2, 3, 4, 5])
def test_the_version_chosen_for_a_space_without_a_backend_engine_is_unchanged(version):
    space = (pubmed_v2() if version == 2 else pubmed(version=version)).space
    assert cx.written_version(space, version) == version


def test_a_new_recipe_with_an_argv_engine_is_still_written_at_5():
    c = from_recipe("gutenberg", recipe(), platform_image=PLATFORM)
    assert c.version == cx.CODEX_VERSION == 5


# ---- telling a backend engine ----------------------------------------------------------

@pytest.mark.parametrize("args,backend", [
    (("${BACKEND}/run.sh", "--addr", "127.0.0.1:${PORT}"), True),
    (("${BACKEND}/bin/llama", "-m", "${WEIGHTS}"), True),
    (("llama-server", "-m", "${WEIGHTS}"), False),
    (("/backends/llama-cpp/run.sh",), False),
    (("${BACKEND}run.sh",), False),            # no separator: not the directory
    (("x${BACKEND}/run.sh",), False),          # not at the start
    ((), False),
])
def test_a_backend_engine_is_told_by_its_program(args, backend):
    assert cx.is_backend_args(args) is backend
    assert cx.is_backend_args(list(args)) is backend


# ---- the Codex: accept / refuse ----------------------------------------------------------

def test_a_version_6_codex_with_a_backend_engine_round_trips():
    c = _with(BACKEND, version=6)
    raw = cx.dumps(c)
    again = cx.loads(raw)
    assert again.version == 6 and again.space.has_backend
    assert again.space.encoder.engine == BACKEND
    assert cx.dumps(again) == raw
    # a version-6 Codex's layers carry `input`, as every one from version 5
    assert again.to_dict()["layers"][0]["input"] == cx.INPUT_TEXT


def test_the_backend_engine_is_outside_space_id():
    """A version-5 and a version-6 Codex of one recipe share a space: the
    engine never enters `space_id` (02 §2.4), and both carry `input`."""
    assert _with(BACKEND, version=6).space_id == pubmed(version=5).space_id


def test_a_backend_engine_on_the_query_side_alone_makes_version_6():
    """"A Codex NONE of whose engines is a backend engine is written at 5"."""
    c = _with(ENGINE, BACKEND, version=6)
    assert cx.loads(cx.dumps(c)).version == 6
    with pytest.raises(CodexError, match="admitted from Codex version 6"):
        cx.dumps(_with(ENGINE, BACKEND, version=5))


@pytest.mark.parametrize("version", [3, 4, 5])
def test_a_backend_engine_below_version_6_is_refused_on_write(version):
    engine = BACKEND if version >= cx.OPTIONS_VERSION else replace(BACKEND, options=None)
    with pytest.raises(CodexError, match="admitted from Codex version 6"):
        cx.dumps(_with(engine, version=version))


def test_a_backend_engine_in_a_version_5_file_is_refused_on_read():
    d = _with(BACKEND, version=6).to_dict()
    d["version"] = 5
    with pytest.raises(CodexError, match="admitted from Codex version 6"):
        cx.loads(canonical_json(d))


def test_a_version_6_codex_without_a_backend_engine_is_refused():
    """09 §9.3, §9.18: version 6 is "written for a Codex that carries one
    alone — every other is written at 5"."""
    with pytest.raises(CodexError, match="written at version 5"):
        cx.dumps(pubmed(version=6))
    d = pubmed(version=5).to_dict()
    d["version"] = 6
    with pytest.raises(CodexError, match="written at version 5"):
        cx.loads(canonical_json(d))


def test_an_unknown_codex_version_is_still_refused():
    d = _with(BACKEND, version=6).to_dict()
    # 7 is the annotations Codex since 2026-10-09 (02 §2.10): a version-6
    # object relabelled 7 lacks `annotates` and is refused for that; 8 is the
    # token cut's since 2026-10-18 (tests/hold/test_cut.py); 9 is unknown
    d["version"] = 7
    with pytest.raises(CodexError, match="missing field.*annotates"):
        cx.loads(canonical_json(d))
    d["version"] = 9
    with pytest.raises(CodexError, match="unsupported Codex version 9"):
        cx.loads(canonical_json(d))


@pytest.mark.parametrize("version", [3, 4, 5, 6])
@pytest.mark.parametrize("args", [
    ("llama-server", "-m", "${WEIGHTS}", "--lib", "${BACKEND}/lib", "--port", "${PORT}"),
    ("${BACKEND}run.sh", "--addr", "127.0.0.1:${PORT}"),
    ("/srv/run.sh", "--root", "${BACKEND}"),
])
def test_backend_placeholder_outside_a_backend_engines_program_is_refused(version, args):
    """`${BACKEND}` is a backend engine's alone, at any version."""
    bad = replace(ENGINE, args=args)
    query = BACKEND if version == 6 else ENGINE      # so a v6 Codex is otherwise valid
    with pytest.raises(CodexError, match="only a backend engine"):
        cx.dumps(_with(bad, query, version=version))


def test_a_backend_engine_may_name_its_directory_again_in_its_arguments():
    ok = replace(BACKEND, args=("${BACKEND}/run.sh", "--lib", "${BACKEND}/lib",
                                "--addr", "127.0.0.1:${PORT}"))
    cx.loads(cx.dumps(_with(ok, version=6)))


@pytest.mark.parametrize("image", [
    "5d" * 32,                                   # no prefix
    "sha256:" + "5D" * 32,                       # not lowercase
    "sha256:" + "5d" * 31,                       # short
    "sha512:" + "5d" * 64,
    "localai/llama-cpp:latest-gpu-intel-sycl-f16",   # the gallery's tag
])
def test_a_backend_engines_image_is_a_tree_digest(image):
    with pytest.raises(CodexError, match="tree digest"):
        cx.dumps(_with(replace(BACKEND, image=image), version=6))


# ---- the writers choose 6 for a backend engine alone ---------------------------------------

def _backend_recipe(**over):
    r = recipe(**over)
    table = dict(BACKEND.to_dict())
    r["space"] = dict(r["space"], encoder=dict(r["space"]["encoder"], engine=table))
    return r


def test_a_new_recipe_with_a_backend_engine_is_written_at_6():
    c = from_recipe("gutenberg", _backend_recipe(), platform_image=PLATFORM)
    assert c.version == 6
    cx.loads(cx.dumps(c))
    # the same recipe with an argv engine: version 5, the same space
    argv = from_recipe("gutenberg", recipe(), platform_image=PLATFORM)
    assert argv.version == 5 and argv.space_id == c.space_id


def test_a_space_from_copy_of_a_version_6_space_is_6_and_the_same_space():
    """02 §2.7 (amended 2026-10-08): "the copy carries that engine, so it is
    written at version 6, as any Codex carrying one is, and a copy of a
    version-5 space stays at 5"."""
    v6 = from_recipe("gutenberg", _backend_recipe(), platform_image=PLATFORM,
                     space_version=6)
    v5 = from_recipe("gutenberg", recipe(), platform_image=PLATFORM, space_version=5)
    assert (v6.version, v5.version) == (6, 5)
    assert v6.space_id == v5.space_id


def test_create_space_from_a_version_6_source(tmp_path, monkeypatch):
    """The whole `create --space-from` path: the copy is version 6, carries
    the source's engine, and is the source's space."""
    from app import louise
    from app.bundles import keys as node_keys
    root = tmp_path / "collections"
    source = col.create(root, _with(BACKEND, version=6), name="pubmed-vega")
    monkeypatch.setattr(node_keys, "load_signing_key", lambda: KEY)
    monkeypatch.setattr(louise, "_embed_for_recipe",
                        lambda space, url, model=None, **kw:
                        fake_query_embed if "query" in url else fake_embed)
    codex, _ = louise.create_from(root, "gutenberg",
                                  recipe(cleaning_version=source.cleaning_version),
                                  platform_image=PLATFORM, space_from=source.collection_id,
                                  encoder="http://enc.invalid/upstream/article",
                                  query_encoder="http://enc.invalid/upstream/query")
    assert codex.version == 6
    assert codex.space.encoder.engine == BACKEND
    assert codex.space_id == source.space_id


def test_a_sister_naming_a_backend_engine_in_a_version_5_mothers_space_is_6(tmp_path):
    mother = a_root(tmp_path, pubmed(version=5))
    sp = mother.space
    vega = replace(sp, encoder=replace(sp.encoder, engine=BACKEND),
                   query=replace(sp.query, encoder=replace(sp.query.encoder, engine=BACKEND)))
    sister = col.derive(tmp_path, mother.collection_id, space=vega,
                        embed=fake_embed, embed_query=fake_query_embed, key=KEY, now=NOW)
    assert sister.version == 6 and sister.space_id == mother.space_id
    weekly = col.derive(tmp_path, mother.collection_id, update=cx.Update("weekly", 1, 0))
    assert weekly.version == 5


def test_a_version_6_mothers_sisters_follow_their_own_engines(tmp_path):
    mother = a_root(tmp_path, _with(BACKEND, version=6))
    weekly = col.derive(tmp_path, mother.collection_id, update=cx.Update("weekly", 1, 0))
    assert weekly.version == 6 and weekly.space_id == mother.space_id
    sp = mother.space
    argv = replace(sp, encoder=replace(sp.encoder, engine=ENGINE),
                   query=replace(sp.query, encoder=replace(sp.query.encoder, engine=ENGINE)))
    sister = col.derive(tmp_path, mother.collection_id, space=argv,
                        embed=fake_embed, embed_query=fake_query_embed, key=KEY, now=NOW)
    assert sister.version == 5 and sister.space_id == mother.space_id


def test_a_sister_cannot_bring_a_backend_engine_into_a_version_4_space(tmp_path):
    """Version 6's layers carry `input` and a version-4 mother's do not, so
    she could not keep the space she claims to stay in."""
    mother = a_root(tmp_path, pubmed())
    sp = mother.space
    vega = replace(sp, encoder=replace(sp.encoder, engine=BACKEND),
                   query=replace(sp.query, encoder=replace(sp.query.encoder, engine=BACKEND)))
    with pytest.raises(CodexError, match="cannot stay in her mother's space"):
        col.derive(tmp_path, mother.collection_id, space=vega,
                   embed=fake_embed, embed_query=fake_query_embed, key=KEY, now=NOW)


def test_derive_space_from_a_version_6_source_is_6(tmp_path):
    mother = a_root(tmp_path, pubmed(version=5, update=cx.Update("weekly", 1, 0)))
    source = a_root(tmp_path, _with(BACKEND, version=6))
    sister = col.derive(tmp_path, mother.collection_id,
                        space_from=col.read(tmp_path, source.collection_id))
    assert sister.version == 6 and sister.space_id == source.space_id


def test_a_corrective_sister_and_a_daughter_keep_version_6_with_the_engine(tmp_path):
    from .test_collection import A_DEFECT
    mother = a_root(tmp_path, _with(BACKEND, version=6))
    fixed = col.replace(tmp_path, mother.collection_id, defect=A_DEFECT)
    assert fixed.version == 6 and fixed.space.encoder.engine == BACKEND
    daughter = col.continue_(tmp_path, mother.collection_id, platform_image=PLATFORM,
                             continues_through="2026-10")
    assert daughter.version == 6 and daughter.space_id == mother.space_id
    plain = col.replace(tmp_path, a_root(tmp_path, pubmed(version=5)).collection_id,
                        defect=A_DEFECT)
    assert plain.version == 5


# ---- the peer's calibration statement ------------------------------------------------------

def _vega_run(codex, engine=BACKEND):
    run = {"driver": "kernel 6.18.0; xe 9F2C1A; libze_intel_gpu.so.1 sha256:" + "ab" * 32,
           "engine": engine.to_dict(), "weights": codex.space.encoder.weights}
    return emb.StatedRun(run_id=digest(canonical_json(run)), engine=run["engine"],
                         weights=run["weights"], driver=run["driver"])


def _resigned(body: dict) -> dict:
    from app.hold import envelope as env
    return env.sign(env.CALIBRATION, body, a_key())


def _body(stated) -> dict:
    codex = codex_768(version=5)
    ref = reference()
    run = gate(codex, ref, ref=ref, stated=stated(codex))
    return emb.read_statement(emb.statement(run, codex, a_key(), now=NOW))


def test_a_backend_engines_run_is_stated_at_4():
    body = _body(_vega_run)
    assert body["version"] == emb.BACKEND_STATEMENT_VERSION == 4
    assert body["engine"] == BACKEND.to_dict()
    assert emb.benchmarks_entry(body)["encoder_engine"] == BACKEND.engine_id


def test_every_other_run_is_still_stated_at_3():
    assert _body(a_stated_run)["version"] == emb.STATEMENT_VERSION == 3


def test_a_backend_engine_in_a_version_3_statement_is_refused():
    body = _body(_vega_run)
    with pytest.raises(emb.GateError, match="stated at version 4"):
        emb.read_statement(_resigned(dict(body, version=3)))
    no_options = {k: v for k, v in body["engine"].items() if k != "options"}
    with pytest.raises(emb.GateError, match="stated at version 4"):
        emb.read_statement(_resigned(dict(body, version=2, engine=no_options)))


def test_a_version_4_statement_of_an_argv_run_is_refused():
    body = _body(a_stated_run)
    with pytest.raises(emb.GateError, match="backend engine's run alone"):
        emb.read_statement(_resigned(dict(body, version=4)))


def test_an_unknown_statement_version_is_refused():
    body = _body(_vega_run)
    with pytest.raises(emb.GateError, match="version 5"):
        emb.read_statement(_resigned(dict(body, version=5)))


def test_a_run_whose_engine_misplaces_backend_is_not_signed():
    bad = replace(ENGINE, args=ENGINE.args + ("--lib", "${BACKEND}/lib"))
    codex = codex_768(version=5)
    ref = reference()
    run = gate(codex, ref, ref=ref, stated=_vega_run(codex, bad))
    with pytest.raises(emb.GateError, match="only a backend engine"):
        emb.statement(run, codex, a_key(), now=NOW)

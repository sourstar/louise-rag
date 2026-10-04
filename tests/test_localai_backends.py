"""deploy/localai/backends — the three Python models as LocalAI backends
(migration step 15e-2). The models themselves are their servers', tested
beside them (test_madlad_translate_server.py, test_medcpt_rerank_server.py)
and measured on the node; here, with stand-in models and stand-in gRPC
messages, that each backend reads its settings from the load call alone and
answers in the messages LocalAI reads — and, for the translator, that the
platform's client and the backend agree on the one prompt that carries a batch.

The stand-in messages take only the fields LocalAI's backend.proto gives them
(58830f7), so a misspelt field fails here and not on the node.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKENDS = ROOT / "deploy" / "localai" / "backends"


def _exec(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _backend(directory: str, server_name: str, server: str):
    """backend.py as the image lays it out: louise_backend.py and the server,
    under its own name, beside it (deploy/rage/Dockerfile.localai)."""
    shared = {"louise_backend": BACKENDS / "louise_backend.py", server_name: ROOT / server}
    saved = {k: sys.modules.get(k) for k in shared}
    try:
        for name, path in shared.items():
            sys.modules[name] = _exec(name, path)
        return _exec(f"backend_{server_name}", BACKENDS / directory / "backend.py")
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


lb = _exec("louise_backend_under_test", BACKENDS / "louise_backend.py")
medcpt_backend = _backend("louise-medcpt-rerank", "medcpt", "deploy/medcpt-rerank/server.py")
dar_backend = _backend("louise-dar-legal-it", "dar_legal_it", "deploy/dar-legal-it/server.py")
madlad_backend = _backend("louise-madlad", "madlad", "deploy/madlad-translate/server.py")


# ---- stand-in gRPC ------------------------------------------------------------------------

def _message(name: str, fields: tuple[str, ...]):
    def init(self, **kw):
        unknown = set(kw) - set(fields)
        if unknown:
            raise TypeError(f"{name} has no field {sorted(unknown)} (backend.proto)")
        self.__dict__.update(kw)
    return type(name, (), {"__init__": init, "__repr__": lambda s: f"{name}({s.__dict__})"})


PB = SimpleNamespace(
    Reply=_message("Reply", ("message", "tokens", "prompt_tokens")),
    Result=_message("Result", ("message", "success", "metadata")),
    EmbeddingResult=_message("EmbeddingResult",
                             ("embeddings", "tokens", "dim", "prompt_tokens", "layout")),
    RerankResult=_message("RerankResult", ("usage", "results")),
    Usage=_message("Usage", ("total_tokens", "prompt_tokens")),
    DocumentResult=_message("DocumentResult", ("index", "text", "relevance_score")),
    EMBEDDING_LAYOUT_FINAL=1)
STATUS = SimpleNamespace(**{c: c for c in ("FAILED_PRECONDITION", "INVALID_ARGUMENT",
                                           "RESOURCE_EXHAUSTED", "INTERNAL")})


class Aborted(Exception):
    pass


class Context:
    def abort(self, code, details):
        raise Aborted(code, details)


def load_call(model_file, *, options=(), context_size=0, batch=0):
    return SimpleNamespace(ModelFile=str(model_file), Options=list(options),
                           ContextSize=context_size, NBatch=batch)


def aborted(call) -> tuple[str, str]:
    with pytest.raises(Aborted) as e:
        call()
    return e.value.args


# ---- what they share ---------------------------------------------------------------------

def test_options_are_typed_and_a_misspelt_one_is_refused_not_defaulted():
    known = {"device": lb.device, "beams": lb.positive, "prefix": str}
    got = lb.options(["device:cpu", "prefix:query: "], known, {"device": "xpu", "beams": 1})
    assert got == {"device": "cpu", "beams": 1, "prefix": "query: "}, \
        "a value keeps every colon after the first"
    for bad, why in ((["devise:cpu"], "reads"), (["device"], "reads"),
                     (["device:gpu"], "one of"), (["beams:0"], "positive"),
                     (["beams:1", "beams:2"], "twice")):
        with pytest.raises(lb.LoadError, match=why):
            lb.options(bad, known, {})


def test_weights_that_are_not_on_disk_are_an_error_never_a_download(tmp_path):
    with pytest.raises(lb.LoadError, match="Nothing is fetched"):
        lb.weights(str(tmp_path / "absent"))
    with pytest.raises(lb.LoadError, match="names no weights"):
        lb.weights("")
    assert lb.weights(str(tmp_path)) == tmp_path


def test_run_sh_execs_the_backends_own_python_on_its_backend_py(tmp_path):
    """LocalAI lists the pid it spawned; `exec` keeps it the backend's, whose
    program RAGE finds in the image's backends path."""
    d = tmp_path / "louise-x"
    (d / "venv" / "bin").mkdir(parents=True)
    (d / "run.sh").write_bytes((BACKENDS / "run.sh").read_bytes())
    (d / "run.sh").chmod(0o755)
    python = d / "venv" / "bin" / "python"
    python.write_text('#!/bin/sh\necho "$$ $0 $* LD=${LD_LIBRARY_PATH:-}"\n')
    python.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin"}
    ran = subprocess.run(["sh", "-c", f'echo "$$"; exec {d}/run.sh --addr 127.0.0.1:4001'],
                         capture_output=True, text=True, env=env, check=True)
    shell_pid, line = ran.stdout.split("\n", 1)
    pid, program, rest = line.split(" ", 2)
    assert pid == shell_pid, "the same process all the way: exec, never a child"
    assert program == f"{d}/venv/bin/python"
    assert rest.strip() == f"{d}/backend.py --addr 127.0.0.1:4001 LD="
    ran = subprocess.run(["./run.sh", "--addr", "x"], cwd=d, capture_output=True, text=True,
                         env=env, check=True)
    assert ran.stdout.split(" ")[1] == f"{d}/venv/bin/python", \
        "started as LocalAI starts it, from its own directory: still an absolute program"
    (d / "lib").mkdir()
    ran = subprocess.run([str(d / "run.sh"), "--addr", "x"], capture_output=True, text=True,
                         env=env, check=True)
    assert ran.stdout.strip().endswith(f"LD={d}/lib"), "lib/ goes in front"


# ---- MedCPT: Rerank ----------------------------------------------------------------------

def _medcpt_dir(tmp_path):
    d = tmp_path / "medcpt-cross-ov"
    d.mkdir()
    (d / "openvino_model.xml").write_text("<net/>")
    (d / "tokenizer.json").write_text("{}")
    return d


def _medcpt(tmp_path, seen=None):
    """A stand-in model whose logit is the text's length minus 5."""
    seen = {} if seen is None else seen

    def encoder(tokenizer_json, max_tokens):
        seen["encoder"] = (tokenizer_json, max_tokens)
        return lambda pairs: pairs

    def infer(xml, device, cache_dir):
        seen["infer"] = (xml, device, cache_dir)
        return lambda pairs: [float(len(t)) - 5.0 for _q, t in pairs]
    return medcpt_backend.Servicer(PB, STATUS, encoder=encoder, infer=infer)


def test_medcpt_loads_from_the_load_call_and_its_models_env(tmp_path, monkeypatch):
    seen: dict = {}
    s = _medcpt(tmp_path, seen)
    d = _medcpt_dir(tmp_path)
    monkeypatch.setenv("OV_CACHE_DIR", "/state/ov-cache")
    r = s.LoadModel(load_call(d, options=["device:xpu"], context_size=512, batch=16), None)
    assert r.success, r.message
    assert seen["encoder"] == (str(d / "tokenizer.json"), 512), "ContextSize: tokens per pair"
    assert seen["infer"] == (str(d / "openvino_model.xml"), "GPU", "/state/ov-cache")
    assert s.reranker.batch == 16, "NBatch: pairs per inference"


@pytest.mark.parametrize("call,why", [
    (lambda d: load_call(d / "absent", context_size=512, batch=16), "absent"),
    (lambda d: load_call(d, options=["devise:cpu"], context_size=512, batch=16), "reads"),
    (lambda d: load_call(d, context_size=512), "batch"),
    (lambda d: load_call(d, batch=16), "context_size"),
])
def test_medcpt_refuses_a_load_it_cannot_honour(tmp_path, call, why):
    r = _medcpt(tmp_path).LoadModel(call(_medcpt_dir(tmp_path)), None)
    assert not r.success and why in r.message


def test_medcpt_scores_every_document_at_its_own_index_as_teis_sigmoid(tmp_path):
    s = _medcpt(tmp_path)
    s.LoadModel(load_call(_medcpt_dir(tmp_path), context_size=512, batch=2), None)
    docs = ["abc", "abcdefgh", "a", "abcde", "ab"]
    out = s.Rerank(SimpleNamespace(query="q", documents=docs, top_n=0), Context())
    assert [r.index for r in out.results] == [1, 3, 0, 4, 2]
    assert [r.text for r in out.results] == [docs[r.index] for r in out.results]
    assert [round(r.relevance_score, 6) for r in out.results] == \
        [round(1 / (1 + math.exp(-x)), 6) for x in (3.0, 0.0, -2.0, -3.0, -4.0)]
    top = s.Rerank(SimpleNamespace(query="q", documents=docs, top_n=2), Context())
    assert [r.index for r in top.results] == [1, 3]
    assert s.Rerank(SimpleNamespace(query="q", documents=[], top_n=0), Context()).results == []


def test_medcpt_unloaded_says_so(tmp_path):
    code, _ = aborted(lambda: _medcpt(tmp_path).Rerank(
        SimpleNamespace(query="q", documents=["a"], top_n=0), Context()))
    assert code == "FAILED_PRECONDITION"


def test_the_platform_reads_localais_rerank_of_it_as_jina(tmp_path):
    """LocalAI's /v1/rerank answers the backend's results in Jina's shape
    (`core/http/endpoints/jina/rerank.go`), which `api = "jina"` reads."""
    from app import retrieval
    from app.collections import RerankRecipe
    s = _medcpt(tmp_path)
    s.LoadModel(load_call(_medcpt_dir(tmp_path), context_size=512, batch=4), None)

    def localai(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "medcpt-rerank"
        out = s.Rerank(SimpleNamespace(query=body["query"], documents=body["documents"],
                                       top_n=body.get("top_n", 0)), Context())
        return httpx.Response(200, json={"model": body["model"], "usage": {}, "results": [
            {"index": r.index, "document": {"text": r.text},
             "relevance_score": r.relevance_score} for r in out.results]})

    hits = [{"text": t, "id": i} for i, t in enumerate(["abc", "abcdefgh", "a"])]
    recipe = RerankRecipe(url="http://rage/upstream/medcpt-rerank/v1/rerank", api="jina",
                          model="medcpt-rerank")
    with httpx.Client(transport=httpx.MockTransport(localai)) as client:
        old, retrieval.httpc = retrieval.httpc, client
        try:
            ranked, ok = retrieval._rerank_list("q", hits, recipe, budget_s=5.0)
        finally:
            retrieval.httpc = old
    assert ok and [h["id"] for h in ranked] == [1, 0, 2]


# ---- DAR-legal-it: Embedding -------------------------------------------------------------

def _dar(seen=None):
    seen = [] if seen is None else seen

    def build(model_dir, device, max_seq):
        seen.append(("build", model_dir, device, max_seq))

        def encode(batch):
            seen.append(("encode", list(batch)))
            return [[float(len(t)), 0.5] for t in batch]
        return encode, 2
    return dar_backend.Servicer(PB, STATUS, build=build)


def test_dar_legal_it_loads_from_the_load_call(tmp_path):
    seen: list = []
    s = _dar(seen)
    r = s.LoadModel(load_call(tmp_path, options=["device:cpu"], context_size=8192), None)
    assert r.success, r.message
    assert seen == [("build", str(tmp_path), "cpu", 8192)], "ContextSize: the longest text"
    r = _dar().LoadModel(load_call(tmp_path), None)
    assert not r.success and "context_size" in r.message


def test_dar_legal_it_embeds_one_text_as_sent_and_declares_it_pooled(tmp_path):
    """Louise prepends the Codex's query instruction itself (app/embeddings.py
    `_asked`); a prefix added here would be a query side no Codex names."""
    seen: list = []
    s = _dar(seen)
    s.LoadModel(load_call(tmp_path, context_size=8192), None)
    out = s.Embedding(SimpleNamespace(Embeddings="query: art. 2043 c.c."), Context())
    assert seen[-1] == ("encode", ["query: art. 2043 c.c."]), "the text, untouched"
    assert out.embeddings == [21.0, 0.5] and out.dim == 2 and out.tokens == 1
    assert out.layout == PB.EMBEDDING_LAYOUT_FINAL, "LocalAI then pools nothing itself"


def test_dar_legal_it_refuses_rather_than_embedding_nothing(tmp_path):
    assert aborted(lambda: _dar().Embedding(SimpleNamespace(Embeddings="x"),
                                            Context()))[0] == "FAILED_PRECONDITION"
    s = _dar()
    s.LoadModel(load_call(tmp_path, context_size=8192), None)
    assert aborted(lambda: s.Embedding(SimpleNamespace(Embeddings=""),
                                       Context()))[0] == "INVALID_ARGUMENT"


# ---- MADLAD: Predict, the batch in one prompt --------------------------------------------

KNOWN_TARGETS = {"<2de>", "<2it>", "<2la>"}


def _madlad(seen=None):
    """Upper-cases each segment behind its target tag; records what it is
    loaded with and the batches it is handed."""
    seen = {"batches": []} if seen is None else seen

    def load(model_id, device, dtype_name, beams=None):
        seen["load"] = (model_id, device, dtype_name, beams)

        def generate(prompts, max_new):
            seen["batches"].append(list(prompts))
            return [p.split(" ", 1)[0] + "|" + p.split(" ", 1)[1].upper() for p in prompts]
        return (lambda t: len(t.split()), generate, lambda t: t in KNOWN_TARGETS,
                ["de", "it", "la"], lambda: 7, None, None)
    return madlad_backend.Servicer(PB, STATUS, load=load,
                                   settle=lambda path: seen.setdefault("settled", path))


def _loaded_madlad(tmp_path, seen=None, options=("device:cpu", "max_items:3"), batch=4):
    s = _madlad(seen)
    r = s.LoadModel(load_call(tmp_path, options=options, batch=batch), None)
    assert r.success, r.message
    return s


def _predict(s, prompt: str) -> object:
    return json.loads(s.Predict(SimpleNamespace(Prompt=prompt), Context()).message)


def test_madlad_loads_from_the_load_call_and_warms_before_it_answers(tmp_path):
    seen: dict = {"batches": []}
    s = _loaded_madlad(tmp_path, seen, options=("device:cpu", "dtype:float32", "beams:2",
                                                "max_seg_tokens:20"), batch=8)
    assert seen["load"] == (str(tmp_path), "cpu", "float32", 2)
    assert s.translator.batch == 8, "NBatch: segments per pass"
    assert s.translator.max_seg == 20
    assert seen["batches"][0] == ["<2de> the first pass compiles its kernels"]
    assert seen["settled"] == str(tmp_path)


@pytest.mark.parametrize("options,batch,why", [
    (["dtype:fp8"], 4, "one of"), (["park:true"], 4, "reads"), ([], 0, "batch"),
])
def test_madlad_refuses_a_load_it_cannot_honour(tmp_path, options, batch, why):
    r = _madlad().LoadModel(load_call(tmp_path, options=options, batch=batch), None)
    assert not r.success and why in r.message


def test_a_whole_batch_is_one_prompt_and_one_pass(tmp_path):
    seen: dict = {"batches": []}
    s = _loaded_madlad(tmp_path, seen)
    out = _predict(s, json.dumps({"items": [{"text": "uno", "to": "de"},
                                            {"text": "due", "to": "xx"},
                                            {"text": "tre", "to": "la"}]}))
    assert out["translations"] == ["<2de>|UNO", None, "<2la>|TRE"], \
        "null for a target it does not know, never the input handed back"
    assert seen["batches"][-1] == ["<2de> uno", "<2la> tre"], "one pass for the batch"
    assert out["peak_mb"] == 7
    assert _predict(s, '{"languages": true}') == {"languages": ["de", "it", "la"]}


@pytest.mark.parametrize("prompt,code", [
    ("<2de> plain text", "INVALID_ARGUMENT"),
    ('{"items": [{"text": "x"}]}', "INVALID_ARGUMENT"),
    (json.dumps({"items": [{"text": "x", "to": "de"}] * 4}), "RESOURCE_EXHAUSTED"),
])
def test_a_prompt_that_is_not_a_request_is_refused_not_translated(tmp_path, prompt, code):
    s = _loaded_madlad(tmp_path)
    assert aborted(lambda: s.Predict(SimpleNamespace(Prompt=prompt), Context()))[0] == code


def test_the_platforms_client_and_the_backend_agree_on_the_one_prompt(tmp_path, monkeypatch):
    """app/translate.py under TRANSLATE_API=completion, through LocalAI's
    /v1/completions as it is at 58830f7 for a model with no template: the
    prompt reaches `Predict` as sent and the reply comes back as the text."""
    from app import config
    from app import translate as mt
    seen: dict = {"batches": []}
    s = _loaded_madlad(tmp_path, seen, options=("device:cpu",))
    asked = []

    def localai(url, json=None, timeout=None, headers=None):
        assert url == "http://rage:8080/upstream/madlad/v1/completions"
        asked.append(json)
        reply = s.Predict(SimpleNamespace(Prompt=json["prompt"]), Context())
        return httpx.Response(200, json={"object": "text_completion", "choices": [
            {"index": 0, "text": reply.message.decode("utf-8")}]},
            request=httpx.Request("POST", url))

    monkeypatch.setattr(config, "TRANSLATE_URL", "http://rage:8080/upstream/madlad")
    monkeypatch.setattr(config, "TRANSLATE_API", "completion")
    monkeypatch.setattr(mt.httpx, "post", localai)
    mt.reset()
    assert mt.languages() == frozenset({"de", "it", "la"})
    assert mt.translate([("über", "it"), ("x", "zz"), ("y", "la")]) == \
        ["<2it>|ÜBER", None, "<2la>|Y"]
    assert len(asked) == 2 and set(asked[-1]) == {"prompt"}, \
        "the model is the URL's: RAGE's front names it (rage-over-localai §7)"
    assert sorted(seen["batches"][-1]) == ["<2it> über", "<2la> y"], "one pass for the batch"
    assert mt.health_url() == "http://rage:8080/upstream/madlad/v1/models"
    mt.reset()

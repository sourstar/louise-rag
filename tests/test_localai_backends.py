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
import re
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
    program VeGa finds in the image's backends path."""
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
    recipe = RerankRecipe(url="http://models/upstream/medcpt-rerank/v1/rerank", api="jina",
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
                ["de", "it", "la"], lambda: 7)
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
    """app/translate.py, through LocalAI's
    /v1/completions as it is at 58830f7 for a model with no template: the
    prompt reaches `Predict` as sent and the reply comes back as the text."""
    from app import config
    from app import translate as mt
    seen: dict = {"batches": []}
    s = _loaded_madlad(tmp_path, seen, options=("device:cpu",))
    asked = []

    def localai(url, json=None, timeout=None, headers=None):
        assert url == "http://models:8080/upstream/madlad/v1/completions"
        asked.append(json)
        reply = s.Predict(SimpleNamespace(Prompt=json["prompt"]), Context())
        return httpx.Response(200, json={"object": "text_completion", "choices": [
            {"index": 0, "text": reply.message.decode("utf-8")}]},
            request=httpx.Request("POST", url))

    monkeypatch.setattr(config, "TRANSLATE_URL", "http://models:8080/upstream/madlad")
    monkeypatch.setattr(mt.httpx, "post", localai)
    mt.reset()
    assert mt.languages() == frozenset({"de", "it", "la"})
    assert mt.translate([("über", "it"), ("x", "zz"), ("y", "la")]) == \
        ["<2it>|ÜBER", None, "<2la>|Y"]
    assert len(asked) == 2 and set(asked[-1]) == {"prompt", "max_tokens"}, \
        "the model is the URL's (`…/upstream/<model>`, 07 §7.8); the bound rides along (the backend ignores it)"
    assert all(0 < a["max_tokens"] <= 32768 for a in asked), "every batch request bounded (translate._completion)"
    assert sorted(seen["batches"][-1]) == ["<2it> über", "<2la> y"], "one pass for the batch"
    assert mt.health_url() == "http://models:8080/upstream/madlad/v1/models"
    mt.reset()


# ---- MedCPT's article encoder: Embedding of a text pair --------------------------------------

article_backend = _backend("louise-medcpt-article-ov", "medcpt", "deploy/medcpt-rerank/server.py")


def _article(seen=None):
    """A stand-in tokenizer that records what it is given, and a stand-in
    model whose [CLS] vector is (len(first), len(second), segment-1 tokens)."""
    seen = {"encoded": []} if seen is None else seen

    def encoder(tokenizer_json, max_tokens):
        seen["encoder"] = (tokenizer_json, max_tokens)

        def encode(items):
            seen["encoded"].append(list(items))
            (item,) = items
            first, second = item if isinstance(item, tuple) else (item, None)
            return {"items": items, "attention_mask": [[1] * (3 if second is None else 5)],
                    "token_type_ids": [[0, 0, 0] + ([] if second is None else [1, 1])]}
        return encode

    def infer(xml, device, cache_dir):
        seen["infer"] = (xml, device, cache_dir)

        def run(inputs):
            (item,) = inputs["items"]
            first, second = item if isinstance(item, tuple) else (item, "")
            return [[float(len(first)), float(len(second)),
                     float(sum(inputs["token_type_ids"][0]))]]
        return run
    return article_backend.Servicer(PB, STATUS, encoder=encoder, infer=infer)


def _loaded_article(tmp_path, seen=None, options=("device:xpu", "pairs:true")):
    s = _article(seen)
    r = s.LoadModel(load_call(_medcpt_dir(tmp_path), options=options, context_size=512), None)
    assert r.success, r.message
    return s


def test_article_encoder_loads_from_the_load_call_and_its_models_env(tmp_path, monkeypatch):
    seen: dict = {"encoded": []}
    monkeypatch.setenv("OV_CACHE_DIR", "/state/ov-cache")
    s = _loaded_article(tmp_path, seen)
    d = tmp_path / "medcpt-cross-ov"
    assert seen["encoder"] == (str(d / "tokenizer.json"), 512), "NCBI's max_length"
    assert seen["infer"] == (str(d / "openvino_model.xml"), "GPU", "/state/ov-cache")
    assert seen["encoded"][0] == [("warm-up", "the first inference compiles its kernels")]
    assert s.dim == 3 and s.pairs is True
    cpu = _article(seen)
    assert cpu.LoadModel(load_call(d, options=["device:cpu"], context_size=512), None).success
    assert seen["infer"][1] == "CPU" and cpu.pairs is False


@pytest.mark.parametrize("call,why", [
    (lambda d: load_call(d / "absent", context_size=512), "absent"),
    (lambda d: load_call(d, options=["pairs:yes"], context_size=512), "true or false"),
    (lambda d: load_call(d, options=["pooling:mean"], context_size=512), "reads"),
    (lambda d: load_call(d), "context_size"),
])
def test_article_encoder_refuses_a_load_it_cannot_honour(tmp_path, call, why):
    r = _article().LoadModel(call(_medcpt_dir(tmp_path)), None)
    assert not r.success and why in r.message


def test_a_marked_string_is_encoded_as_a_true_pair(tmp_path):
    seen: dict = {"encoded": []}
    s = _loaded_article(tmp_path, seen)
    marked = lb.PAIR_MARK + json.dumps(["Aspirin.", "An abstract\n\nin two parts."])
    out = s.Embedding(SimpleNamespace(Embeddings=marked), Context())
    assert seen["encoded"][-1] == [("Aspirin.", "An abstract\n\nin two parts.")], \
        "the tokenizer gets the pair, never one joined text"
    assert out.embeddings == [8.0, 26.0, 2.0] and out.dim == 3 and out.tokens == 5
    assert out.layout == PB.EMBEDDING_LAYOUT_FINAL, "[CLS] pooled here; LocalAI pools nothing"


def test_a_plain_string_is_one_text(tmp_path):
    seen: dict = {"encoded": []}
    s = _loaded_article(tmp_path, seen)
    out = s.Embedding(SimpleNamespace(Embeddings="title\n\nabstract"), Context())
    assert seen["encoded"][-1] == ["title\n\nabstract"]
    assert out.embeddings == [15.0, 0.0, 0.0] and out.tokens == 3


@pytest.mark.parametrize("text,code", [
    (lb.PAIR_MARK + "not json", "INVALID_ARGUMENT"),
    (lb.PAIR_MARK + '["one"]', "INVALID_ARGUMENT"),
    ("a text " + lb.PAIR_MARK + '["a", "b"]', "INVALID_ARGUMENT"),
    ("", "INVALID_ARGUMENT"),
])
def test_article_encoder_refuses_what_is_neither_a_text_nor_a_pair(tmp_path, text, code):
    s = _loaded_article(tmp_path)
    assert aborted(lambda: s.Embedding(SimpleNamespace(Embeddings=text), Context()))[0] == code


def test_a_pair_to_a_model_loaded_without_pairs_is_refused(tmp_path):
    s = _loaded_article(tmp_path, options=("device:xpu",))
    marked = lb.PAIR_MARK + json.dumps(["a", "b"])
    code, why = aborted(lambda: s.Embedding(SimpleNamespace(Embeddings=marked), Context()))
    assert code == "INVALID_ARGUMENT" and "pairs:true" in why
    assert aborted(lambda: _article().Embedding(SimpleNamespace(Embeddings="x"),
                                                Context()))[0] == "FAILED_PRECONDITION"


def test_dar_legal_it_hands_torch_cache_back_after_a_long_text():
    """12.6–13.6 GB held on the node for a ~1.5 GB model (2026-10-14): torch's
    caching allocator kept one long article's attention peak. Over
    RELEASE_ABOVE reserved, the cache goes back to the driver; under it,
    nothing is done (a short text pays no re-allocation)."""
    from types import SimpleNamespace
    server = dar_backend.dar_legal_it
    calls = []
    reserved = {"n": 13 << 30}
    xpu = SimpleNamespace(memory_reserved=lambda: reserved["n"],
                          empty_cache=lambda: calls.append("empty"))
    torch = SimpleNamespace(xpu=xpu)
    assert server.release(torch, "xpu") is True and calls == ["empty"]
    reserved["n"] = 1 << 30
    assert server.release(torch, "xpu") is False and calls == ["empty"]
    assert server.release(torch, "cpu") is False, "the CPU has no device cache to hand back"
    assert server.release(SimpleNamespace(), "xpu") is False


# ---- louise-python: the four as one backend (docs/notes/backend-consolidation.md) ------------

PYTHON_DIR = BACKENDS / "louise-python"

def _dispatcher():
    """backend.py imports louise_backend, beside it in the image."""
    saved = sys.modules.get("louise_backend")
    sys.modules["louise_backend"] = lb
    try:
        return _exec("backend_louise_python_under_test", PYTHON_DIR / "backend.py")
    finally:
        if saved is None:
            sys.modules.pop("louise_backend", None)
        else:
            sys.modules["louise_backend"] = saved


python_backend = _dispatcher()
UNIMPLEMENTED = SimpleNamespace(**vars(STATUS), UNIMPLEMENTED="UNIMPLEMENTED")


#: the model env every torch task loads with (invariant 12, fail-closed 2026-10-20)
DETERMINISTIC = {"LOUISE_TORCH_DETERMINISTIC": "1"}


def _combined(made: list | None = None, *, environ=DETERMINISTIC, check_determinism=True,
              **servicers):
    """The dispatcher with each task's Servicer handed in: the real classes of
    the backends it replaces, with their stand-in models. `check_determinism`
    False only to exercise the ROUTING of a task the rule refuses."""
    made = [] if made is None else made

    def load_module(name):
        made.append(name)
        return SimpleNamespace(Servicer=servicers[name])
    return python_backend.Servicer(PB, UNIMPLEMENTED, load_module=load_module, environ=environ,
                                   check_determinism=check_determinism)


def test_every_task_is_the_backend_it_replaces_copied_by_the_dockerfile():
    """TASKS names modules; the Dockerfile lays each one down from the backend
    it replaces, beside the model code that backend imports."""
    dockerfile = (PYTHON_DIR / "Dockerfile").read_text()
    replaced = {"task_dar_legal_it": "louise-dar-legal-it", "task_madlad": "louise-madlad",
                "task_medcpt_article": "louise-medcpt-article-ov",
                "task_medcpt_rerank": "louise-medcpt-rerank"}
    native = {"task_jina_v3": "deploy/localai/backends/louise-python/task_jina_v3.py"}
    assert set(python_backend.TASKS.values()) == set(replaced) | set(native)
    for module, source in native.items():
        assert any(source in line and line.rstrip().endswith(f"/out/{module}.py")
                   for line in dockerfile.splitlines()), f"{module} is written for louise-python"
    for module, directory in replaced.items():
        assert f"backends/{directory}/backend.py" in dockerfile.split(f"/out/{module}.py")[0] \
            .splitlines()[-1], f"{module} is {directory}'s backend.py"
    for server, as_name in (("deploy/dar-legal-it/server.py", "dar_legal_it.py"),
                            ("deploy/madlad-translate/server.py", "madlad.py"),
                            ("deploy/medcpt-rerank/server.py", "medcpt.py"),
                            ("deploy/jina-embeddings-v3/server.py", "jina_v3.py")):
        assert any(server in line and line.rstrip().endswith(f"/out/{as_name}")
                   for line in dockerfile.splitlines()), as_name
    assert "\nFROM scratch\nCOPY --from=build /out/ /\n" in dockerfile, \
        "the image IS the backend directory, run.sh at its root (LocalAI refuses one without)"


def test_the_task_option_picks_the_backend_and_the_rest_go_to_it_unchanged(tmp_path):
    made: list = []
    seen: list = []
    s = _combined(made, task_dar_legal_it=lambda pb, st: _dar(seen))
    r = s.LoadModel(load_call(tmp_path, options=["device:cpu", "task:dar-legal-it"],
                              context_size=8192), None)
    assert r.success, r.message
    assert made == ["task_dar_legal_it"] and s.task == "dar-legal-it"
    assert seen == [("build", str(tmp_path), "cpu", 8192)], \
        "device:cpu reached it; task: did not (it would refuse an unknown option)"
    out = s.Embedding(SimpleNamespace(Embeddings="art. 1"), Context())
    assert out.embeddings == [6.0, 0.5] and out.layout == PB.EMBEDDING_LAYOUT_FINAL


@pytest.mark.parametrize("options, why", [
    ([], "no task"), (["task:madlad", "task:madlad"], "several"),
    (["task:llama"], "this backend runs")])
def test_a_load_without_one_known_task_is_refused(tmp_path, options, why):
    r = _combined().LoadModel(load_call(tmp_path, options=options), None)
    assert not r.success and why in r.message


def test_one_process_serves_one_task(tmp_path):
    s = _combined(task_dar_legal_it=lambda pb, st: _dar(),
                  task_madlad=lambda pb, st: _madlad())
    assert s.LoadModel(load_call(tmp_path, options=["task:dar-legal-it"],
                                 context_size=8192), None).success
    r = s.LoadModel(load_call(tmp_path, options=["task:madlad"], batch=4), None)
    assert not r.success and "another process" in r.message
    assert s.task == "dar-legal-it"


def test_a_refused_load_leaves_nothing_loaded(tmp_path):
    s = _combined(task_dar_legal_it=lambda pb, st: _dar())
    assert not s.LoadModel(load_call(tmp_path, options=["task:dar-legal-it"]), None).success
    assert s.inner is None and s.task is None
    assert aborted(lambda: s.Embedding(SimpleNamespace(Embeddings="x"),
                                       Context()))[0] == "FAILED_PRECONDITION"


def test_a_call_the_task_does_not_answer_is_unimplemented(tmp_path):
    s = _combined(check_determinism=False, task_medcpt_rerank=lambda pb, st: _medcpt(tmp_path))
    r = s.LoadModel(load_call(_medcpt_dir(tmp_path), options=["task:medcpt-rerank"],
                              context_size=512, batch=4), None)
    assert r.success, r.message
    code, message = aborted(lambda: s.Embedding(SimpleNamespace(Embeddings="x"), Context()))
    assert code == "UNIMPLEMENTED" and "medcpt-rerank" in message


def test_madlad_and_the_article_encoder_answer_through_it_as_before(tmp_path):
    m = _combined(task_madlad=lambda pb, st: _madlad())
    assert m.LoadModel(load_call(tmp_path, options=["task:madlad", "device:cpu"], batch=4),
                       None).success
    got = json.loads(m.Predict(SimpleNamespace(Prompt=json.dumps(
        {"items": [{"text": "ave", "to": "it"}]})), Context()).message)
    assert got["translations"] == ["<2it>|AVE"]
    a = _combined(check_determinism=False, task_medcpt_article=lambda pb, st: _article())
    assert a.LoadModel(load_call(_medcpt_dir(tmp_path),
                                 options=["pairs:true", "task:medcpt-article"],
                                 context_size=512), None).success
    out = a.Embedding(SimpleNamespace(Embeddings=lb.PAIR_MARK + json.dumps(["ab", "cde"])),
                      Context())
    assert out.embeddings == [2.0, 3.0, 2.0], "a true pair: its second segment is segment 1"


def test_run_sh_runs_the_portable_python_wherever_it_is_unpacked(tmp_path):
    """No venv (its pyvenv.cfg names the interpreter by absolute path, and a
    GUI install picks the directory); nothing of the host image's PYTHON*."""
    d = tmp_path / "any-name-the-install-gives"
    (d / "python" / "bin").mkdir(parents=True)
    (d / "run.sh").write_bytes((PYTHON_DIR / "run.sh").read_bytes())
    (d / "run.sh").chmod(0o755)
    python = d / "python" / "bin" / "python3"
    python.write_text('#!/bin/sh\necho "$$ $0 $* LD=${LD_LIBRARY_PATH:-}"\n')
    python.chmod(0o755)
    ran = subprocess.run(["sh", "-c", f'echo "$$"; exec {d}/run.sh --addr 127.0.0.1:4001'],
                         capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
                         check=True)
    shell_pid, line = ran.stdout.split("\n", 1)
    pid, program, rest = line.split(" ", 2)
    assert pid == shell_pid, "exec: the pid LocalAI lists is the backend's"
    assert program == f"{python}"
    assert rest.strip() == f"-B -E -s {d}/backend.py --addr 127.0.0.1:4001 LD={d}/python/lib"


def test_its_pins_are_the_measured_set():
    """docs/notes/backend-consolidation.md: transformers 4.x (DAR-legal-it's
    GteModel does not run on 5), tokenizers 0.21.4 (MedCPT's tokens identical),
    the OpenVINO the IRs were checked on, torch 2.14.0 on the card."""
    pins = dict(line.split("==") for line in
                (PYTHON_DIR / "requirements.txt").read_text().splitlines()
                if "==" in line and not line.startswith("#"))
    assert pins["transformers"] == "4.55.4" and pins["sentence-transformers"] == "5.1.0"
    assert pins["tokenizers"] == "0.21.4" and pins["openvino"] == "2026.3.1"
    assert "torch" not in pins, "torch is the variant's"
    assert "torch==2.14.0+xpu" in (PYTHON_DIR / "requirements-intel.txt").read_text()


def test_a_rocm_or_nvidia_card_is_torchs_cuda_and_openvino_refuses_it(tmp_path):
    """The AMD peer (docs/notes/backend-consolidation.md): torch names ROCm's
    HIP `cuda`; OpenVINO has no plugin for it, and says what to set."""
    assert lb.device("cuda") == "cuda"
    assert lb.openvino_device("xpu") == "GPU" and lb.openvino_device("cpu") == "CPU"
    with pytest.raises(lb.LoadError, match="device:cpu"):
        lb.openvino_device("cuda")
    r = _medcpt(tmp_path).LoadModel(load_call(_medcpt_dir(tmp_path), options=["device:cuda"],
                                              context_size=512, batch=4), None)
    assert not r.success and "set device:cpu" in r.message
    seen: list = []
    r = _dar(seen).LoadModel(load_call(tmp_path, options=["device:cuda"], context_size=8192),
                             None)
    assert r.success and seen[0][2] == "cuda"


def test_madlad_hands_the_cache_back_on_cuda_as_on_xpu():
    """madlad.load's release(): the pass's peak, then the cache back, on
    either card; the CPU has neither."""
    server = madlad_backend.madlad
    src = Path(server.__file__).read_text()
    assert 'kind in ("xpu", "cuda")' in src and "getattr(torch, kind)" in src


def test_the_gallery_lists_a_variant_per_gpu_each_with_its_requirements():
    """deploy/localai/gallery/index.yaml, read as LocalAI reads a backend
    gallery: the meta entry's capabilities name concrete entries, and each
    concrete one is an image of a variant the Dockerfile can build."""
    yaml = pytest.importorskip("yaml")
    entries = {e["name"]: e for e in yaml.safe_load(
        (ROOT / "deploy" / "localai" / "gallery" / "index.yaml").read_text())}
    meta = entries["louise-python"]
    assert "uri" not in meta and set(meta["capabilities"]) == {"intel", "amd"}
    for gpu, name in meta["capabilities"].items():
        concrete = entries[name]
        variant = concrete["uri"].rsplit(":", 1)[1]
        assert concrete["alias"] == "louise-python" and "capabilities" not in concrete
        assert (PYTHON_DIR / f"requirements-{variant}.txt").is_file(), (gpu, variant)


def test_each_staged_model_on_louise_python_is_a_load_its_task_accepts():
    """deploy/vega/stage/models: what the node's LocalAI holds. A model on
    louise-python (or VeGa's wrapper of it) names one task, and every other
    option is one that task reads, at a value it takes."""
    tasks = {"dar-legal-it": dar_backend, "madlad": madlad_backend,
             "medcpt-article": article_backend, "medcpt-rerank": medcpt_backend,
             "jina-v3": jina_task}
    assert set(tasks) == set(python_backend.TASKS)
    seen = set()
    for path in sorted((ROOT / "deploy" / "vega" / "stage" / "models").glob("*.yaml")):
        text = "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                         if not l.lstrip().startswith("#"))
        body = json.loads(text)
        # the tree is installed BESIDE its predecessor under a dated name, and a
        # rebuild moves the YAML's backend line to it (louise-python-rebuild.md §2)
        if not re.fullmatch(r"(vega-)?louise-python(-\d{8}[a-z]?)?", body["backend"]):
            assert not body["backend"].startswith(("louise-", "vega-louise-")), \
                f"{path.name}: on a retired per-model backend"
            continue
        task, rest = python_backend.split_task(body["options"])
        lb.options(rest, tasks[task].KNOWN, tasks[task].DEFAULTS)
        seen.add(task)
    assert seen == set(tasks), "each live task serves a staged model"


def test_a_torch_task_loads_only_with_torchs_deterministic_switch(tmp_path):
    """CLAUDE.md invariant 12, fail-closed (operator, 2026-10-20): torch's
    defaults do not give back their own bytes (DAR-legal-it 1/4 against itself)."""
    for environ in ({}, {"LOUISE_TORCH_DETERMINISTIC": "0"}):
        r = _combined(environ=environ, task_dar_legal_it=lambda pb, st: _dar()).LoadModel(
            load_call(tmp_path, options=["device:cpu", "task:dar-legal-it"], context_size=8192), None)
        assert not r.success and "LOUISE_TORCH_DETERMINISTIC=1" in r.message
    r = _combined(task_dar_legal_it=lambda pb, st: _dar()).LoadModel(
        load_call(tmp_path, options=["device:cpu", "task:dar-legal-it"], context_size=8192), None)
    assert r.success, r.message
    assert python_backend.TORCH_TASKS == {"dar-legal-it", "madlad", "jina-v3"}


@pytest.mark.parametrize("task", ["medcpt-article", "medcpt-rerank"])
def test_an_openvino_task_unmeasured_on_this_tree_is_never_loaded(tmp_path, task):
    """OpenVINO offers no deterministic mode: refused, with the switch or without,
    unless the model's env names THIS tree as the one it was measured bit-exact on."""
    model = _medcpt_dir(tmp_path)
    for environ in (DETERMINISTIC, {python_backend.MEASURED_ENV: "louise-python-19990101"}):
        made: list = []
        r = _combined(made, environ=environ).LoadModel(
            load_call(model, options=[f"task:{task}"], context_size=512, batch=4), None)
        assert not r.success and "OpenVINO" in r.message and made == [], "refused before it is imported"


@pytest.mark.parametrize("task", ["medcpt-article", "medcpt-rerank"])
def test_an_openvino_task_measured_on_this_tree_may_load(task):
    on = {python_backend.MEASURED_ENV: "louise-python-20261021a"}
    assert python_backend.determinism_refusal(task, on, tree="louise-python-20261021a") is None
    assert "measure it" in python_backend.determinism_refusal(task, on, tree="louise-python-20261022a")
    assert python_backend.determinism_refusal("dar-legal-it", on, tree="louise-python-20261021a"),         "a measurement never stands in for torch's switch"


def test_madlad_switches_torch_to_its_deterministic_algorithms():
    madlad = _exec("madlad_server_under_test", ROOT / "deploy" / "madlad-translate" / "server.py")
    calls = []
    torch = SimpleNamespace(use_deterministic_algorithms=calls.append,
                            backends=SimpleNamespace(mkldnn=SimpleNamespace(deterministic=False)))
    assert madlad.determinism(torch, {}) is False and calls == []
    assert madlad.determinism(torch, {"LOUISE_TORCH_DETERMINISTIC": "1"}) is True
    assert calls == [True] and torch.backends.mkldnn.deterministic is True


# ---- jina-v3: wikidata's encoder, a louise-python task of its own ------------------------

def _jina_task():
    """task_jina_v3.py as the image lays it out: louise_backend and the model
    code (deploy/jina-embeddings-v3/server.py, as `jina_v3`) beside it."""
    shared = {"louise_backend": lb,
              "jina_v3": _exec("jina_v3", ROOT / "deploy" / "jina-embeddings-v3" / "server.py")}
    saved = {k: sys.modules.get(k) for k in shared}
    try:
        sys.modules.update(shared)
        return _exec("task_jina_v3_under_test", PYTHON_DIR / "task_jina_v3.py")
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


jina_task = _jina_task()


def _jina(seen=None):
    seen = [] if seen is None else seen

    def build(model_dir, device, adapter, dim, max_tokens):
        seen.append(("build", model_dir, device, adapter, dim, max_tokens))

        def encode(text):
            seen.append(("encode", text))
            return [1.0] + [0.0] * (dim - 1)
        return encode, dim
    return jina_task.Servicer(PB, STATUS, build=build)


def test_jina_v3_loads_the_adapter_and_width_the_load_names(tmp_path):
    """Wikimedia's vectors: retrieval.passage at 512 (docs/notes/wikidata-plugins.md);
    a query encoder is the same weights with adapter:retrieval.query."""
    seen: list = []
    r = _jina(seen).LoadModel(load_call(tmp_path, options=["dim:512"], context_size=8192), None)
    assert r.success, r.message
    assert seen[0] == ("build", str(tmp_path), "xpu", "retrieval.passage", 512, 8192)
    assert seen[1] == ("encode", "the first pass compiles its kernels"), "warm before answering"
    seen.clear()
    assert _jina(seen).LoadModel(load_call(tmp_path, options=["adapter:retrieval.query",
                                                              "device:cuda"],
                                           context_size=8192), None).success
    assert seen[0][2:5] == ("cuda", "retrieval.query", 1024)


@pytest.mark.parametrize("options, context, why", [
    (["dim:500"], 8192, "dim"), (["adapter:"], 8192, "adapter"), ([], 0, "context_size")])
def test_jina_v3_refuses_a_load_it_cannot_honour(tmp_path, options, context, why):
    r = _jina().LoadModel(load_call(tmp_path, options=options, context_size=context), None)
    assert not r.success and why in r.message


def test_jina_v3_embeds_one_text_as_sent_and_declares_it_final(tmp_path):
    seen: list = []
    s = _jina(seen)
    s.LoadModel(load_call(tmp_path, options=["dim:512"], context_size=8192), None)
    out = s.Embedding(SimpleNamespace(Embeddings="Gdańsk, city in Poland"), Context())
    assert seen[-1] == ("encode", "Gdańsk, city in Poland"), \
        "the text untouched: the model's own encode prefixes its task instruction"
    assert len(out.embeddings) == 512 and out.dim == 512
    assert out.layout == PB.EMBEDDING_LAYOUT_FINAL
    assert aborted(lambda: s.Embedding(SimpleNamespace(Embeddings=""),
                                       Context()))[0] == "INVALID_ARGUMENT"


def test_jina_v3_reads_its_remote_code_from_inside_the_weights(tmp_path):
    """The weights digest (09 §9.3) must cover the code that computes the vector."""
    server = jina_task.jina_v3
    with pytest.raises(FileNotFoundError, match="remote code"):
        server.hub_cache(str(tmp_path))
    (tmp_path / "hf-cache").mkdir()
    assert server.hub_cache(str(tmp_path)) == str(tmp_path / "hf-cache")
    assert server.normalised([3.0, 4.0]) == [0.6, 0.8]
    with pytest.raises(ValueError):
        server.normalised([0.0, 0.0])


def test_jina_v3_through_the_dispatcher(tmp_path):
    s = _combined(task_jina_v3=lambda pb, st: _jina())
    assert s.LoadModel(load_call(tmp_path, options=["task:jina-v3", "dim:512"],
                                 context_size=8192), None).success
    assert len(s.Embedding(SimpleNamespace(Embeddings="x"), Context()).embeddings) == 512


def test_the_cross_device_check_changes_only_the_model():
    """tools/cross_device_check.py: the collection's own encoder recipe with the
    document-side model swapped for its twin on another device — nothing else."""
    from app.collections import EmbedRecipe
    from tools import cross_device_check as xdc
    r = EmbedRecipe(url="http://localai:8080", api="vega", model="vega-dar-legal-it",
                    dtype="f16", ctx=8192, dim=768)
    cpu = xdc.cpu_recipe(r, "vega-dar-legal-it-cpu")
    assert cpu.model == "vega-dar-legal-it-cpu"
    assert dataclasses_fields_equal(cpu, r, but={"model"})
    with pytest.raises(ValueError):
        xdc.cpu_recipe(r, "vega-dar-legal-it")


def test_the_cross_device_explain_names_which_side_is_wrong():
    """`--explain`: a stored vector the card got wrong reads low against the
    twin and high against the card now; ids map to texts in the check's order,
    and a held id with no text in that call is never mis-paired."""
    from tools import cross_device_check as xdc
    stored = {"a": [1.0, 0.0], "b": [0.0, 1.0]}
    twin = {"ta": [1.0, 0.0], "tb": [1.0, 0.0]}           # the twin disagrees on b
    r = xdc.Recorder(lambda ids: {i: stored[i] for i in ids if i in stored},
                     lambda texts: [twin[t] for t in texts])
    r.vectors_for(["a", "missing", "b"])
    r.embed(["ta", "tb"])
    rows = r.explain(["b"], lambda texts: [stored["b"] for _ in texts])  # the card repeats itself
    assert rows == [{"chunk": "b", "chars": 2, "stored_twin": 0.0,
                     "stored_card_now": 1.0, "card_now_twin": 0.0}]
    r.vectors_for(["a", "b"])
    r.embed(["ta"])                                         # sizes disagree: not recorded
    assert r.text["a"] == "ta" and r.explain(["zzz"], lambda t: []) == []


def dataclasses_fields_equal(a, b, *, but):
    import dataclasses
    return all(getattr(a, f.name) == getattr(b, f.name)
               for f in dataclasses.fields(a) if f.name not in but)


def test_jina_v3_refuses_a_model_whose_attention_is_not_the_one_measured():
    """docs/notes/gpu-silent-wrong-vectors.md: the eager softmax is wrong on the
    B70 for some lengths, so every SelfAttention goes through SDPA — and a model
    with none is refused rather than run on the path that was not measured."""
    server = jina_task.jina_v3

    class SelfAttention:
        causal, softmax_scale = False, None

        def forward(self, *a):
            return "eager"

    class Other:
        pass

    model = SimpleNamespace(modules=lambda: [Other(), SelfAttention(), SelfAttention()])
    assert server.use_sdpa(model) == 2
    with pytest.raises(ValueError, match="not the one measured"):
        server.use_sdpa(SimpleNamespace(modules=lambda: [Other()]))


def test_sdpa_forward_is_the_eager_attention():
    """The replacement computes their SelfAttention's function (mha.py, bd55a5e):
    softmax(QKᵀ·scale + padding) V, (B, S, 3, H, D) in, (B, S, H, D) out."""
    torch = pytest.importorskip("torch")
    import math
    server = jina_task.jina_v3
    torch.manual_seed(0)
    qkv = torch.randn(2, 37, 3, 4, 16)
    keep = torch.ones(2, 37, dtype=torch.bool)
    keep[1, 30:] = False
    q, k, v = qkv.unbind(dim=2)
    scores = torch.einsum("bthd,bshd->bhts", q, k / math.sqrt(16))
    pad = torch.full((2, 37), -10000.0).masked_fill_(keep, 0.0)
    eager = torch.einsum("bhts,bshd->bthd", torch.softmax(scores + pad[:, None, None, :], dim=-1), v)
    me = SimpleNamespace(causal=False, softmax_scale=None)
    got = server.sdpa_forward(me, qkv, key_padding_mask=keep)
    assert got.shape == eager.shape
    assert torch.allclose(got, eager, atol=1e-5)


def test_the_cross_device_export_is_what_the_check_read_in_its_order():
    """`--export`: each chunk the check read, its input as the encoder is fed it
    (a pair as two strings) and its stored vector, for another machine."""
    from tools import cross_device_check as xdc
    stored = {"a": [1.0, 0.0], "p": [0.0, 1.0]}
    r = xdc.Recorder(lambda ids: {i: stored[i] for i in ids if i in stored},
                     lambda texts: [[0.0, 0.0] for _ in texts])
    r.vectors_for(["a", "p"])
    r.embed(["text a", ("Title", "Abstract")])
    assert r.export() == [{"chunk": "a", "input": "text a", "stored": [1.0, 0.0]},
                          {"chunk": "p", "input": ["Title", "Abstract"], "stored": [0.0, 1.0]}]


def test_jina_v3_switches_torch_determinism_on_from_the_models_env_only():
    """Invariant 12 for torch: the controls are calls, so the MODEL's env switches
    them on (and the stated run carries the switch); unset, torch's defaults —
    the baseline the measurement compares against (docs/notes/louise-python-rebuild.md §4)."""
    jina = _exec("jina_v3_determinism", ROOT / "deploy" / "jina-embeddings-v3" / "server.py")
    calls: list = []
    fake = SimpleNamespace(use_deterministic_algorithms=lambda flag: calls.append(flag),
                           backends=SimpleNamespace(mkldnn=SimpleNamespace(deterministic=False)))
    assert jina.determinism(fake, {}) is False
    assert jina.determinism(fake, {"LOUISE_TORCH_DETERMINISTIC": "0"}) is False
    assert calls == [] and fake.backends.mkldnn.deterministic is False
    assert jina.determinism(fake, {jina.DETERMINISM_ENV: "1"}) is True
    assert calls == [True] and fake.backends.mkldnn.deterministic is True
    # a torch build without oneDNN's flag: the algorithms call alone, no error
    bare = SimpleNamespace(use_deterministic_algorithms=lambda flag: calls.append(flag),
                           backends=SimpleNamespace())
    assert jina.determinism(bare, {jina.DETERMINISM_ENV: "true"}) is True
    assert calls == [True, True]


def test_jina_v3_loads_the_remote_code_from_the_folders_cache_whatever_the_env_says(tmp_path, monkeypatch):
    """München, 2026-10-20: LocalAI starts every backend with TRANSFORMERS_CACHE,
    HF_HOME and HUGGINGFACE_HUB_CACHE = /models, and transformers' `cached_file`
    takes TRANSFORMERS_CACHE over the HF_HUB_CACHE the server sets — so the
    remote code under `<weights>/hf-cache` was never found and, offline, the
    load failed. `cache_dir` is passed EXPLICITLY, and the env LocalAI sets
    is left as it is (it would otherwise enter the stated run)."""
    import types
    jina = _exec("jina_v3_cache_dir", ROOT / "deploy" / "jina-embeddings-v3" / "server.py")
    (tmp_path / "hf-cache").mkdir()
    monkeypatch.setenv("TRANSFORMERS_CACHE", "/models")
    monkeypatch.setenv("HF_HOME", "/models")
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", "/models")
    seen: dict = {}

    class Attn:                                   # what `use_sdpa` looks for, by class name
        causal, softmax_scale = False, 1.0
    Attn.__name__ = "SelfAttention"

    class Model:
        config = SimpleNamespace(lora_adaptations=["retrieval.query", "retrieval.passage"])

        def to(self, device):
            seen["device"] = device
            return self

        def eval(self):
            return self

        def modules(self):
            return [Attn()]

        def encode(self, texts, **kw):
            seen["encode"] = kw
            return [[3.0, 4.0] + [0.0] * (kw["truncate_dim"] - 2)]

    def from_pretrained(path, **kw):
        seen["path"], seen["kw"] = path, kw
        return Model()

    fake_torch = types.ModuleType("torch")
    fake_torch.no_grad = lambda: __import__("contextlib").nullcontext()
    fake_torch.use_deterministic_algorithms = lambda flag: seen.setdefault("det", []).append(flag)
    fake_transformers = types.ModuleType("transformers")
    fake_transformers.AutoModel = SimpleNamespace(from_pretrained=from_pretrained)
    Model.config.task_instructions = {"retrieval.passage": "Represent the document for retrieval: ",
                                      "retrieval.query": "Represent the query for retrieving evidence documents: "}

    def tokenizer(text, add_special_tokens=True):
        seen["tokenized"] = (text, add_special_tokens)
        return {"input_ids": [1] * 7}           # the instruction's tokens, no specials
    fake_transformers.AutoTokenizer = SimpleNamespace(from_pretrained=lambda path: tokenizer)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    encode, dim = jina.build_encoder(str(tmp_path), "cpu", "retrieval.passage", 512, 1024)
    assert seen["path"] == str(tmp_path)
    assert seen["kw"] == {"trust_remote_code": True, "cache_dir": str(tmp_path / "hf-cache")}
    assert os.environ["TRANSFORMERS_CACHE"] == "/models", "LocalAI's env is left as it is"
    assert os.environ["HF_HUB_CACHE"] == str(tmp_path / "hf-cache") and os.environ["HF_HUB_OFFLINE"] == "1"
    assert dim == 512 and encode("a text")[:2] == [0.6, 0.8]
    # the TEXT's window is the load's context_size (the space's ctx); the
    # adapter's instruction (7 tokens here) and the two specials ride on top,
    # so a window-filling chunk keeps its tail (München, 2026-10-20)
    assert seen["tokenized"] == ("Represent the document for retrieval: ", False)
    assert seen["encode"] == {"task": "retrieval.passage", "truncate_dim": 512, "max_length": 1024 + 7 + 2}
    assert "det" not in seen, "determinism stays off unless the model's env asks"
    with pytest.raises(ValueError, match="adapter"):
        jina.build_encoder(str(tmp_path), "cpu", "classification", 512, 1024)


def test_dar_legal_it_switches_torch_determinism_on_from_the_models_env_only():
    """Invariant 12 for DAR-legal-it, the same switch as jina-v3's: it was 1/4
    bit-identical against itself on 2026-10-17 and its fingerprint moved across
    three byte-identical trees; the MODEL's env switches torch's deterministic
    algorithms on, so the stated run carries it (operator, 2026-10-20: test it)."""
    server = dar_backend.dar_legal_it
    calls: list = []
    fake = SimpleNamespace(use_deterministic_algorithms=lambda flag: calls.append(flag),
                           backends=SimpleNamespace(mkldnn=SimpleNamespace(deterministic=False)))
    assert server.determinism(fake, {}) is False and calls == []
    assert server.determinism(fake, {server.DETERMINISM_ENV: "1"}) is True
    assert calls == [True] and fake.backends.mkldnn.deterministic is True
    assert server.DETERMINISM_ENV == "LOUISE_TORCH_DETERMINISTIC", "one switch for every torch task"


@pytest.mark.parametrize("server_py", ["dar-legal-it", "jina-embeddings-v3", "madlad-translate"])
def test_on_cuda_the_switch_also_takes_cudnns_deterministic_algorithms(server_py):
    """NVIDIA (2026-10-21): cuDNN chooses its convolution algorithms by
    timing when `benchmark` is on; the switch fixes them. torch's own
    deterministic mode covers the rest, and cuBLAS's fixed workspace is the
    model's env (CUBLAS_WORKSPACE_CONFIG, which the wrapper requires)."""
    server = _exec(f"cudnn_{server_py}", ROOT / "deploy" / server_py / "server.py")
    cudnn = SimpleNamespace(deterministic=False, benchmark=True)
    fake = SimpleNamespace(use_deterministic_algorithms=lambda flag: None,
                           backends=SimpleNamespace(cudnn=cudnn))
    assert server.determinism(fake, {}) is False and cudnn.deterministic is False
    assert server.determinism(fake, {"LOUISE_TORCH_DETERMINISTIC": "1"}) is True
    assert cudnn.deterministic is True and cudnn.benchmark is False

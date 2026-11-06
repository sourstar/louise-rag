"""Every llama.cpp model the node runs FOR ITSELF — chat, rerank, translate —
runs the MEASURED engine settings, or a measured exception named here
(operator, 2026-11-03, choice C: "B is plug and play but not good for louise
robustness" — the files stay explicit, and drift fails this test instead of
shipping).

NOT AN EMBEDDER (operator, the same day: "Collections should have already a
recommended setting for the embedder they use. A global rule file conflicts
with any peer wanting for whatever reason recommend a different setting for
its own made collection"). An encoder serves a collection's space, and its
settings are the collection's: the engine it was tested on is frozen in the
Codex (`space.encoder.engine`, 09 §9.3), how RAGE drives it is the
collection's signed recommended settings (09 §9.19), and any other run
contributes only once the gate validated it (invariant 1, 12). A file with
`"embeddings": true` and no `"reranking"` is an encoder and is not checked
here.

Why a test and not a default the backend applies: one edit to a backend
default would change every model at its next load, and chat models are not
gated (measuring.md §11), so a bad default would reach all of them unseen.
Here a change to the set is one reviewed commit that touches every file.

WHAT IS CHECKED: the engine's runtime switches — every `GGML_*` key of the
model's `env` — and `flash_attention`. Not batch, ubatch, context or
parallel: those are sized per model and its role, and the wrapper holds
`parallel:1` itself (vega_wrapper.determinism_lacks).

WHERE AN EXCEPTION IS NAMED: in EXCEPTIONS below, beside the set, with where
it was measured — not in the model's file, where any new key would enter the
stated run's env and change its run id (a re-gate for nothing).
"""
from __future__ import annotations

import json
from pathlib import Path

from .conftest import ROOT, load_wrapper

STAGE = ROOT / "deploy" / "vega" / "stage"

#: The settings each engine vendor runs with, measured. A `GGML_*` key not
#: named is the engine's own default and is left unset in the file; `None`
#: for flash attention means unset too.
#:   sycl: the vendor's exact mode (vega_wrapper.DETERMINISM; oneDNN's GEMM
#:         and its fused attention both drift across loads), flash attention
#:         on (faster and exact once oneDNN's attention is off: the
#:         rerankers 2026-10-31, −28 to −36 %; approved-encoders.md). The
#:         rest at the engine's default: GGML_SYCL_ENABLE_OPT (the reorder of
#:         quantized weights) is on and exact for the embedders and rerankers
#:         (Qwen3-Embedding-4B is Q4_K_M). The chat models take it too
#:         (2026-11-03): OPT=0 was theirs only for the same bytes over fresh
#:         loads (approved-encoders.md, 2026-10-21), a check a chat model does
#:         not get (measuring.md §11).
#:   cpu:  llama.cpp's CPU offers no mode (invariant 12); nothing set.
MEASURED: dict[str, dict[str, object]] = {
    "sycl": {"env": {"GGML_SYCL_ENABLE_DNN": "0", "GGML_SYCL_FA_ONEDNN": "0"},
             "flash_attention": "on"},
    "cpu": {"env": {}, "flash_attention": None},
}

#: (model, setting) → (value, where it was measured). Only that model differs
#: from the set, only on that setting. An exception nobody measured is drift.
EXCEPTIONS: dict[tuple[str, str], tuple[str, str]] = {
    ("louise-translate-madlad", "flash_attention"): (
        "off", ("approved-encoders.md \"Known limits\": T5 cannot use flash attention "
                "(its relative attention bias); MADLAD-400 is a T5")),
}


def vendor(backend: str) -> str | None:
    """The engine vendor a backend name runs on this node, or None for one
    that is not llama.cpp. vega-louise-llama-cpp wraps the SYCL engine,
    -cpu the CPU one; an engine installed by its own name says its vendor."""
    if backend == "vega-louise-llama-cpp-cpu" or backend.startswith("louise-cpu-"):
        return "cpu"
    if backend == "vega-louise-llama-cpp" or backend.startswith("louise-sycl-"):
        return "sycl"
    return None


def _read(path: Path) -> dict:
    text = "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                     if not l.lstrip().startswith("#"))
    return json.loads(text)


def encoder(cfg: dict) -> bool:
    """A model that serves a collection's space: its settings are the
    collection's (the Codex's engine, its recommended settings), not the
    node's. A reranker declares embeddings too, and reranking."""
    return bool(cfg.get("embeddings")) and not cfg.get("reranking")


def staged() -> dict[str, tuple[str, dict]]:
    """where → (name, config) of every model file the node takes for itself:
    the staged model files that are not encoders, and the gallery installs'
    overrides."""
    out: dict[str, tuple[str, dict]] = {}
    for path in sorted((STAGE / "models").glob("*.yaml")):
        cfg = _read(path)
        if not encoder(cfg):
            out[f"models/{path.name}"] = (cfg["name"], cfg)
    for path in sorted((STAGE / "gallery").glob("*.json")):
        body = _read(path)
        out[f"gallery/{path.name}"] = (body["name"], body.get("overrides") or {})
    return out


def settings(cfg: dict) -> dict[str, object]:
    out: dict[str, object] = {k: str(v) for k, v in (cfg.get("env") or {}).items()
                              if k.startswith("GGML_")}
    out["flash_attention"] = cfg.get("flash_attention")
    return out


def expected(name: str, vendor_name: str) -> dict[str, object]:
    out: dict[str, object] = dict(MEASURED[vendor_name]["env"])   # type: ignore[arg-type]
    out["flash_attention"] = MEASURED[vendor_name]["flash_attention"]
    for (model, key), (value, _where) in EXCEPTIONS.items():
        if model == name:
            out[key] = value
    return out


def _said(value: object) -> str:
    return "unset (the engine's default)" if value is None else repr(value)


def test_every_llama_cpp_model_runs_the_measured_settings_or_a_measured_exception():
    drift = []
    for where, (name, cfg) in staged().items():
        v = vendor(str(cfg.get("backend", "")))
        if v is None:
            continue
        want, got = expected(name, v), settings(cfg)
        for key in sorted(set(want) | set(got)):
            if want.get(key) != got.get(key):
                drift.append(f"{where} ({name}): {key} is {_said(got.get(key))}, the measured "
                             f"{v} set says {_said(want.get(key))}")
    assert not drift, (
        "engine settings differ from the measured set — take the set, or measure the "
        "difference (approved-encoders.md) and name it in EXCEPTIONS with where:\n  "
        + "\n  ".join(drift))


def test_the_measured_set_holds_each_vendors_exact_mode_and_no_exception_relaxes_it():
    """The set cannot lose the mode invariant 12 requires, and an exception
    cannot drop it either: relaxing it is a near-exact run, declared to the
    wrapper (LOUISE_NEAR_EXACT_MEASURED), never a line here."""
    w = load_wrapper()
    for v, block in MEASURED.items():
        mode = w.determinism_env(f"louise-{v}-f16-llama-cpp-16acc89")
        assert mode.items() <= block["env"].items(), f"{v}: the set lacks {mode}"  # type: ignore[union-attr]
    every_mode = {k for env in w.DETERMINISM.values() for k in env}
    assert not {key for _m, key in EXCEPTIONS} & every_mode


def test_every_exception_names_a_staged_llama_cpp_model_and_where_it_was_measured():
    names = {name: cfg for name, cfg in staged().values()}
    for (model, key), (value, where) in EXCEPTIONS.items():
        assert model in names and vendor(str(names[model].get("backend", ""))), \
            f"{model}: no staged llama.cpp model — a stale exception"
        assert key == "flash_attention" or key.startswith("GGML_")
        assert "approved-encoders.md" in where, f"{model} {key}={value}: where was it measured?"


def test_a_drifted_file_is_named():
    """The rule itself, on files made up for it: a setting the set leaves at
    the default, stated anyway, and an exception's model with the set's
    value, are both drift."""
    chat = {"backend": "vega-louise-llama-cpp", "flash_attention": "on",
            "env": {"GGML_SYCL_ENABLE_DNN": "0", "GGML_SYCL_FA_ONEDNN": "0",
                    "GGML_SYCL_ENABLE_GRAPH": "0"}}
    assert settings(chat) != expected("louise-chat-other", "sycl")
    measured = {**chat, "env": {"GGML_SYCL_ENABLE_DNN": "0", "GGML_SYCL_FA_ONEDNN": "0"}}
    assert settings(measured) == expected("louise-chat-other", "sycl")
    assert settings(measured) != expected("louise-translate-madlad", "sycl"), "its exception unheld"
    cpu = {"backend": "vega-louise-llama-cpp-cpu", "env": {"OMP_NUM_THREADS": "8"}}
    assert settings(cpu) == expected("louise-rerank-cpu", "cpu")
    assert vendor("louise-sycl-f16-llama-cpp-16acc89") == "sycl"
    assert vendor("vega-louise-openvino") is None


def test_an_encoder_is_the_collections_not_the_nodes():
    """A peer may recommend other settings for its own collection's encoder
    (operator, 2026-11-03): no encoder file is held to the node's set, and
    every reranker and chat model is."""
    files = {p.name: _read(p) for p in (STAGE / "models").glob("*.yaml")}
    held = {where.split("/", 1)[1] for where in staged() if where.startswith("models/")}
    for name, cfg in files.items():
        assert (name in held) is (not encoder(cfg)), name
    assert {"louise-rerank-bge.yaml", "louise-chat-qwen3-8b.yaml"} <= held
    assert "vega-nemotron-3-embed-1b.yaml" not in held

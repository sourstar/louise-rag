"""deploy/medcpt-rerank/server.py — MedCPT's cross-encoder on OpenVINO, the
model code the LocalAI backend runs (deploy/localai/backends/louise-medcpt-rerank;
its `Rerank` is tested in tests/test_localai_backends.py). The model itself is
checked against its own weights on the node (docs/notes/reranker-scope.md §12);
here, with a stand-in model, that every pair is scored at its own index, and
the pieces the backend builds it from."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "medcpt_rerank_server", Path(__file__).parent.parent / "deploy" / "medcpt-rerank" / "server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


def test_every_text_is_scored_at_its_own_index_in_length_sorted_batches():
    seen = []

    def encode(pairs):
        seen.append(list(pairs))
        return pairs
    r = server.Reranker(encode, lambda pairs: [float(len(t)) - 5.0 for _q, t in pairs], batch=2)
    texts = ["abc", "abcdefgh", "a", "abcde", "ab"]
    assert r.logits("q", texts) == [-2.0, 3.0, -4.0, 0.0, -3.0]
    assert [len(b) for b in seen] == [2, 2, 1], "batches of `batch` pairs"
    assert [t for b in seen for _q, t in b] == sorted(texts, key=len), "similar lengths together"
    assert all(q == "q" for b in seen for q, _t in b), "the query rides in every pair"


def test_the_sigmoid_is_teis_and_extreme_logits_do_not_overflow():
    assert [server.sigmoid(x) for x in (-1000.0, 0.0, 1000.0)] == [0.0, 0.5, 1.0]


def test_a_model_that_drops_a_score_is_an_error_not_a_misaligned_ranking():
    r = server.Reranker(lambda pairs: pairs, lambda pairs: [1.0] * (len(pairs) - 1), batch=4)
    with pytest.raises(RuntimeError, match="2 scores for 3 pairs"):
        r.logits("q", ["a", "b", "c"])


def test_the_compiled_model_is_kept_where_the_models_yaml_says(monkeypatch):
    """OV_CACHE_DIR reaches OpenVINO as CACHE_DIR; unset, nothing is cached
    (docs/notes/rage.md §20)."""
    import sys
    import types
    calls = []

    class Input:
        def __init__(self, name):
            self.name = name

        def get_any_name(self):
            return self.name

    class Compiled:
        inputs = [Input(n) for n in server.INPUTS]

    class Core:
        def compile_model(self, xml, device, config=None):
            calls.append((xml, device, config))
            return Compiled()

    monkeypatch.setitem(sys.modules, "openvino", types.SimpleNamespace(Core=Core))
    server.make_infer("/m/openvino_model.xml", "GPU", "/state/ov-cache")
    server.make_infer("/m/openvino_model.xml", "GPU")
    assert calls == [("/m/openvino_model.xml", "GPU", {"CACHE_DIR": "/state/ov-cache"}),
                     ("/m/openvino_model.xml", "GPU", {})]

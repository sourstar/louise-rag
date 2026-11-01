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


def test_a_fixed_reranker_infers_full_batches_and_drops_the_fillers():
    """Every inference `batch` pairs (one static shape): a short batch is
    filled with copies of its first pair, whose scores are dropped, so a
    document's score depends on (query, document) alone."""
    seen = []

    def infer(pairs):
        seen.append(len(pairs))
        return [float(len(t)) for _q, t in pairs]
    r = server.Reranker(lambda pairs: pairs, infer, batch=4, fixed=True)
    texts = ["abc", "abcdefgh", "a", "abcde", "ab"]
    assert r.logits("q", texts) == [3.0, 8.0, 1.0, 5.0, 2.0]
    assert seen == [4, 4], "the second batch, of one pair, filled to four"
    alone = server.Reranker(lambda pairs: pairs, infer, batch=4, fixed=True)
    assert alone.logits("q", ["abcde"]) == [5.0]


def test_the_sigmoid_is_teis_and_extreme_logits_do_not_overflow():
    assert [server.sigmoid(x) for x in (-1000.0, 0.0, 1000.0)] == [0.0, 0.5, 1.0]


def test_a_model_that_drops_a_score_is_an_error_not_a_misaligned_ranking():
    r = server.Reranker(lambda pairs: pairs, lambda pairs: [1.0] * (len(pairs) - 1), batch=4)
    with pytest.raises(RuntimeError, match="2 scores for 3 pairs"):
        r.logits("q", ["a", "b", "c"])


def test_the_compiled_model_is_kept_where_the_models_yaml_says(monkeypatch):
    """OV_CACHE_DIR reaches OpenVINO as CACHE_DIR; unset, nothing is cached
    (model-pool.md@c572c3b §20)."""
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


def test_the_article_encoders_vector_is_the_cls_row_of_the_last_hidden_state(monkeypatch):
    """NCBI's `last_hidden_state[:, 0, :]` (louise-medcpt-article-ov)."""
    import sys
    import types

    import numpy as np

    class Input:
        def __init__(self, name):
            self.name = name

        def get_any_name(self):
            return self.name

    hidden = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)

    class Request:
        def __init__(self, inputs):
            self.inputs = inputs

        def values(self):
            return [hidden]

    class Compiled:
        inputs = [Input(n) for n in server.INPUTS]

        def infer_new_request(self, inputs):
            return Request(inputs)

    class Core:
        def compile_model(self, xml, device, config=None):
            return Compiled()

    monkeypatch.setitem(sys.modules, "openvino", types.SimpleNamespace(Core=Core))
    infer = server.make_cls_infer("/m/openvino_model.xml", "GPU")
    assert infer({}) == [[0.0, 1.0, 2.0, 3.0], [12.0, 13.0, 14.0, 15.0]]
    hidden = np.zeros((2, 4), dtype=np.float32)
    with pytest.raises(RuntimeError, match="batch, tokens, dim"):
        infer({})


@pytest.mark.parametrize("make", ["make_infer", "make_cls_infer"])
def test_one_forward_pass_on_the_card_at_a_time(monkeypatch, make):
    """Concurrent requests on OpenVINO's GPU plugin deadlocked the article
    encoder on the node (2026-10-10): every inference of one compiled model
    runs alone, however many gRPC threads call it."""
    import sys
    import threading
    import time
    import types

    import numpy as np

    class Input:
        def __init__(self, name):
            self.name = name

        def get_any_name(self):
            return self.name

    inside, most = [0], [0]
    guard = threading.Lock()

    class Request:
        def values(self):
            return [np.zeros((1, 2, 3), dtype=np.float32)]

    class Compiled:
        inputs = [Input(n) for n in server.INPUTS]

        def infer_new_request(self, inputs):
            with guard:
                inside[0] += 1
                most[0] = max(most[0], inside[0])
            time.sleep(0.02)
            with guard:
                inside[0] -= 1
            return Request()

    class Core:
        def compile_model(self, xml, device, config=None):
            return Compiled()

    monkeypatch.setitem(sys.modules, "openvino", types.SimpleNamespace(Core=Core))
    infer = getattr(server, make)("/m/openvino_model.xml", "GPU")
    threads = [threading.Thread(target=infer, args=({},)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert most[0] == 1


def test_a_fixed_encoder_gives_one_static_shape_and_the_same_real_tokens(tmp_path):
    """`fixed`: every input padded to max_tokens (OpenVINO's GPU plugin was
    not bit-reproducible at some dynamic shapes, 2026-10-30); the attention
    mask still counts the real tokens alone."""
    pytest.importorskip("tokenizers")
    from tokenizers import Tokenizer, models, pre_tokenizers
    tok = Tokenizer(models.WordLevel({"[PAD]": 0, "[UNK]": 1, "a": 2, "b": 3}, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    path = tmp_path / "tokenizer.json"
    tok.save(str(path))
    dynamic = server.make_encoder(str(path), 8)([("a b", "a")])
    fixed = server.make_encoder(str(path), 8, fixed=True)([("a b", "a"), ("a", "b b a")])
    assert dynamic["input_ids"].shape == (1, 3)
    assert fixed["input_ids"].shape == (2, 8)
    assert [int(sum(m)) for m in fixed["attention_mask"]] == [3, 4]
    assert list(fixed["input_ids"][0][:3]) == list(dynamic["input_ids"][0])

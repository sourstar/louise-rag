"""deploy/medcpt-rerank/server.py — MedCPT's cross-encoder behind TEI's
/rerank, on OpenVINO. The model itself is checked against its own weights on
the node (docs/notes/reranker-scope.md §12); here, with a stand-in model,
that every candidate gets its own score back at its own index, and that the
platform's reranker client reads the answer as TEI's."""
import importlib.util
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import retrieval
from app.collections import RerankRecipe

_spec = importlib.util.spec_from_file_location(
    "medcpt_rerank_server", Path(__file__).parent.parent / "deploy" / "medcpt-rerank" / "server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


def _by_length_reranker(batch=2, seen=None):
    """A stand-in model whose logit is the text's length, and which records
    the pairs it was handed."""
    def encode(pairs):
        if seen is not None:
            seen.extend(pairs)
        return pairs

    def infer(pairs):
        return [float(len(t)) - 5.0 for _q, t in pairs]
    return server.Reranker(encode, infer, batch=batch)


def test_every_text_is_scored_at_its_own_index_best_first():
    seen = []
    status, out = server.handle(_by_length_reranker(seen=seen),
                                {"query": "q", "texts": ["abc", "abcdefgh", "a", "abcde", "ab"],
                                 "raw_scores": True})
    assert status == 200
    assert [o["index"] for o in out] == [1, 3, 0, 4, 2]
    assert [o["score"] for o in out] == [3.0, 0.0, -2.0, -3.0, -4.0]
    assert sorted(t for _q, t in seen) == sorted(["abc", "abcdefgh", "a", "abcde", "ab"])
    assert all(q == "q" for q, _t in seen), "the query rides in every pair"


def test_scores_are_sigmoid_unless_raw_and_extreme_logits_do_not_overflow():
    r = server.Reranker(lambda pairs: pairs, lambda pairs: [-1000.0, 0.0, 1000.0][:len(pairs)],
                        batch=3)
    status, out = server.handle(r, {"query": "q", "texts": ["x", "y", "z"]})
    assert status == 200
    assert {o["index"]: o["score"] for o in out} == {0: 0.0, 1: 0.5, 2: 1.0}


def test_malformed_requests_are_refused_not_scored():
    r = _by_length_reranker()
    assert server.handle(r, {"query": "q"})[0] == 422
    assert server.handle(r, {"query": 1, "texts": ["a"]})[0] == 422
    assert server.handle(r, {"query": "q", "texts": ["a", 2]})[0] == 422
    assert server.handle(r, ["not", "an", "object"])[0] == 422
    assert server.handle(r, {"query": "q", "texts": ["a"] * 3}, max_texts=2)[0] == 413
    assert server.handle(r, {"query": "q", "texts": []}) == (200, [])


def test_a_model_that_drops_a_score_is_an_error_not_a_misaligned_ranking():
    r = server.Reranker(lambda pairs: pairs, lambda pairs: [1.0] * (len(pairs) - 1), batch=4)
    try:
        server.handle(r, {"query": "q", "texts": ["a", "b", "c"]})
    except RuntimeError as e:
        assert "2 scores for 3 pairs" in str(e)
    else:
        raise AssertionError("a short answer must not be scored")


def test_the_platform_reads_it_as_tei_end_to_end():
    server.Handler.reranker = _by_length_reranker(batch=3)
    server.Handler.model = "stand-in"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/rerank"
        hits = [{"chunk_id": str(i), "text": t} for i, t in enumerate(["aa", "aaaaaa", "aaaa"])]
        ranked, ok = retrieval._rerank_list("q", hits, RerankRecipe(url=url, api="tei"), 5.0)
        assert ok is True
        assert [h["chunk_id"] for h in ranked] == ["1", "2", "0"]
        import httpx
        assert httpx.get(url, timeout=5).status_code == 200, "the health ping"
    finally:
        httpd.shutdown()


def test_sigterm_stops_the_server_at_once(tmp_path):
    """It runs as PID 1, where SIGTERM has no default action: without its own
    handler every `docker stop` waited 10 s and ended in SIGKILL. Here it is
    not PID 1, so a missing handler shows as death BY the signal (-15)
    instead of the clean exit 0 the handler gives."""
    import signal
    import socket
    import subprocess
    import sys
    import time
    import urllib.request
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    script = tmp_path / "run.py"
    script.write_text(
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('srv', {str(_spec.origin)!r})\n"
        "srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)\n"
        "srv.make_encoder = lambda path: (lambda pairs: pairs)\n"
        "srv.make_infer = lambda xml, device, cache=None: (lambda pairs: [0.0] * len(pairs))\n"
        "srv.main()\n")
    proc = subprocess.Popen([sys.executable, str(script)], env={"PORT": str(port), "PATH": ""},
                            stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 20
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except OSError:
                assert proc.poll() is None and time.time() < deadline, proc.stderr.read()
                time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0
    finally:
        proc.kill()
        proc.wait()


def test_the_compiled_model_is_kept_where_rage_toml_says(monkeypatch):
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

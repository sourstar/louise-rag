"""VeGa end to end, on real gRPC: a fake real backend (fake_backend.py, a
process started as LocalAI starts one), the wrapper in front of it, the
receipts service, a fake LocalAI doing what v4.11.0 does (localai_fake.py),
and Louise's client.

Skipped where grpcio, protobuf and grpcio-tools are absent (conftest.py).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("grpc", reason="grpcio is not in this image (VeGa's gRPC tests)")

import grpc  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.vega import runs as runs_mod  # noqa: E402
from app.vega import client, service  # noqa: E402
from app.vega import tag as vtag  # noqa: E402

from . import localai_fake  # noqa: E402
from .conftest import load_wrapper  # noqa: E402

vw = load_wrapper()
FAKE = Path(__file__).with_name("fake_backend.py")
DIGEST = "sha256:" + "cd" * 32
HALFWAY = struct.unpack("<f", bytes.fromhex("fd43ae15"))[0]


@dataclass
class Node:
    running: Any
    localai: TestClient
    vega_http: TestClient
    models: dict[str, localai_fake.Model]
    options: dict[str, Any]
    log: Path
    weights: Path
    real_run_sh: Path
    stubs: Any

    def embed(self, items, model="vega-fake", window=None, **extra):
        batch = client.tag_batch(items, window=window)
        r = self.localai.post("/v1/embeddings",
                              json={"model": model, "input": list(batch.inputs), **extra})
        return batch, r

    def verify(self, batch, r):
        def get(url, params=None, timeout=None):     # TestClient takes no timeout
            return self.vega_http.get(url, params=params)
        return client.verify_response(batch, r.content, base_url="http://testserver", get=get)

    def calls(self, method):
        lines = [json.loads(x) for x in self.log.read_text().splitlines() if x]
        return [x for x in lines if x["method"] == method]

    def channel(self):
        return grpc.insecure_channel(f"127.0.0.1:{self.running.port}")

    def healthy(self):
        stub = self.stubs.rpc.BackendStub(self.channel())
        deadline = time.monotonic() + 30
        while True:
            try:
                return stub.Health(self.stubs.pb.HealthMessage(), timeout=2)
            except grpc.RpcError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)


@pytest.fixture
def make_node(tmp_path, stubs, vega):
    made = []

    def make(*, pairs=False, receipt_timeout_s=2.0, socket=None, tokenizer=False,
             cuts=None, token_ids=False) -> Node:
        backends = tmp_path / "backends"
        real = backends / "fake-llama-cpp"
        real.mkdir(parents=True)
        run_sh = real / "run.sh"
        run_sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        run_sh.chmod(0o755)
        (real / "metadata.json").write_text(json.dumps(
            {"name": "fake-llama-cpp", "version": "v4.11.0", "digest": DIGEST}))
        pkg = backends / "vega-fake-llama-cpp"
        pkg.mkdir()
        (pkg / "vega.json").write_text(json.dumps(
            {"backend": "fake-llama-cpp", "receipts_socket": str(socket or vega.socket),
             **({"cuts": cuts} if cuts else {})}))
        weights = tmp_path / "models" / "fake.bin"
        weights.parent.mkdir()
        weights.write_bytes(b"weights " * 1000)
        log = tmp_path / "backend.log"
        log.touch()
        env = dict(os.environ, FAKE_BACKEND_LOG=str(log), VEGA_TEST_ENV="on",
                   **({"FAKE_TOKENIZER": "1"} if tokenizer else {}),
                   **({"FAKE_TOKEN_IDS": "1"} if token_ids else {}),
                   PYTHONPATH=os.pathsep.join([str(stubs.dir), os.environ.get("PYTHONPATH", "")]))
        cfg = vw.load_config(pkg / "vega.json", backends)
        running = vw.serve(cfg, "127.0.0.1:0", pb=stubs.pb, environ=env,
                           receipt_timeout_s=receipt_timeout_s)
        options = {"Model": "fake.bin", "ModelFile": str(weights),
                   "ModelPath": str(weights.parent), "ContextSize": 512, "Embeddings": True,
                   "RopeFreqBase": 10000.5,
                   "Options": ["device:cpu", "pairs:true", "parallel:1"] if pairs else ["device:cpu", "parallel:1"],
                   "EnvVars": {"VEGA_TEST_ENV": "on"}}
        models = {"vega-fake": localai_fake.Model(f"127.0.0.1:{running.port}", options)}
        api = FastAPI()
        api.include_router(service.router(vega.receipts, vega.runs))
        node = Node(running, TestClient(localai_fake.create(models, stubs)), TestClient(api),
                    models, options, log, weights, run_sh.resolve(), stubs)
        made.append(node)
        return node

    yield make
    for node in made:
        node.running.stop(2)


def test_tagged_texts_come_back_with_receipts_and_the_run_is_what_was_launched(make_node):
    node = make_node()
    batch, r = node.embed(["hello", "wörld"])
    assert r.status_code == 200, r.text
    vectors, run_ids = node.verify(batch, r)
    assert [c["text"] for c in node.calls("Embedding")] == ["hello", "wörld"], "the tag is stripped"
    (run_id,) = run_ids
    run = node.vega_http.get(f"/vega/runs/{run_id}").json()
    assert runs_mod.run_id(run) == run_id
    assert run["weights"] == "sha256:" + hashlib.sha256(node.weights.read_bytes()).hexdigest()
    assert run["driver"] == "cpu"
    engine = run["engine"]
    assert engine["image"] == vw.tree_digest(node.real_run_sh.parent) != DIGEST, \
        "the files installed, never the registry's digest"
    assert engine["name"] == "localai/fake-llama-cpp" and engine["build"] == "v4.11.0"
    assert engine["args"] == ["${BACKEND}/run.sh", "--addr", "127.0.0.1:${PORT}"], \
        "relative to the backend, whose bytes image names (09 §9.3)"
    assert engine["env"] == {"VEGA_TEST_ENV": "on"} and engine["device"] == "cpu"
    assert engine["options"]["ModelFile"] == "${WEIGHTS}"
    assert engine["options"]["ModelPath"] == "${MODELS}"
    assert engine["options"]["RopeFreqBase"] == "10000.5"
    assert "EnvVars" not in engine["options"], "the environment is env's, stated once"
    v = vectors[0]
    assert v[5] == 0.0 and math.copysign(1.0, v[5]) == -1.0, "negative zero kept"
    assert struct.pack("<f", v[6]) == bytes.fromhex("fd43ae15"), "the halfway float kept"


def test_base64_vectors_verify_as_the_bytes_they_are(make_node):
    node = make_node()
    batch, r = node.embed(["one"], encoding_format="base64")
    assert r.status_code == 200
    vectors, _runs = node.verify(batch, r)
    assert len(vectors[0]) == 8


def test_an_untagged_input_is_refused_before_the_backend_sees_it(make_node):
    node = make_node()
    r = node.localai.post("/v1/embeddings", json={"model": "vega-fake", "input": ["plain"]})
    assert r.status_code == 500
    message = r.json()["error"]["message"]
    assert "InvalidArgument" in message and "without a VeGa tag" in message
    assert node.calls("Embedding") == []
    assert not client.is_deferral(r.status_code, r.text)


def test_an_answer_without_a_receipt_is_a_deferral(make_node):
    node = make_node()
    node.healthy()
    # LocalAI routing the same model to the backend without VeGa in front
    node.models["plain-route"] = localai_fake.Model(
        f"127.0.0.1:{node.running.child.port}", node.options)
    batch, r = node.embed(["hello"], model="plain-route")
    assert r.status_code == 200, "the backend embedded the tag itself"
    with pytest.raises(client.Deferral, match="no receipt"):
        node.verify(batch, r)


def test_a_vector_changed_by_the_runner_is_refused(make_node):
    node = make_node()
    node.models["vega-fake"].alter_vector = lambda v: [v[0] + 2.0 ** -12] + v[1:]
    batch, r = node.embed(["hello"])
    assert r.status_code == 200
    with pytest.raises(client.Refusal, match="the vector LocalAI returned"):
        node.verify(batch, r)


def test_an_input_changed_by_the_runner_is_refused(make_node):
    node = make_node()

    def retag(s):
        tid, form, content = vtag.parse(s)
        return vtag.tag(form, content + " (edited)", id=tid)[1]
    node.models["vega-fake"].alter_input = retag
    batch, r = node.embed(["hello"])
    assert r.status_code == 200
    with pytest.raises(client.Refusal, match="received another input"):
        node.verify(batch, r)


def test_a_pair_to_a_backend_loaded_without_pairs_is_refused(make_node):
    node = make_node(pairs=False)
    batch, r = node.embed([("Aspirin", "An abstract.")])
    assert r.status_code == 500
    assert "InvalidArgument" in r.text and "pairs:true" in r.text
    assert node.calls("Embedding") == []


def test_a_pair_reaches_a_pairs_backend_in_the_form_it_reads(make_node):
    node = make_node(pairs=True)
    batch, r = node.embed([("Aspirin", "An abstract.\n\nTwo parts.")])
    assert r.status_code == 200
    node.verify(batch, r)
    (call,) = node.calls("Embedding")
    assert call["text"] == vtag.backend_pair("Aspirin", "An abstract.\n\nTwo parts.")


def test_an_unrelated_method_is_forwarded_as_bytes(make_node):
    node = make_node()
    node.healthy()
    pb, rpc = node.stubs.pb, node.stubs.rpc
    stub = rpc.BackendStub(node.channel())
    reply, call = stub.Predict.with_call(pb.PredictOptions(Prompt="hi"),
                                         metadata=[("x-test", "42")], timeout=10)
    assert reply.message == b"echo:hi"
    assert ("x-fake", "trailer") in tuple(call.trailing_metadata())
    assert node.calls("Predict")[0]["metadata"] == {"x-test": "42"}, "metadata passed on"
    stream = [r.message for r in stub.PredictStream(pb.PredictOptions(Prompt="s"), timeout=10)]
    assert stream == [b"s:0", b"s:1", b"s:2"]
    unknown = node.channel().unary_unary("/backend.Backend/NoSuchMethodYet")
    with pytest.raises(grpc.RpcError) as e:
        unknown(b"\x0a\x01x", timeout=10)
    assert e.value.code() == grpc.StatusCode.UNIMPLEMENTED, "the backend's own answer"


def test_calls_without_a_deadline_are_served(make_node):
    """LocalAI v4.11.0 sets no deadline on LoadModel, Embedding or Predict.
    grpcio reports that as 9.2e18 s remaining, which passed on as a timeout
    was a deadline already past: every load on the node failed at once
    (docs/notes/vega.md §10)."""
    node = make_node()
    batch, r = node.embed(["no deadline"])         # the fake sets none, as LocalAI
    assert r.status_code == 200, r.text
    node.verify(batch, r)
    stub = node.stubs.rpc.BackendStub(node.channel())
    assert stub.Predict(node.stubs.pb.PredictOptions(Prompt="hi")).message == b"echo:hi"


def test_the_receipt_is_held_before_the_vector_leaves(make_node, vega):
    node = make_node(receipt_timeout_s=10)
    node.embed(["warm"])                                  # loaded, the run told
    entered, release = threading.Event(), threading.Event()
    store_put = vega.receipts.put

    def held_put(receipt):
        entered.set()
        release.wait(10)
        return store_put(receipt)
    vega.receipts.put = held_put
    out: dict[str, Any] = {}
    batch = client.tag_batch(["ordered"])
    t = threading.Thread(target=lambda: out.update(r=node.localai.post(
        "/v1/embeddings", json={"model": "vega-fake", "input": list(batch.inputs)})))
    t.start()
    assert entered.wait(10), "the receipt reached VeGa"
    assert node.calls("Embedding")[-1]["text"] == "ordered", "the backend has answered"
    time.sleep(0.3)
    assert t.is_alive() and "r" not in out, "and no vector is out before the receipt is held"
    release.set()
    t.join(10)
    assert out["r"].status_code == 200
    node.verify(batch, out["r"])


def test_no_receipts_service_means_no_vector(make_node, tmp_path):
    node = make_node(receipt_timeout_s=0.3, socket=tmp_path / "nobody.sock")
    t0 = time.monotonic()
    batch, r = node.embed(["hello"])
    assert r.status_code == 500 and "Unavailable" in r.text
    assert client.is_deferral(r.status_code, r.text)
    assert node.calls("Embedding"), "the backend answered; the vector stayed in the wrapper"
    assert time.monotonic() - t0 < 30


def test_load_options_the_proto_cannot_name_are_refused(make_node):
    node = make_node()
    pb = node.stubs.pb
    # field 1000, varint 1: what a newer LocalAI's option would look like here
    raw = pb.ModelOptions(**node.options).SerializeToString() + bytes([0xC0, 0x3E, 0x01])
    answer = node.channel().unary_unary(vw.LOAD)(raw, timeout=10)
    result = pb.Result.FromString(answer)
    assert not result.success and "cannot name" in result.message
    assert node.calls("LoadModel") == [], "refused before the backend loads anything"


def test_an_upgraded_backend_is_a_new_run_from_its_next_load(make_node):
    """The tree is read at every load: files changed on disk make the next
    load another run, and only the changed files are read again."""
    node = make_node()
    batch, r = node.embed(["before"])
    _vectors, (first,) = node.verify(batch, r)
    files = node.running.gate.files
    files.reads.clear()
    (node.real_run_sh.parent / "lib").mkdir()
    (node.real_run_sh.parent / "lib" / "libnew.so.1").write_bytes(b"an upgrade")
    node.models["vega-fake"].loaded = False                # LocalAI loads the model again
    batch, r = node.embed(["after"])
    assert r.status_code == 200, r.text
    _vectors, (second,) = node.verify(batch, r)
    assert second != first
    assert node.vega_http.get(f"/vega/runs/{second}").json()["engine"]["image"] == \
        vw.tree_digest(node.real_run_sh.parent)
    assert files.reads == [os.path.realpath(node.real_run_sh.parent / "lib" / "libnew.so.1")]
    # the per-file lists behind both runs are kept by VeGa, and their diff names the byte
    from app.vega import trees
    before = node.vega_http.get(f"/vega/runs/{first}").json()["engine"]["image"]
    after = node.vega_http.get(f"/vega/runs/{second}").json()["engine"]["image"]
    a, b = (node.vega_http.get(f"/vega/trees/{i}").json() for i in (before, after))
    d = trees.diff(a, b)
    assert set(d["added"]) == {"lib", "lib/libnew.so.1"} and not d["removed"] and not d["changed"]
    assert d["added"]["lib/libnew.so.1"]["size"] == len(b"an upgrade")


def test_no_vector_before_a_stated_load(make_node):
    node = make_node()
    pb = node.stubs.pb
    _tid, tagged = vtag.tag("text", "early")
    with pytest.raises(grpc.RpcError) as e:
        node.channel().unary_unary(vw.EMBED)(pb.PredictOptions(Embeddings=tagged)
                                             .SerializeToString(), timeout=10)
    assert e.value.code() == grpc.StatusCode.FAILED_PRECONDITION


# ---- the wrapper as LocalAI starts it ---------------------------------------------------

#: LocalAI's grpc_auth and parent_watch are copied into the package from the
#: LocalAI release at build; these stand-ins have their names and nothing else
STAND_INS = {
    "grpc_auth.py": "import grpc\n\nclass TokenAuthInterceptor(grpc.ServerInterceptor):\n"
                    "    def __init__(self, token):\n        self.token = token\n\n"
                    "    def intercept_service(self, continuation, details):\n"
                    "        return continuation(details)\n",
    "parent_watch.py": "def start_parent_death_watcher():\n    pass\n",
}


def _start(tmp_path, stubs, make_node_dirs):
    import subprocess
    for name, text in STAND_INS.items():
        (stubs.dir / name).write_text(text)
    env = dict(os.environ, VEGA_CONFIG=str(make_node_dirs / "vega.json"),
               FAKE_BACKEND_LOG=str(tmp_path / "backend.log"),
               PYTHONPATH=os.pathsep.join([str(stubs.dir), os.environ.get("PYTHONPATH", "")]))
    port = vw.free_port()
    proc = subprocess.Popen([sys.executable, str(vw.__file__), "--addr", f"127.0.0.1:{port}"],
                            env=env, stderr=subprocess.PIPE, text=True)
    return proc, port


def _package(tmp_path, metadata):
    backends = tmp_path / "backends"
    real = backends / "fake-llama-cpp"
    real.mkdir(parents=True)
    run_sh = real / "run.sh"
    run_sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    run_sh.chmod(0o755)
    (real / "metadata.json").write_text(json.dumps(metadata))
    pkg = backends / "vega-fake-llama-cpp"
    pkg.mkdir()
    (pkg / "vega.json").write_text(json.dumps({"backend": "fake-llama-cpp",
                                               "receipts_socket": str(tmp_path / "r.sock")}))
    return pkg


def test_the_wrapper_starts_as_localai_starts_it_and_stops_on_sigterm(tmp_path, stubs):
    import signal
    import subprocess
    pkg = _package(tmp_path, {"name": "fake-llama-cpp", "version": "v1", "digest": DIGEST})
    (tmp_path / "backend.log").touch()
    proc, port = _start(tmp_path, stubs, pkg)
    try:
        stub = stubs.rpc.BackendStub(grpc.insecure_channel(f"127.0.0.1:{port}"))
        deadline = time.monotonic() + 30
        while True:
            try:
                assert stub.Health(stubs.pb.HealthMessage(), timeout=2).message == b"OK"
                break
            except grpc.RpcError:
                assert proc.poll() is None and time.monotonic() < deadline
                time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    err = proc.stderr.read()
    child = int(err.split("(pid ")[1].split(")")[0])
    state = Path(f"/proc/{child}/stat")
    assert not state.exists() or state.read_text().rpartition(")")[2].split()[0] == "Z", \
        "the real backend stopped with its wrapper"


def test_a_directory_that_is_no_backend_refuses_to_start(tmp_path, stubs):
    pkg = _package(tmp_path, {"name": "fake-llama-cpp", "version": "v1"})
    (pkg.parent / "fake-llama-cpp" / "run.sh").unlink()
    proc, _port = _start(tmp_path, stubs, pkg)
    assert proc.wait(timeout=20) == 2
    assert "no run.sh" in proc.stderr.read()


# ---- the cut to a tag's window (docs/notes/char-cut-truncation.md) ----------------

WORDS = " ".join(f"w{i}" for i in range(20))


def test_a_long_text_is_cut_at_its_window_by_the_models_own_tokens(make_node):
    """09 §9.3: a chunk is truncated to `ctx` TOKENS. The backend adds two
    special tokens, so a window of 10 keeps the first 8 of the text's own."""
    node = make_node(tokenizer=True)
    batch, r = node.embed([WORDS], window=10)
    assert r.status_code == 200, r.text
    node.verify(batch, r)
    embedded = [c["text"] for c in node.calls("Embedding")]
    assert embedded[-1].split() == [f"w{i}" for i in range(8)], embedded
    assert WORDS.startswith(embedded[-1]), "a prefix of the input itself"
    assert batch.input_digests[0] == vtag.input_digest("text", WORDS, 10), \
        "the receipt binds the text AND its window"
    assert batch.input_digests[0] != vtag.input_digest("text", WORDS)


def test_a_text_within_its_window_goes_whole(make_node):
    node = make_node(tokenizer=True)
    batch, r = node.embed(["w1 w2 w3"], window=10)
    assert r.status_code == 200, r.text
    node.verify(batch, r)
    assert node.calls("Embedding")[-1]["text"] == "w1 w2 w3"
    assert len(node.calls("TokenizeString")) == 2, "the probe at load, then the input once"


def test_a_backend_whose_tokens_cannot_be_counted_cuts_nothing(make_node):
    """Without a tokenizer it can reach, the wrapper refuses an input with a
    window rather than guess a cut; one without a window is embedded whole,
    as before."""
    node = make_node(tokenizer=False)
    _batch, r = node.embed([WORDS], window=10)
    assert r.status_code == 500 and "could not be counted at load" in r.text
    batch, r = node.embed([WORDS])
    assert r.status_code == 200
    node.verify(batch, r)


def test_a_backend_that_cuts_itself_takes_its_own_window_only(make_node):
    """`cuts: context_size` (louise-python): the input goes whole, and the
    window must be the size it was loaded with (512 here)."""
    node = make_node(cuts="context_size")
    batch, r = node.embed([WORDS], window=512)
    assert r.status_code == 200, r.text
    node.verify(batch, r)
    assert node.calls("Embedding")[-1]["text"] == WORDS
    _batch, r = node.embed([WORDS], window=1024)
    assert r.status_code == 500 and "context_size of 512" in r.text


def test_a_pair_is_never_cut_by_the_wrapper(make_node):
    node = make_node(pairs=True, tokenizer=True)
    _batch, r = node.embed([("Aspirin", "An abstract.")], window=10)
    assert r.status_code == 500 and "cannot cut a pair" in r.text


def test_a_backend_that_embeds_other_than_the_cut_is_refused(make_node, monkeypatch):
    node = make_node(tokenizer=True)
    real = vw.Gate._cut

    def short_count(self, *a, **k):
        text, expect = real(self, *a, **k)
        return text, expect + 1                      # the answer will disagree
    monkeypatch.setattr(vw.Gate, "_cut", short_count)
    _batch, r = node.embed([WORDS], window=10)
    assert r.status_code == 500 and "embedded 10 token(s) of an input cut to 11" in r.text


def test_where_no_prefix_has_the_tokens_the_pieces_read_back_are_sent(stubs):
    """The second way of `Gate._cut`: no prefix of the input tokenizes to the
    first `keep` tokens (a word segmented otherwise once cut), so the text read
    back from those tokens' pieces is sent — checked to tokenize to them."""
    from types import SimpleNamespace
    pb = stubs.pb
    want = [7, 8]

    def tokens(_call, _req, text):
        if text == "alpha beta":                     # the pieces read back, the space dropped
            return want
        if text == "alphabetagammadelta":            # the whole input
            return [7, 8, 9, 9, 9]
        return [7, 9, 9, 9, 9, 9][:max(1, len(text) // 4)]   # every prefix: another second token
    gate = SimpleNamespace(pb=pb, cfg=SimpleNamespace(cuts=vw.TOKENIZER),
                           backend=SimpleNamespace(name="fake"), _tokens=tokens)

    def call(method, raw):
        assert method == vw.DETOKENIZE and list(pb.DetokenizeRequest.FromString(raw).tokens) == want
        return pb.DetokenizeResponse(content=" alpha beta").SerializeToString()
    state = SimpleNamespace(specials=2, context_size=0, token_ids=False)
    text, expect = vw.Gate._cut(gate, state, call, pb.PredictOptions(), "text",
                                "alphabetagammadelta", 4)
    assert (text, expect) == ("alpha beta", 4)


def test_a_window_that_fills_the_models_context_is_refused_at_once(make_node):
    """llama.cpp embeds at most context_size − 1 tokens (`n_tokens >= n_ctx` is
    refused): a window of 512 on a model loaded at 512 could never pass whole,
    so the wrapper says so before cutting anything."""
    node = make_node(tokenizer=True)                 # loaded with ContextSize 512
    _batch, r = node.embed([WORDS], window=512)
    assert r.status_code == 500 and "needs a context_size above it" in r.text
    batch, r = node.embed([WORDS], window=511)
    assert r.status_code == 200, r.text


def test_a_backend_that_embeds_token_ids_is_sent_the_first_tokens_themselves(make_node):
    """09 §9.3's cut as the book makes it: the text's tokens, the first kept,
    those embedded — no text is looked for (the 3 of 512 no text could cut
    exactly on llama.cpp, the AMD peer 2026-10-18). Our llama.cpp backend
    embeds them (localai-bugs P2); the probe at load found that it does."""
    node = make_node(tokenizer=True, token_ids=True)
    batch, r = node.embed([WORDS], window=10)
    assert r.status_code == 200, r.text
    node.verify(batch, r)
    sent = node.calls("Embedding")[-1]
    assert sent["text"] == "" and len(sent["tokens"]) == 8, sent
    assert not node.calls("Detokenize"), "no text is read back"
    _b, whole = node.embed([" ".join(f"w{i}" for i in range(8))])
    assert whole.json()["data"][0]["embedding"] == r.json()["data"][0]["embedding"],         "the 8 ids sent ARE the first 8 tokens, w0…w7"
    assert batch.input_digests[0] == vtag.input_digest("text", WORDS, 10)
    batch, r = node.embed(["w1 w2 w3"], window=10)
    assert r.status_code == 200 and node.calls("Embedding")[-1]["text"] == "w1 w2 w3",         "an input within its window goes as its text"


def test_a_backend_that_ignores_token_ids_is_found_out_at_load(make_node):
    """Stock LocalAI v4.11.0's llama.cpp backend drops `EmbeddingTokens`
    (localai-bugs B14). The probe sends the ids WITH NO TEXT, so the backend
    refuses instead of embedding a text beside them, and every cut stays a
    text cut, exact or refused."""
    node = make_node(tokenizer=True)
    batch, r = node.embed([WORDS], window=10)
    assert r.status_code == 200, r.text
    probes = [c for c in node.calls("Embedding") if c["tokens"]]
    assert len(probes) == 1 and probes[0]["text"] == "", "the ids alone, once, at load"
    assert node.calls("Embedding")[-1]["tokens"] == [] and         node.calls("Embedding")[-1]["text"].split() == [f"w{i}" for i in range(8)]

"""The t5 shape of app/translate.py (2026-10-27): MADLAD on our llama.cpp
backend (docs/notes/localai-bugs.md P3, P4) takes one raw "<2xx> segment"
prompt per segment. The shape is determined from the server — a tokenizer
that makes ONE token of "<2en>" — never configured; the client cuts as the
torch backend cut, sends a call's segments at once (one wave on the server),
puts the pieces back with their paragraphs, and reads the target codes off
the model's own vocabulary."""
import importlib.util
import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from app import config, dispatch
from app import translate as mt

_spec = importlib.util.spec_from_file_location(
    "madlad_translate_server", Path(__file__).parent.parent / "deploy" / "madlad-translate" / "server.py")
torch_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(torch_server)

CODES = ["en", "fr", "de", "it", "fi", "ja", "zh", "fil", "la"]


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)

    def json(self):
        return self._data


def _t5_server(monkeypatch, *, vocab=5000, lang_ids=None, calls=None, fail_after=None, concurrent=None):
    """A LocalAI serving MADLAD on llama.cpp: /v1/tokenize (5 bytes a token,
    `<2xx>` one token), /v1/detokenize (the `<2xx>` pieces at `lang_ids`, a
    chunk past `vocab` fails), /v1/completions (`xx|SEGMENT` upper-cased,
    padded with the spaces the clean-up strips)."""
    monkeypatch.setattr(config, "TRANSLATE_URL", "http://gpu:30286")
    monkeypatch.setattr(config, "TRANSLATE_MODEL", "louise-translate-madlad-gguf")
    lang_ids = lang_ids if lang_ids is not None else {4990 + i: c for i, c in enumerate(CODES)}
    in_flight = {"now": 0, "max": 0}
    lock = threading.Lock()

    def post(url, json=None, timeout=None, headers=None):
        if calls is not None:
            calls.append((url.rsplit("/", 1)[-1], json))
        if url.endswith("/v1/tokenize"):
            text = json["content"]
            if text.startswith("<2") and text.endswith(">") and text.count("<") == 1:
                return _Resp({"tokens": [4990]})
            return _Resp({"tokens": list(range((len(text.encode()) + 4) // 5))})
        if url.endswith("/v1/detokenize"):
            ids = json["tokens"]
            if ids[-1] >= vocab:
                return _Resp({"error": "token out of range"}, 500)
            return _Resp({"content": "".join(f"<2{lang_ids[i]}>" if i in lang_ids else "x" for i in ids)})
        assert url.endswith("/v1/completions"), url
        assert json["temperature"] == 0 and json["model"] == "louise-translate-madlad-gguf"
        code, _, seg = json["prompt"].partition(" ")
        assert code.startswith("<2") and code.endswith(">"), json["prompt"]
        if fail_after is not None and seg != "OK" \
                and len([c for c in (calls or []) if c[0] == "completions" and not c[1]["prompt"].endswith(" OK")]) > fail_after:
            raise httpx.ConnectError("gone")      # the warm probe ("<2it> OK") never fails here
        with lock:
            in_flight["now"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["now"])
        if concurrent is not None:
            time.sleep(0.05)                     # long enough for the others to be in flight
        with lock:
            in_flight["now"] -= 1
        if seg == "RUNAWAY":
            return _Resp({"choices": [{"text": "x" * 2000}]})
        return _Resp({"choices": [{"text": f" {code[2:-1]}|{seg.upper()} "}]})

    monkeypatch.setattr(mt.httpx, "post", post)
    mt.reset()
    return in_flight


@pytest.fixture(autouse=True)
def _no_gate(monkeypatch):
    from contextlib import nullcontext
    monkeypatch.setattr(dispatch, "call", lambda *a, **k: nullcontext())
    monkeypatch.setattr(mt.model_load, "ensure_loaded", lambda url, model, warm, **k: warm(5.0))
    yield
    mt.reset()


def test_the_shape_is_read_off_the_server_and_cached(monkeypatch):
    calls = []
    _t5_server(monkeypatch, calls=calls)
    assert mt.shape() == mt.SHAPE_T5
    assert mt.shape() == mt.SHAPE_T5
    assert [c for c in calls if c[0] == "tokenize"] == [("tokenize", {"content": "<2en>", "model": "louise-translate-madlad-gguf"})], \
        "one probe, then the cache"
    mt.reset()
    # the python backend has no tokenizer route: the batch shape
    monkeypatch.setattr(mt.httpx, "post", lambda url, json=None, timeout=None, headers=None: _Resp({"error": "unimplemented"}, 500))
    assert mt.shape() == mt.SHAPE_BATCH
    mt.reset()
    # a server that cannot be asked: the batch shape for this call, not cached

    def down(url, json=None, timeout=None, headers=None):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(mt.httpx, "post", down)
    assert mt.shape() == mt.SHAPE_BATCH
    assert mt._shape_cache is None


def test_the_target_codes_are_the_models_own_tokens(monkeypatch):
    calls = []
    _t5_server(monkeypatch, calls=calls)
    got = mt.languages()
    assert set(CODES) <= got and "tl" in got, "the <2xx> tokens, plus the platform's alias of fil"
    assert "xx" not in got
    scans = [c for c in calls if c[0] == "detokenize"]
    assert 1 < len(scans) < 40, "chunks of 4096 ids, halved at the vocabulary's end"
    assert all(c[1]["tokens"][0] < 5000 for c in scans if c[1]["tokens"][0] < 5000)
    calls.clear()
    mt.languages()
    assert not [c for c in calls if c[0] == "detokenize"], "cached"


def test_each_segment_is_one_raw_prompt_sent_with_the_others(monkeypatch):
    calls = []
    in_flight = _t5_server(monkeypatch, calls=calls, concurrent=True)
    out = mt.translate([("The keeper lit the lamp. It burned.\n\nMorning came.", "fr"),
                        ("short", "de"), ("unknown target", "xx"), ("hello", "tl")])
    assert out[0] == "fr|THE KEEPER LIT THE LAMP. IT BURNED.\n\nfr|MORNING CAME."
    assert out[1] == "de|SHORT" and out[2] is None and out[3] == "fil|HELLO", \
        "an unknown target is None; a platform code reaches the model's token"
    prompts = [c[1]["prompt"] for c in calls if c[0] == "completions" and c[1]["prompt"] != "<2it> OK"]
    assert prompts == ["<2fr> The keeper lit the lamp. It burned.", "<2fr> Morning came.",
                       "<2de> short", "<2fil> hello"]
    assert in_flight["max"] >= 2, "a call's segments are in flight together (one wave on the server)"
    bodies = [c[1] for c in calls if c[0] == "completions" and c[1]["prompt"] != "<2it> OK"]
    assert all(b["top_k"] == 1 and b["seed"] == 0 and 0 < b["max_tokens"] <= 512 for b in bodies)


def test_the_clean_up_is_transformers_and_a_runaway_or_a_failure_is_honest(monkeypatch):
    _t5_server(monkeypatch)
    # transformers' clean_up_tokenization, rule for rule (" n't" -> "n't" eats the space before it too)
    assert mt.clean_up(" Il a dit : « non » . Vous ' re ici , n't ? ") == "Il a dit : « non ». Vous're ici,n't?"
    out = mt.translate([("RUNAWAY", "fr"), ("fine", "fr")])
    assert out == [None, "fr|FINE"], "an output many times its input is no translation"
    calls = []
    _t5_server(monkeypatch, calls=calls, fail_after=1)
    assert mt.translate([("a", "fr"), ("b", "fr"), ("c", "fr")]) == [], "one failed request fails the call, as a failed batch did"


def test_the_cut_is_the_torch_backends_cut(monkeypatch):
    """The same `split_segments` as deploy/madlad-translate/server.py, so the
    two shapes make the same segments of a text given the same count."""
    text = ("one two three. four five six. seven eight nine ten eleven twelve thirteen\n\n"
            "A second paragraph\nwrapped at a column. And more.")

    def words(t):
        return len(t.split())
    assert mt.split_segments(text, 4, words) == torch_server.split_segments(text, 4, words)
    assert mt.split_segments(text, 96, words) == torch_server.split_segments(text, 96, words)
    # the estimate: CJK a token a character, Latin about 3.5 characters a token
    assert mt.estimate_tokens("日本語の文章") == 7
    assert 9 <= mt.estimate_tokens("The keeper of the lighthouse climbed") <= 11
    assert mt.target_code("TL") == "fil" and mt.target_code("fr") == "fr"


def test_warm_sends_the_t5_probe_and_a_four_token_completion(monkeypatch):
    calls = []
    _t5_server(monkeypatch, calls=calls)
    mt.warm()
    kinds = [c[0] for c in calls]
    assert kinds == ["tokenize", "completions"]
    assert calls[1][1]["max_tokens"] == 4 and calls[1][1]["prompt"] == "<2it> OK"


def test_the_batch_shape_is_untouched(monkeypatch):
    """The python backend: the request as one JSON prompt, the answer as the
    completion's text — as before this shape existed."""
    monkeypatch.setattr(config, "TRANSLATE_URL", "http://gpu:30286")
    monkeypatch.setattr(config, "TRANSLATE_MODEL", "louise-translate-madlad")
    seen = []

    def post(url, json=None, timeout=None, headers=None):
        seen.append(url.rsplit("/", 1)[-1])
        if url.endswith("/v1/tokenize"):
            return _Resp({"error": "unimplemented"}, 500)
        asked = __import__("json").loads(json["prompt"])
        if asked == {"languages": True}:
            return _Resp({"choices": [{"text": __import__("json").dumps({"languages": ["it", "fr"]})}]})
        return _Resp({"choices": [{"text": __import__("json").dumps(
            {"translations": [f"{it['to']}:{it['text']}" for it in asked["items"]]})}]})
    monkeypatch.setattr(mt.httpx, "post", post)
    mt.reset()
    assert mt.translate([("hello", "it")]) == ["it:hello"]
    assert mt.languages() == frozenset({"it", "fr"})
    assert seen.count("tokenize") == 1, "the shape probe once; the batch shape thereafter"

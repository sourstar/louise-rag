"""deploy/madlad-translate/server.py — MADLAD-400 behind POST /translate on
the Intel GPU. The model itself is measured on the node; here, with a
stand-in model, that every item comes back whole, in its own place, with
its paragraphs, and that a target the model does not know is null — never
the input handed back as a translation."""
import importlib.util
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "madlad_translate_server",
    Path(__file__).parent.parent / "deploy" / "madlad-translate" / "server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

KNOWN = {"<2de>", "<2it>", "<2fil>", "<2la>"}


def words(text: str) -> int:
    return len(text.split())


def stand_in(seen=None, batch=4, max_seg=8):
    """Upper-cases each segment and keeps its target tag, recording the
    batches it was handed."""
    def generate(prompts, max_new):
        if seen is not None:
            seen.append(list(prompts))
        return [p.split(" ", 1)[0] + "|" + p.split(" ", 1)[1].upper() for p in prompts]
    return server.Translator(words, generate, lambda t: t in KNOWN,
                             batch=batch, max_seg_tokens=max_seg)


def test_paragraphs_survive_and_line_wraps_do_not():
    text = "The keeper lit\nthe lamp. It burned.\n\nMorning came."
    assert server.split_segments(text, 16, words) == [
        ["The keeper lit the lamp. It burned."], ["Morning came."]]


def test_a_long_paragraph_is_cut_on_sentences_and_a_long_sentence_on_words():
    text = "one two three. four five six. seven eight nine ten eleven twelve thirteen"
    segs = server.split_segments(text, 4, words)[0]
    assert all(words(s) <= 4 for s in segs), segs
    assert " ".join(segs) == text, "nothing is lost between the pieces"


def test_every_item_comes_back_whole_in_its_own_place():
    seen = []
    t = stand_in(seen=seen, batch=3, max_seg=4)
    out = t.translate([("alpha beta. gamma delta epsilon.\n\nzeta", "de"),
                       ("short", "it"),
                       ("unknown target", "xx")])
    # two sentences fit one segment (the floor is 16 tokens); the
    # paragraph break is kept
    assert out[0] == "<2de>|ALPHA BETA. GAMMA DELTA EPSILON.\n\n<2de>|ZETA"
    assert out[1] == "<2it>|SHORT"
    assert out[2] is None, "an unknown target is null, never the input"
    assert all(len(b) <= 3 for b in seen), "batches are the size asked for"


def test_the_platforms_codes_reach_the_models_tokens():
    out = stand_in().translate([("salamat", "tl")])
    assert out == ["<2fil>|SALAMAT"], "Tagalog is MADLAD's fil"


def test_a_request_is_validated_before_the_model_is_asked():
    t = stand_in()
    assert server.handle(t, {"items": [{"text": "x"}]})[0] == 422
    assert server.handle(t, {"items": "x"})[0] == 422
    assert server.handle(t, {"items": [{"text": "x", "to": "de"}] * 3}, max_items=2)[0] == 413
    status, body = server.handle(t, {"items": [{"text": "hello", "to": "de"}]})
    assert status == 200 and body["translations"] == ["<2de>|HELLO"]


def test_the_http_round_trip():
    server.Handler.translator = stand_in()
    server.Handler.languages = ["de", "it"]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        langs = json.load(urllib.request.urlopen(f"{base}/languages", timeout=5))
        assert langs == {"languages": ["de", "it"]}
        req = urllib.request.Request(
            f"{base}/translate", method="POST",
            data=json.dumps({"items": [{"text": "hello", "to": "it"}]}).encode(),
            headers={"Content-Type": "application/json"})
        got = json.load(urllib.request.urlopen(req, timeout=5))
        assert got["translations"] == ["<2it>|HELLO"]
    finally:
        httpd.shutdown()


def test_the_weights_leave_the_page_cache_once_loaded(tmp_path, monkeypatch):
    # every file under a local MODEL_ID is advised away — the 2.5 GB TrueNAS
    # counted against the container — and a hub id not cached here is no error
    (tmp_path / "sub").mkdir()
    for p in ("model.safetensors", "config.json", "sub/spiece.model"):
        (tmp_path / p).write_bytes(b"x")
    advised = []
    monkeypatch.setattr(server.os, "posix_fadvise",
                        lambda fd, off, n, how: advised.append(how))
    assert server._drop_file_cache(str(tmp_path)) == 3
    assert advised == [server.os.POSIX_FADV_DONTNEED] * 3
    assert server._drop_file_cache("no-such/model-anywhere") == 0

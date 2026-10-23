"""tools/measure/classical_guard.py shuts down Louise's idle models to give the
card back after a measurement: whether a model is free is read LIVE from
LocalAI, since Louise pins a model while it processes with it
(app/model_pins.py). A static list let it shut down an encoder that was
embedding: the worker's embeddings count as quiet there."""
import importlib.util
import io
import json
import sys
from pathlib import Path


def _guard(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["classical_guard.py", str(tmp_path), "19000", "translator"])
    path = Path(__file__).resolve().parents[1] / "tools" / "measure" / "classical_guard.py"
    spec = importlib.util.spec_from_file_location("classical_guard_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_model_is_free_only_when_localai_says_it_is_not_pinned(monkeypatch, tmp_path):
    g = _guard(monkeypatch, tmp_path)
    state = {"vega-dar-legal-it": True, "madlad": False}

    def urlopen(url, timeout=None):
        model = url.rsplit("/", 1)[1]
        if model not in state:
            raise OSError("unreachable")
        return io.BytesIO(json.dumps({"pinned": state[model]}).encode())
    monkeypatch.setattr(g.urllib.request, "urlopen", urlopen)
    assert g.pinned("vega-dar-legal-it") is True      # held by Louise now: never shut down
    assert g.pinned("madlad") is False
    assert g.pinned("unknown") is True, "unreadable counts as pinned: never shut down blind"

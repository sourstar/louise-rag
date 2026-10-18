"""tools/vega_cut_scan.py: every text alone, tagged with the window; the
refusals listed with their index and the wrapper's reason."""
from __future__ import annotations

from app.vega import tag as vtag
from tools import vega_cut_scan


class Answer:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


def test_each_text_goes_alone_with_its_window_and_refusals_are_named():
    sent = []

    def post(url, json, timeout):
        assert url == "http://localai:30286/v1/embeddings" and json["model"] == "vega-bge-m3"
        (tagged,) = json["input"]
        _id, _form, content, window = vtag.parse_window(tagged)
        sent.append((content, window))
        if content.startswith("Refer"):
            return Answer(500, {"error": {"message": "VeGa: this input cannot be cut at 1024 "
                                                     "tokens exactly"}})
        return Answer(200, {"data": [{"embedding": [0.0]}]})

    texts = [{"i": 0, "text": "Cold ash in the grate"},
             {"i": 372, "text": "Refer', inquis, ad senatum"}]
    refused = vega_cut_scan.scan(texts, url="http://localai:30286/", model="vega-bge-m3",
                                 window=1024, post=post)
    assert sent == [("Cold ash in the grate", 1024), ("Refer', inquis, ad senatum", 1024)]
    assert [(r["i"], r["status"]) for r in refused] == [(372, 500)]
    assert "cannot be cut" in refused[0]["message"]

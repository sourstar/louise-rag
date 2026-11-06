"""Speech for the programs that read Louise aloud (app/speech.py; operator,
2026-11-06): no citations read, a voice that speaks the text's language, the
model kept loaded while sentences come."""
import time

import pytest
from fastapi.testclient import TestClient

from app import api, speech
from tests.deposit.test_deposit_identity import node  # noqa: F401

PHILIPPE = "localai://voice-profiles/1c834741"
PROFILES = [{"name": "Valentina", "language": "it-IT", "voice": "localai://voice-profiles/db55",
             "created_at": "2026-10-16T22:20:25Z"},
            {"name": "Philippe", "language": "it-IT", "voice": PHILIPPE, "created_at": "2026-10-06T17:11:09Z"}]


@pytest.mark.parametrize("text, said", [
    ("Dante wrote it [3].", "Dante wrote it."),
    ("Dante [3] [4] [5] and Petrarch [7].", "Dante and Petrarch."),
    ("It is [3]-[5] there.", "It is there."),
    ("See [3,4] and [6-8].", "See and."),
    ("A record [gutenberg:1001], quoted.", "A record, quoted."),
    ("Chapter [IV] stays, and so does a list [a].", "Chapter [IV] stays, and so does a list [a]."),
])
def test_the_citations_are_not_read(text, said):
    assert speech.speakable(text) == said


def test_adjacent_citations_get_a_space_even_across_two_chunks():
    """Open WebUI reads "[3][4]" as a reference link and speaks "3"."""
    assert speech.spaced_citations("Dante [3][4][5].") == "Dante [3] [4] [5]."
    assert speech.spaced_citations("[4] then", tail="Dante [3]") == " [4] then"
    assert speech.spaced_citations("[link][ref] and a[1]") == "[link][ref] and a[1]"


@pytest.mark.parametrize("text, lang", [
    ("La Divina Commedia è stata scritta da Dante, ed è il poema della lingua italiana.", "it"),
    ("The Divine Comedy was written by Dante in the early fourteenth century.", "en"),
    ("Die Göttliche Komödie ist das Hauptwerk von Dante und wird in der Schule gelesen.", "de"),
    ("Dante.", None),
])
def test_the_language_is_told_by_its_commonest_words(text, lang):
    assert speech.language(text) == lang


def test_the_voice_follows_the_text_s_language():
    assert speech.voice_for(PHILIPPE, "it", PROFILES) == PHILIPPE, "the voice asked for, when it speaks it"
    assert speech.voice_for("localai://voice-profiles/db55", "it", PROFILES) == "localai://voice-profiles/db55"
    assert speech.voice_for(PHILIPPE, "en", PROFILES) is None, "no English profile: the model's own voice"
    assert speech.voice_for(None, "it", PROFILES) == PHILIPPE, "the first recorded Italian profile"
    assert speech.voice_for(PHILIPPE, None, PROFILES) == PHILIPPE, "a language not told: as asked"
    assert speech.voice_for("alloy", "en", PROFILES) == "alloy", "a model's named voice is kept"


@pytest.fixture
def server(monkeypatch):
    calls, pins = [], []

    class R:
        def __init__(self, status, content=b"", ctype="audio/wav", data=None):
            self.status_code, self.content, self.headers, self._data = status, content, {"content-type": ctype}, data

        def json(self):
            return self._data

    monkeypatch.setattr(speech.httpc, "get", lambda url, **kw: R(200, data={"data": PROFILES}))
    monkeypatch.setattr(speech.httpc, "post", lambda url, json, **kw: calls.append((url, json)) or R(200, b"RIFF"))
    monkeypatch.setattr(speech.model_pins, "hold", lambda url, model, holder: pins.append(("hold", model, holder)) or True)
    monkeypatch.setattr(speech.model_pins, "release", lambda holder: pins.append(("release", holder)))
    monkeypatch.setattr(speech, "_profiles", (0.0, []))
    monkeypatch.setattr(speech, "IDLE_S", 0.2)
    return calls, pins


def test_a_sentence_is_relayed_clean_in_its_language_s_voice_and_the_model_stays_pinned(server):
    calls, pins = server
    for text in ("Buonasera, la Divina Commedia è di Dante [3][4].", "E il poema della lingua italiana [5]."):
        got = speech.synthesize({"model": "omnivoice-cpp", "input": text, "voice": PHILIPPE}, url="http://ai:8080")
        assert got == (200, b"RIFF", "audio/wav")
    assert [c[0] for c in calls] == ["http://ai:8080/v1/audio/speech"] * 2
    assert calls[0][1] == {"model": "omnivoice-cpp", "input": "Buonasera, la Divina Commedia è di Dante.",
                           "voice": PHILIPPE, "language": "it"}
    english = speech.synthesize({"model": "omnivoice-cpp", "input": "The poem was written by Dante [1].",
                                 "voice": PHILIPPE}, url="http://ai:8080")
    assert english[0] == 200 and "voice" not in calls[-1][1] and calls[-1][1]["language"] == "en"
    assert pins == [("hold", "omnivoice-cpp", "speech")] * 3, "pinned from the first sentence, not released between"
    time.sleep(0.5)
    assert pins[-1] == ("release", "speech"), "released once the sentences stop"


def test_a_text_that_is_only_citations_is_refused(server):
    calls, _ = server
    assert speech.synthesize({"model": "m", "input": "[3][4]"}, url="http://ai")[0] == 400 and calls == []


def test_the_route_answers_audio(node, server):  # noqa: F811
    r = TestClient(api.app).post("/v1/audio/speech", json={"model": "omnivoice-cpp", "input": "Dante [1].",
                                                            "voice": PHILIPPE})
    assert r.status_code == 200 and r.content == b"RIFF" and r.headers["content-type"] == "audio/wav"

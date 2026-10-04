"""The HTTP surface (app/rage/front.py; docs/notes/rage.md §6, §11, §12).

What Louise sees of RAGE: a URL that names a model, `Louise-Run` on every
answer it may write from, a 503 that defers and never evicts, and the
operator's routes behind the admin token.
"""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.rage import front
from app.rage.clock import Clock
from app.rage.front import RUN_HEADER, create_app, read_admin_token

TOKEN = "s3cret-operator-token"
ADMIN = {"Authorization": f"Bearer {TOKEN}"}


def _app(rage, *, clock=None, token=TOKEN):
    swap = rage.swap()
    placement = rage.placement(swap)
    return create_app(placement, swap, clock or Clock(None), token, watch=False), placement


def test_every_answer_names_the_run_that_gave_it(rage):
    app, placement = _app(rage)
    with TestClient(app) as client:
        r = client.post("/upstream/bge-m3/v1/embeddings", json={"input": ["one text"]})
        assert r.status_code == 200 and r.json()["data"][0]["embedding"] == [0.5]
        run_id = r.headers[RUN_HEADER]
        assert run_id == placement.placed["bge-m3"].run and run_id.startswith("sha256:")

        described = client.get(f"/louise/runs/{run_id}").json()
        assert sorted(described) == ["driver", "engine", "weights"], "09 §9.12's object"
        assert described["engine"]["image"].startswith("sha256:")
        assert client.get("/louise/runs/sha256:" + "00" * 32).status_code == 404


def test_a_streamed_answer_names_its_run_too(rage):
    """The header goes out before the first token; the body is passed on as
    it comes."""
    app, _ = _app(rage)
    with TestClient(app) as client:
        with client.stream("POST", "/upstream/bge-m3/v1/chat/completions",
                           json={"stream": True}) as r:
            assert r.headers[RUN_HEADER].startswith("sha256:")
            assert b"".join(r.iter_raw()) == b"data: one\n\ndata: two\n\ndata: [DONE]\n\n"


def test_a_run_that_cannot_be_stated_is_served_without_the_header(rage):
    rage.launch("bge-m3@xpu")
    rage.running.pop("bge-m3@xpu")              # a second copy: two processes match
    app, _ = _app(rage)
    with TestClient(app) as client:
        r = client.post("/upstream/bge-m3/v1/embeddings", json={"input": ["x"]})
        assert r.status_code == 200 and RUN_HEADER not in r.headers
        row = client.get("/louise/models").json()[0]
        assert row["state"] == "unstated" and "2 processes" in row["reason"]


def test_a_child_replaced_during_the_request_gets_no_run(rage):
    """Checked again once the answer's headers are in: what answered may not
    be what was checked, and Louise writes nothing from an answer with no run."""
    app, _ = _app(rage)
    with TestClient(app) as client:
        assert RUN_HEADER in client.post("/upstream/bge-m3/v1/embeddings", json={}).headers
        rage.during_request = lambda: rage.kill("bge-m3@xpu")
        r = client.post("/upstream/bge-m3/v1/embeddings", json={})
        assert r.status_code == 200 and RUN_HEADER not in r.headers


def test_a_url_names_a_model_and_the_first_request_loads_it_through_admission(rage):
    app, placement = _app(rage)
    with TestClient(app) as client:
        assert client.get("/louise/models").json()[1]["state"] == "unloaded"
        assert client.post("/upstream/madlad/translate", json={}).status_code == 200
        assert placement.placed["madlad"].device == "xpu", "its default placement"

        r = client.post("/upstream/bge-m3/v1/embeddings", json={})
        assert r.status_code == 503 and r.headers["Retry-After"] == "30"
        body = r.json()
        assert body["admission"]["loaded"] == ["madlad"] and "1400 MiB" in body["error"]
        assert "madlad" in placement.placed, "refused, never evicted"
        assert client.post("/upstream/ghost/v1/embeddings", json={}).status_code == 404


def test_each_request_goes_on_as_it_came(rage):
    """Never merged, never reordered: the gate measures batch invariance at
    the peer's own batching (03 §3.3)."""
    app, _ = _app(rage)
    with TestClient(app) as client:
        for text in ("first", "second"):
            client.post("/upstream/bge-m3/v1/embeddings?x=1", json={"input": [text]},
                        headers={"Louise-Level": "corpus"})
    assert [(path, json.loads(body)["input"]) for path, body in rage.requests] == \
        [("v1/embeddings", ["first"]), ("v1/embeddings", ["second"])]


# ---- the operator's routes --------------------------------------------------------

@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"},
                                     {"Authorization": TOKEN}])
def test_the_operators_routes_want_the_token(rage, headers):
    app, _ = _app(rage)
    with TestClient(app) as client:
        for method, url in (("POST", "/louise/models/bge-m3/load"),
                            ("POST", "/louise/models/bge-m3/unload"),
                            ("POST", "/louise/models/bge-m3/restart"),
                            ("PUT", "/louise/models/extra"),
                            ("DELETE", "/louise/models/extra")):
            assert client.request(method, url, headers=headers, json={}).status_code == 401
        assert client.get("/louise/models").status_code == 200, "reading is open, as today"
    assert rage.launches == []


def test_with_no_token_configured_the_operators_routes_answer_401(rage):
    app, _ = _app(rage, token=None)
    with TestClient(app) as client:
        assert client.post("/louise/models/bge-m3/load", headers=ADMIN).status_code == 401


def test_the_token_is_read_from_a_file_and_never_shown(tmp_path):
    f = tmp_path / "admin.token"
    f.write_text(TOKEN + "\n")
    assert read_admin_token({"ADMIN_TOKEN_FILE": str(f)}) == TOKEN
    assert read_admin_token({}) is None
    with pytest.raises(RuntimeError) as e:
        read_admin_token({"ADMIN_TOKEN_FILE": str(tmp_path / "missing")})
    assert "missing" in str(e.value) and TOKEN not in str(e.value)


def test_load_unload_and_the_device_chosen_at_load(rage):
    app, placement = _app(rage)
    with TestClient(app) as client:
        row = client.post("/louise/models/bge-m3/load", headers=ADMIN,
                          json={"device": "cpu"}).json()
        assert row["state"] == "ready" and row["device"] == "cpu"
        assert row["memory"] == {"declared": {"xpu": 1400, "cpu": 2600},
                                 "host": {"xpu": 450},
                                 "observed": {"rss_mib": 500, "ram_mib": 300, "mapped_mib": 200,
                                              "vram_mib": 1400}}, \
            "what it holds, beside what rage.toml says it holds"
        assert client.post("/louise/models/bge-m3/load", headers=ADMIN,
                           json={"device": "xpu"}).status_code == 409

        assert client.post("/louise/models/bge-m3/unload", headers=ADMIN).status_code == 200
        assert placement.placed == {}
        assert client.post("/louise/models/bge-m3/unload", headers=ADMIN).status_code == 404
        assert client.post("/louise/models/bge-m3/load", headers=ADMIN).json()["device"] == "xpu"


EXTRA = {"engine": {"name": "llama.cpp", "build": "b10630", "variant": "sycl"},
         "weights": "/models/extra.gguf", "device": "cpu", "memory": {"cpu": 300},
         "cmd": {"cpu": ["/opt/llama/llama-server", "-m", "${WEIGHTS}", "--port", "${PORT}"]}}


def test_adding_a_model_writes_a_file_of_its_own_never_the_operators(rage):
    app, placement = _app(rage)
    before = rage.config.read_text()
    with TestClient(app) as client:
        assert client.put("/louise/models/extra", headers=ADMIN, json=EXTRA).status_code == 200
        assert (rage.config.parent / "rage.d" / "extra.toml").is_file()
        assert rage.config.read_text() == before
        assert [r["id"] for r in client.get("/louise/models").json()] == \
            ["bge-m3", "extra", "madlad"]
        assert '"extra@cpu"' in placement.yaml_path.read_text(), "llama-swap's yaml follows"

        bad = dict(EXTRA, cmd={"cpu": ["/opt/llama/llama-server", "-m", "${WEIGHTS}"]})
        r = client.put("/louise/models/extra2", headers=ADMIN, json=bad)
        assert r.status_code == 400 and "${PORT}" in r.json()["error"]

        r = client.put("/louise/models/bge-m3", headers=ADMIN, json=EXTRA)
        assert r.status_code == 409 and "rage.toml" in r.json()["error"]
        assert client.delete("/louise/models/bge-m3", headers=ADMIN).status_code == 409

        assert client.delete("/louise/models/extra", headers=ADMIN).status_code == 200
        assert "extra" not in placement.cfg.models
        assert client.delete("/louise/models/extra", headers=ADMIN).status_code == 404


# ---- the clock ----------------------------------------------------------------------

def _card(tmp_path):
    d = tmp_path / "freq0"
    d.mkdir()
    (d / "min_freq").write_text("400\n")
    (d / "rp0_freq").write_text("2800\n")
    (d / "rpn_freq").write_text("400\n")
    return d


def test_the_clock_is_held_while_a_person_waits_and_only_then(rage, tmp_path):
    d = _card(tmp_path)
    seen = []
    rage.during_request = lambda: seen.append(int((d / "min_freq").read_text()))
    app, _ = _app(rage, clock=Clock(d))
    with TestClient(app) as client:
        client.post("/upstream/bge-m3/v1/embeddings", json={}, headers={"Louise-Level": "corpus"})
        client.post("/upstream/bge-m3/v1/embeddings", json={}, headers={"Louise-Level": "query"})
        with client.stream("POST", "/upstream/bge-m3/v1/chat/completions", json={},
                           headers={"Louise-Level": "chat"}) as r:
            b"".join(r.iter_raw())
        assert seen == [400, 1400, 1400]
        assert int((d / "min_freq").read_text()) == 400, "given back after the last token"
        assert client.get("/louise/gpu").json()["clock"]["running"] is True


def test_stopping_the_front_gives_the_clock_back(rage, tmp_path):
    d = _card(tmp_path)
    clock = Clock(d)
    app, _ = _app(rage, clock=clock)
    with TestClient(app):
        clock.enter()                       # someone is mid-answer when SIGTERM lands
        assert int((d / "min_freq").read_text()) == 1400
    assert int((d / "min_freq").read_text()) == 400


# ---- ten first requests, over HTTP -----------------------------------------------------

def test_ten_first_requests_over_http_are_one_load(rage):
    rage.load_delay = 0.05
    app, _ = _app(rage)

    async def ten():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://rage") as client:
            return await asyncio.gather(*(client.post("/upstream/bge-m3/v1/embeddings", json={})
                                          for _ in range(10)))

    answers = asyncio.run(ten())
    assert [a.status_code for a in answers] == [200] * 10
    assert len({a.headers[RUN_HEADER] for a in answers}) == 1
    assert rage.launches == ["bge-m3@xpu"]


def test_the_front_watches_its_files():
    assert front.WATCH_S <= 10, "an edit to rage.toml applies within seconds, not at restart"


# ---- a caller that hangs up while its model loads ------------------------------------------
#
# The node, 2026-09-21: four `starlette.requests.ClientDisconnect` tracebacks
# in RAGE's log, each right after a model finished loading — three during a
# restart's preload, one when the translator came back on demand. The front
# awaited the load and only THEN read the request body, and uvicorn answers
# every read after a caller has hung up with "disconnected", even when the
# body had already arrived. `receive` below does exactly that.

_EMBED = "/upstream/bge-m3/v1/embeddings"


def _scope(path: str = _EMBED) -> dict:
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": path,
            "raw_path": path.encode(), "root_path": "", "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "server": ("rage", 8080), "client": ("127.0.0.1", 40000)}


def _caller(body: bytes, gone: dict):
    """A caller as uvicorn presents it: its body while it is there, and
    "disconnected" for every read once it has left."""
    async def receive() -> dict:
        if gone["now"]:
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": body, "more_body": False}
    return receive


def test_a_caller_that_hangs_up_while_its_model_loads_is_not_an_error(rage):
    swap = rage.swap()
    p = rage.placement(swap)
    app = create_app(p, swap, Clock(None), None, watch=False)
    gone = {"now": False}
    launch = rage.launch

    def gives_up_during_the_load(variant: str) -> int:
        gone["now"] = True                  # the caller's timeout ran out mid-load
        return launch(variant)

    rage.launch = gives_up_during_the_load
    body = b'{"input": "chi paga l\'IMU?"}'
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app(_scope(), _caller(body, gone), send))   # raised ClientDisconnect before

    assert rage.launches == ["bge-m3@xpu"], "the model was still loaded, for the next caller"
    assert rage.requests[-1] == ("v1/embeddings", body), "the body read before the wait"
    assert p.placed["bge-m3"].in_flight == 0, "and nothing is left in flight"


def test_a_caller_gone_before_it_finished_asking_loads_nothing(rage):
    swap = rage.swap()
    p = rage.placement(swap)
    app = create_app(p, swap, Clock(None), None, watch=False)
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app(_scope(), _caller(b"", {"now": True}), send))

    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 499
    assert rage.launches == [] and "bge-m3" not in p.placed, "no load for nobody"

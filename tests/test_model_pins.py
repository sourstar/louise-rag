"""The scoped pin with restore (app/model_pins.py; operator, 2026-10-23):
read the model's pinned state, pin it if it was not, restore that state when
the last holder leaves — and every failed call an error, counted and shown."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app import model_pins

SERVER = "http://runner:8080"
URL = f"{SERVER}/v1/embeddings"
M = "vega-qwen3-embedding-4b"


class Runner:
    """LocalAI's two routes: config-json (read) and toggle-pinned (write)."""

    def __init__(self, pinned: dict[str, bool] | None = None):
        self.pinned = dict(pinned or {})
        self.calls: list[tuple[str, str]] = []
        self.read_status = 200
        self.put_status = 200
        self.fail_unpin = False
        self.down = False

    def get(self, url, **kw):
        assert kw.get("timeout") == model_pins.CALL_TIMEOUT_S
        self.calls.append(("GET", url))
        if self.down:
            raise ConnectionError("refused")
        model = url.rsplit("/", 1)[1]
        return SimpleNamespace(status_code=self.read_status,
                               json=lambda: {"name": model, "pinned": self.pinned.get(model, False)})

    def put(self, url, **kw):
        assert kw.get("timeout") == model_pins.CALL_TIMEOUT_S
        self.calls.append(("PUT", url))
        if self.down:
            raise ConnectionError("refused")
        *_, model, action = url.split("/")
        if action == "unpin" and self.fail_unpin:
            return SimpleNamespace(status_code=500)
        if self.put_status == 200:
            self.pinned[model] = action == "pin"
        return SimpleNamespace(status_code=self.put_status)

    def puts(self):
        return [u.rsplit("/", 1)[1] for m, u in self.calls if m == "PUT"]


@pytest.fixture
def runner(monkeypatch, tmp_path):
    from app import httpc
    r = Runner()
    monkeypatch.setattr(httpc, "get", r.get)
    monkeypatch.setattr(httpc, "put", r.put)
    monkeypatch.setattr(model_pins, "_path", lambda: tmp_path / "state" / "model-pins.json")
    monkeypatch.setattr(model_pins, "_applies", lambda m: m.startswith(model_pins.EMBEDDER_PREFIX))
    said: list[tuple[str, str]] = []
    monkeypatch.setattr(model_pins, "_say", lambda level, text: said.append((level, text)))
    r.said = said
    return r


def errors(r):
    return [t for lvl, t in r.said if lvl == "error"]


def test_an_unpinned_model_is_pinned_while_held_and_unpinned_after(runner):
    assert model_pins.hold(URL, M, "worker embed gutenberg", collection="gutenberg")
    assert runner.pinned[M] is True
    model_pins.release("worker embed gutenberg")
    assert runner.pinned[M] is False
    assert runner.puts() == ["pin", "unpin"]
    assert model_pins.status()["pins"] == [] and not errors(runner)


def test_a_model_pinned_before_is_never_touched(runner):
    runner.pinned[M] = True
    assert model_pins.hold(URL, M, "h")
    model_pins.release("h")
    assert runner.pinned[M] is True and runner.puts() == []


def test_two_holders_share_and_the_last_restores(runner):
    model_pins.hold(URL, M, "worker embed gutenberg")
    model_pins.hold(URL, M, "embed pid 77")            # a command beside the worker
    model_pins.release("worker embed gutenberg")
    assert runner.pinned[M] is True                   # the command still processes
    model_pins.release("embed pid 77")
    assert runner.pinned[M] is False
    assert runner.puts() == ["pin", "unpin"]          # one read and one pin, shared


def test_a_failed_read_is_an_error_and_processing_goes_on_unpinned(runner):
    runner.read_status = 500
    assert model_pins.hold(URL, M, "worker embed pubmed", collection="pubmed") is False
    [line] = errors(runner)
    assert "read of " + M in line and "HTTP 500" in line and "worker embed pubmed" in line
    st = model_pins.status()
    assert st["failures"]["count"] == 1
    [f] = st["failures"]["recent"]
    assert (f["call"], f["model"], f["holder"], f["collection"]) == \
        ("read", M, "worker embed pubmed", "pubmed")
    assert model_pins.failures_for("pubmed") == [f] and model_pins.failures_for("gutenberg") == []
    # asked again no sooner than RETRY_S: one error a minute, not one per call
    assert model_pins.hold(URL, M, "worker embed pubmed") is False
    assert len(errors(runner)) == 1


def test_a_failed_pin_is_an_error(runner):
    runner.put_status = 404
    assert model_pins.hold(URL, M, "h") is False
    [line] = errors(runner)
    assert "pin of" in line and "HTTP 404" in line
    assert model_pins.counters() == {"pinned": 0, "stuck": 0, "failures": 1}


def test_an_unreachable_runner_never_hangs_or_raises(runner):
    runner.down = True
    assert model_pins.hold(URL, M, "h") is False
    assert "ConnectionError" in errors(runner)[0]


def test_a_failed_restore_stays_shown_and_is_retried_until_it_succeeds(runner):
    model_pins.hold(URL, M, "h")
    runner.fail_unpin = True
    model_pins.release("h")
    assert runner.pinned[M] is True
    st = model_pins.status()
    assert [p["model"] for p in st["stuck"]] == [M] and st["pins"] == []
    assert "restore of" in errors(runner)[0]
    model_pins.reconcile()                            # still failing: counted, not repeated
    assert len(errors(runner)) == 1 and model_pins.counters()["failures"] == 2
    runner.fail_unpin = False
    model_pins.reconcile()
    assert runner.pinned[M] is False and model_pins.status()["stuck"] == []


def test_a_silent_holder_is_dropped_and_its_model_restored(runner):
    model_pins.hold(URL, M, "embed pid 99", now=1000.0)
    model_pins.reconcile(now=1000.0 + model_pins.HOLDER_TTL_S - 1)
    assert runner.pinned[M] is True
    model_pins.reconcile(now=1000.0 + model_pins.HOLDER_TTL_S + 1)
    assert runner.pinned[M] is False


def test_a_live_holder_is_kept_however_old(runner):
    model_pins.hold(URL, M, "worker embed gutenberg", now=1000.0)
    model_pins.reconcile(now=1e9, live={"worker embed gutenberg"})
    assert runner.pinned[M] is True


def test_boot_drops_a_killed_workers_holders_and_says_so(runner):
    other = "vega-dar-legal-it"
    model_pins.hold(URL, M, "worker embed gutenberg")
    model_pins.hold(URL, other, "embed pid 5")         # a command: not the worker's
    runner.said.clear()
    dropped = model_pins.reconcile(boot=True)
    assert dropped == [(M, "worker embed gutenberg")]
    assert runner.pinned[M] is False and runner.pinned[other] is True
    [(level, line)] = runner.said[-1:]
    assert level == "warn" and "at boot" in line and M in line


def test_boot_with_nothing_left_says_so(runner):
    model_pins.reconcile(boot=True)
    assert runner.said[-1][0] == "info" and "nothing left pinned" in runner.said[-1][1]


def test_this_process_holders_go_once_idle(runner):
    model_pins.hold_for_process(URL, M, "embed")
    holder = model_pins.process_holder("embed")
    assert holder.endswith(f" pid {os.getpid()}")
    import time
    model_pins.reconcile(now=time.time() + 10, own_idle_s=120)
    assert runner.pinned[M] is True
    model_pins.reconcile(now=time.time() + 121, own_idle_s=120)
    assert runner.pinned[M] is False


def test_an_encoder_outside_localai_is_not_pinned_nor_reported(runner):
    assert model_pins.hold("http://llama:8080/v1/embeddings", "bge-m3", "h") is False
    assert runner.calls == [] and runner.said == []


def test_a_holder_renews_without_a_write_each_call(runner, tmp_path):
    model_pins.hold(URL, M, "h", now=1000.0)
    p = tmp_path / "state" / "model-pins.json"
    before = p.read_bytes()
    model_pins.hold(URL, M, "h", now=1000.0 + model_pins.RENEW_S - 1)
    assert p.read_bytes() == before
    model_pins.hold(URL, M, "h", now=1000.0 + model_pins.RENEW_S + 1)
    assert p.read_bytes() != before


def test_holding_releases_on_an_exception(runner):
    with pytest.raises(RuntimeError):
        with model_pins.holding(URL, M, "gate gutenberg document (pid 1)"):
            assert runner.pinned[M] is True
            raise RuntimeError("the gate failed")
    assert runner.pinned[M] is False


def test_a_command_lease_pins_per_batch_and_releases_at_its_end(runner, monkeypatch):
    from app import encoder_use, node_state
    monkeypatch.setattr(node_state, "lease_encoder", lambda *a, **k: None)
    monkeypatch.setattr(node_state, "release_encoder", lambda *a, **k: None)
    recipe = SimpleNamespace(url=URL, model=M)
    leased = encoder_use.leasing(lambda chunks: [[0.0]] * len(chunks), recipe,
                                 holder="embed pid 3", collection="gutenberg")
    leased(["a"])
    assert runner.pinned[M] is True
    encoder_use.release("embed pid 3")
    assert runner.pinned[M] is False


def test_the_embed_client_pins_processing_and_not_a_query(runner, monkeypatch):
    from app import embeddings
    recipe = SimpleNamespace(url=URL, model=M, api="openai", query_url=None, query_model=None)
    monkeypatch.setattr(embeddings, "_embed_texts", lambda items, **kw: [[0.0]] * len(items))
    embeddings.embed_query(None, "a question", recipe=recipe)
    assert runner.calls == []
    embeddings._pin(recipe, collection="gutenberg")
    assert runner.pinned[M] is True


def test_the_worker_releases_an_embed_stage_at_its_idle_boundary(runner, monkeypatch):
    from app import worker
    worker._pins.clear()
    c = SimpleNamespace(name="gutenberg")
    recipe = SimpleNamespace(url=URL, model=M)
    pin = worker._pin_begin(c, recipe)
    assert runner.pinned[M] is True
    import time
    # still in use: never released, however long the document takes
    assert worker._release_idle_pins(now=time.time() + 10_000) == []
    worker._pin_end(pin)
    assert worker._release_idle_pins(now=time.time() + 1) == []
    assert worker._release_idle_pins(now=time.time() + worker.PIN_IDLE_S + 1) == [pin]
    assert runner.pinned[M] is False and worker._pins == {}

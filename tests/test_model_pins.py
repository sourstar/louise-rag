"""The scoped pin with restore (app/model_pins.py; operator, 2026-10-23):
read the model's pinned state, pin it if it was not, restore that state when
the last holder leaves — and every failed call an error, counted and shown."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app import model_pins

#: the real resolution, taken before conftest's autouse stub replaces it
REAL_APPLIES = model_pins._applies
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


def _system(server):
    from app import model_load
    if "llama" in server:
        raise model_load._NoRoute()      # answers, and not as LocalAI does
    return frozenset()


@pytest.fixture
def runner(monkeypatch, tmp_path):
    from app import httpc
    r = Runner()
    monkeypatch.setattr(httpc, "get", r.get)
    monkeypatch.setattr(httpc, "put", r.put)
    monkeypatch.setattr(model_pins, "_path", lambda: tmp_path / "state" / "model-pins.json")
    # the real resolution, against a runner that answers /system as LocalAI
    # does (and `llama:8080`, as llama-server does)
    from app import model_load
    monkeypatch.setattr(model_pins, "_applies", REAL_APPLIES)
    monkeypatch.setattr(model_load, "_fetch_system", _system)
    model_load.reset()
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


def test_any_localai_model_is_pinned_a_reranker_or_the_chat_model_too(runner):
    """By USE, not by kind (München, 2026-10-23): LocalAI's pin API serves
    every model, and a map-reduce over the chat model breaks the same way an
    embed batch does if the model is evicted between its calls."""
    for m in ("louise-rerank-medcpt", "qwen3-8b"):
        with model_pins.holding(f"{SERVER}/v1/chat/completions", m, f"job {m}"):
            assert runner.pinned[m] is True
        assert runner.pinned[m] is False


def test_a_runner_that_cannot_be_asked_is_tried_and_its_failure_reported(runner, monkeypatch):
    from app import model_load
    monkeypatch.setattr(model_load, "_fetch_system", lambda server: None)   # unreachable now
    model_load.reset()
    runner.down = True
    assert model_pins.hold(URL, M, "h") is False
    assert "read of" in errors(runner)[0]


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
    embeddings.embed_calibration(recipe, ["a calibration text"])
    assert runner.pinned[M] is True
    assert model_pins.in_use() == set(), "the call ended: nothing in flight"


def test_a_call_in_flight_is_never_dropped_however_long_it_runs(runner):
    """München, 2026-10-23: the holder's stamp was its call's START, so a
    call longer than the worker's 120 s idle bound could be dropped, and its
    model unpinned, mid-call — what the operator forbade."""
    import time
    with model_pins.using(URL, M, "embed"):
        model_pins.reconcile(now=time.time() + 10_000, own_idle_s=120)
        assert runner.pinned[M] is True, "in flight: alive whatever its stamp's age"
    # ended: the idle clock starts at the END of the call
    model_pins.reconcile(now=time.time() + 60, own_idle_s=120)
    assert runner.pinned[M] is True
    model_pins.reconcile(now=time.time() + 121, own_idle_s=120)
    assert runner.pinned[M] is False


def test_an_idle_release_is_said_as_one_not_as_a_dead_holder(runner):
    """The live node logged the worker's own 120 s releases as "silent for
    over 900 s dropped", one or two minutes after they were taken."""
    import time
    model_pins.hold_for_process(URL, M, "embed")
    model_pins.hold(URL, "vega-dar-legal-it", "embed pid 999999", now=time.time() - 1000)
    runner.said.clear()
    model_pins.reconcile(now=time.time() + 121, own_idle_s=120)
    lines = {lvl: t for lvl, t in runner.said}
    assert "released, idle for over 120 s" in lines["info"] and M in lines["info"]
    assert "silent for over 900 s" in lines["warn"] and "vega-dar-legal-it" in lines["warn"]
    assert M not in lines["warn"]


def test_the_end_of_a_call_does_not_pin_again_what_was_released(runner):
    with model_pins.using(URL, M, "embed"):
        model_pins.release(model_pins.process_holder("embed"))
        assert runner.pinned[M] is False
    assert runner.pinned[M] is False and runner.puts() == ["pin", "unpin"]


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


def test_the_log_writer_ends_once_its_lines_are_written(monkeypatch):
    """A worker's stop leaves no thread behind (tests/test_worker_boot_node_state.py)."""
    import time
    written: list[str] = []
    monkeypatch.setattr(model_pins, "_publish", lambda level, text: written.append(text))
    model_pins._queued("info", "one")
    model_pins._queued("error", "two")
    deadline = time.time() + 5
    while model_pins._writer is not None and time.time() < deadline:
        time.sleep(0.05)
    assert written == ["one", "two"] and model_pins._writer is None


def test_a_long_holding_block_beats_so_no_reconcile_takes_it_for_dead(runner, monkeypatch):
    """A book's summary is an hour of chapters under one holder: stamped
    only at its start, another process's reconcile would drop it at 900 s."""
    import time
    monkeypatch.setattr(model_pins, "HEARTBEAT_S", 0.05)
    with model_pins.holding(URL, M, "summary gutenberg-11 (pid 1)"):
        t0 = time.time()
        time.sleep(0.3)
        [entry] = model_pins.status()["pins"]
        assert entry["holders"]["summary gutenberg-11 (pid 1)"] > t0
    assert runner.pinned[M] is False


def test_a_book_summary_holds_the_chat_model_across_its_chapters(runner, monkeypatch):
    from app import config, summarize
    monkeypatch.setattr(config, "CHAT_URL", f"{SERVER}/v1")
    monkeypatch.setattr(config, "CHAT_MODEL", "qwen3-8b")
    seen = []

    class Plugin:
        def split_text(self, text):
            return [("I", "a"), ("II", "b"), ("III", "c")]

    class Con:
        def execute(self, sql, args=()):
            row = {"uid": "g-1", "title": "T", "authors": "A"}
            return SimpleNamespace(fetchone=lambda: row)
    monkeypatch.setattr(summarize, "get_cached", lambda con, book_id: None if not seen else {"ok": 1})
    monkeypatch.setattr(summarize.collections_mod, "default_collection",
                        lambda: SimpleNamespace(plugin="gutenberg", plugin_instance=lambda: Plugin()))
    monkeypatch.setattr(summarize.plugin_io, "full_text", lambda plugin, row: "x" * 50_000)
    monkeypatch.setattr(summarize.db, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(summarize.llm, "chat",
                        lambda messages, **kw: seen.append(runner.pinned.get("qwen3-8b")) or "s")
    summarize.summarize_book(Con(), 1)
    assert seen == [True, True, True, True], "pinned at every chapter and at the reduce"
    assert runner.pinned["qwen3-8b"] is False, "and its state before restored after"

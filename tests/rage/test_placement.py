"""Which model is where, and which run it is (app/rage/placement.py;
docs/notes/rage.md §4, §7, §8).
"""
import asyncio

import pytest

from app.rage import model
from app.rage.placement import OOM_CHILD, READY, UNSTATED, Refused


def run(coro):
    return asyncio.run(coro)


def test_a_load_states_the_run_of_the_process_it_checked(rage):
    p = rage.placement()
    placed = run(p.ensure("bge-m3"))
    assert placed.state == READY and placed.device == "xpu"
    assert placed.pid == rage.running["bge-m3@xpu"]
    assert placed.observed == {"rss_mib": 500, "vram_mib": 1400}, "beside the declaration"
    described = p.store.get(placed.run)
    assert sorted(described) == ["driver", "engine", "weights"]
    assert described["engine"]["args"][2] == "${WEIGHTS}", "the template, not the process"
    assert described["engine"]["device"] == "Intel(R) Arc(TM) Pro B70 Graphics"
    assert described["driver"].startswith("kernel 6.18.0-production; xe ")
    assert (rage.proc / str(placed.pid) / "oom_score_adj").read_text() == str(OOM_CHILD), \
        "an out-of-memory kill takes the model that grew, never the front"


def test_the_same_engine_relaunched_is_the_same_run(rage):
    """Another port, another container host name, another pid: the run the
    first design would have changed at every restart."""
    p = rage.placement()
    first = run(p.ensure("bge-m3")).run
    run(p.unload("bge-m3"))
    rage.hostname = "container-2"
    assert run(p.ensure("bge-m3")).run == first
    assert rage.launches == ["bge-m3@xpu", "bge-m3@xpu"]


def test_other_weights_another_device_or_another_image_is_another_run(rage):
    p = rage.placement()
    first = run(p.ensure("bge-m3")).run
    run(p.unload("bge-m3"))

    assert run(p.ensure("bge-m3", "cpu")).run != first, "the device is in the run"
    run(p.unload("bge-m3"))

    (rage.models / "bge-m3-FP16.gguf").write_bytes(b"GGUF bge-m3 weights, one byte more")
    assert run(p.ensure("bge-m3")).run != first, "hashed at every launch"

    other = rage.placement(image="sha256:" + "cd" * 32)
    rage.kill("bge-m3@xpu")
    assert run(other.ensure("bge-m3")).run not in (first, p.placed["bge-m3"].run)


def test_ten_first_requests_are_one_load(rage):
    rage.load_delay = 0.05
    p = rage.placement()

    async def ten():
        return await asyncio.gather(*(p.ensure("bge-m3") for _ in range(10)))

    placed = run(ten())
    assert rage.launches == ["bge-m3@xpu"]
    assert len({id(x) for x in placed}) == 1 and placed[0].state == READY


def test_a_place_is_held_from_admission_so_two_loads_cannot_both_squeeze_in(rage):
    """madlad (8200) and bge-m3 (1400) do not both fit 10000 less 1000. The
    one still LOADING already counts in full."""
    rage.load_delay = 0.05
    p = rage.placement()

    async def both():
        return await asyncio.gather(p.ensure("madlad"), p.ensure("bge-m3"),
                                    return_exceptions=True)

    first, second = run(both())
    assert first.state == READY
    assert isinstance(second, Refused) and second.status == 503
    assert second.body["admission"]["loaded"] == ["madlad"]
    assert rage.launches == ["madlad@xpu"], "refused before anything was launched"

    run(p.unload("madlad"))
    assert run(p.ensure("bge-m3")).state == READY, "an unload gives the place back"


def test_a_refusal_is_a_deferral_with_a_reason_never_an_eviction(rage):
    p = rage.placement()
    run(p.ensure("madlad"))
    with pytest.raises(Refused) as e:
        run(p.ensure("bge-m3"))
    assert e.value.status == 503 and "needs 1400 MiB" in e.value.body["error"]
    assert "madlad" in p.placed and rage.unloads == [], "what to give up is Louise's decision"
    assert run(p.ensure("bge-m3", "cpu")).device == "cpu", "the other device has room"


def test_a_model_is_on_one_device_and_a_url_never_says_which(rage):
    p = rage.placement()
    run(p.ensure("bge-m3", "cpu"))
    assert run(p.ensure("bge-m3")).device == "cpu", "a plain request goes where it is"
    with pytest.raises(Refused) as e:
        run(p.ensure("bge-m3", "xpu"))
    assert e.value.status == 409 and e.value.body["device"] == "cpu"
    with pytest.raises(Refused) as e:
        run(p.ensure("bge-m3", "cuda"))
    assert e.value.status == 400
    with pytest.raises(Refused) as e:
        run(p.ensure("ghost"))
    assert e.value.status == 404


def test_absent_weights_are_refused_by_path_and_nothing_is_fetched(rage):
    (rage.models / "bge-m3-FP16.gguf").unlink()
    p = rage.placement()
    assert [r["state"] for r in p.describe()] == ["absent", "unloaded"]
    with pytest.raises(Refused) as e:
        run(p.ensure("bge-m3"))
    assert e.value.status == 503 and "bge-m3-FP16.gguf" in e.value.body["error"]
    assert rage.launches == []


def test_a_child_that_fails_to_start_holds_no_place(rage):
    rage.fail.add("madlad@xpu")
    p = rage.placement()
    with pytest.raises(Refused) as e:
        run(p.ensure("madlad"))
    assert e.value.status == 502 and "madlad" not in p.placed
    assert run(p.ensure("bge-m3")).state == READY


def test_a_load_that_breaks_half_way_holds_no_place(rage, monkeypatch):
    """Not a refusal — a bug, or a request cancelled mid-load. Left as
    "loading" it would hold 8 GB of the card's budget for a model that is
    not there, until the container restarted."""
    from app.rage import runs

    def broken(*_a, **_k):
        raise RuntimeError("unexpected")

    p = rage.placement()
    monkeypatch.setattr(runs, "match", broken)
    with pytest.raises(RuntimeError):
        run(p.ensure("madlad"))
    assert p.placed == {}
    monkeypatch.undo()
    rage.kill("madlad@xpu")
    assert run(p.ensure("bge-m3")).state == READY, "its place was given back"


def test_a_process_that_does_not_match_serves_but_states_no_run(rage):
    """03 §3.3: an endpoint that states no run may be searched with, and
    nothing is written through it."""
    p = rage.placement()
    rage.launch("bge-m3@xpu")
    rage.running.pop("bge-m3@xpu")          # a stray second copy of the same engine
    placed = run(p.ensure("bge-m3"))
    assert placed.state == UNSTATED and placed.run is None
    assert "2 processes" in placed.reason
    assert any("no run stated" in line for line in p.logs)


def test_a_child_restarted_behind_the_front_is_another_launch(rage):
    """llama-swap restarts a child that died on its next request. The weights
    are hashed and the process matched again before any run is named."""
    p = rage.placement()
    first = run(p.ensure("bge-m3"))
    old_run, old_pid = first.run, first.pid
    assert p.still_the_run(first)

    rage.kill("bge-m3@xpu")
    assert not p.still_the_run(first), "an answer from here on carries no run"
    (rage.models / "bge-m3-FP16.gguf").write_bytes(b"replaced while it was down")
    again = run(p.ensure("bge-m3"))
    assert again.pid != old_pid and again.run != old_run and again.state == READY
    assert rage.launches == ["bge-m3@xpu", "bge-m3@xpu"]


def test_an_edit_that_does_not_parse_changes_nothing(rage):
    p = rage.placement()
    run(p.ensure("bge-m3"))
    rage.config.write_text("[models.broken\n", encoding="utf-8")
    assert p.reload() is False
    assert sorted(p.cfg.models) == ["bge-m3", "madlad"] and "bge-m3" in p.placed
    assert any("previous configuration stands" in line for line in p.logs)


def test_applying_the_files_preloads_and_unloads_what_they_say(rage):
    text = rage.config.read_text(encoding="utf-8")
    rage.config.write_text(text.replace("[rage]\n", '[rage]\npreload = ["bge-m3"]\n'),
                           encoding="utf-8")
    p = rage.placement()
    run(p.apply())
    assert list(p.placed) == ["bge-m3"], "through admission, like any load"

    cut = rage.config.read_text(encoding="utf-8").replace('preload = ["bge-m3"]\n', "")
    rage.config.write_text(cut[:cut.index("[models.bge-m3]")]
                           + cut[cut.index("[models.madlad]"):], encoding="utf-8")
    assert p.reload()
    run(p.apply())
    assert p.placed == {} and rage.unloads == ["bge-m3@xpu"], "no longer configured"
    assert "bge-m3" not in p.yaml_path.read_text()


def test_restart_is_one_child_and_nothing_else(rage):
    p = rage.placement()
    run(p.ensure("bge-m3", "cpu"))
    run(p.ensure("madlad"))
    madlad_pid = p.placed["madlad"].pid
    again = run(p.restart("bge-m3"))
    assert again.device == "cpu", "on the device it was on"
    assert p.placed["madlad"].pid == madlad_pid and rage.unloads == ["bge-m3@cpu"]


def test_the_front_carries_none_of_the_manifest_layer():
    """RAGE's image holds app/rage and four light modules, no more of the
    platform (app/rage/__init__.py). An import that drags in the manifest
    layer would make the image need Louise's state to hold a clock."""
    import re
    from pathlib import Path

    allowed = {"hold.codex", "gpu_clock"}
    pattern = re.compile(r"^\s*from \.\.([\w.]*) import ([\w, ]+)", re.M)
    for source in Path(model.__file__).parent.glob("*.py"):
        for package, names in pattern.findall(source.read_text(encoding="utf-8")):
            used = {package} if package else {n.strip() for n in names.split(",")}
            assert used <= allowed, f"{source.name} imports {used - allowed} from the platform"

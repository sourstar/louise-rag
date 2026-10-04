"""Admission (app/rage/admission.py) and the clock (app/rage/clock.py).

llama-swap loads on the first request and knows nothing of memory; without
admission a query for a dormant collection launches 8 GB unchecked. The
out-of-memory killer took the CPU encoder four times on 2026-09-15.
"""
import dataclasses

import pytest

from app.rage import admission, model
from app.rage.clock import Clock, is_interactive


def _unknown(_device):
    return None


def test_what_fits_is_declared_memory_plus_the_reserve(rage):
    cfg = model.load(rage.config)                     # xpu: 10000 capacity, 1000 reserve
    assert admission.fits(cfg, {}, "madlad", "xpu", _unknown).ok

    v = admission.fits(cfg, {"madlad": "xpu"}, "bge-m3", "xpu", _unknown)
    assert not v.ok, "8200 held + 1400 + 1000 reserved > 10000"
    assert v.device == "xpu" and v.loaded == ("madlad",) and v.free_mib == 800
    assert "bge-m3 needs 1400 MiB" in v.reason and "madlad" in v.reason

    assert admission.fits(cfg, {"madlad": "xpu"}, "bge-m3", "cpu", _unknown).ok, \
        "the card's budget is not the host's"


def test_a_model_on_the_gpu_is_not_free_on_the_host(rage):
    """Measured on the node, 2026-09-19: the eight GPU servers RAGE replaces
    held 6.1 GiB of host RAM at rest and 22.0 GiB at their load-time peaks.
    The first version counted host RAM for CPU placements only, and would
    have let CPU models fill what the GPU models were already holding."""
    cfg = dataclasses.replace(model.load(rage.config),
                              capacity_mib={"xpu": 10000, "cpu": 7000})   # reserve 2000
    v = admission.fits(cfg, {"madlad": "xpu"}, "bge-m3", "cpu", _unknown)
    assert not v.ok, "3400 held on the host by madlad@xpu + 2600 + 2000 reserved > 7000"
    assert v.device == "cpu" and v.loaded == ("madlad",) and "host RAM" in v.reason

    tight = dataclasses.replace(cfg, capacity_mib={"xpu": 10000, "cpu": 5000})
    v = admission.fits(tight, {}, "madlad", "xpu", _unknown)
    assert not v.ok and v.device == "cpu" and v.need_mib == 3400, \
        "the card has room; the host has not: a GPU load is refused by the host's budget"

    roomy = admission.fits(cfg, {"bge-m3": "xpu"}, "madlad", "xpu", _unknown)
    assert not roomy.ok and roomy.device == "xpu", "the card is asked first"


def test_a_model_is_never_counted_against_itself(rage):
    cfg = model.load(rage.config)
    assert admission.fits(cfg, {"madlad": "xpu"}, "madlad", "xpu", _unknown).ok


def test_a_card_is_declared_never_probed(rage):
    """Its name enters every run identity and its size is what admission
    places against; reading either would mean importing the GPU probe, which
    sees the host's processes and is imported by nothing."""
    text = rage.config.read_text(encoding="utf-8")
    for line, key in (("capacity_mib = { xpu = 10000, cpu = 20000 }\n", "capacity_mib.xpu"),
                      ('device_names = { xpu = "Intel(R) Arc(TM) Pro B70 Graphics" }\n',
                       "device_names.xpu")):
        assert line in text
        rage.config.write_text(text.replace(line, ""), encoding="utf-8")
        with pytest.raises(model.RageConfigError, match=key):
            model.load(rage.config)


def test_an_unknown_capacity_refuses_and_names_the_key_to_set(rage):
    cfg = dataclasses.replace(model.load(rage.config), capacity_mib={})
    v = admission.fits(cfg, {}, "bge-m3", "cpu", _unknown)
    assert not v.ok and "capacity_mib.cpu" in v.reason
    assert admission.fits(cfg, {}, "bge-m3", "cpu", lambda d: 32000).ok, \
        "the container's limit, where it can be read"
    assert admission.system_capacity("xpu") is None, "a card is never read"


def test_the_containers_limit_is_the_cpus_capacity(tmp_path):
    (tmp_path / "memory.max").write_text("34359738368\n")
    assert admission.cgroup_capacity_mib(tmp_path) == 32768
    (tmp_path / "memory.max").write_text("max\n")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       65536000 kB\n")
    assert admission.cgroup_capacity_mib(tmp_path, meminfo) == 64000


# ---- the clock -----------------------------------------------------------------

def _card(tmp_path, *, min_mhz=400):
    d = tmp_path / "freq0"
    d.mkdir()
    (d / "min_freq").write_text(f"{min_mhz}\n")
    (d / "rp0_freq").write_text("2800\n")
    (d / "rpn_freq").write_text("400\n")
    return d


def _min(d):
    return int((d / "min_freq").read_text())


def test_only_a_request_with_a_person_behind_it_holds_the_clock():
    assert [is_interactive(l) for l in ("corpus", "translate", "chat", "rerank", "query")] \
        == [False, True, True, True, True]
    assert not is_interactive(None) and not is_interactive("urgent"), \
        "a request without a level is background work"
    assert is_interactive(" Chat ")


def test_the_clock_is_held_from_the_first_waiter_to_the_last(tmp_path):
    d = _card(tmp_path)
    clock = Clock(d)
    clock.enter()
    clock.enter()
    assert _min(d) == 1400
    clock.leave()
    assert _min(d) == 1400, "someone still waits"
    clock.leave()
    assert _min(d) == 400
    clock.leave()
    assert clock.status()["waiting"] == 0, "never below zero"


def test_stopping_gives_the_clock_back_whatever_is_in_flight(tmp_path):
    d = _card(tmp_path)
    clock = Clock(d)
    clock.enter()
    clock.close()
    assert _min(d) == 400


def test_absent_is_allowed(tmp_path):
    for clock in (Clock(None), Clock(tmp_path / "nothing-here")):
        clock.enter()
        clock.leave()
        assert clock.status()["running"] is False and clock.status()["error"]

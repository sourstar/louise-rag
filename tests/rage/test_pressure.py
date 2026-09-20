"""The card's real memory, a stranger on it, and what RAGE does about both
(app/rage/placement.py: `card_memory`, `relieve`; docs/notes/rage.md §19).

The node, 2026-09-20: a container OUTSIDE RAGE took 21.4 GiB of the 32 GiB
card. RAGE's admission counted only what its own models declare, so it saw
nothing; `xe` moved every resident model into host RAM without an error to
anyone; the host ran out of memory and the kernel killed three models.

Operator, the same day: read the card's real free memory before a load; when
a process outside RAGE takes the card, "wisely unload the unused models to
avoid an oom crash of the other model"; and "if Louise needs a model that
RAGE cannot fit in memory I want a notification to Louise".
"""
import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import gpu_memory
from app.rage import admission, model, placement as placement_mod
from app.rage.clock import Clock
from app.rage.front import create_app
from app.rage.placement import READY, Refused

MIB = 1 << 20
PCI = "0000:03:00.0"


def run(coro):
    return asyncio.run(coro)


class Card:
    """The driver's count for the one card: 10,000 MiB, and whatever is used."""

    def __init__(self, used_mib: int = 0, total_mib: int = 10_000) -> None:
        self.total_mib, self.used_mib = total_mib, used_mib

    def __call__(self, pci: str):
        assert pci == PCI
        return self.total_mib * MIB, self.used_mib * MIB, None


@pytest.fixture
def card(rage) -> Card:
    """The harness, with the card on the bus where rage.toml says it is."""
    device = rage.root / "sys" / "bus" / "pci" / "devices" / PCI
    device.mkdir(parents=True)
    (rage.root / "sys" / "bus" / "pci" / "drivers" / "xe").mkdir(parents=True)
    (device / "driver").symlink_to(rage.root / "sys" / "bus" / "pci" / "drivers" / "xe")
    text = rage.config.read_text(encoding="utf-8")
    rage.config.write_text(
        text.replace("[rage]\n", f'[rage]\npci = {{ xpu = "{PCI}" }}\n'
                                 f'preload = ["bge-m3"]\n', 1), encoding="utf-8")
    return Card()


# ---- admission: a stranger is counted as observed --------------------------------------

def test_what_a_stranger_holds_is_counted_and_the_refusal_says_so(rage, card):
    p = rage.placement(card=card)
    card.used_mib = 8_000                       # nothing of ours is loaded: all a stranger's
    mem = p.card_memory("xpu")
    assert (mem["read"], mem["own_mib"], mem["foreign_mib"], mem["free_mib"]) == \
        (True, 0, 8_000, 2_000)
    with pytest.raises(Refused) as e:
        run(p.ensure("bge-m3"))                 # 1400 + 1000 reserved > 10000 - 8000
    assert e.value.status == 503
    assert "8000 held by processes outside RAGE" in e.value.body["error"]
    assert e.value.body["admission"]["foreign_mib"] == 8_000
    assert e.value.body["admission"]["free_mib"] == 1_000
    assert "bge-m3" not in p.placed and rage.launches == [], "refused before anything started"

    card.used_mib = 7_000                       # the stranger gave some back
    assert run(p.ensure("bge-m3")).state == READY


def test_our_own_models_are_counted_as_declared_never_twice(rage, card):
    """The driver's count includes OUR children. What they are seen to hold
    comes off it, so they enter the budget once — as declared."""
    p = rage.placement(card=card)
    run(p.ensure("bge-m3"))                     # the fake child holds 1400 MiB of the card
    card.used_mib = 1_400
    mem = p.card_memory("xpu")
    assert (mem["own_mib"], mem["foreign_mib"], mem["declared_held_mib"]) == (1_400, 0, 1_400)
    # 1400 held + 8200 + 1000 reserved > 10000: the LEDGER refuses, no stranger named
    with pytest.raises(Refused) as e:
        run(p.ensure("madlad"))
    assert "outside RAGE" not in e.value.body["error"]


def test_a_child_still_loading_is_ours_not_a_stranger(rage, card):
    """It has no pid on its placement yet. Everything in this container's own
    PID namespace that holds GPU memory is RAGE's — counted from there, or a
    load in progress would be a stranger AND its own declaration."""
    p = rage.placement(card=card)
    rage.spawn(["/opt/llama/llama-server", "-m", "still-loading"], {})   # 1400 MiB, no placement
    card.used_mib = 1_400
    mem = p.card_memory("xpu")
    assert (mem["own_mib"], mem["foreign_mib"]) == (1_400, 0)


def test_a_card_that_cannot_be_read_leaves_the_ledger_to_decide_and_says_why(rage, card):
    p = rage.placement(card=lambda pci: (10_000 * MIB, None, "the driver tells what is used "
                                                             "only to CAP_PERFMON"))
    mem = p.card_memory("xpu")
    assert mem["read"] is False and mem["total_mib"] == 10_000 and mem["foreign_mib"] is None
    assert "CAP_PERFMON" in mem["why_not"]
    assert run(p.ensure("bge-m3")).state == READY, "as before this existed"
    assert run(p.relieve()) == [], "and nothing is given up on a guess"

    def broken(pci):
        raise OSError(5, "Input/output error")
    q = rage.placement(card=broken)
    assert q.card_memory("xpu")["read"] is False and "OSError" in q.card_memory("xpu")["why_not"]


def test_with_no_address_declared_the_card_is_not_read_and_the_reason_says_what_to_declare(rage):
    p = rage.placement()
    mem = p.card_memory("xpu")
    assert mem["read"] is False and "declare [rage] pci.xpu" in mem["why_not"]


def test_admission_alone_a_stranger_only_ever_refuses(rage):
    cfg = model.load(rage.config)
    free = admission.fits(cfg, {}, "bge-m3", "xpu", lambda d: None)
    assert free.ok and free.foreign_mib == 0
    squeezed = admission.fits(cfg, {}, "bge-m3", "xpu", lambda d: None, {"xpu": 7_700})
    assert not squeezed.ok and squeezed.free_mib == 1_300 and squeezed.foreign_mib == 7_700
    assert admission.fits(cfg, {}, "bge-m3", "xpu", lambda d: None, {"xpu": -50}).ok, \
        "a reading below zero is no credit"
    assert admission.fits(cfg, {}, "bge-m3", "cpu", lambda d: 20_000, {"xpu": 9_999}).ok, \
        "a stranger on the card is nothing to a CPU placement"


# ---- the refusal is Louise's notification ---------------------------------------------------

def test_a_refusal_is_kept_for_louise_and_repeated_asking_is_one_event(rage, card):
    p = rage.placement(card=card)
    card.used_mib = 9_500
    for _ in range(5):                          # a worker that asks again every round
        with pytest.raises(Refused):
            run(p.ensure("bge-m3"))
    event, = p.events
    assert (event["kind"], event["model"], event["device"], event["count"]) == \
        ("refused", "bge-m3", "xpu", 5)
    assert "9500 held by processes outside RAGE" in event["detail"]


def test_the_front_reports_the_memory_whose_it_is_and_what_happened(rage, card):
    swap = rage.swap()
    p = rage.placement(swap, card=card)
    card.used_mib = 9_500
    with TestClient(create_app(p, swap, Clock(None), None, watch=False)) as c:
        assert c.post("/upstream/madlad/translate", json={"q": "x"}).status_code == 503
        body = c.get("/louise/gpu").json()
    assert body["container"], "so a reader of the host's processes can tell RAGE's from the rest"
    mem = body["memory"]["xpu"]
    assert (mem["total_mib"], mem["used_mib"], mem["squeezed"]) == (10_000, 9_500, True)
    assert "refused" in [e["kind"] for e in body["events"]]


# ---- giving up idle models under outside pressure -----------------------------------------

def test_a_squeezed_card_gives_up_idle_models_and_never_one_in_use(rage, card):
    p = rage.placement(card=card)
    run(p.ensure("bge-m3"))
    rage.set_gpu("bge-m3@xpu", card_mib=1_400, host_mib=40)
    card.used_mib = 1_400
    assert run(p.relieve(now=p.placed["bge-m3"].last_used + 600)) == [], "a quiet card: nothing"

    card.used_mib = 9_600                       # a stranger took 8,200: 400 free < 1000 reserved
    now = p.placed["bge-m3"].last_used
    assert run(p.relieve(now=now + 5)) == [], "used 5 s ago: not idle"
    p.placed["bge-m3"].in_flight = 1
    assert run(p.relieve(now=now + 600)) == [], "answering a request: never taken from under it"
    p.placed["bge-m3"].in_flight = 0

    assert run(p.relieve(now=now + 600)) == ["bge-m3"]
    assert "bge-m3" not in p.placed and rage.unloads == ["bge-m3@xpu"]
    event = p.events[-1]
    assert event["kind"] == "unloaded-under-pressure" and "8200 MiB" in event["detail"]
    row = next(r for r in p.describe() if r["id"] == "bge-m3")
    assert row["state"] == "unloaded" and "memory pressure" in row["reason"]
    assert any("UNLOADED UNDER PRESSURE" in line for line in p.logs)


def test_only_as_many_as_the_deficit_asks_for_least_recently_used_first(rage, card):
    text = rage.config.read_text(encoding="utf-8").replace("xpu = 10000", "xpu = 20000")
    rage.config.write_text(text, encoding="utf-8")
    card.total_mib = 20_000
    p = rage.placement(card=card)
    first, second = run(p.ensure("bge-m3")), run(p.ensure("madlad"))
    first.last_used, second.last_used = 1_000.0, 2_000.0       # bge-m3 is the older
    rage.set_gpu("bge-m3@xpu", card_mib=1_400, host_mib=0)
    rage.set_gpu("madlad@xpu", card_mib=8_200, host_mib=0)
    card.used_mib = 19_500                      # 9,900 a stranger's; 500 free, 500 short
    assert run(p.relieve(now=10_000.0)) == ["bge-m3"], "1,400 covers the 500: madlad stays"
    assert "madlad" in p.placed


def test_a_model_pushed_off_the_card_is_given_up_when_idle_even_with_no_reading(rage, card):
    """Seen from the children's own files, with no capability at all: most of
    its buffers are in host RAM. It holds real host memory and answers slowly;
    its reload puts it back on the card."""
    p = rage.placement(card=lambda pci: (None, None, "no reader"))
    placed = run(p.ensure("bge-m3"))
    rage.set_gpu("bge-m3@xpu", card_mib=9, host_mib=1_290)
    assert p.card_memory("xpu")["pushed_out"] == ["bge-m3"]
    assert run(p.relieve(now=placed.last_used + 600)) == ["bge-m3"]
    assert "pushed off the card" in p.events[-1]["detail"]


def test_it_can_be_switched_off_and_the_settings_are_checked(rage, card):
    text = rage.config.read_text(encoding="utf-8")
    rage.config.write_text(text.replace("[rage]\n", "[rage]\npressure = { unload = false }\n", 1),
                           encoding="utf-8")
    p = rage.placement(card=card)
    placed = run(p.ensure("bge-m3"))
    card.used_mib = 9_900
    assert run(p.relieve(now=placed.last_used + 600)) == []
    assert p.card_memory("xpu")["squeezed"] is True, "still SAID"
    for bad in ("pressure = { unload = 1 }", "pressure = { idle_s = -1 }",
                "pressure = { patience = 3 }"):
        rage.config.write_text(text.replace("[rage]\n", f"[rage]\n{bad}\n", 1), encoding="utf-8")
        with pytest.raises(model.RageConfigError, match="pressure"):
            model.load(rage.config)
    rage.config.write_text(text, encoding="utf-8")
    assert model.load(rage.config).pressure == {"unload": True, "idle_s": 60, "restore_s": 120}


# ---- coming back -----------------------------------------------------------------------------

def test_a_preloaded_model_comes_back_once_the_card_is_quiet_and_really_has_room(rage, card):
    p = rage.placement(card=card)
    placed = run(p.ensure("bge-m3"))
    rage.set_gpu("bge-m3@xpu", card_mib=1_400, host_mib=0)
    t0 = placed.last_used + 600
    card.used_mib = 9_600
    assert run(p.relieve(now=t0)) == ["bge-m3"]

    card.used_mib = 8_200                       # ours is gone; the stranger is still there
    run(p.relieve(now=t0 + 10))                 # quiet from here: 1,800 free >= 1,000 reserved
    run(p.relieve(now=t0 + 500))
    assert "bge-m3" not in p.placed, "quiet for long enough, but 1,800 free is no room for it"

    card.used_mib = 300                         # the stranger left
    run(p.relieve(now=t0 + 510))
    assert p.placed["bge-m3"].state == READY, "quiet since t0+10, and now there is room"
    assert [e["kind"] for e in p.events] == ["unloaded-under-pressure", "restored"]
    assert p.card_memory("xpu")["unloaded_under_pressure"] == []


def test_it_does_not_come_back_the_moment_the_squeeze_ends(rage, card):
    p = rage.placement(card=card)
    placed = run(p.ensure("bge-m3"))
    rage.set_gpu("bge-m3@xpu", card_mib=1_400, host_mib=0)
    t0 = placed.last_used + 600
    card.used_mib = 9_600
    run(p.relieve(now=t0))
    card.used_mib = 300
    run(p.relieve(now=t0 + 3))
    run(p.relieve(now=t0 + 60))
    assert "bge-m3" not in p.placed, "a stranger between two batches is still a stranger"
    run(p.relieve(now=t0 + 3 + 121))
    assert "bge-m3" in p.placed


def test_a_model_given_up_comes_back_on_its_next_request_through_admission(rage, card):
    p = rage.placement(card=card)
    placed = run(p.ensure("bge-m3"))
    rage.set_gpu("bge-m3@xpu", card_mib=1_400, host_mib=0)
    card.used_mib = 9_600
    run(p.relieve(now=placed.last_used + 600))
    card.used_mib = 8_200
    with pytest.raises(Refused, match="outside RAGE"):
        run(p.ensure("bge-m3"))                 # asked for while the stranger holds the card
    card.used_mib = 2_000
    assert run(p.ensure("bge-m3")).state == READY
    assert p.card_memory("xpu")["unloaded_under_pressure"] == []


def test_a_request_counts_as_in_flight_until_its_answer_has_ended(rage, card):
    swap = rage.swap()
    p = rage.placement(swap, card=card)
    seen = []
    rage.during_request = lambda: seen.append(p.placed["bge-m3"].in_flight)
    with TestClient(create_app(p, swap, Clock(None), None, watch=False)) as c:
        assert c.post("/upstream/bge-m3/v1/embeddings", json={"input": "x"}).status_code == 200
        assert seen == [1] and p.placed["bge-m3"].in_flight == 0
        rage.fail.add("bge-m3@xpu")
        assert c.post("/upstream/bge-m3/v1/embeddings", json={"input": "x"}).status_code >= 500
        assert p.placed["bge-m3"].in_flight == 0, "a failed request is not in flight for ever"


# ---- the reader, one per kernel driver, behind one verb ---------------------------------------

def test_the_shared_reader_names_no_driver_to_its_caller(tmp_path):
    sys_root = tmp_path / "sys"
    for address, driver in ((PCI, "xe"), ("0000:13:00.0", "amdgpu"), ("0000:20:00.0", "nouveau")):
        device = sys_root / "bus" / "pci" / "devices" / address
        (device / "drm" / "renderD128").mkdir(parents=True)
        (sys_root / "bus" / "pci" / "drivers" / driver).mkdir(parents=True)
        (device / "driver").symlink_to(sys_root / "bus" / "pci" / "drivers" / driver)
    amd = sys_root / "bus" / "pci" / "devices" / "0000:13:00.0"
    (amd / "mem_info_vram_total").write_text("536870912\n")
    (amd / "mem_info_vram_used").write_text("41943040\n")
    assert gpu_memory.read("0000:13:00.0", sys_root=sys_root) == (536870912, 41943040, None)
    total, used, why = gpu_memory.read("0000:20:00.0", sys_root=sys_root)
    assert (total, used) == (None, None) and "no memory reader for the nouveau driver" in why
    assert "no device" in gpu_memory.read("0000:99:00.0", sys_root=sys_root)[2]

    def ioctl(*a):
        raise OSError(25, "Inappropriate ioctl for device")
    dri = tmp_path / "dri"
    dri.mkdir()
    (dri / "renderD128").write_bytes(b"")
    total, used, why = gpu_memory.read(PCI, sys_root=sys_root, dev_dri=dri, ioctl=ioctl)
    assert (total, used) == (None, None) and "memory query failed" in why


def test_the_front_itself_names_no_manufacturer():
    """docs/notes/rage.md §18: a driver is named in ONE place, the shared
    reader's table — never in the code of the front."""
    front = Path(placement_mod.__file__).parent
    for name in ("placement.py", "admission.py", "front.py", "procfs.py"):
        text = (front / name).read_text(encoding="utf-8")
        code = re.sub(r'""".*?"""', "", text, flags=re.S)
        code = "\n".join(line.split("#")[0] for line in code.splitlines())
        assert not re.search(r"\b(xe|i915|amdgpu|nvidia|intel|nouveau)\b", code, re.I), name

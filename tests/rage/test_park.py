"""Parked: the card freed, the process kept, its weights in host RAM
(app/rage/placement.py: `park`, `_unpark`, `_give_up`; docs/notes/rage.md §21).

Operator, 2026-10-01: the translator is the model RAGE gives up first, and
bringing it back costs a whole launch — 20 s on the node — although its
process could keep its weights in host RAM and copy them back. So a model
that declares `park` is parked where it would have been unloaded, when the
host has room for it parked, and comes back to the card at its next request:
the same process, hence the same run. Every test counts what was UNLOADED and
what was asked of the child, not only what answered.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.rage import model
from app.rage.front import RUN_HEADER
from app.rage.placement import PARKED, READY, Refused

from .test_evict_for_load import _add, _card, _idle, _rank, run
from .test_front import ADMIN, _app

PARK = 'park = { memory = { xpu = 300 }, host = { xpu = %d } }\n'


def _parkable(rage, host_mib: int = 7_000) -> None:
    text = rage.config.read_text(encoding="utf-8")
    rage.config.write_text(text.replace("[models.madlad]\n",
                                        "[models.madlad]\n" + PARK % host_mib, 1),
                           encoding="utf-8")


def _moves(rage) -> list[str]:
    return [rest for rest, _ in rage.requests if rest in ("park", "unpark")]


def _night(rage, host_mib: int = 7_000):
    """The card of 2026-09-21 at a fifth of its size: the translator (0,
    parkable), a reranker (2), an encoder, all idle; a newcomer that fits only
    if something goes."""
    _card(rage, 20_000)
    _rank(rage, "madlad", 0)
    _parkable(rage, host_mib)
    _add(rage, "reranker", 2_200, priority=2)
    _add(rage, "newcomer", 9_000)
    p = rage.placement()
    for m in ("madlad", "reranker", "bge-m3"):
        run(p.ensure(m))
    _idle(p, "madlad", "reranker", "bge-m3")
    rage.unloads.clear()
    return p


# ---- the declaration -----------------------------------------------------------------

@pytest.mark.parametrize("park,rule", [
    ('park = { host = { xpu = 7000 } }', "park.memory.xpu is missing"),
    ('park = { memory = { xpu = 300 } }', "park.host.xpu is missing or zero"),
    ('park = { memory = { xpu = 300 }, host = { xpu = 7000 }, when = "idle" }',
     "unknown key(s) ['when']"),
    ('park = { memory = { xpu = 300 }, host = { xpu = 7000 }, path = "/x", unpark = "/x" }',
     "path and unpark must differ"),
    ('park = { memory = { xpu = 300, gpu1 = 1 }, host = { xpu = 7000 } }',
     "not cards of this model"),
])
def test_a_park_that_breaks_a_rule_is_refused_by_name(rage, park, rule):
    text = rage.config.read_text(encoding="utf-8")
    rage.config.write_text(text.replace("[models.madlad]\n", f"[models.madlad]\n{park}\n", 1),
                           encoding="utf-8")
    with pytest.raises(model.RageConfigError) as e:
        model.load(rage.config)
    assert rule in str(e.value)


def test_park_and_priority_survive_the_api_s_own_file(rage):
    """The file the API writes is read back as it was given. `priority` was
    dropped on the way until this test (2026-10-01): a model PUT with
    priority 0 came back with the encoders' 3."""
    _parkable(rage)
    _rank(rage, "madlad", 0)
    entry = model.load(rage.config).models["madlad"]
    again = model.entry_from_table(
        "madlad", __import__("tomllib").loads(model.entry_to_toml(entry))["models"]["madlad"])
    assert again.priority == 0
    assert again.park == entry.park and again.park.host == {"xpu": 7000}


# ---- parked instead of unloaded --------------------------------------------------------

def test_a_load_that_needs_the_card_parks_the_translator_instead_of_unloading_it(rage):
    p = _night(rage)
    assert run(p.ensure("newcomer")).state == READY
    assert rage.unloads == [], "nothing was stopped"
    assert _moves(rage) == ["park"]
    assert p.placed["madlad"].state == PARKED
    assert p.events[-1]["kind"] == "parked"
    assert "parked in host RAM to make room for newcomer" in p.events[-1]["detail"]
    # the ledger counts it at its parked size: 300 of the card, not 8,200
    assert p.card_memory("xpu")["declared_held_mib"] == 300 + 2_200 + 1_400 + 9_000


def test_the_next_request_brings_it_back_as_the_same_process_and_the_same_run(rage):
    p = _night(rage)
    before = p.placed["madlad"]
    run_id, pid, launches = before.run, before.pid, len(rage.launches)
    assert run(p.park("madlad")) is True
    placed = run(p.ensure("madlad"))
    assert placed.state == READY and placed.run == run_id and placed.pid == pid
    assert len(rage.launches) == launches, "no launch: the copy back is all it costs"
    assert _moves(rage) == ["park", "unpark"]
    assert p.still_the_run(placed)


def test_unparking_is_admitted_like_a_load_and_waits_parked_when_it_does_not_fit(rage):
    p = _night(rage)
    run(p.ensure("newcomer"))                   # the translator parked to make room
    rage.requests.clear()
    with pytest.raises(Refused) as e:
        run(p.ensure("madlad"))                 # rank 0: it may give up nothing for itself
    assert e.value.status == 503 and e.value.body["state"] == PARKED
    assert p.placed["madlad"].state == PARKED and _moves(rage) == []
    assert p.events[-1]["kind"] == "refused"


def test_without_room_in_host_ram_it_is_unloaded_as_before(rage):
    p = _night(rage, host_mib=17_500)           # parked, it would not fit the host's 18,000
    assert run(p.ensure("newcomer")).state == READY
    assert rage.unloads == ["madlad@xpu"] and "madlad" not in p.placed
    assert _moves(rage) == []
    assert p.events[-1]["kind"] == "unloaded-under-pressure"


def test_when_the_host_says_no_what_rage_parked_goes_first(rage):
    """Host 18,000: the translator parked holds 14,000 of it, the others 550;
    a model of 4,000 on the CPU does not fit until the translator goes."""
    p = _night(rage, host_mib=14_000)
    assert run(p.park("madlad"))
    _add(rage, "cpu-model", 4_000)
    p.reload()
    rage.unloads.clear()
    placed = run(p.ensure("cpu-model", "cpu"))
    assert placed.state == READY and placed.device == "cpu"
    assert rage.unloads == ["madlad@xpu"], "the parked translator, unloaded for host RAM"


def test_a_model_answering_is_never_parked_from_under_its_answer(rage):
    p = _night(rage)
    p.placed["madlad"].in_flight = 1
    assert run(p.park("madlad", only_if_idle=True)) is False
    with pytest.raises(Refused) as e:
        run(p.park("madlad", only_if_idle=False))
    assert e.value.status == 409 and _moves(rage) == []


def test_a_model_that_declares_no_park_is_only_ever_unloaded(rage):
    p = _night(rage)
    with pytest.raises(Refused) as e:
        run(p.park("reranker"))
    assert e.value.status == 409 and "declares no park" in str(e.value)


def test_a_park_that_fails_stops_the_process_and_the_next_request_is_a_new_launch(rage):
    p = _night(rage)
    rage.fail_paths.add("madlad@xpu/park")
    launches = len(rage.launches)
    with pytest.raises(Refused) as e:
        run(p.park("madlad"))
    assert e.value.status == 502
    assert "madlad" not in p.placed and rage.unloads == ["madlad@xpu"], \
        "a half-moved child holds what nobody knows: it goes whole"
    assert run(p.ensure("madlad")).state == READY
    assert len(rage.launches) == launches + 1


def test_a_caller_that_hangs_up_leaves_no_model_half_moved(rage):
    p = _night(rage)
    rage.call_delay = 0.05

    async def scenario():
        parking = asyncio.create_task(p.park("madlad"))
        await asyncio.sleep(0.01)
        parking.cancel()                        # the operator closed the page
        placed = await p.ensure("madlad")       # waits for the park, then unparks
        return placed.state

    assert run(scenario()) == READY
    assert _moves(rage) == ["park", "unpark"]


def test_a_parked_preloaded_model_stays_parked_when_the_files_are_applied(rage):
    p = _night(rage)
    p.cfg = model.load(rage.config)
    object.__setattr__(p.cfg, "preload", ("madlad",))
    run(p.park("madlad"))
    run(p.apply())
    assert p.placed["madlad"].state == PARKED and _moves(rage) == ["park"]


# ---- through the front -------------------------------------------------------------------

def test_the_operator_parks_and_the_next_request_unparks(rage):
    _parkable(rage)
    app, placement = _app(rage)
    with TestClient(app) as client:
        assert client.post("/louise/models/madlad/load", headers=ADMIN).status_code == 200
        assert client.post("/louise/models/madlad/park").status_code == 401
        row = client.post("/louise/models/madlad/park", headers=ADMIN).json()
        assert row["state"] == "parked"
        assert row["memory"]["parked"] == {"declared": {"xpu": 300}, "host": {"xpu": 7000}}
        run_id = placement.placed["madlad"].run
        r = client.post("/upstream/madlad/translate", json={"items": []})
        assert r.status_code == 200 and r.headers[RUN_HEADER] == run_id
        assert placement.placed["madlad"].state == READY
        assert client.post("/louise/models/bge-m3/park", headers=ADMIN).status_code == 409


def test_a_squeeze_parks_what_it_can_and_the_card_is_freed_all_the_same(rage):
    """A stranger squeezes the card (rage.md §19): the translator, idle, is
    parked — its 8,200 MiB off the card — not unloaded."""
    from .test_pressure import PCI, Card

    device = rage.root / "sys" / "bus" / "pci" / "devices" / PCI
    device.mkdir(parents=True)
    (rage.root / "sys" / "bus" / "pci" / "drivers" / "xe").mkdir(parents=True)
    (device / "driver").symlink_to(rage.root / "sys" / "bus" / "pci" / "drivers" / "xe")
    text = rage.config.read_text(encoding="utf-8")
    rage.config.write_text(text.replace("[rage]\n", f'[rage]\npci = {{ xpu = "{PCI}" }}\n', 1),
                           encoding="utf-8")
    _parkable(rage)
    card = Card()
    p = rage.placement(card=card)
    run(p.ensure("madlad"))
    rage.set_gpu("madlad@xpu", card_mib=8_200, host_mib=40)
    card.used_mib = 9_600                       # a stranger holds 1,400: 400 free < 1,000
    now = p.placed["madlad"].last_used + 600
    assert run(p.relieve(now=now)) == ["madlad"]
    assert p.placed["madlad"].state == PARKED and rage.unloads == []
    assert _moves(rage) == ["park"] and p.events[-1]["kind"] == "parked"


def test_a_process_that_is_gone_is_not_started_again_only_to_be_parked(rage):
    p = _night(rage)
    rage.kill("madlad@xpu")                     # the kernel's killer, say
    launches = len(rage.launches)
    with pytest.raises(Refused) as e:
        run(p.park("madlad"))
    assert e.value.status == 409 and "is gone" in str(e.value)
    assert len(rage.launches) == launches and _moves(rage) == []

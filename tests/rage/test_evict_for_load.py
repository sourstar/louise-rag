"""Giving up idle models to make room for a load, by priority
(app/rage/placement.py: `_make_room`, `victims_by_priority`).

The node, 2026-09-21: DAR-legal-it asked for 6,200 MiB of a card holding seven
preloaded models and was refused — the ledger had 1,125 MiB, while the
translator sat idle on 8,200 of them. RAGE never gave anything up for a load:
that was Louise's decision, and Louise never made it. The same night the
reranker was given up under pressure, came back with nobody asking, and was
given up again — because giving up by SIZE always picks the small model every
search uses.

Operator, the same night: "RAGE should free VRAM on demand", the translator
first — "it is only an optimization" — then chat, then the rerankers, then
the encoders. Every test below counts what was UNLOADED, not only what
answered: a rule that gives up the wrong model still answers the load.
"""
import asyncio
import time

import pytest

from app.rage import model
from app.rage.placement import READY, Placed, Refused, victims_by_priority

MODEL = """
[models.{id}]
engine  = {{ name = "llama.cpp", build = "b10630", variant = "sycl" }}
weights = "{models}/{id}.gguf"
device  = "xpu"
memory  = {{ xpu = {mib}, cpu = {mib} }}
host    = {{ xpu = 100 }}
cmd.xpu = ["/opt/llama/llama-server", "-m", "${{WEIGHTS}}", "--embedding", "-ngl", "99", "--port", "${{PORT}}"]
cmd.cpu = ["/opt/llama/llama-server", "-m", "${{WEIGHTS}}", "--embedding", "-ngl", "0", "--port", "${{PORT}}"]
{priority}"""


def run(coro):
    return asyncio.run(coro)


def _card(rage, capacity_mib: int) -> None:
    text = rage.config.read_text(encoding="utf-8")
    rage.config.write_text(text.replace("xpu = 10000", f"xpu = {capacity_mib}", 1),
                           encoding="utf-8")


def _rank(rage, model_id: str, priority: int) -> None:
    text = rage.config.read_text(encoding="utf-8")
    rage.config.write_text(text.replace(f"[models.{model_id}]\n",
                                        f"[models.{model_id}]\npriority = {priority}\n", 1),
                           encoding="utf-8")


def _add(rage, model_id: str, mib: int, priority: int | None = None) -> None:
    (rage.models / f"{model_id}.gguf").write_bytes(f"GGUF {model_id}".encode())
    line = "" if priority is None else f"priority = {priority}\n"
    with rage.config.open("a", encoding="utf-8") as f:
        f.write(MODEL.format(id=model_id, mib=mib, models=rage.models.as_posix(),
                             priority=line))


def _idle(p, *models: str) -> None:
    for m in models:
        p.placed[m].last_used = 0.0             # decades ago: well past idle_s


def _users_night_file(rage) -> None:
    """The shape of the node that night, at a fifth of the size: the
    translator (0), a reranker (2) and an encoder (3), and a newcomer (3) that
    fits only if something goes. 20,000 MiB, 1,000 reserved."""
    _card(rage, 20_000)
    _rank(rage, "madlad", 0)                    # 8,200
    _add(rage, "reranker", 2_200, priority=2)
    _add(rage, "newcomer", 9_000)               # no priority: the encoders' rank


def _loaded_and_idle(rage, *models: str):
    p = rage.placement()
    for m in models:
        run(p.ensure(m))
    _idle(p, *models)
    rage.unloads.clear()
    return p


def _users_night(rage):
    _users_night_file(rage)
    # 8,200 + 2,200 + 1,400 = 11,800 held; free 7,200; the newcomer is 1,800 short
    return _loaded_and_idle(rage, "madlad", "reranker", "bge-m3")


# ---- the rule, alone ------------------------------------------------------------------

def _placed(model_id: str, last_used: float = 0.0) -> Placed:
    return Placed(model=model_id, device="xpu", state=READY, since=0.0, last_used=last_used)


def test_priority_decides_and_size_only_breaks_a_tie():
    rank = {"translator": 0, "reranker": 2}.get
    holding = [(_placed("translator"), 8_200), (_placed("reranker"), 2_200)]
    # 1,800 short: the reranker ALONE covers it with 6,000 MiB less given up,
    # and is still not the one — that was the size rule, and it was wrong
    assert [p.model for p in victims_by_priority(holding, 1_800, rank, must_cover=True)] \
        == ["translator"]


def test_within_one_priority_the_fewest_mib_are_given_up():
    same = {"a": 2, "b": 2, "c": 2}.get
    holding = [(_placed("a"), 5_000), (_placed("b"), 1_000), (_placed("c"), 1_200)]
    assert [p.model for p in victims_by_priority(holding, 900, same, must_cover=True)] == ["b"]


def test_a_whole_priority_goes_before_the_next_is_touched():
    rank = {"t": 0, "u": 0, "r": 2}.get
    holding = [(_placed("t"), 1_000), (_placed("u"), 1_000), (_placed("r"), 5_000)]
    chosen = victims_by_priority(holding, 2_500, rank, must_cover=True)
    assert [p.model for p in chosen] == ["t", "u", "r"], "both of rank 0, then rank 2"


def test_a_load_takes_nothing_when_everything_it_may_take_falls_short():
    rank = {"t": 0, "r": 2}.get
    holding = [(_placed("t"), 1_000), (_placed("r"), 1_000)]
    assert victims_by_priority(holding, 5_000, rank, must_cover=True) == []
    # a squeeze still takes what helps: every MiB keeps a busy model off host RAM
    assert len(victims_by_priority(holding, 5_000, rank, must_cover=False)) == 2


# ---- the load -------------------------------------------------------------------------

def test_a_load_that_does_not_fit_gives_up_the_lowest_priority_idle_model(rage):
    p = _users_night(rage)
    assert run(p.ensure("newcomer")).state == READY
    assert rage.unloads == ["madlad@xpu"], "the translator, and ONLY the translator"
    assert {"reranker", "bge-m3", "newcomer"} <= set(p.placed)
    assert p.events[-1]["kind"] == "unloaded-under-pressure"
    assert "to make room for newcomer" in p.events[-1]["detail"]
    assert any("GIVEN UP FOR A LOAD" in line for line in p.logs)


def test_nothing_is_given_up_when_that_would_not_be_enough(rage):
    """Giving up four models and refusing anyway destroys them for nothing."""
    _users_night_file(rage)
    # 18,500 against 7,200 free: 11,300 short. Below its rank are the translator
    # and the reranker, 10,400 together; bge-m3 is its own rank and stays.
    _add(rage, "giant", 18_500)
    p = _loaded_and_idle(rage, "madlad", "reranker", "bge-m3")
    with pytest.raises(Refused) as e:
        run(p.ensure("giant"))
    assert e.value.status == 503
    assert rage.unloads == [], "refused, and every model still where it was"
    assert {"madlad", "reranker", "bge-m3"} <= set(p.placed)


def test_a_load_never_gives_up_a_model_ranked_at_or_above_it(rage):
    """Bringing the translator back for a search must not give up chat and a
    reranker: the lowest-priority model there is cannot push anything out."""
    _card(rage, 20_000)
    _rank(rage, "madlad", 0)
    _add(rage, "chat", 7_600, priority=1)
    _add(rage, "reranker", 2_200, priority=2)
    p = rage.placement()
    for m in ("chat", "reranker", "bge-m3"):    # 11,200 held: madlad's 8,200 is 400 short
        run(p.ensure(m))
    _idle(p, "chat", "reranker", "bge-m3")
    rage.unloads.clear()
    with pytest.raises(Refused):
        run(p.ensure("madlad"))
    assert rage.unloads == []


def test_two_models_of_one_rank_do_not_evict_each_other(rage):
    _card(rage, 20_000)
    _add(rage, "reranker-a", 9_000, priority=2)
    _add(rage, "reranker-b", 9_000, priority=2)
    p = rage.placement()
    run(p.ensure("reranker-a"))
    run(p.ensure("bge-m3"))                     # 10,400 held, 8,600 free: b is 400 short
    _idle(p, "reranker-a", "bge-m3")
    rage.unloads.clear()
    with pytest.raises(Refused):
        run(p.ensure("reranker-b"))
    assert rage.unloads == [], "equal rank is not below: no loop between the two"


def test_a_model_in_use_is_never_given_up_for_a_load(rage):
    p = _users_night(rage)
    p.placed["madlad"].in_flight = 1            # translating right now
    p.placed["reranker"].last_used = time.time()  # asked a moment ago: a batch between requests
    with pytest.raises(Refused):
        run(p.ensure("newcomer"))
    assert rage.unloads == [], "the only models that could cover it are busy"


def test_switching_pressure_off_switches_off_giving_up_for_a_load_too(rage):
    _users_night_file(rage)
    text = rage.config.read_text(encoding="utf-8")
    rage.config.write_text(text.replace("[rage]\n", "[rage]\npressure = { unload = false }\n", 1),
                           encoding="utf-8")
    p = _loaded_and_idle(rage, "madlad", "reranker", "bge-m3")
    with pytest.raises(Refused):
        run(p.ensure("newcomer"))
    assert rage.unloads == [], "one switch for everything RAGE gives up by itself"


# ---- the file -------------------------------------------------------------------------

def test_a_file_written_before_priority_existed_parses_and_ranks_everything_on_top(rage):
    cfg = model.load(rage.config)               # the harness's file names no priority
    assert {m: e.priority for m, e in cfg.models.items()} == \
        {"bge-m3": model.DEFAULT_PRIORITY, "madlad": model.DEFAULT_PRIORITY}
    assert cfg.pressure["restore_min_priority"] == model.DEFAULT_PRIORITY


@pytest.mark.parametrize("bad", ["-1", '"high"', "true", "1.5"])
def test_a_priority_that_is_not_a_whole_number_is_refused(rage, bad):
    _rank(rage, "madlad", 0)
    text = rage.config.read_text(encoding="utf-8").replace("priority = 0", f"priority = {bad}")
    rage.config.write_text(text, encoding="utf-8")
    with pytest.raises(model.RageConfigError, match="madlad.priority"):
        model.load(rage.config)


def test_restore_min_priority_is_checked_as_a_rank(rage):
    text = rage.config.read_text(encoding="utf-8")
    rage.config.write_text(
        text.replace("[rage]\n", "[rage]\npressure = { restore_min_priority = -1 }\n", 1),
        encoding="utf-8")
    with pytest.raises(model.RageConfigError, match="whole number of a model priority"):
        model.load(rage.config)

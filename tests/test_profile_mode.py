"""A node profile's `mode` (app/node_profile.py, docs/notes/node-profiles-design.md
decision 6; the operator, 2026-11-02: "During the day keep gpu at full power,
suspend processing during the quiet time frames in the morning, at lunch time
and in the evening"): the stricter of the node's mode and the profile's wins,
at every gate that asks `node_state.runs`."""
from __future__ import annotations

from datetime import datetime, timedelta

from app import cpu_limit, health, jobs, node_profile, node_state
from app.node_profile import Active, GpuLevel, Profile

F, S, P, I, E = (node_state.FETCH, node_state.SEED, node_state.PARSE, node_state.INDEX,
                 node_state.EMBED)
UNTIL = datetime.now().replace(second=0, microsecond=0) + timedelta(hours=1)


def _profile(monkeypatch, mode: str | None, cores: int | None = 12) -> None:
    """node_profile.active answers this profile (None: the node declares none)."""
    monkeypatch.setattr(cpu_limit, "TTL_S", 0.0)
    cpu_limit.reset()
    a = None if mode is None else Active(profile=Profile("quiet" if mode != "running" else "day",
                                                         cores, "full", mode),
                                         level=GpuLevel("full"), until=UNTIL, next="day")
    monkeypatch.setattr(node_profile, "active", lambda now=None: a)


def test_no_profile_holds_nothing(monkeypatch):
    _profile(monkeypatch, None)
    assert all(node_state.runs("running", x) for x in (F, S, P, I, E))
    assert not node_state.runs("acquire-only", E)


def test_a_quiet_profile_suspends_processing_and_keeps_bytes_moving(monkeypatch):
    _profile(monkeypatch, "acquire-only", cores=0)
    assert node_state.runs("running", F) and node_state.runs("running", S)
    assert not any(node_state.runs("running", x) for x in (P, I, E))
    # the table alone is unchanged: the Library's buttons promise what a mode runs
    assert node_state.mode_runs("running", E)


def test_the_stricter_of_the_two_wins(monkeypatch):
    _profile(monkeypatch, "acquire-only")
    assert node_state.runs("seed-only", S) and not node_state.runs("seed-only", F)
    assert not node_state.runs("paused", S)
    _profile(monkeypatch, "running")
    assert node_state.runs("running", E) and not node_state.runs("acquire-only", P)


def test_a_bad_file_keeps_the_last_good_mode(monkeypatch):
    _profile(monkeypatch, "acquire-only")
    assert not node_state.runs("running", E)

    def broken(now=None):
        raise node_profile.ProfileError("[profiles.quiet] mode = 'loud' is not a node mode")
    monkeypatch.setattr(node_profile, "active", broken)
    lim = cpu_limit.current()
    assert lim.mode == "acquire-only" and lim.error
    assert not node_state.runs("running", E) and node_state.runs("running", F)


def test_the_line_says_what_is_suspended_and_until_when(monkeypatch):
    _profile(monkeypatch, "acquire-only", cores=0)
    line = cpu_limit.current().says()
    assert line.startswith("profile quiet: processing suspended until ")
    assert "downloads, seeding and searches go on" in line
    said: list[str] = []
    w = cpu_limit.Watch(said.append)
    w.limit()
    w.limit()
    _profile(monkeypatch, "running")
    w.limit()
    assert len(said) == 2 and "suspended" in said[0] and "suspended" not in said[1]


def test_health_names_the_schedule_as_the_reason(monkeypatch):
    _profile(monkeypatch, "acquire-only", cores=0)
    for stage in ("rdf", "fts", "embed", "archive"):
        why = health._held_reason("running", stage)
        assert "processing suspended" in why and "platform.toml" in why, stage
    # the node's own mode, where it is the stricter, is named first
    assert health._held_reason("acquire-only", "embed").startswith("the node is acquire-only")
    _profile(monkeypatch, "running", cores=0)
    assert health._held_reason("running", "embed") == ""
    assert "embedding goes on" in health._held_reason("running", "fts")


def test_a_create_job_waits_for_the_schedule_and_says_so(monkeypatch):
    _profile(monkeypatch, "acquire-only", cores=0)
    job = jobs.Job(id=1, kind=jobs.CREATE, args={}, state=jobs.QUEUED, result=None, error=None,
                   created=0.0, started=None, finished=None)
    assert jobs.CREATE not in jobs.kinds_allowed("running")
    why = jobs.waiting_because(job, "running")
    assert "processing suspended" in why and "schedule" in why
    _profile(monkeypatch, "running")
    assert jobs.CREATE in jobs.kinds_allowed("running")


def test_the_workers_gates_follow_the_profile(isolated_config, monkeypatch):
    from app import worker
    node_state.set_worker_state("running")
    _profile(monkeypatch, "acquire-only", cores=0)
    assert worker._maintenance_allowed() is False
    _profile(monkeypatch, "running")
    assert worker._maintenance_allowed() is True

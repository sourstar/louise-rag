"""The node's quiet / day / night profiles (app/node_profile.py;
docs/notes/node-profiles-design.md, operator 2026-10-27)."""
from __future__ import annotations

import tomllib
from datetime import datetime
from pathlib import Path

import pytest

from app import node_profile, platform_config
from app.transfer import schedule

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "notes" / "node-profiles.example.toml"
TUESDAY = datetime(2026, 10, 27)


def _at(hhmm: str, day: datetime = TUESDAY) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return day.replace(hour=h, minute=m)


@pytest.fixture
def profiles():
    return node_profile.parse(tomllib.loads(EXAMPLE.read_text(encoding="utf-8")))


@pytest.mark.parametrize("hhmm,name", [
    ("05:59", "night"), ("06:00", "quiet"), ("07:59", "quiet"), ("08:00", "day"),
    ("12:00", "quiet"), ("13:30", "day"), ("16:30", "quiet"), ("22:29", "quiet"),
    ("22:30", "night"), ("00:00", "night"),
])
def test_the_operators_schedule(profiles, hhmm, name):
    assert profiles.active(_at(hhmm)).profile.name == name


def test_each_profile_carries_its_cores_and_gpu_level(profiles):
    quiet = profiles.active(_at("07:00"))
    assert quiet.profile.cpu_cores == 0 and quiet.level.name == "quiet"
    assert quiet.level.power_limit == "50%" and quiet.level.fan_max_rpm == 1200
    day, night = profiles.active(_at("09:00")), profiles.active(_at("23:00"))
    assert (day.profile.cpu_cores, day.level.power_limit) == (8, "max")
    assert (night.profile.cpu_cores, night.level.power_limit) == (12, "max")


def test_until_is_the_next_change(profiles):
    a = profiles.active(_at("07:15"))
    assert a.until == _at("08:00") and a.next == "day"
    a = profiles.active(_at("23:00"))
    assert a.until == _at("06:00", TUESDAY.replace(day=28)) and a.next == "quiet"


def test_a_weekday_line_overrides_the_profile_line(profiles):
    raw = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    raw["schedule"]["sat"] = "08:00-22:30 quiet; else night"
    p = node_profile.parse(raw)
    saturday = datetime(2026, 10, 31)
    assert p.active(_at("10:00", saturday)).profile.name == "quiet"
    assert p.active(_at("10:00")).profile.name == "day"


def test_no_schedule_is_no_profile():
    assert node_profile.parse({}) is None
    assert node_profile.parse({"bandwidth": {}}) is None


@pytest.mark.parametrize("edit,match", [
    (lambda r: r["schedule"].update(profile="06:00-08:00 loud; else night"), "not a profile"),
    (lambda r: r["schedule"].update(profile="06:00-08:00 quiet"), "without a profile"),
    (lambda r: r["profiles"]["day"].update(gpu="turbo"), "not a level"),
    (lambda r: r["profiles"]["day"].update(cpu_cores=-1), "0 or more"),
    (lambda r: r["profiles"]["day"].update(sustained_embedding=True), "takes cpu_cores and gpu"),
    (lambda r: r["gpu"]["levels"]["quiet"].update(power_limit="half"), "power_limit"),
    (lambda r: r["gpu"]["levels"]["quiet"].update(power_limit="150%"), "1-100"),
    # each key takes its own unit: a power limit in MHz, a clock in watts
    (lambda r: r["gpu"]["levels"]["quiet"].update(power_limit="1400MHz"), "number of W"),
    (lambda r: r["gpu"]["levels"]["quiet"].update(max_clock="120W"), "number of MHz"),
    (lambda r: r["schedule"].update(holiday="else night"), "weekdays"),
])
def test_a_malformed_file_is_refused_at_load(edit, match):
    raw = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    edit(raw)
    with pytest.raises(node_profile.ProfileError, match=match):
        node_profile.parse(raw)


def test_platform_toml_carries_them_and_refuses_a_bad_one(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    (tmp_path / "platform.toml").write_bytes(EXAMPLE.read_bytes())
    assert platform_config.load().profiles.active(_at("07:00")).profile.name == "quiet"
    assert node_profile.cpu_cores(_at("23:00")) == 12
    (tmp_path / "platform.toml").write_text('[schedule]\nprofile = "else nowhere"\n[profiles.day]\n')
    with pytest.raises(schedule.ScheduleError):
        platform_config.load()


def test_no_platform_toml_limits_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    assert node_profile.active() is None and node_profile.cpu_cores() is None

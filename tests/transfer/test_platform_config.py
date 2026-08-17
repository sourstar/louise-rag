"""Tier-2 platform.toml reader. The load-bearing property is the FIRST test:
an existing deployment with no platform.toml must see unlimited bandwidth and
an always-full schedule, i.e. zero behavior change (CLAUDE.md invariant 10 puts
this in Tier 2, and collections.py sets the precedent that an absent file means
defaults rather than an error).
"""
import pytest

from app import platform_config
from app.transfer import schedule as sched


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    d = tmp_path / "config"
    d.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(d))
    return d


def test_absent_file_means_unlimited_and_no_behavior_change(cfg_dir):
    pc = platform_config.load()
    assert pc.source_path is None
    assert pc.bandwidth.down_max_bps is None      # None = unlimited
    assert pc.bandwidth.up_max_bps is None
    assert pc.bandwidth.schedule.factor_at(0) == 1.0
    assert pc.bandwidth.schedule.factor_at(13 * 60) == 1.0
    assert pc.transfer.workers == platform_config.DEFAULT_WORKERS
    assert pc.transfer.per_endpoint == platform_config.DEFAULT_PER_ENDPOINT
    assert pc.transfer.segments_per_file == 1


def test_doc_section_5_2_surface_loads_verbatim(cfg_dir):
    (cfg_dir / "platform.toml").write_text(
        '[bandwidth]\n'
        'down_max = "400Mbit"\n'
        'up_max = "40Mbit"\n'
        'reserve_interactive = "20%"\n'
        'schedule = "01:00-07:00 full; else 30%"\n'
        '[transfer]\n'
        'workers = 8\n'
        'per_endpoint = 4\n'
        'segments_per_file = 1\n')
    pc = platform_config.load()
    assert pc.bandwidth.down_max_bps == 50_000_000     # 400 Mbit/s in bytes/s
    assert pc.bandwidth.up_max_bps == 5_000_000
    assert pc.bandwidth.reserve_interactive == pytest.approx(0.20)
    assert pc.bandwidth.schedule.factor_at(2 * 60) == 1.0
    assert pc.bandwidth.schedule.factor_at(12 * 60) == 0.30
    assert pc.bandwidth.schedule_text == "01:00-07:00 full; else 30%"
    assert pc.transfer.workers == 8
    assert pc.transfer.per_endpoint == 4
    assert pc.source_path == cfg_dir / "platform.toml"


def test_partial_file_keeps_defaults_for_everything_else(cfg_dir):
    (cfg_dir / "platform.toml").write_text('[bandwidth]\ndown_max = "10Mbit"\n')
    pc = platform_config.load()
    assert pc.bandwidth.down_max_bps == 1_250_000
    assert pc.bandwidth.up_max_bps is None
    assert pc.bandwidth.reserve_interactive == platform_config.DEFAULT_RESERVE_INTERACTIVE
    assert pc.transfer.workers == platform_config.DEFAULT_WORKERS


@pytest.mark.parametrize("value,expected", [
    ('"20%"', 0.20),
    ("0.2", 0.20),
    ("20", 0.20),      # a bare number > 1 reads as a percentage
    ('"0%"', 0.0),
])
def test_reserve_interactive_spellings(cfg_dir, value, expected):
    (cfg_dir / "platform.toml").write_text(
        f'[bandwidth]\nreserve_interactive = {value}\n')
    assert platform_config.load().bandwidth.reserve_interactive == pytest.approx(expected)


def test_reserve_interactive_rejects_100_percent(cfg_dir):
    """A 100% reserve would leave nothing for any other class."""
    (cfg_dir / "platform.toml").write_text('[bandwidth]\nreserve_interactive = "100%"\n')
    with pytest.raises(sched.ScheduleError):
        platform_config.load()


def test_segments_per_file_above_one_refuses_loudly(cfg_dir):
    """§5.2 defaults it to 1 and calls multi-segment default-off; refusing
    beats silently ignoring the knob."""
    (cfg_dir / "platform.toml").write_text('[transfer]\nsegments_per_file = 4\n')
    with pytest.raises(sched.ScheduleError, match="not implemented in P0.7"):
        platform_config.load()


@pytest.mark.parametrize("body", [
    '[transfer]\nworkers = 0\n',
    '[transfer]\nper_endpoint = -1\n',
    '[transfer]\nworkers = "eight"\n',
    '[bandwidth]\ndown_max = "400Furlongs"\n',
    '[bandwidth]\nschedule = "01:00-07:00"\n',
])
def test_malformed_config_raises_at_load_not_at_0100(cfg_dir, body):
    """Config errors must surface at boot, not six weeks later when a window
    happens to open."""
    (cfg_dir / "platform.toml").write_text(body)
    with pytest.raises(sched.ScheduleError):
        platform_config.load()


def test_schedule_under_bandwidth_matches_section_14_spelling(cfg_dir):
    """§14's example nests schedule under [bandwidth] with no [transfer] table
    at all — that spelling must load too."""
    (cfg_dir / "platform.toml").write_text(
        '[bandwidth]\n'
        'down_max = "400Mbit"\n'
        'up_max = "40Mbit"\n'
        'schedule = "01:00-07:00 full; else 30%"\n')
    pc = platform_config.load()
    assert pc.bandwidth.schedule.factor_at(3 * 60) == 1.0
    assert pc.transfer.workers == platform_config.DEFAULT_WORKERS

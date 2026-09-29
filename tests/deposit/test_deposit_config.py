"""tests/deposit/test_deposit_config.py — `[deposit]` in platform.toml (07 §7.8)."""
import pytest

from app import platform_config
from app.transfer import schedule


def load(tmp_path, text):
    p = tmp_path / "platform.toml"
    p.write_text(text, encoding="utf-8")
    return platform_config._load_one(p)


def test_absent_means_nothing_is_named(tmp_path):
    d = load(tmp_path, "").deposit
    assert (d.encoder, d.space, d.space_from, d.by_name) == (None, None, None, {})


def test_encoder_space_and_one_name(tmp_path):
    d = load(tmp_path, '[deposit]\nencoder = "http://rage/upstream/granite"\n'
                       'space = "granite-space.toml"\n'
                       '[deposit.collections.alice]\nspace_from = "sha256:ab"\n').deposit
    assert d.encoder == "http://rage/upstream/granite"
    assert d.space == tmp_path / "granite-space.toml"     # beside platform.toml
    assert d.by_name == {"alice": "sha256:ab"}


@pytest.mark.parametrize("text", [
    '[deposit]\nencodr = "x"\n',                              # a typo is refused
    '[deposit]\nencoder = 3\n',
    '[deposit.collections.alice]\nspace_from = "a"\nother = 1\n',
])
def test_what_would_silently_pick_another_encoder_is_refused(tmp_path, text):
    with pytest.raises(schedule.ScheduleError):
        load(tmp_path, text)

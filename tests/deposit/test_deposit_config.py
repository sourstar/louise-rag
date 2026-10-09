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


# ---- the [encoder] a deposit collection's file is written with (amended 2026-10-09)

def test_api_and_models_default_to_openai_and_the_url(tmp_path):
    d = load(tmp_path, '[deposit]\nencoder = "http://rage/upstream/granite"\n').deposit
    assert (d.api, d.embed_model, d.query_model) == ("openai", None, None)


def test_a_vega_deposit_names_both_models(tmp_path):
    d = load(tmp_path, '[deposit]\nencoder = "http://localai:8080"\napi = "vega"\n'
                       'embed_model = "vega-granite"\nquery_model = "vega-granite"\n').deposit
    assert (d.encoder, d.api, d.embed_model, d.query_model) == (
        "http://localai:8080", "vega", "vega-granite", "vega-granite")


@pytest.mark.parametrize("text,why", [
    ('[deposit]\nencoder = "http://x:1"\napi = "grpc"\n', "deposit.api is one of"),
    ('[deposit]\nencoder = "http://x:1"\napi = "vega"\nembed_model = "vega-g"\n',
     "requires embed_model and query_model"),
    ('[deposit]\nencoder = "http://x:1"\napi = "vega"\n', "requires embed_model"),
    ('[deposit]\nembed_model = "g"\n', "deposit.encoder, which is not set"),
    ('[deposit]\nencoder = "http://rage/upstream/granite"\nembed_model = "other"\n',
     "must agree"),
    ('[deposit]\nencoder = "http://x:1"\nquery_model = 7\n', "is a string"),
])
def test_a_deposit_encoder_that_could_not_embed_is_refused_at_load(tmp_path, text, why):
    with pytest.raises(schedule.ScheduleError, match=why):
        load(tmp_path, text)

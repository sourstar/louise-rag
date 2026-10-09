"""tools/profile_models.py: what it derives from a profile (the load bounds
app/model_load.py reads) and the texts it sends."""
from tools import profile_models as pm


def test_a_bound_is_three_times_the_slowest_load_never_under_30_s():
    profile = {"models": {
        "vega-granite": {"cold": [{"s": 6.7, "status": 200}, {"s": 6.9, "status": 200}]},
        "louise-translate-madlad": {"cold": [{"s": 38.0, "status": 200},
                                             {"s": 16.8, "status": 200}]},
        "broken": {"cold": [{"s": 1.0, "status": 500}]},
    }}
    got = pm.bounds(profile, "x.json")["models"]
    # granite: the earlier first load from disk (9.2 s) is the slowest -> 27.6 -> floor 30
    assert got["vega-granite"] == {"cold_max_s": 9.2, "load_timeout_s": 30}
    # madlad: 38.0 s today -> 114 -> rounded up to 115
    assert got["louise-translate-madlad"] == {"cold_max_s": 38.0, "load_timeout_s": 115}
    assert "broken" not in got, "no successful cold start, no bound: the node default applies"


def test_texts_are_cut_to_the_size_asked():
    texts = pm.Texts()
    for n in (20, 300, 1000, 3000):
        for t in texts.take(4, n):
            assert 0.8 * n <= len(t) <= n

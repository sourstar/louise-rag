"""tests/hold/test_embedder_calibrate.py — `hold embedder calibrate`.

07 §7.3: the tolerance a space can bear, measured between the reference
engine and each `--against` encoder over the calibration set, before
`collection create`. 03 §3.3: the creator writes a tolerance at or below
the worst cosine. The statistics are tools/calibrate_space.py's; these
tests hold the command to them through the real client, with the HTTP
call replaced by fake encoders — no network.
"""
from __future__ import annotations

import hashlib
import json
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

import httpx
import numpy as np
import pytest

from app import embeddings, louise
from app.hold import calibration as cal

DIM = 32
CTX = 100_000          # no calibration text is cut: both lanes see it whole
W_REF = "sha256:" + "11" * 32
W_OTHER = "sha256:" + "22" * 32
IMAGE = "sha256:" + "33" * 32
REF_URL = "http://ref.test:1"
OTHER_URL = "http://other.test:1"
DOWN_URL = "http://down.test:1"


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


def _base(text: str) -> np.ndarray:
    seed = int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "little")
    return _unit(np.random.default_rng(seed).normal(size=DIM) + 0.3)


def _perturbed(text: str, scale: float) -> np.ndarray:
    """The reference vector plus a known, deterministic perturbation."""
    seed = int.from_bytes(hashlib.blake2b(b"p" + text.encode(), digest_size=8).digest(),
                          "little")
    return _unit(_base(text) + np.random.default_rng(seed).normal(scale=scale, size=DIM))


def _fake_endpoints(monkeypatch, *, scale: float = 1e-3, other_dim: int = DIM,
                    failing: dict[str, Exception] | None = None) -> list[str]:
    """`embeddings._request_vectors` answered by fake encoders by URL."""
    asked: list[str] = []
    failing = failing or {}

    def fake(batch, timeout, recipe, runs=None):
        asked.append(recipe.url)
        if recipe.url in failing:
            raise failing[recipe.url]
        if runs is not None:
            runs.add("sha256:run-" + recipe.url.split("//")[1].split(".")[0])
        if recipe.url == REF_URL:
            return [_base(t).tolist() for t in batch]
        if recipe.url == OTHER_URL:
            return [_perturbed(t, scale)[:other_dim].tolist() if other_dim <= DIM
                    else np.resize(_perturbed(t, scale), other_dim).tolist()
                    for t in batch]
        raise AssertionError(recipe.url)

    monkeypatch.setattr(embeddings, "_request_vectors", fake)
    return asked


ENGINE = """
[engine]
name = "llama.cpp"
build = "b10630"
variant = "sycl"
image = "{image}"
args = ["llama-server", "-m", "${{WEIGHTS}}", "--embedding", "--port", "${{PORT}}"]
device = "Intel(R) Arc(TM) Pro B70 Graphics"
"""


def _file(tmp_path: Path, name: str, *, url: str, weights: str | None = None,
          extra: str = "", image: str = IMAGE) -> str:
    head = f'url = "{url}"\n' + (f'weights = "{weights}"\n' if weights else "") + extra
    p = tmp_path / name
    p.write_text(head + ENGINE.format(image=image), encoding="utf-8")
    return str(p)


def _run(ref: str, *against: str, extra: list[str] | None = None) -> int:
    argv = ["hold", "embedder", "calibrate", "--encoder-weights", W_REF,
            "--engine", ref, "--ctx", str(CTX)]
    if against:
        argv += ["--against", *against]
    return louise.main(argv + (extra or []))


def _expected(scale: float) -> tuple[float, str]:
    """tools/calibrate_space.py's placement, recomputed here from the known
    perturbation: the worst cosine minus three standard deviations."""
    _, texts = cal.load("text")
    cos = np.array([float(_base(t) @ _perturbed(t, scale)) for t in texts])
    tau = float(cos.min() - 3.0 * cos.std())
    return tau, format(Decimal(repr(tau)).quantize(Decimal("0.000001"),
                                                   rounding=ROUND_FLOOR), "f")


@pytest.mark.parametrize("scale", [1e-3, 2e-2])
def test_a_known_perturbation_yields_the_expected_tolerance(tmp_path, monkeypatch,
                                                            capsys, scale):
    asked = _fake_endpoints(monkeypatch, scale=scale)
    ref = _file(tmp_path, "ref.toml", url=REF_URL)
    other = _file(tmp_path, "other.toml", url=OTHER_URL, weights=W_OTHER)
    assert _run(ref, other) == 0
    out = json.loads(capsys.readouterr().out)

    tau, tolerance = _expected(scale)
    assert out["recommendation"]["tau"] == pytest.approx(tau, abs=1e-12)
    assert out["tolerance"] == tolerance
    # at or below the worst cosine measured (03 §3.3)
    worst = out["against"][0]["cos"]["min"]
    assert Decimal(out["tolerance"]) <= Decimal(repr(worst))
    assert out["set"] == cal.load("text")[0] and out["texts"] == cal.SIZE
    assert out["dim"] == DIM
    assert out["reference"]["weights"] == W_REF
    assert out["against"][0]["weights"] == W_OTHER
    assert out["against"][0]["n_pairs_measured"] == cal.SIZE
    assert out["reference"]["runs"] == ["sha256:run-ref"]
    assert out["against"][0]["runs"] == ["sha256:run-other"]
    assert set(asked) == {REF_URL, OTHER_URL}


def test_a_larger_perturbation_bears_a_lower_tolerance(tmp_path, monkeypatch, capsys):
    ref = _file(tmp_path, "ref.toml", url=REF_URL)
    other = _file(tmp_path, "other.toml", url=OTHER_URL, weights=W_OTHER)
    got = []
    for scale in (1e-3, 5e-2):
        _fake_endpoints(monkeypatch, scale=scale)
        assert _run(ref, other) == 0
        got.append(Decimal(json.loads(capsys.readouterr().out)["tolerance"]))
    assert got[1] < got[0] < 1


def test_an_unreachable_endpoint_defers(tmp_path, monkeypatch, capsys):
    _fake_endpoints(monkeypatch, failing={DOWN_URL: httpx.ConnectError("refused")})
    ref = _file(tmp_path, "ref.toml", url=REF_URL)
    down = _file(tmp_path, "down.toml", url=DOWN_URL, weights=W_OTHER)
    assert _run(ref, down) == 3
    cap = capsys.readouterr()
    assert cap.out == "" and "down.toml" in cap.err and "unreachable" in cap.err


def test_an_endpoint_that_is_not_serving_defers(tmp_path, monkeypatch, capsys):
    req = httpx.Request("POST", DOWN_URL)
    err = httpx.HTTPStatusError("503", request=req,
                                response=httpx.Response(503, request=req, text="loading"))
    _fake_endpoints(monkeypatch, failing={DOWN_URL: err})
    ref = _file(tmp_path, "ref.toml", url=REF_URL)
    down = _file(tmp_path, "down.toml", url=DOWN_URL, weights=W_OTHER)
    assert _run(ref, down) == 3
    capsys.readouterr()


@pytest.mark.parametrize("case", [
    "no-url", "unknown-key", "bad-weights", "image-tag", "no-engine",
    "weights-disagree", "missing-file", "not-toml", "no-against",
])
def test_bad_files_are_refused(tmp_path, monkeypatch, capsys, case):
    asked = _fake_endpoints(monkeypatch)
    ref = _file(tmp_path, "ref.toml", url=REF_URL)
    other = _file(tmp_path, "other.toml", url=OTHER_URL, weights=W_OTHER)
    against = [other]
    if case == "no-url":
        other = tmp_path / "other.toml"
        other.write_text(f'weights = "{W_OTHER}"\n' + ENGINE.format(image=IMAGE))
        against = [str(other)]
    elif case == "unknown-key":
        against = [_file(tmp_path, "o2.toml", url=OTHER_URL, weights=W_OTHER,
                         extra='endpoint = "x"\n')]
    elif case == "bad-weights":
        against = [_file(tmp_path, "o2.toml", url=OTHER_URL, weights="md5:abc")]
    elif case == "image-tag":
        against = [_file(tmp_path, "o2.toml", url=OTHER_URL, weights=W_OTHER,
                         image="llama.cpp:latest")]
    elif case == "no-engine":
        p = tmp_path / "o2.toml"
        p.write_text(f'url = "{OTHER_URL}"\nweights = "{W_OTHER}"\n')
        against = [str(p)]
    elif case == "weights-disagree":
        ref = _file(tmp_path, "r2.toml", url=REF_URL, weights=W_OTHER)
    elif case == "missing-file":
        against = [str(tmp_path / "nowhere.toml")]
    elif case == "not-toml":
        p = tmp_path / "o2.toml"
        p.write_text("[engine\n")
        against = [str(p)]
    elif case == "no-against":
        against = []
    assert _run(ref, *against) == 2
    assert asked == []          # refused before anything was embedded
    capsys.readouterr()


def test_a_set_this_platform_does_not_ship_is_refused(tmp_path, monkeypatch, capsys):
    asked = _fake_endpoints(monkeypatch)
    ref = _file(tmp_path, "ref.toml", url=REF_URL)
    other = _file(tmp_path, "other.toml", url=OTHER_URL, weights=W_OTHER)
    assert _run(ref, other, extra=["--set", "sha256:" + "00" * 32]) == 2
    assert asked == []
    capsys.readouterr()


def test_the_shipped_set_named_by_its_id_is_accepted(tmp_path, monkeypatch, capsys):
    _fake_endpoints(monkeypatch)
    ref = _file(tmp_path, "ref.toml", url=REF_URL)
    other = _file(tmp_path, "other.toml", url=OTHER_URL, weights=W_OTHER)
    set_id = cal.load("text")[0]
    assert _run(ref, other, extra=["--set", set_id]) == 0
    assert json.loads(capsys.readouterr().out)["set"] == set_id


def test_a_different_width_is_refused(tmp_path, monkeypatch, capsys):
    _fake_endpoints(monkeypatch, other_dim=DIM // 2)
    ref = _file(tmp_path, "ref.toml", url=REF_URL)
    other = _file(tmp_path, "other.toml", url=OTHER_URL, weights=W_OTHER)
    assert _run(ref, other) == 2
    assert "width" in capsys.readouterr().err


def test_a_relative_path_cannot_pose_as_the_harness_lanes(tmp_path, monkeypatch, capsys):
    """tools/calibrate_space.py reads a lane named `reference/…` as its
    synthetic convention; a relative engine file there must not switch it."""
    _fake_endpoints(monkeypatch)
    (tmp_path / "reference").mkdir()
    (tmp_path / "same").mkdir()
    _file(tmp_path / "reference", "e.toml", url=REF_URL)
    _file(tmp_path / "same", "e.toml", url=OTHER_URL, weights=W_OTHER)
    monkeypatch.chdir(tmp_path)
    assert _run("reference/e.toml", "same/e.toml") == 0
    assert json.loads(capsys.readouterr().out)["tolerance"] == _expected(1e-3)[1]

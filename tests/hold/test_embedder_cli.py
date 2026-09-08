"""tests/hold/test_embedder_cli.py — `hold embedder` and its exit codes.

07 §7.7 fixes the codes and 03 §3.1 fixes the three outcomes; the pair
that matters here is that an unreachable encoder is DEFERRED (3) and
never a failure (1), or a network problem would flag a collection.
"""
from pathlib import Path

import pytest

from app import louise
from tests.hold.conftest import PLATFORM
from tests.hold.test_template import recipe


def _collection(tmp_path: Path) -> Path:
    root = tmp_path / "collections"
    from app.hold import collection as col
    col.create_from_recipe(root, "gutenberg", recipe(), platform_image=PLATFORM,
                           name="gutenberg")
    return root


def test_the_verbs_are_in_the_tree():
    p = louise.build_parser()
    for argv in (["hold", "embedder", "check", "g"],
                 ["hold", "embedder", "check", "g", "--accept"],
                 ["hold", "embedder", "benchmark", "g"],
                 ["hold", "embedder", "calibrate", "--encoder-image", "d"]):
        assert p.parse_args(argv).fn is not None


def test_check_without_a_calibration_member_is_refused_not_deferred(tmp_path):
    """Exit 2: "a rule stood in the way before anything was done" (07 §7.7).
    The member is written by `hold collection create` (09 §9.12) and without
    it there is nothing to compare against — that is not a network problem."""
    root = _collection(tmp_path)
    assert louise.main(["hold", "embedder", "check", "gutenberg",
                        "--root", str(root)]) == 2


def test_check_defers_when_no_encoder_is_configured(tmp_path, monkeypatch, capsys):
    """07 §7.8: "`EMBED_URL` unset means everything that needs an encoder …
    defers (exit 3)". 03 §3.1: inconclusive is neither a failure nor a
    pass."""
    monkeypatch.delenv("EMBED_URL", raising=False)
    root = _collection(tmp_path)
    # give it a calibration member so the deferral is about the ENCODER
    save_file = pytest.importorskip(
        "safetensors.numpy",
        reason="09 §9.12 fixes the calibration member as .safetensors; the "
               "dependency is not yet in requirements.txt (asked, not "
               "added unilaterally -- CLAUDE.md)").save_file
    from app.hold import calibration as cal, collection as col
    import numpy as np
    codex = col.read(root, "gutenberg")
    d = root / "gutenberg" / "drag" / "calibration"
    d.mkdir(parents=True, exist_ok=True)
    v = np.zeros((cal.SIZE, codex.space.dim), dtype="float32")
    save_file({"vectors": v, "envelope": np.zeros((2, codex.space.dim), "float32")},
              str(d / f"{codex.space.calibration.set.split(':')[1]}.safetensors"))
    assert louise.main(["hold", "embedder", "check", "gutenberg",
                        "--root", str(root)]) == 3
    assert "deferred" in capsys.readouterr().err


def test_calibrate_says_what_it_needs_rather_than_pretending(tmp_path):
    """It compares two encoder BUILDS over the set, so it wants two live
    endpoints — the one part of step 3 a mock cannot stand in for."""
    root = _collection(tmp_path)
    assert louise.main(["hold", "embedder", "calibrate",
                        "--encoder-image", "sha256:" + "aa" * 32,
                        "--root", str(root)]) == 2

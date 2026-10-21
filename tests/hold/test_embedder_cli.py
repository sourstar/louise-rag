"""tests/hold/test_embedder_cli.py — `hold embedder` and its exit codes.

07 §7.7 fixes the codes and 03 §3.1 fixes the three outcomes; the pair
that matters here is that an unreachable encoder is DEFERRED (3) and
never a failure (1), or a network problem would flag a collection.
"""
from pathlib import Path

import pytest

from app import louise
from tests.hold.conftest import fake_query_embed  # noqa: E402,F811
from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed
from tests.hold.test_template import recipe


def _collection(tmp_path: Path) -> Path:
    root = tmp_path / "collections"
    from app.hold import collection as col
    col.create_from_recipe(root, "gutenberg", recipe(), platform_image=PLATFORM,
                           embed=fake_embed, embed_query=fake_query_embed, name="gutenberg", key=KEY, now=NOW)
    return root


def test_the_verbs_are_in_the_tree():
    p = louise.build_parser()
    for argv in (["hold", "embedder", "check", "g"],
                 ["hold", "embedder", "check", "g", "--accept"],
                 ["hold", "embedder", "benchmark", "g"],
                 ["hold", "embedder", "calibrate", "--encoder-weights", "d",
                  "--engine", "e.toml", "--against", "f.toml", "--ctx", "512"]):
        assert p.parse_args(argv).fn is not None


def test_check_without_a_calibration_member_is_refused_not_deferred(tmp_path, monkeypatch):
    """Exit 2: "a rule stood in the way before anything was done" (07 §7.7).
    The member is written by `hold collection create` (09 §9.12) and without
    it there is nothing to compare against — that is not a network problem.

    ON A NODE THAT NAMES NO ENCODER, deliberately: this passed on a checkout
    whose own `collections/gutenberg.toml` happened to name one, and failed
    on the reference node, where the encoder was looked for before the member
    and the command read "deferred" (2026-09-19). The member comes first."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "no-config-here"))
    root = _collection(tmp_path)
    # `create` wrote it; a SHARD that lost it is the case under test.
    member, = (root / "gutenberg" / "shard" / "calibration").glob("*.safetensors")
    member.unlink()
    assert louise.main(["hold", "embedder", "check", "gutenberg",
                        "--root", str(root)]) == 2


def test_check_defers_when_no_encoder_is_configured(tmp_path, monkeypatch, capsys):
    """07 §7.8: "`EMBED_URL` unset means everything that needs an encoder …
    defers (exit 3)". 03 §3.1: inconclusive is neither a failure nor a
    pass."""
    monkeypatch.delenv("EMBED_URL", raising=False)
    root = _collection(tmp_path)
    # give it a calibration member so the deferral is about the ENCODER
    from safetensors.numpy import save_file
    from app.hold import calibration as cal, collection as col
    import numpy as np
    codex = col.read(root, "gutenberg")
    d = root / "gutenberg" / "shard" / "calibration"
    d.mkdir(parents=True, exist_ok=True)
    v = np.zeros((cal.SIZE, codex.space.dim), dtype="float32")
    save_file({"vectors": v, "envelope": np.zeros((2, codex.space.dim), "float32")},
              str(d / f"{codex.space.calibration.set.split(':')[1]}.safetensors"))
    assert louise.main(["hold", "embedder", "check", "gutenberg",
                        "--root", str(root)]) == 3
    assert "deferred" in capsys.readouterr().err


def test_calibrate_names_the_engine_it_measures():
    """07 §7.3: `--encoder-weights D --engine FILE`. The Codex-version-2 shape
    (`--encoder-image`) is never stated anew (09 §9.3), so it is not a flag;
    tests/hold/test_embedder_calibrate.py runs the command itself."""
    with pytest.raises(SystemExit) as e:
        louise.build_parser().parse_args(
            ["hold", "embedder", "calibrate", "--encoder-image", "sha256:" + "aa" * 32])
    assert e.value.code == 2


# ---- 07 §7.6: the previous names, retired ------------------------------------

def test_the_previous_names_are_retired(capsys):
    """07 §7.6's aliases lived one release and were retired with app/cli.py
    (2026-10-08): an old name is now an unknown command, exit 2 from
    argparse, never a silent second implementation."""
    for old in (["pack", "build", "g"], ["corpus", "status", "g"],
                ["snapshot", "record", "g"], ["snapshot", "list", "g"]):
        with pytest.raises(SystemExit) as e:
            louise.build_parser().parse_args(old)
        assert e.value.code == 2
    capsys.readouterr()


def test_check_prints_the_whole_set_per_role(tmp_path, monkeypatch, capsys):
    """The operator's cross-vendor reference (approved-encoders.md, 2026-10-21)
    reads all 512 cosines per role; the per-role check printed only the
    quorum's (Perugia found the line missing on gutenberg)."""
    from types import SimpleNamespace
    from app import run_check
    from app.hold import collection as col, runs as hold_runs
    root = _collection(tmp_path)
    codex = col.read(root, "gutenberg")
    assert hold_runs.applies(codex)
    monkeypatch.setattr(louise, "_recipe_for", lambda codex, name: object())
    monkeypatch.setattr(hold_runs, "passing", lambda body, codex: True)
    cosines = (0.9990,) * 500 + (0.9932,) * 12

    def validate(root_, name, codex_, recipe, *, role, on_run=None, **kw):
        on_run(SimpleNamespace(all_cosines=cosines))
        return {"space_id": codex.space_id, "run": "sha256:" + "ab" * 32, "engine": {},
                "driver": "cpu", "passed": 16, "sampled": list(range(16)),
                "worst": "0.999000", "median": "0.999000", "fingerprint": "f",
                "batch_invariant": True, "tokens_per_s": 1}
    monkeypatch.setattr(run_check, "validate", validate)
    assert louise.main(["hold", "embedder", "check", "gutenberg", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    roles = 2 if codex.space.query.encoder is not None else 1
    assert out.count("whole set   worst 0.993200, median 0.999000, below 0.995: 12 of 512") == roles

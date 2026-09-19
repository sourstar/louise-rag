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
                 ["hold", "embedder", "calibrate", "--encoder-image", "d"]):
        assert p.parse_args(argv).fn is not None


def test_check_without_a_calibration_member_is_refused_not_deferred(tmp_path):
    """Exit 2: "a rule stood in the way before anything was done" (07 §7.7).
    The member is written by `hold collection create` (09 §9.12) and without
    it there is nothing to compare against — that is not a network problem."""
    root = _collection(tmp_path)
    # `create` wrote it; a drag that lost it is the case under test.
    member, = (root / "gutenberg" / "drag" / "calibration").glob("*.safetensors")
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


# ---- 07 §7.6's deprecation aliases ----------------------------------------

class TestDeprecatedNames:
    """07 §7.6: "Old names are accepted for one release as hidden aliases —
    `louise <old …>` and `python -m app.cli <old …>` both — each dispatching
    to the new function and printing `deprecated: use louise <new …>` on
    stderr."

    Steps 0 to 3 landed without a single one of these, and step 12's opening
    instruction is "remove the aliases" — of which there were none. These
    cover the `louise <old …>` half for every previous name whose new
    command exists on this branch.
    """

    PAIRS = [
        (["pack", "build"], "hold records build"),
        (["corpus", "status"], "hold collection show"),
        (["snapshot", "record"], "hold snapshot create"),
        (["snapshot", "list"], "hold snapshot list"),
    ]

    def test_every_alias_parses(self):
        p = louise.build_parser()
        for old, _ in self.PAIRS:
            assert p.parse_args(old + ["a-collection"]).fn is not None

    def test_an_alias_dispatches_to_the_same_function(self):
        """ONE function, two names — not a second implementation that can
        drift from the first."""
        p = louise.build_parser()
        alias = p.parse_args(["pack", "build", "g"])
        new = p.parse_args(["hold", "records", "build", "g"])
        assert alias.fn is not new.fn, "the alias wraps it to warn"
        assert alias.fn.__closure__ is not None
        wrapped = [c.cell_contents for c in alias.fn.__closure__]
        assert new.fn in wrapped, "and the thing it wraps IS the new function"

    def test_an_alias_takes_the_same_arguments(self):
        """Built from one argument definition, so the two cannot drift."""
        p = louise.build_parser()
        a = p.parse_args(["pack", "build", "g", "--corpus", "/c",
                          "--shard-records", "7"])
        b = p.parse_args(["hold", "records", "build", "g", "--corpus", "/c",
                          "--shard-records", "7"])
        for field in ("collection", "corpus", "records_per_shard", "snapshot_id"):
            assert getattr(a, field) == getattr(b, field)

    def test_an_alias_names_its_replacement_on_stderr(self, tmp_path, capsys):
        """The line is the whole point: a user on an old name learns the new
        one without reading a document."""
        root = _collection(tmp_path)
        assert louise.main(["snapshot", "list", "gutenberg",
                            "--root", str(root)]) == 0
        assert "deprecated: use louise hold snapshot list" in capsys.readouterr().err

    def test_the_aliases_are_hidden_from_help(self, capsys):
        """"hidden aliases" — the old names work and `--help` does not teach
        them to anyone new. Note `argparse.SUPPRESS` does NOT do this for a
        subparser: it prints the literal `==SUPPRESS==`. Omitting `help`
        entirely is what leaves a subparser unlisted."""
        with pytest.raises(SystemExit):
            louise.build_parser().parse_args(["--help"])
        out = capsys.readouterr().out
        assert "hold" in out
        for word in ("pack", "corpus", "snapshot", "SUPPRESS"):
            assert word not in out

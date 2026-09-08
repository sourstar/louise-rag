"""tests/hold/test_calibration_member.py — `create` embeds the set.

docs/06-plugins.md §6.6 step 2: "the calibration comes first and needs no
record: the calibration set is a corpus-independent file shipped with the
platform, `create` embeds it with the named encoder, writes the reference
vectors' hash into `space.calibration` and their envelope into
`space.envelope`, and stores the vectors as the drag member
`calibration/<set>.safetensors`". docs/09-formats.md §9.12 fixes the member.

These exist because the two hashes in a Codex's space block are the only
thing binding what a peer embeds to what the creator embedded, and every way
of getting them wrong is silent: the Codex validates, the collection is
created, and the first stranger's gate then fails for no stated reason.
"""
from pathlib import Path

import pytest
from nacl import signing

from app.hold import (calibration as cal, collection as col, drag,
                      envelope as env, template)
from app.hold.calibration import CalibrationError
from app.hold.codex import CodexError
from app.hold.template import TemplateError
from tests.hold.conftest import CAL_SET, DIM, PLATFORM, fake_embed
from tests.hold.test_template import recipe

NOW = "2026-09-08T00:00:00Z"
A_HASH = "sha256:" + "aa" * 32


def created(root: Path, **kw):
    """The Codex and its drag, with the member `create` wrote in it."""
    codex = col.create_from_recipe(root, "gutenberg", recipe(),
                                   platform_image=PLATFORM, embed=fake_embed,
                                   **kw)
    return codex, root / "gutenberg" / "drag"


def paths(codex):
    return drag.calibration_paths(codex.space.calibration.set)


def member_of(drag_dir: Path, codex):
    return cal.read_member(drag_dir / paths(codex)[0],
                           expect_vectors=codex.space.calibration.vectors,
                           expect_envelope=codex.space.envelope)


# ---- the two hashes are outputs, never recipe inputs ----------------------

def test_a_recipe_may_not_assert_the_reference_vectors():
    """A recipe author has not run the encoder. If they could name the hash
    anyway it would simply be believed: these two ARE the reference, and no
    later check recomputes them from anything else."""
    space = dict(recipe()["space"])
    space["calibration"] = dict(space["calibration"], vectors=A_HASH)
    with pytest.raises(TemplateError, match="never computed"):
        template.from_recipe("gutenberg", recipe(space=space),
                             platform_image=PLATFORM,
                             calibration=(A_HASH, A_HASH))


def test_a_recipe_may_not_assert_the_envelope():
    space = dict(recipe()["space"], envelope=A_HASH)
    with pytest.raises(TemplateError, match="never computed"):
        template.from_recipe("gutenberg", recipe(space=space),
                             platform_image=PLATFORM,
                             calibration=(A_HASH, A_HASH))


def test_a_recipe_must_name_a_set(tmp_path: Path):
    space = dict(recipe()["space"])
    space["calibration"] = {"quorum": {"n": 16, "min_pass": 12}}
    with pytest.raises(CodexError, match="calibration.set"):
        col.create_from_recipe(tmp_path, "gutenberg", recipe(space=space),
                               platform_image=PLATFORM, embed=fake_embed)


def test_a_set_this_platform_does_not_ship_is_refused(tmp_path: Path):
    """The recipe names the set by hash and the platform holds one file per
    modality. Embedding a different 512 texts and calling the result a
    reference would compare nothing."""
    space = dict(recipe()["space"])
    space["calibration"] = dict(space["calibration"], set="sha256:" + "ff" * 32)
    with pytest.raises(CalibrationError, match="calibration set is"):
        col.create_from_recipe(tmp_path, "gutenberg", recipe(space=space),
                               platform_image=PLATFORM, embed=fake_embed)
    assert not list(tmp_path.iterdir()), "nothing is created"


# ---- what create writes ---------------------------------------------------

def test_create_embeds_the_set_and_records_its_own_hashes(tmp_path: Path):
    codex, d = created(tmp_path)
    vectors, envelope = member_of(d, codex)
    assert vectors.shape == (cal.SIZE, DIM)
    assert envelope.shape == (2, DIM)


def test_the_whole_set_is_embedded(tmp_path: Path):
    """Not a sample: T0's envelope, T1's probe set and the statement all
    report over the whole 512, so embedding fewer would leave the three
    disagreeing about what was measured."""
    seen = {}

    def embed(texts):
        seen["n"] = len(texts)
        return fake_embed(texts)

    col.create_from_recipe(tmp_path, "gutenberg", recipe(),
                           platform_image=PLATFORM, embed=embed)
    assert seen["n"] == cal.SIZE == 512


def test_the_envelope_is_the_mean_and_the_deviation(tmp_path: Path):
    """09 §9.12: "row 0 the per-dimension mean of `vectors`, row 1 the
    per-dimension standard deviation". It is what T0 turns a stranger's
    bundle away on before anything more expensive runs (03 §3.5)."""
    import numpy as np
    codex, d = created(tmp_path)
    vectors, envelope = member_of(d, codex)
    assert np.allclose(envelope[0], vectors.mean(axis=0), atol=1e-6)
    assert np.allclose(envelope[1], vectors.std(axis=0), atol=1e-6)


def test_the_member_carries_09_9_12s_headers(tmp_path: Path):
    from safetensors import safe_open
    codex, d = created(tmp_path)
    with safe_open(str(d / paths(codex)[0]), framework="numpy") as f:
        meta = f.metadata()
    assert meta["set"] == codex.space.calibration.set
    assert meta["encoder_image"] == codex.space.encoder.image
    assert meta["encoder_weights"] == codex.space.encoder.weights


def test_the_member_is_named_for_the_set_not_the_collection(tmp_path: Path):
    """Two collections on one encoder share one calibration (03 §3.3), so
    the file is keyed on the set and not on who is holding it."""
    codex, _ = created(tmp_path)
    assert paths(codex)[0] == f"calibration/{cal.segment(CAL_SET)}.safetensors"


# ---- the normalisation is the space's, not the endpoint's -----------------

def test_normalise_is_applied_here_and_not_left_to_the_endpoint(tmp_path: Path):
    """`normalise` is inside `space_id`, so it is a property of the space and
    not of whichever deployment answered. An endpoint that returns vectors of
    another scale must land in the same place.

    Note what this asserts and what it does not. The two references agree to
    within float32 rounding, NOT byte for byte: `v/‖v‖` on a vector already
    scaled by seven rounds differently in the last bits. That is exactly why
    03 §3.3 gates on a cosine against a `tolerance` and never on a hash — a
    hash comparison here would fail two honest builds of the same encoder.
    """
    import numpy as np

    def scaled(texts):
        return [[x * 7.0 for x in v] for v in fake_embed(texts)]

    plain, plain_drag = created(tmp_path / "a")
    other = tmp_path / "b"
    codex = col.create_from_recipe(other, "gutenberg", recipe(),
                                   platform_image=PLATFORM, embed=scaled)
    a, _ = member_of(plain_drag, plain)
    b, _ = member_of(other / "gutenberg" / "drag", codex)
    assert np.allclose(np.linalg.norm(b, axis=1), 1.0, atol=1e-5), "unit"
    assert np.allclose((a * b).sum(axis=1), 1.0, atol=1e-5), "the same directions"


# ---- what the encoder may not return --------------------------------------

def test_an_encoder_that_returns_the_wrong_count_creates_nothing(tmp_path: Path):
    with pytest.raises(CalibrationError, match="returned 3 vectors"):
        col.create_from_recipe(tmp_path, "gutenberg", recipe(),
                               platform_image=PLATFORM,
                               embed=lambda t: fake_embed(t[:3]))
    assert not list(tmp_path.iterdir())


def test_an_encoder_that_returns_the_wrong_dimension_creates_nothing(tmp_path: Path):
    with pytest.raises(CalibrationError, match=r"the space is \(512, 768\)"):
        col.create_from_recipe(tmp_path, "gutenberg", recipe(),
                               platform_image=PLATFORM,
                               embed=lambda t: fake_embed(t, dim=384))
    assert not list(tmp_path.iterdir())


# ---- reading it back ------------------------------------------------------

def test_a_member_whose_tensors_hash_otherwise_is_refused(tmp_path: Path):
    """09 §9.12. The Codex's hashes bind the member, so a file that is not
    the one the Codex names compares nothing however well-formed it is."""
    import numpy as np
    from safetensors.numpy import save_file
    codex, d = created(tmp_path)
    save_file({"vectors": np.zeros((cal.SIZE, DIM), "float32"),
               "envelope": np.zeros((2, DIM), "float32")},
              str(d / paths(codex)[0]))
    with pytest.raises(CalibrationError, match="hashes to"):
        member_of(d, codex)


def test_a_member_missing_a_tensor_is_refused(tmp_path: Path):
    import numpy as np
    from safetensors.numpy import save_file
    codex, d = created(tmp_path)
    save_file({"vectors": np.zeros((cal.SIZE, DIM), "float32")},
              str(d / paths(codex)[0]))
    with pytest.raises(CalibrationError, match="no `envelope` tensor"):
        member_of(d, codex)


# ---- the signature beside it ----------------------------------------------

def test_a_key_writes_a_signature_that_verifies(tmp_path: Path):
    """09 §9.12: a signature rests beside the member. It says who produced
    the reference — "the Codex's hashes, not the signature, are what bind
    it" — so it is optional and the hashes never are."""
    import json
    key = signing.SigningKey(b"\x05" * 32)
    codex, d = created(tmp_path, key=key, now=NOW)
    body = env.verify(env.ATTESTATION,
                      json.loads((d / paths(codex)[1]).read_text(encoding="utf-8")))
    assert body["vectors"] == codex.space.calibration.vectors
    assert body["envelope"] == codex.space.envelope
    assert body["space_id"] == codex.space_id
    assert body["time"] == NOW


def test_the_member_stands_without_a_signature(tmp_path: Path):
    codex, d = created(tmp_path)
    assert not (d / paths(codex)[1]).exists()
    member_of(d, codex), "and it still reads and verifies"


def test_signing_needs_a_time_and_never_reads_the_clock(tmp_path: Path):
    """The rule the snapshots already follow: nothing under `hold` reads the
    clock, so a signed body's `time` is always something a caller decided
    and a test can therefore pin."""
    with pytest.raises(CodexError, match="needs the time"):
        col.create_from_recipe(tmp_path, "gutenberg", recipe(),
                               platform_image=PLATFORM, embed=fake_embed,
                               key=signing.SigningKey(b"\x05" * 32))


# ---- creating twice -------------------------------------------------------

def test_re_creating_writes_the_same_member(tmp_path: Path):
    """Idempotence is by identity (07 §7.3). The set, the encoder and the
    normalisation are all fixed, so the second run's vectors are the first
    run's vectors and the member is byte for byte unchanged."""
    a, d = created(tmp_path)
    before = (d / paths(a)[0]).read_bytes()
    b, _ = created(tmp_path)
    assert a.collection_id == b.collection_id
    assert (d / paths(b)[0]).read_bytes() == before


def test_the_hashes_are_not_inside_space_id(tmp_path: Path):
    """02 §2.4 lists what `space_id` is computed over and calibration is not
    in it — which is what lets two nodes with the same encoder build land in
    one space even though their reference vectors differ in the last bit."""
    import dataclasses
    codex, _ = created(tmp_path)
    moved = dataclasses.replace(
        codex.space, envelope=A_HASH,
        calibration=dataclasses.replace(codex.space.calibration, vectors=A_HASH))
    assert dataclasses.replace(codex, space=moved).space_id == codex.space_id


def test_the_member_bytes_are_deterministic(tmp_path: Path):
    """Measured, not assumed: `safetensors.save_file` serialises its header
    from a Rust hash map and six identical calls produce six different files.
    A member's transport root is a pieces root over the FILE (09 §9.4), so a
    header that reorders itself would give two honest nodes with the same
    encoder snapshots that disagree about a member neither of them changed."""
    import numpy as np
    from app.hold import calibration as c
    v = np.zeros((4, 3), "float32")
    e = np.ones((2, 3), "float32")
    out = set()
    for i in range(6):
        p = tmp_path / f"{i}.safetensors"
        c.write_member(p, v, e, set_id_="sha256:aa", encoder_image="i",
                       encoder_weights="w", key="")
        out.add(p.read_bytes())
    assert len(out) == 1


def test_a_canonicalised_member_still_reads_as_safetensors(tmp_path: Path):
    """The header is re-serialised, not the data: `data_offsets` are relative
    to the start of the buffer, so safetensors' own reader must still load
    it — which is the only thing that makes the rewrite safe."""
    codex, d = created(tmp_path)
    vectors, envelope = member_of(d, codex)
    assert vectors.shape == (cal.SIZE, DIM) and envelope.shape == (2, DIM)

"""Invariant 9 on the three `hold` commands that never asked.

Found by the verification pass of the `louise health` review, 2026-09-13:
`hold vectors export` builds a shareable bundle, `hold vectors check`
embeds sampled text (and `--drop` deletes vectors), `hold check` embeds
sampled text and writes attestations made to be exchanged. `hold vectors
import` and `hold vectors build` already asked `licensing.require_ack`;
these three did not.

The refusal is `LicenseAckRequiredError`, which `louise.main` turns into
07 §7.7's exit 2. The control proves each command otherwise gets past the
gate to its next step, so what refuses is the gate and not a stub.
"""
import argparse
import types

import pytest

from app import licensing, louise
from app.acquire.spec import LicenseAckRequiredError
from app.hold import collection as col, records as rec


class _Reached(Exception):
    """The step after the gate."""


@pytest.fixture
def args(monkeypatch, tmp_path):
    monkeypatch.setattr(col, "resolve", lambda root, name: (name, object()))
    # a stand-in carrying the one attribute the refusal message names
    monkeypatch.setattr(rec, "local_collection",
                        lambda root, name, codex, **k: types.SimpleNamespace(name=name))

    def reached(*a, **k):
        raise _Reached()

    monkeypatch.setattr(louise, "_recipe_for", reached)
    monkeypatch.setattr(louise, "_latest_snapshot", reached)
    monkeypatch.setattr(licensing, "terms_path_for", lambda c: None)
    monkeypatch.setattr(licensing, "license_class_for", lambda c: "yellow")
    return argparse.Namespace(root=tmp_path, collection="pm", snapshot=None, seed=None)


@pytest.mark.parametrize("command", ["cmd_vectors_export", "cmd_vectors_check", "cmd_check"])
def test_a_withheld_collection_is_refused_before_anything_is_read(args, monkeypatch, command):
    monkeypatch.setattr(licensing, "state_for", lambda c: licensing.MISSING)
    with pytest.raises(LicenseAckRequiredError, match="invariant 9"):
        getattr(louise, command)(args)

    monkeypatch.setattr(licensing, "state_for", lambda c: licensing.NOT_REQUIRED)
    with pytest.raises(_Reached):
        getattr(louise, command)(args)


@pytest.mark.parametrize("command", ["cmd_vectors_build", "cmd_share_contribute"])
def test_embedding_for_a_withheld_collection_is_refused(args, monkeypatch, command):
    """`hold vectors build` embeds the text; `share contribute` wraps it and
    shares the bundles. The retired `snapshot vectors` and `contribute`
    each had a test of their gate (app/cli.py, 2026-09-13); these are their
    successors', and `share contribute` had none — the gate was left to
    quarantine, which only reports on a node that does not enforce it."""
    from app import copy_state, identity
    from app.bundles import keys as node_keys

    def reached(*a, **k):
        raise _Reached()

    monkeypatch.setattr(col, "resolve",
                        lambda root, name: (name, types.SimpleNamespace(privacy="public")))
    monkeypatch.setattr(louise, "_take_write_lock", lambda *a, **k: None)
    monkeypatch.setattr(copy_state, "require_in_use", reached)   # the step after the gate
    monkeypatch.setattr(node_keys, "load_signing_key", lambda *a, **k: object())
    monkeypatch.setattr(identity, "own", lambda: "ed25519:" + "a" * 64)
    monkeypatch.setattr(louise.signal, "signal", lambda *a: None)
    a = argparse.Namespace(**vars(args), delta=None, shard=None, slice=None, dry_run=False,
                           once=True, interval=900)

    monkeypatch.setattr(licensing, "state_for", lambda c: licensing.MISSING)
    with pytest.raises(LicenseAckRequiredError, match="invariant 9"):
        getattr(louise, command)(a)

    monkeypatch.setattr(licensing, "state_for", lambda c: licensing.NOT_REQUIRED)
    with pytest.raises(_Reached):
        getattr(louise, command)(a)


def test_a_codex_that_tightens_its_plugin_is_judged_by_its_codex(isolated_config, tmp_path):
    """gutenberg's plugin is green; this collection's Codex tightens it to
    yellow (04 §4.4). THE resolution read only the plugin's registry class,
    so every gate but the build's presence check judged it green — and an
    ack.json whose hash no longer matched the pinned terms passed that
    presence check (audit, 2026-09-13)."""
    import hashlib
    import json

    from app.hold import records as rec_real
    from tests.hold.test_build_and_snapshot import _collection

    body = b"Terms this collection tightens to.\n"
    terms = tmp_path / "TERMS.txt"
    terms.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    root = _collection(tmp_path, license={
        "class": "yellow", "terms_sha256": "sha256:" + digest, "terms_path": str(terms)})
    name, codex = col.resolve(root, "gutenberg")
    state = root / "gutenberg" / "state"
    state.mkdir(parents=True, exist_ok=True)

    (state / "ack.json").write_text(json.dumps(
        {"time": "2026-09-13T00:00:00Z", "terms_sha256": "sha256:" + "0" * 64}))
    with pytest.raises(LicenseAckRequiredError, match="license_class=yellow"):
        rec_real.local_collection(root, name, codex)        # a stale ack: refused

    (state / "ack.json").write_text(json.dumps(
        {"time": "2026-09-13T00:00:00Z", "terms_sha256": "sha256:" + digest}))
    local = rec_real.local_collection(root, name, codex)    # the pinned terms: accepted
    assert licensing.license_class_for(local) == "yellow"
    assert licensing.state_for(local) == licensing.ACKNOWLEDGED


def test_a_snapshot_is_not_signed_for_a_collection_whose_ack_went_stale(isolated_config,
                                                                         tmp_path):
    """`hold snapshot create` signs what is published with the SHARD; its
    input exists only after a gated build, but the terms can change in
    between (audit, 2026-09-13) — the gate is asked again."""
    import hashlib
    import json

    from app.hold import snapshot as sn
    from app.hold.codex import CodexError
    from tests.hold.test_build_and_snapshot import _collection

    body = b"Terms this collection tightens to.\n"
    terms = tmp_path / "TERMS.txt"
    terms.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    root = _collection(tmp_path, license={
        "class": "yellow", "terms_sha256": "sha256:" + digest, "terms_path": str(terms)})
    state = root / "gutenberg" / "state"
    state.mkdir(parents=True, exist_ok=True)

    (state / "ack.json").write_text(json.dumps(
        {"time": "2026-09-13T00:00:00Z", "terms_sha256": "sha256:" + "0" * 64}))
    with pytest.raises(LicenseAckRequiredError):
        sn.create(root, "gutenberg", snapshot_id="2026-09", log=lambda *_: None)

    (state / "ack.json").write_text(json.dumps(
        {"time": "2026-09-13T00:00:00Z", "terms_sha256": "sha256:" + digest}))
    with pytest.raises(CodexError, match="no build to freeze"):   # control: past the gate
        sn.create(root, "gutenberg", snapshot_id="2026-09", log=lambda *_: None)

"""Mechanical guards for CLAUDE.md invariants that were previously enforced
by review alone. Each is cheap, and each turns a rule into a failing test
the moment it drifts.

- Invariant 7 (GPU boundary): nothing under app/ imports torch / CUDA /
  SYCL. The DNABERT-2 server under deploy/ is its own container and is
  deliberately outside this walk.
- Invariant 8 (secrets): ADMIN_TOKEN_FILE (a path) wins over ADMIN_TOKEN
  (a value); a set-but-unreadable file is a startup error that names the
  path and never the material.
- Invariant 9 (license gates): the acknowledgement is sha256 over the
  terms text, so TERMS.txt is byte-exact — a CRLF checkout would silently
  invalidate every recorded ack. .gitattributes guards it; this asserts it.
- Lance: only embeddings, vector_index and spaces open it — one layer,
  one maintenance regime, for every collection.
- Logging: every stdout line names its collection (db.ManifestConnection),
  because seven collections share one worker stdout.
"""
import re
from pathlib import Path

import pytest

from app import collections as collections_mod, config, db

APP = Path(__file__).resolve().parent.parent / "app"

_GPU_IMPORT = re.compile(
    r"^\s*(?:import|from)\s+(?:torch|intel_extension_for_pytorch|cuda|pycuda|sycl)\b",
    re.MULTILINE)


def test_app_never_imports_a_gpu_stack():
    offenders = [str(p.relative_to(APP.parent))
                 for p in APP.rglob("*.py") if _GPU_IMPORT.search(p.read_text(encoding="utf-8"))]
    assert offenders == [], f"invariant 7: GPU stack imported by {offenders}"


# Lance is ONE layer for every collection: vectors are written by
# embeddings, maintained (prune, compact, refresh) by vector_index, and
# checked against the Codex by spaces. A plugin or stage that opened Lance
# itself would be a second maintenance regime nobody schedules.
_LANCE_LAYER = {"app/embeddings.py", "app/spaces.py", "app/vector_index.py"}
_LANCE_TOUCH = re.compile(
    r"^\s*(?:import|from)\s+(?:lancedb|lance)\b|lancedb\.connect\(|\.to_lance\(",
    re.MULTILINE)


def test_lance_is_managed_in_one_place_for_every_collection():
    touching = {p.relative_to(APP.parent).as_posix()
                for p in APP.rglob("*.py") if _LANCE_TOUCH.search(p.read_text(encoding="utf-8"))}
    assert touching - _LANCE_LAYER == set(), \
        f"Lance opened outside the common layer: {sorted(touching - _LANCE_LAYER)}"


def test_admin_token_file_wins_over_the_env_value(tmp_path):
    f = tmp_path / "admin.token"
    f.write_text("from-file\n", encoding="utf-8")
    assert config._resolve_admin_token(
        {"ADMIN_TOKEN_FILE": str(f), "ADMIN_TOKEN": "from-env"}) == "from-file"
    assert config._resolve_admin_token({"ADMIN_TOKEN": "from-env"}) == "from-env"
    assert config._resolve_admin_token({}) == ""


def test_unreadable_admin_token_file_is_a_startup_error_naming_the_path(tmp_path):
    missing = tmp_path / "nope.token"
    with pytest.raises(RuntimeError) as e:
        config._resolve_admin_token({"ADMIN_TOKEN_FILE": str(missing),
                                     "ADMIN_TOKEN": "must-not-be-used"})
    assert str(missing) in str(e.value)
    assert "must-not-be-used" not in str(e.value)


def test_terms_texts_are_byte_exact_lf():
    terms = sorted(APP.glob("plugins/*/TERMS.txt"))
    assert terms, "no TERMS.txt found — the glob is wrong, not the corpus"
    crlf = [str(p.relative_to(APP.parent)) for p in terms if b"\r" in p.read_bytes()]
    assert crlf == [], f"invariant 9: CRLF in terms text would change every ack hash: {crlf}"


def test_log_lines_name_their_collection(tmp_path, capsys):
    c = collections_mod.Collection(
        name="beta", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "beta" / "manifest.db",
        fts_dir=tmp_path / "beta" / "fts", lance_dir=tmp_path / "beta" / "lancedb")
    con = db.connect(c)
    assert isinstance(con, db.ManifestConnection) and con.collection_name == "beta"
    db.log_event(con, "info", "fts", "indexed 200 documents")
    out = capsys.readouterr().out
    assert re.search(r"\[beta info:fts\] indexed 200 documents", out), out

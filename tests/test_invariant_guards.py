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


# ONE entry point to the encoder (docs/notes/input-forms.md, operator
# 2026-10-08): a chunk's input is built from its layer's form by
# `embeddings.embed_chunks`, and the low-level client is private to
# app/embeddings.py. A caller that fed it a text directly could feed a pair
# layer's chunk as text — the very drift the 0.95 tolerance hid on pubmed.
# Non-chunk texts go through `embed_query` / `embed_calibration`.
_PRIVATE_CLIENT = {"_embed_texts", "_embed_batch", "_embed_one_at_a_time",
                   "_request_vectors",
                   # the public names they had, so neither comes back
                   "embed_texts", "embed_one_at_a_time"}
TOOLS = APP.parent / "tools"


def _names_used(path: Path) -> set[str]:
    """Every NAME token of the file — code only: comments and strings are
    not tokens of this kind, so a docstring may still name the client."""
    import io
    import tokenize
    src = path.read_text(encoding="utf-8")
    return {tok.string for tok in tokenize.generate_tokens(io.StringIO(src).readline)
            if tok.type == tokenize.NAME}


def test_the_embedding_client_is_private_to_embeddings():
    own = APP / "embeddings.py"
    offenders = sorted(
        f"{p.relative_to(APP.parent).as_posix()}: {', '.join(sorted(used))}"
        for root in (APP, TOOLS) for p in root.rglob("*.py")
        if p != own and (used := _names_used(p) & _PRIVATE_CLIENT))
    assert offenders == [], (
        "the encoder is reached through embeddings.embed_chunks (a chunk, in its "
        "layer's form) or embed_query / embed_calibration (not a chunk, by design); "
        f"these call the client directly: {offenders}")


def test_the_guard_sees_a_call_and_not_a_comment(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("# embeddings._embed_texts in a comment\n'_embed_batch in a string'\n",
                 encoding="utf-8")
    assert _names_used(p) & _PRIVATE_CLIENT == set()
    p.write_text("from app import embeddings\nembeddings._embed_texts(['t'])\n",
                 encoding="utf-8")
    assert _names_used(p) & _PRIVATE_CLIENT == {"_embed_texts"}


def test_no_route_needs_python_multipart_unless_it_is_a_declared_dependency():
    """FastAPI registers a `Form`/`File`/`UploadFile` route only where
    python-multipart is installed, else `app.api` fails at import and the web
    does not start. The package is not in requirements.txt; a container that
    happens to carry it hid exactly that (2026-10-19: the node's build
    refused a tip the suite here had passed). Uploads are raw bodies with
    query parameters instead — the bundle import, the claims file."""
    requirements = (APP.parent / "requirements.txt").read_text(encoding="utf-8").lower()
    declared = "python-multipart" in requirements or "python_multipart" in requirements
    if declared:
        return
    uses = []
    for path in sorted(APP.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"\bfrom fastapi import[^\n]*\b(Form|File|UploadFile)\b", text) or \
                re.search(r"(?<![\w.])(Form|File)\(|\bUploadFile\b", text):
            uses.append(path.relative_to(APP).as_posix())
    assert uses == [], f"routes needing python-multipart, which requirements.txt does not declare: {uses}"

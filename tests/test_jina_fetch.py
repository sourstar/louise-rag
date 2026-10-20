"""tests/test_jina_fetch.py — deploy/jina-embeddings-v3/fetch.py: the jina-v3
weights folder is the SAME folder wherever it is written, and its digest is
the platform's (09 §9.3), because that digest is inside `space_id`."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

from app.vega import runs

ROOT = Path(__file__).resolve().parents[1]


def _exec(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module          # a dataclass under `from __future__ import annotations` looks itself up here
    spec.loader.exec_module(module)
    return module


fetch = _exec("jina_fetch_under_test", ROOT / "deploy" / "jina-embeddings-v3" / "fetch.py")
server = _exec("jina_v3_under_fetch_test", ROOT / "deploy" / "jina-embeddings-v3" / "server.py")

CODE_COMMIT = "bd55a5e" + "0" * 33


def blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# ---- the layout -----------------------------------------------------------------

def test_the_layout_is_the_weights_at_the_root_and_the_code_as_an_offline_hub_cache():
    plan = fetch.layout(CODE_COMMIT)
    assert set(fetch.WEIGHTS_FILES) <= set(plan)
    snap = f"hf-cache/models--jinaai--xlm-roberta-flash-implementation/snapshots/{CODE_COMMIT}"
    assert {f"{snap}/{f}" for f in fetch.CODE_FILES} <= set(plan)
    assert fetch.ref_path(CODE_COMMIT) == \
        "hf-cache/models--jinaai--xlm-roberta-flash-implementation/refs/main"
    # what the load reads and nothing else: no second copy of the tensors, no
    # other runtime, no sentence-transformers wrapper, no README
    for absent in ("pytorch_model.bin", "onnx/model.onnx", "modules.json", "custom_st.py",
                   "1_Pooling/config.json", "README.md", ".gitattributes"):
        assert absent not in plan
    # the import closure of modeling_lora, not the conversion script
    assert "modeling_lora.py" in fetch.CODE_FILES and "mha.py" in fetch.CODE_FILES
    assert "convert_roberta_weights_to_flash.py" not in fetch.CODE_FILES
    assert len(fetch.WEIGHTS_COMMIT) == 40


# ---- the digest is the platform's -------------------------------------------------

def test_the_scripts_digest_is_app_vega_runs_weights_digest(tmp_path):
    (tmp_path / "config.json").write_bytes(b'{"a":1}\n')
    (tmp_path / "hf-cache" / "m--x" / "snapshots" / "abc").mkdir(parents=True)
    (tmp_path / "hf-cache" / "m--x" / "snapshots" / "abc" / "mod.py").write_bytes(b"x = 1\n")
    (tmp_path / "hf-cache" / "m--x" / "refs").mkdir()
    (tmp_path / "hf-cache" / "m--x" / "refs" / "main").write_bytes(b"abc")
    (tmp_path / "model.safetensors").write_bytes(os.urandom(70000))
    assert fetch.weights_digest(tmp_path) == runs.weights_digest(tmp_path)


def test_both_digests_refuse_a_symbolic_link(tmp_path):
    (tmp_path / "config.json").write_bytes(b"{}")
    os.symlink(tmp_path / "config.json", tmp_path / "link.json")
    with pytest.raises(fetch.Refused):
        fetch.weights_digest(tmp_path)
    with pytest.raises(runs.WeightsError):
        runs.weights_digest(tmp_path)


# ---- one file against the commit's listing -----------------------------------------

def hashes(data: bytes, size: int | None = None) -> "fetch.Hashes":
    h = fetch.Hashes(len(data) if size is None else size)
    h.update(data)
    return h


def test_a_plain_file_is_the_commits_by_size_and_git_blob_id():
    data = b"import torch\n"
    plain = fetch.Entry(size=len(data), blob=blob_id(data), sha256=None)
    assert fetch.verdict(hashes(data), plain) is None
    assert "git blob" in fetch.verdict(hashes(b"import torch \n", len(data)),
                                       fetch.Entry(size=len(data) + 1, blob=blob_id(data),
                                                   sha256=None)).replace("bytes", "git blob")
    assert "bytes" in fetch.verdict(hashes(data[:-1]), plain)


def test_an_lfs_file_is_the_commits_by_its_sha256_and_the_tokenizer_by_the_layers_pin():
    data = os.urandom(300)
    lfs = fetch.Entry(size=300, blob=None, sha256=hashlib.sha256(data).hexdigest())
    assert fetch.verdict(hashes(data), lfs) is None
    assert "sha256" in fetch.verdict(hashes(os.urandom(300)), lfs)
    assert "tokenizer" in fetch.verdict(hashes(data), lfs, sha256_pin="f" * 64)
    assert fetch.verdict(hashes(data), lfs, sha256_pin=lfs.sha256) is None


def test_the_tokenizer_pin_is_the_wikidata_text_layers_parameter():
    """D6: the chunker cuts at the tokens of the file the encoder reads."""
    assert fetch.TOKENIZER_SHA256 == \
        "f59925fcb90c92b894cb93e51bb9b4a6105c5c249fe54ce1c704420ac39b81af"


# ---- the destination ---------------------------------------------------------------

def test_dest_is_absent_empty_or_this_scripts_own_unfinished_work(tmp_path):
    fetch.check_dest(tmp_path / "absent", ["config.json"])
    fetch.check_dest(tmp_path, ["config.json"])
    (tmp_path / "config.json.part").write_bytes(b"cut")
    fetch.check_dest(tmp_path, ["config.json"])
    assert not (tmp_path / "config.json.part").exists(), "a cut transfer is fetched again"
    (tmp_path / "README.md").write_bytes(b"#")
    with pytest.raises(fetch.Refused, match="not the layout"):
        fetch.check_dest(tmp_path, ["config.json"])


def test_dest_with_a_symbolic_link_is_refused(tmp_path):
    os.symlink(tmp_path, tmp_path / "self")
    with pytest.raises(fetch.Refused, match="symbolic link"):
        fetch.check_dest(tmp_path, ["config.json"])


# ---- the whole install, against a fake Hub -------------------------------------------

def fake_hub(monkeypatch, *, tokenizer=b"{tok}", code_commit=CODE_COMMIT):
    """Bytes per file, a tree listing that describes them, and a fetch that
    writes them — the Hub without the network."""
    files = {(fetch.WEIGHTS_REPO, f): f"[{f}]".encode() for f in fetch.WEIGHTS_FILES}
    files[(fetch.WEIGHTS_REPO, "tokenizer.json")] = tokenizer
    files[(fetch.WEIGHTS_REPO, "model.safetensors")] = os.urandom(5000)
    files.update({(fetch.CODE_REPO, f): f"# {f}\n".encode() for f in fetch.CODE_FILES})
    monkeypatch.setattr(fetch, "TOKENIZER_SHA256", hashlib.sha256(tokenizer).hexdigest())
    fetched: list[str] = []

    def resolve(repo, revision, token):
        return code_commit if repo == fetch.CODE_REPO else fetch.WEIGHTS_COMMIT

    def tree(repo, commit, token):
        out = {}
        for (r, f), data in files.items():
            if r != repo:
                continue
            lfs = f in ("model.safetensors", "tokenizer.json")
            out[f] = fetch.Entry(size=len(data), blob=None if lfs else blob_id(data),
                                 sha256=hashlib.sha256(data).hexdigest() if lfs else None)
        out["README.md"] = fetch.Entry(size=1, blob=blob_id(b"#"), sha256=None)
        return out

    def fetch_file(repo, commit, file, path, entry, token, *, sha256_pin=None, say=print):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = files[(repo, file)]
        h = fetch.Hashes(entry.size)
        h.update(data)
        why = fetch.verdict(h, entry, sha256_pin=sha256_pin)
        if why:
            raise fetch.Refused(f"{file}: {why}")
        path.write_bytes(data)
        fetched.append(file)

    return dict(resolve=resolve, tree=tree, fetch=fetch_file, say=lambda *a, **k: None), fetched, files


def test_install_writes_the_layout_the_ref_and_reports_the_platforms_digest(tmp_path, monkeypatch):
    hub, fetched, files = fake_hub(monkeypatch)
    dest = tmp_path / "jina-embeddings-v3"
    commit, digest = fetch.install(dest, None, **hub)
    assert commit == CODE_COMMIT
    assert sorted(fetched) == sorted(list(fetch.WEIGHTS_FILES) + list(fetch.CODE_FILES))
    ref = dest / fetch.ref_path(CODE_COMMIT)
    assert ref.read_bytes() == CODE_COMMIT.encode(), "as huggingface_hub reads it: no newline"
    snap = dest / "hf-cache" / "models--jinaai--xlm-roberta-flash-implementation" / "snapshots" / CODE_COMMIT
    assert (snap / "modeling_lora.py").read_bytes() == files[(fetch.CODE_REPO, "modeling_lora.py")]
    assert (dest / "model.safetensors").read_bytes() == files[(fetch.WEIGHTS_REPO, "model.safetensors")]
    assert not any(p.is_symlink() for p in dest.rglob("*"))
    assert digest == runs.weights_digest(dest) == fetch.weights_digest(dest)
    # the server finds the remote code where the layout put it
    assert server.hub_cache(str(dest)) == str(dest / "hf-cache")


def test_a_second_run_keeps_what_is_whole_and_gives_the_same_digest(tmp_path, monkeypatch):
    hub, fetched, _ = fake_hub(monkeypatch)
    dest = tmp_path / "w"
    _, first = fetch.install(dest, None, **hub)
    fetched.clear()
    snap = dest / "hf-cache" / "models--jinaai--xlm-roberta-flash-implementation" / "snapshots" / CODE_COMMIT
    (snap / "mha.py.part").write_bytes(b"cut")                # a transfer cut off beside a whole file
    (dest / fetch.ref_path(CODE_COMMIT)).unlink()
    (dest / "config.json").write_bytes(b"damaged")
    _, again = fetch.install(dest, None, **hub)
    assert fetched == ["config.json"], "only the damaged file moved"
    assert not (snap / "mha.py.part").exists()
    assert again == first


def test_a_tokenizer_that_is_not_the_layers_refuses_the_install(tmp_path, monkeypatch):
    hub, _, _ = fake_hub(monkeypatch)
    monkeypatch.setattr(fetch, "TOKENIZER_SHA256", "0" * 64)
    with pytest.raises(fetch.Refused, match="tokenizer"):
        fetch.install(tmp_path / "w", None, **hub)


def test_a_code_commit_that_does_not_begin_with_the_pin_is_refused(monkeypatch):
    monkeypatch.setattr(fetch, "api", lambda path, token: {"sha": "a" * 40})
    with pytest.raises(fetch.Refused, match="pin"):
        fetch.resolve(fetch.CODE_REPO, fetch.CODE_COMMIT, None)
    monkeypatch.setattr(fetch, "api", lambda path, token: {"sha": CODE_COMMIT})
    assert fetch.resolve(fetch.CODE_REPO, fetch.CODE_COMMIT, None) == CODE_COMMIT


def test_a_commit_missing_a_file_of_the_layout_is_refused(tmp_path, monkeypatch):
    hub, _, _ = fake_hub(monkeypatch)
    real_tree = hub["tree"]

    def tree(repo, commit, token):
        out = real_tree(repo, commit, token)
        out.pop("mha.py", None)
        return out
    hub["tree"] = tree
    with pytest.raises(fetch.Refused, match="mha.py"):
        fetch.install(tmp_path / "w", None, **hub)


def test_main_compares_the_digest_it_was_told_to_expect(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fetch, "install", lambda dest, token: (CODE_COMMIT, "sha256:" + "1" * 64))
    assert fetch.main([str(tmp_path / "w"), "--expect", "sha256:" + "1" * 64]) == 0
    assert fetch.main([str(tmp_path / "w"), "--expect", "sha256:" + "2" * 64]) == 2
    assert "sha256:" + "1" * 64 in capsys.readouterr().out

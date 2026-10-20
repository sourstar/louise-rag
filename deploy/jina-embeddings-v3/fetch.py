"""deploy/jina-embeddings-v3/fetch.py — the jina-v3 weights folder, written once
and the same everywhere (docs/notes/wikidata-build-plan.md §4 step 6).

What the folder IS decides the weights digest (09 §9.3: every regular file
under it, by path; a symbolic link is refused), and the digest sits inside
`space_id` for the life of the collection. So nothing here is left to a
cache layout or to what the Hub serves today:

- the WEIGHTS, `jinaai/jina-embeddings-v3` at the pinned commit — the files
  `AutoModel.from_pretrained(folder)` and the model's own `encode` read:
  config.json (the LoRA tasks and their instructions), model.safetensors,
  and the tokenizer's three files. Not pytorch_model.bin (the same tensors
  again, 1.1 GB), not onnx/ (another runtime), not the sentence-transformers
  wrappers (modules.json, 1_Pooling/, custom_st.py,
  config_sentence_transformers.json), not README.md or .gitattributes:
  nothing reads them at a load, and a file nobody reads inside the digest
  would make the Hub's next edit of a README a new space;
- the REMOTE CODE config.json names (`jinaai/xlm-roberta-flash-implementation`)
  at the pinned commit, under `hf-cache/` in the one layout transformers
  reads offline: `models--jinaai--xlm-roberta-flash-implementation/refs/main`
  holding the commit, `snapshots/<commit>/<file>` holding the files — as
  REGULAR FILES, where huggingface_hub's own cache writes symbolic links
  into blobs/, which the digest refuses. The ten modules `modeling_lora`
  imports, directly or through `modeling_xlm_roberta`, and nothing else:
  not the weight-conversion script, not the README.

Every file is checked against the commit's own tree listing — the git
blob id of a plain file, the LFS sha256 of a large one — and its size, so
a cut transfer or a moved tag cannot leave a folder that is almost right.
tokenizer.json is checked against the sha256 the `wikidata-text` layer
names too (D6), which ties the chunker's tokenizer to the encoder's.

    python3 -I deploy/jina-embeddings-v3/fetch.py DEST [--token-file F] [--expect sha256:…]

DEST is absent, empty, or a run of this script cut off: a file already
whole is kept, a partial one is fetched again, anything else refuses.
On success the folder's weights digest is printed (the algorithm of
app/vega/runs.weights_digest, tests/test_jina_fetch.py keeps them equal);
with --expect it is compared, so a second peer installing the folder
refuses a different one. Standard library only; the proxy is the
environment's (HTTPS_PROXY). A token (gated or rate-limited Hub) comes
from a FILE, never an env value or an argument (invariant 8).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

HUB = "https://huggingface.co"

WEIGHTS_REPO = "jinaai/jina-embeddings-v3"
WEIGHTS_COMMIT = "ab036b023d30b4d1138c4c3bfa9f0c445ab455d6"
WEIGHTS_FILES = ("config.json", "model.safetensors", "special_tokens_map.json",
                 "tokenizer.json", "tokenizer_config.json")
#: the `wikidata-text` layer's `tokenizer` parameter (docs/notes/wikidata-recipe.toml):
#: the chunker cuts at this file's tokens, the encoder reads the same file
TOKENIZER_SHA256 = "f59925fcb90c92b894cb93e51bb9b4a6105c5c249fe54ce1c704420ac39b81af"

CODE_REPO = "jinaai/xlm-roberta-flash-implementation"
#: the commit docs/notes/wikidata-build-plan.md pins; the full id is resolved
#: from the Hub and must begin with it
CODE_COMMIT = "bd55a5e"
#: `modeling_lora` → `configuration_xlm_roberta`, `modeling_xlm_roberta` → `rotary`,
#: `block`, `embedding`, `mha`, `mlp`, `xlm_padding`; `block` → `stochastic_depth`
CODE_FILES = ("block.py", "configuration_xlm_roberta.py", "embedding.py", "mha.py", "mlp.py",
              "modeling_lora.py", "modeling_xlm_roberta.py", "rotary.py", "stochastic_depth.py",
              "xlm_padding.py")

HF_CACHE = "hf-cache"
CHUNK = 8 << 20
TIMEOUT_S = 120


class Refused(Exception):
    """The folder would not be the one the digest names."""


@dataclass(frozen=True)
class Entry:
    size: int
    blob: str | None        # git blob id (sha1) of a plain file; None for an LFS file
    sha256: str | None      # the LFS object's sha256; None for a plain file


# ---- the layout --------------------------------------------------------------

def cache_dir(repo: str) -> str:
    return "models--" + repo.replace("/", "--")


def layout(code_commit: str) -> dict[str, tuple[str, str, str]]:
    """relative path in the folder -> (repo, commit, file in the repo)."""
    out = {f: (WEIGHTS_REPO, WEIGHTS_COMMIT, f) for f in WEIGHTS_FILES}
    snap = f"{HF_CACHE}/{cache_dir(CODE_REPO)}/snapshots/{code_commit}"
    out.update({f"{snap}/{f}": (CODE_REPO, code_commit, f) for f in CODE_FILES})
    return out


def ref_path(code_commit: str) -> str:
    return f"{HF_CACHE}/{cache_dir(CODE_REPO)}/refs/main"


# ---- the digest, as app/vega/runs.weights_digest ----------------------------

def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def weights_digest(root: Path) -> str:
    """09 §9.3: `H` of the canonical JSON mapping every regular file's path,
    relative and `/`-separated, to its digest; a symbolic link is refused."""
    root = Path(root)
    if root.is_symlink():
        raise Refused(f"{root} is a symbolic link")
    manifest: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            raise Refused(f"{p} is a symbolic link; a model directory holds its files (09 §9.3)")
        if p.is_file():
            manifest[p.relative_to(root).as_posix()] = file_digest(p)
    if not manifest:
        raise Refused(f"{root} holds no file")
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False).encode("utf-8")
    return _digest(canonical)


# ---- the Hub -------------------------------------------------------------------

def _headers(token: str | None) -> dict[str, str]:
    h = {"User-Agent": "louise-jina-v3-fetch/1"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def api(path: str, token: str | None):
    req = urllib.request.Request(f"{HUB}/api/{path}", headers=_headers(token))
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.load(r)


def resolve(repo: str, revision: str, token: str | None) -> str:
    """The full commit id a revision names; it must begin with the pin."""
    sha = str(api(f"models/{repo}/revision/{revision}", token).get("sha") or "")
    if len(sha) != 40 or not sha.startswith(revision):
        raise Refused(f"{repo}@{revision}: the Hub resolves it to {sha!r}, not a commit "
                      f"beginning with the pin")
    return sha


def tree(repo: str, commit: str, token: str | None) -> dict[str, Entry]:
    """Every file of the commit, with what verifies it."""
    out: dict[str, Entry] = {}
    for item in api(f"models/{repo}/tree/{commit}?recursive=true", token):
        if item.get("type") != "file":
            continue
        lfs = item.get("lfs") or {}
        out[item["path"]] = Entry(size=int(lfs.get("size", item.get("size", 0))),
                                  blob=None if lfs else str(item.get("oid") or ""),
                                  sha256=str(lfs["oid"]) if lfs else None)
    return out


# ---- checking and fetching ------------------------------------------------------

class Hashes:
    """sha256 of the bytes and the git blob id over them, in one pass."""

    def __init__(self, size: int) -> None:
        self.sha = hashlib.sha256()
        self.blob = hashlib.sha1(f"blob {size}\0".encode("ascii"))
        self.n = 0

    def update(self, block: bytes) -> None:
        self.sha.update(block)
        self.blob.update(block)
        self.n += len(block)


def verdict(h: Hashes, entry: Entry, *, sha256_pin: str | None = None) -> str | None:
    """None when the bytes are the entry's, else why not."""
    if h.n != entry.size:
        return f"{h.n} bytes, the commit's file is {entry.size}"
    if entry.sha256 is not None and h.sha.hexdigest() != entry.sha256:
        return f"sha256 {h.sha.hexdigest()[:16]}…, the commit's object is {entry.sha256[:16]}…"
    if entry.blob is not None and h.blob.hexdigest() != entry.blob:
        return f"git blob {h.blob.hexdigest()[:12]}…, the commit's is {entry.blob[:12]}…"
    if sha256_pin is not None and h.sha.hexdigest() != sha256_pin:
        return f"sha256 {h.sha.hexdigest()[:16]}…, the layer's tokenizer is {sha256_pin[:16]}…"
    return None


def hash_file(path: Path, size: int) -> Hashes:
    h = Hashes(size)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h


def whole(path: Path, entry: Entry, sha256_pin: str | None = None) -> bool:
    """Is the file already the commit's? (a cut run resumed)"""
    if not path.is_file() or path.is_symlink() or path.stat().st_size != entry.size:
        return False
    return verdict(hash_file(path, entry.size), entry, sha256_pin=sha256_pin) is None


def fetch(repo: str, commit: str, file: str, path: Path, entry: Entry, token: str | None,
          *, sha256_pin: str | None = None, say=print) -> None:
    """One file, streamed to `<path>.part`, verified, then moved into place."""
    part = path.with_name(path.name + ".part")
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"{HUB}/{repo}/resolve/{commit}/{file}"
    req = urllib.request.Request(url, headers=_headers(token))
    h = Hashes(entry.size)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r, open(part, "wb") as out:
        for block in iter(lambda: r.read(CHUNK), b""):
            out.write(block)
            h.update(block)
            if entry.size > CHUNK:
                say(f"  {file}: {h.n * 100 // entry.size:3d} %", end="\r", file=sys.stderr)
    why = verdict(h, entry, sha256_pin=sha256_pin)
    if why is not None:
        part.unlink(missing_ok=True)
        raise Refused(f"{repo}@{commit[:7]}/{file}: {why}")
    os.replace(part, path)
    say(f"  {path.name:<32} {entry.size:>13,d} B  sha256:{h.sha.hexdigest()}", file=sys.stderr)


def check_dest(dest: Path, expected: Iterable[str]) -> None:
    """DEST is absent, empty, or this script's own unfinished work."""
    if dest.is_symlink():
        raise Refused(f"{dest} is a symbolic link")
    if not dest.exists():
        return
    if not dest.is_dir():
        raise Refused(f"{dest} is not a directory")
    allowed = set(expected)
    for p in sorted(dest.rglob("*")):
        if p.is_symlink():
            raise Refused(f"{p}: a symbolic link; this folder is written without any")
        if p.is_dir():
            continue
        rel = p.relative_to(dest).as_posix()
        if rel.endswith(".part") and rel[:-5] in allowed:
            p.unlink()                                  # a transfer cut off: fetched again
            continue
        if rel not in allowed:
            raise Refused(f"{dest} holds {rel}, which is not the layout's; refusing to write "
                          f"into a folder that is not this script's")


def install(dest: Path, token: str | None, *, say=print,
            resolve=resolve, tree=tree, fetch=fetch) -> tuple[str, str]:
    """The folder at `dest`; (code commit, weights digest)."""
    code_commit = resolve(CODE_REPO, CODE_COMMIT, token)
    if resolve(WEIGHTS_REPO, WEIGHTS_COMMIT, token) != WEIGHTS_COMMIT:
        raise Refused(f"{WEIGHTS_REPO}: the Hub does not know commit {WEIGHTS_COMMIT}")
    plan = layout(code_commit)
    ref = ref_path(code_commit)
    check_dest(dest, list(plan) + [ref])

    trees = {WEIGHTS_REPO: tree(WEIGHTS_REPO, WEIGHTS_COMMIT, token),
             CODE_REPO: tree(CODE_REPO, code_commit, token)}
    for repo, listing in trees.items():
        wanted = WEIGHTS_FILES if repo == WEIGHTS_REPO else CODE_FILES
        missing = [f for f in wanted if f not in listing]
        if missing:
            raise Refused(f"{repo}: the commit has no {', '.join(missing)}")

    for rel, (repo, commit, file) in plan.items():
        entry = trees[repo][file]
        pin = TOKENIZER_SHA256 if file == "tokenizer.json" else None
        path = dest / rel
        if whole(path, entry, pin):
            say(f"  {path.name:<32} {entry.size:>13,d} B  kept (already the commit's)",
                file=sys.stderr)
            continue
        fetch(repo, commit, file, path, entry, token, sha256_pin=pin, say=say)

    ref_file = dest / ref
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    ref_file.write_bytes(code_commit.encode("ascii"))     # as huggingface_hub reads it: no newline

    present = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
    extra = present - set(plan) - {ref}
    if extra:
        raise Refused(f"{dest} holds files outside the layout: {sorted(extra)}")
    return code_commit, weights_digest(dest)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dest", type=Path, help="the folder to write (the model's ModelFile)")
    ap.add_argument("--token-file", type=Path, default=None,
                    help="a file holding a Hugging Face token (never the token itself)")
    ap.add_argument("--expect", default=None,
                    help="the weights digest this folder must have (sha256:…)")
    a = ap.parse_args(argv)
    token = a.token_file.read_text().strip() if a.token_file else None
    try:
        code_commit, digest = install(a.dest, token)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(f"weights   {WEIGHTS_REPO} @ {WEIGHTS_COMMIT}")
    print(f"code      {CODE_REPO} @ {code_commit}")
    print(f"folder    {a.dest}")
    print(f"digest    {digest}")
    if a.expect and a.expect != digest:
        print(f"refused: the folder's digest is not {a.expect}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

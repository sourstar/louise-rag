"""tests/hold/test_image_equivalence.py — 03 §3.4, amended 2026-10-07: an
image is equivalent for a collection when it re-parses the same origin bytes
into the same record lines as the Codex's reference image (operator: "the
source compliancy is after the parsing of the useless information, that
only matters"). Gutenberg's origin moved in content after its build, so
`records-vs-origin` fails under every image and could never show one
equivalent; this compares the parsers, not the records."""
import hashlib
import json

from app import louise
from app.hold import check as chk, equivalence
from tests.hold.test_check import SEED, _run, checkable  # noqa: F401
from tools import reparse_lines


def a_reference(root, snapshot, codex, corpus_dir, *, image=None, tamper=None):
    reparse = louise._reparse_from(codex, "gutenberg", corpus_dir)
    from tests.hold.test_check import _all_records
    lines = {}
    for r in _all_records(root, snapshot):
        digest = hashlib.sha256(reparse(r["uid"], r["origin"])).hexdigest()
        lines[r["uid"]] = [r["origin"], ("00" * 32) if r["uid"] == tamper else digest]
    return {"collection_id": codex.collection_id, "snapshot_hash": snapshot.hash,
            "image": image or codex.toolchain.platform_image, "seed": SEED.hex(), "shards": [],
            "lines": lines, "errors": {}}


def test_the_same_parse_as_the_reference_image_passes_and_a_different_one_fails(checkable, corpus_dir):  # noqa: F811
    root, snapshot, codex = checkable
    reparse = louise._reparse_from(codex, "gutenberg", corpus_dir)
    o = _run(root, codex, snapshot, ["image-equivalence"], reparse=reparse,
             against=a_reference(root, snapshot, codex, corpus_dir)).outcome("image-equivalence")
    assert o.result == chk.PASS, o.why
    assert o.detail["against"] == "reference-image" and o.detail["failed"] == [] and o.detail["compared"] > 0
    ref = a_reference(root, snapshot, codex, corpus_dir)
    victim = sorted(ref["lines"])[0]
    o = _run(root, codex, snapshot, ["image-equivalence"], reparse=reparse,
             against=a_reference(root, snapshot, codex, corpus_dir, tamper=victim)).outcome("image-equivalence")
    assert o.result == chk.FAIL and o.detail["failed"] == [victim]


def test_a_reference_not_made_by_the_codex_s_image_proves_nothing(checkable, corpus_dir):  # noqa: F811
    root, snapshot, codex = checkable
    reparse = louise._reparse_from(codex, "gutenberg", corpus_dir)
    other = a_reference(root, snapshot, codex, corpus_dir, image="sha256:" + "ab" * 32)
    o = _run(root, codex, snapshot, ["image-equivalence"], reparse=reparse, against=other).outcome("image-equivalence")
    assert o.result == chk.INCONCLUSIVE and "not by" in o.why
    o = _run(root, codex, snapshot, ["image-equivalence"], reparse=reparse).outcome("image-equivalence")
    assert o.result == chk.INCONCLUSIVE and "--against" in o.why


def test_it_counts_toward_equivalence_beside_chunks_and_never_toward_trust():
    from app.share import coverage as cov

    class Bodies(list):
        def by_image(self, key, check):
            return {b["platform_image"]: (b["result"] == "pass", b) for b in self
                    if b["key"] == key and b["check"] == check}
    new = "sha256:" + "4c" * 32

    class Codex:
        class toolchain:
            platform_image = "sha256:" + "b8" * 32
    bodies = Bodies([{"key": "k", "check": "image-equivalence", "result": "pass", "platform_image": new}])
    assert new not in equivalence.reference_images(bodies, "k", Codex), "chunks too, under the same image"
    bodies.append({"key": "k", "check": "chunks", "result": "pass", "platform_image": new})
    assert new in equivalence.reference_images(bodies, "k", Codex)
    assert "image-equivalence" not in cov.DETERMINISTIC, "it says nothing about the records: no trust level counts it"


def test_the_tool_writes_a_reference_the_check_accepts(checkable, corpus_dir, monkeypatch, tmp_path):  # noqa: F811
    root, snapshot, codex = checkable
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", codex.toolchain.platform_image)
    monkeypatch.setenv("LOUISE_ROOT", str(root.parent))
    out = tmp_path / "ref.json"
    assert reparse_lines.main(["gutenberg", "--origin", str(corpus_dir), "--seed", SEED.hex(),
                               "--sample", "1", "--out", str(out)]) == 0
    ref = json.loads(out.read_text())
    assert ref["lines"] and ref["errors"] == {} and ref["image"] == codex.toolchain.platform_image
    o = _run(root, codex, snapshot, ["image-equivalence"],
             reparse=louise._reparse_from(codex, "gutenberg", corpus_dir), against=ref).outcome("image-equivalence")
    assert o.result == chk.PASS, o.why

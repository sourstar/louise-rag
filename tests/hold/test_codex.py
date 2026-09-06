"""tests/hold/test_codex.py — the Codex is the identity (docs/02 §2.1, §2.4, §2.7, §2.8)."""
import json
from dataclasses import replace

import pytest

from app.hold import codex as cx
from app.hold.codex import Codex, CodexError, Certifier, Defect, Layer, Space, Toolchain

IMG = "sha256:" + "a" * 64
ENC = "sha256:" + "b" * 64
IMG2 = "sha256:" + "c" * 64
ENC2 = "sha256:" + "d" * 64


def root_codex(**over) -> Codex:
    base = dict(
        plugin="gutenberg",
        origin={"transport": "https", "url": "https://gutenberg.example/", "form": "plain-text",
                "why": "the ePub is a derivative of it"},
        toolchain=Toolchain(IMG, ENC),
        parser_id="gutenberg", parser_version=3, cleaning_version=1,
        layers=(Layer("fixed-512", 2),),
        slicing={"keys": ["year"], "year": {"field": "date", "bins": ["<1950", "5y<2000", "1y"]}},
        space=Space("qwen3-embedding-8b", 4096, "f16", 8192, "0.95"),
        license_class="green", license_terms_sha256="sha256:" + "e" * 64,
    )
    base.update(over)
    return Codex(**base)


# ---- canonical form -------------------------------------------------------

def test_canonical_json_is_sorted_compact_utf8():
    b = cx.canonical_json({"b": 1, "a": {"z": None, "y": [True, "é"]}})
    assert b == '{"a":{"y":[true,"é"],"z":null},"b":1}'.encode("utf-8")


def test_canonical_json_refuses_floats_anywhere():
    with pytest.raises(CodexError, match=r"\$\.space\.tol.*float"):
        cx.canonical_json({"space": {"tol": 0.95}})
    with pytest.raises(CodexError, match=r"\$\[1\]"):
        cx.canonical_json([1, 2.0])


def test_key_order_does_not_change_identity():
    a = root_codex()
    b = root_codex(origin={"why": "the ePub is a derivative of it", "form": "plain-text",
                           "url": "https://gutenberg.example/", "transport": "https"})
    assert a.collection_id == b.collection_id


def test_any_field_changes_identity():
    a = root_codex()
    assert a.collection_id != root_codex(parser_version=4).collection_id
    assert a.collection_id != root_codex(toolchain=Toolchain(IMG2, ENC)).collection_id
    assert a.collection_id != root_codex(grace_days=8).collection_id
    assert a.collection_id != root_codex(license_terms_sha256="sha256:" + "f" * 64).collection_id


def test_roundtrip_through_bytes_is_the_same_codex():
    a = root_codex()
    b = cx.loads(cx.dumps(a))
    assert a == b
    assert cx.dumps(b) == cx.dumps(a)
    assert cx.digest(cx.dumps(a)) == a.collection_id      # the file's hash is the id


def test_to_dict_carries_kind_version_and_fixes_content_for_certified():
    d = root_codex().to_dict()
    assert d["kind"] == "louise-codex" and d["version"] == 1 and "content" not in d
    c = root_codex(publication="certified", certifier=Certifier("ab" * 32, "https://x.example/.well-known/louise/certifier.json"))
    assert c.to_dict()["content"] == "manifest-only"


# ---- space_id -------------------------------------------------------------

def test_space_id_depends_only_on_encoder_layers_cleaning_epoch():
    a = root_codex()
    # not the parser, origin, slicing, licence, platform image
    same = [root_codex(parser_version=9), root_codex(origin={"transport": "rsync"}),
            root_codex(slicing={"keys": ["lang"]}), root_codex(license_class="yellow"),
            root_codex(toolchain=Toolchain(IMG2, ENC))]
    for c in same:
        assert c.space_id == a.space_id
    # but each of these
    assert root_codex(toolchain=Toolchain(IMG, ENC2)).space_id != a.space_id
    assert root_codex(layers=(Layer("fixed-512", 3),)).space_id != a.space_id
    assert root_codex(cleaning_version=2).space_id != a.space_id
    assert root_codex(space=replace(a.space, epoch="x")).space_id != a.space_id


# ---- validation: shape -------------------------------------------------------

@pytest.mark.parametrize("over,msg", [
    (dict(toolchain=Toolchain("latest", ENC)), "sha256: digest"),
    (dict(layers=()), "at least one layer"),
    (dict(license_class="blue"), "green|yellow|red"),
    (dict(space=Space("e", 1, "f16", 1, "0,95")), "decimal string"),
    (dict(cadence="weekly"), "monthly"),
    (dict(publication="private"), "open|certified"),
    (dict(certifier=Certifier("k", "https://a.example/x")), "only a certified"),
    (dict(embedding="certifier-only"), "embedding is open"),
])
def test_validate_refuses(over, msg):
    with pytest.raises(CodexError, match=msg):
        cx.validate(root_codex(**over))


def test_certified_needs_certifier_with_anchor():
    with pytest.raises(CodexError, match="names its certifier"):
        cx.validate(root_codex(publication="certified"))
    with pytest.raises(CodexError, match="https URL"):
        cx.validate(root_codex(publication="certified", certifier=Certifier("k", "http://a.example/x")))
    ok = root_codex(publication="certified", certifier=Certifier("k", "https://a.example/.well-known/louise/c.json"),
                    embedding="certifier-only", succession="open-after:P6M")
    cx.validate(ok)


# ---- validation: lineage -----------------------------------------------------

def test_at_most_one_relation():
    with pytest.raises(CodexError, match="at most one"):
        cx.validate(root_codex(continues="sha256:" + "1" * 64, from_snapshot="2026-09",
                               derived_from="sha256:" + "2" * 64))


def test_daughter_needs_from_snapshot_and_only_daughter_has_it():
    with pytest.raises(CodexError, match="from_snapshot"):
        cx.validate(root_codex(continues="sha256:" + "1" * 64))
    with pytest.raises(CodexError, match="belong to a daughter"):
        cx.validate(root_codex(from_snapshot="2026-09"))


def test_daughter_keeps_parent_space_or_is_refused():
    parent = root_codex()
    ok = replace(parent, toolchain=Toolchain(IMG2, ENC), origin={"url": "https://moved.example/"},
                 continues=parent.collection_id, from_snapshot="2026-10")
    cx.validate(ok, parent)
    assert ok.space_id == parent.space_id and ok.relation == "daughter"
    disguised = replace(ok, toolchain=Toolchain(IMG2, ENC2))
    with pytest.raises(CodexError, match="sister in disguise"):
        cx.validate(disguised, parent)


def test_sister_must_change_space_and_share_corpus():
    parent = root_codex()
    sister = replace(parent, toolchain=Toolchain(IMG, ENC2), derived_from=parent.collection_id)
    cx.validate(sister, parent)
    assert sister.space_id != parent.space_id and sister.relation == "sister"
    same_space = replace(parent, derived_from=parent.collection_id)
    with pytest.raises(CodexError, match="same space is the same collection"):
        cx.validate(same_space, parent)
    other_corpus = replace(sister, origin={"url": "https://elsewhere.example/"})
    with pytest.raises(CodexError, match="shares the parent's corpus"):
        cx.validate(other_corpus, parent)


def test_corrective_sister_needs_defect_and_salted_epoch():
    parent = root_codex()
    defect = Defect("records-vs-origin", IMG2, ("year=1975-1979/00017",), "sha256:aa", "sha256:bb",
                    "two-column PDFs were merged line by line")
    bare = replace(parent, replaces=parent.collection_id)
    with pytest.raises(CodexError, match="reproducible defect"):
        cx.validate(bare)
    unsalted = replace(bare, defect=defect)
    with pytest.raises(CodexError, match="salted"):
        cx.validate(unsalted)
    ok = replace(unsalted, space=replace(parent.space, epoch=cx.corrective_epoch(parent.collection_id)))
    cx.validate(ok, parent)
    assert ok.relation == "corrective-sister"
    assert ok.space_id != parent.space_id, "from zero even with identical encoder and layers"
    with pytest.raises(CodexError, match="only a corrective sister"):
        cx.validate(replace(parent, defect=defect))


def test_lineage_id_walks_continues_and_stops_at_unknown():
    root = root_codex()
    d1 = replace(root, toolchain=Toolchain(IMG2, ENC), continues=root.collection_id, from_snapshot="2026-10")
    d2 = replace(d1, toolchain=Toolchain("sha256:" + "9" * 64, ENC), continues=d1.collection_id, from_snapshot="2027-01")
    held = {c.collection_id: c for c in (root, d1, d2)}
    assert cx.lineage_id(d2, held.get) == root.collection_id
    assert cx.lineage_id(root, held.get) == root.collection_id
    # a peer that never saw the root reports the last link it can name
    assert cx.lineage_id(d2, {d2.collection_id: d2, d1.collection_id: d1}.get) == root.collection_id
    assert cx.lineage_id(d2, {d2.collection_id: d2}.get) == d1.collection_id
    assert [c.collection_id for c in cx.walk_lineage(d2, held.get)] == [d2.collection_id, d1.collection_id, root.collection_id]


def test_loads_rejects_wrong_kind_or_version():
    d = root_codex().to_dict()
    d["kind"] = "other"
    with pytest.raises(CodexError, match="not a Codex"):
        cx.from_dict(d)
    d = root_codex().to_dict()
    d["version"] = 2
    with pytest.raises(CodexError, match="unsupported"):
        cx.from_dict(d)

"""`louise hold config apply <collection> --set KEY=VALUE …` (07 §7.8
"Pickup", built 2026-10-25): one value of the collection's file written
through the file's ONE writer — the Library's (app/collection_file.py) —
then applied as the node applies the files at start.

Desired state goes into the file and nowhere else (invariant 10): the
state database changes only through the apply, exactly as at start. A key
that is not the file's, a value the node would refuse, or `--set` with no
collection is exit 2 with the file untouched byte for byte.
"""
from __future__ import annotations

import json
import tomllib

import pytest
from fastapi.testclient import TestClient

from app import collection_file, collections as collections_mod, config, db, louise, spaces, web


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _file():
    return collections_mod.config_toml_path("gutenberg")


def _toml() -> dict:
    return tomllib.loads(_file().read_text(encoding="utf-8"))


def _scope_mode() -> str:
    con = db.connect(collections_mod.by_name("gutenberg"))
    try:
        return db.get_scope(con)["embed_mode"]
    finally:
        con.close()


def test_set_embed_writes_the_file_then_applies_it(isolated_config, capsys):
    """07 §7.6: the Library's embed-mode control is `--set embed=…`. The
    runtime override in force gives way to the file's new value."""
    c = collections_mod.by_name("gutenberg")
    con = db.connect(c)
    db.set_scope(con, {**db.get_scope(con), "embed_mode": "all"})   # a Tier-3 override
    con.close()
    assert louise.main(["hold", "config", "apply", "gutenberg", "--set", "embed=off"]) == 0
    got = _out(capsys)
    assert got["written"]["set"] == {"embed": "off"} and got["written"]["also"] == {}
    assert _toml()["embed"] == "off"
    assert collections_mod.by_name("gutenberg").embed_mode_default == "off"
    assert got["applied"] == [{"name": "gutenberg", "embed_mode": "off", "replaced": "all",
                               "encoder": "named in its file"}]
    assert _scope_mode() == "off"
    # the document's word on disk (07 §7.8), the token in the runtime table
    assert louise.main(["hold", "config", "apply", "gutenberg", "--set", "embed=on-demand"]) == 0
    assert _out(capsys)["written"]["set"] == {"embed": "on-demand"}
    assert _toml()["embed"] == "on-demand" and _scope_mode() == "lazy"
    # `hold config show` names the key `embed_mode`: it is accepted as well
    assert louise.main(["hold", "config", "apply", "gutenberg", "--set", "embed_mode=all"]) == 0
    capsys.readouterr()
    assert _toml()["embed"] == "all"
    assert louise.main(["hold", "config", "show", "gutenberg"]) == 0
    v = _out(capsys)["collections"][0]["values"]["embed_mode"]
    assert (v["value"], v["tier"]) == ("all", "files")


@pytest.mark.parametrize("assignment, says", [
    ("colour=blue", "the keys are embed, share"),
    ("codex=sha256:00", "the keys are"),          # never edited (07 §7.8)
    ("source=origin", "the keys are"),
    ("embed", "KEY=VALUE"),
    ("embed=sideways", "embed must be one of"),
    ("share=maybe", "share is true or false"),
    ("kinds=records,bogus", "02 §2.6's kinds are closed"),
    ("slices=not a slice", "slices"),
    ("share_kinds=terms", "the member kinds served are"),
    ("encoder.api=grpc", "encoder.api is one of"),
    ("reranker.window=-1", "reranker.window is a number"),
    # a URL that names another model than the file's: the loader's refusal
    ("encoder.url=http://nas:30450/upstream/medcpt-article", "names 'medcpt-article'"),
])
def test_a_refused_value_writes_nothing(isolated_config, capsys, assignment, says):
    before = _file().read_bytes()
    assert louise.main(["hold", "config", "apply", "gutenberg", "--set", assignment]) == 2
    err = capsys.readouterr().err
    assert says in err, err
    assert _file().read_bytes() == before


def test_set_needs_a_collection_and_a_key_once(isolated_config, capsys):
    assert louise.main(["hold", "config", "apply", "--set", "embed=off"]) == 2
    assert "--set writes one collection's file" in capsys.readouterr().err
    assert louise.main(["hold", "config", "apply", "gutenberg", "--set", "embed=off",
                        "--set", "embed=all"]) == 2
    assert "given twice" in capsys.readouterr().err
    assert louise.main(["hold", "config", "apply", "nope", "--set", "embed=off"]) == 2
    assert "no such collection" in capsys.readouterr().err


def test_p2p_and_sharing_meet_as_the_library_writes_them(isolated_config, capsys):
    """07 §7.8, 2026-10-19: p2p publishes what it embeds, so asking for it
    turns sharing on; sharing off under p2p takes the mode to all local;
    asking for both at once is a contradiction."""
    assert collections_mod.by_name("gutenberg").share is False
    assert louise.main(["hold", "config", "apply", "gutenberg", "--set", "embed=p2p"]) == 0
    assert _out(capsys)["written"]["also"] == {"share": True}
    assert (_toml()["embed"], _toml()["share"]) == ("p2p", True)
    assert louise.main(["hold", "config", "apply", "gutenberg", "--set", "share=false"]) == 0
    assert _out(capsys)["written"]["also"] == {"embed": "all"}
    assert (_toml()["embed"], _toml()["share"]) == ("all", False)
    before = _file().read_bytes()
    assert louise.main(["hold", "config", "apply", "gutenberg", "--set", "embed=p2p",
                        "--set", "share=false"]) == 2
    assert "set embed=all, or share=true" in capsys.readouterr().err   # what to do instead
    assert _file().read_bytes() == before


def test_values_are_set_together_and_the_rest_is_carried(isolated_config, capsys):
    before = collections_mod.by_name("gutenberg")
    assert louise.main(["hold", "config", "apply", "gutenberg",
                        "--set", 'title=Libri "liberi" \\ PG', "--set", "share_kinds=records,vectors",
                        "--set", "kinds=records"]) == 0
    capsys.readouterr()
    after = collections_mod.by_name("gutenberg")
    assert after.title == 'Libri "liberi" \\ PG', "escaped as TOML reads it back"
    assert after.share_kinds == ("records", "vectors") and after.kinds == ("records",)
    assert after.codex.collection_id == before.codex.collection_id
    assert after.embed == before.embed and after.corpus_dir == before.corpus_dir
    assert louise.main(["hold", "config", "apply", "gutenberg", "--set", "title=",
                        "--set", "share_kinds=all"]) == 0
    capsys.readouterr()
    after = collections_mod.by_name("gutenberg")
    assert after.title is None and after.share_kinds is None
    assert "title" not in _toml() and "share_kinds" not in _toml()


def test_the_encoder_and_the_reranker_are_set_by_their_dotted_names(isolated_config, capsys):
    """`hold config show` prints `encoder.url`; `--set encoder.url=…` writes
    it. A collection that names no encoder is given one this way — the
    command line's answer to 07 §7.8's "until the operator adds one"."""
    assert louise.main(["hold", "config", "apply", "gutenberg",
                        "--set", "encoder.url=http://nas:30450/upstream/medcpt-article",
                        "--set", "encoder.embed_model=medcpt-article",
                        "--set", "encoder.query_url=http://nas:30450/upstream/medcpt-query",
                        "--set", "encoder.query_model=medcpt-query"]) == 0
    capsys.readouterr()
    enc = collections_mod.by_name("gutenberg").embed
    assert (enc.url, enc.model, enc.query_url, enc.query_model) == (
        "http://nas:30450/upstream/medcpt-article", "medcpt-article",
        "http://nas:30450/upstream/medcpt-query", "medcpt-query")
    space = collections_mod.by_name("gutenberg").codex.space
    assert (enc.dim, enc.ctx) == (space.dim, space.ctx), "the space is the Codex's, never the file's"

    assert louise.main(["hold", "config", "apply", "gutenberg",
                        "--set", "reranker.url=http://rr:30447/v1/rerank",
                        "--set", "reranker.rerank_model=bge-reranker-v2-m3",
                        "--set", "reranker.window=1024"]) == 0
    capsys.readouterr()
    rr = collections_mod.by_name("gutenberg").reranker
    assert (rr.url, rr.api, rr.model, rr.window) == ("http://rr:30447/v1/rerank", "jina",
                                                     "bge-reranker-v2-m3", 1024)
    assert louise.main(["hold", "config", "apply", "gutenberg", "--set", "reranker.url="]) == 0
    capsys.readouterr()
    assert collections_mod.by_name("gutenberg").reranker is None and "reranker" not in _toml()


@pytest.mark.strict_encoder
def test_a_collection_with_no_encoder_is_given_one(isolated_config, capsys):
    """Refused by the node (no [encoder]), still editable: `--set` names the
    encoder and the apply then serves it (exit 0, nothing refused)."""
    assert "encoder" not in _toml()
    assert louise.main(["hold", "config", "apply", "gutenberg", "--set", "embed=off"]) == 1
    assert [r["name"] for r in _out(capsys)["refused"]] == ["gutenberg"]
    assert louise.main(["hold", "config", "apply", "gutenberg", "--set", "encoder.url=http://enc:1",
                        "--set", "encoder.embed_model=m", "--set", "encoder.query_model=m"]) == 0
    got = _out(capsys)
    assert got["refused"] == [] and got["applied"][0]["name"] == "gutenberg"
    assert _toml()["encoder"] == {"url": "http://enc:1", "embed_model": "m", "query_model": "m"}


def test_never_share_is_never_shared(isolated_config, monkeypatch, capsys):
    c = collections_mod.by_name("gutenberg")
    import dataclasses
    real = collections_mod._load_one
    monkeypatch.setattr(collections_mod, "_load_one", lambda p: dataclasses.replace(
        real(p), codex=dataclasses.replace(c.codex, privacy="never-share")))
    before = _file().read_bytes()
    assert louise.main(["hold", "config", "apply", "gutenberg", "--set", "share=true"]) == 2
    assert "never-share" in capsys.readouterr().err
    assert _file().read_bytes() == before


def test_the_library_writes_what_set_writes(isolated_config, monkeypatch, capsys, tmp_path):
    """ONE writer: the Library's embed-mode control and `--set embed=…`
    leave the same bytes (07 §7.6)."""
    original = _file().read_bytes()
    assert louise.main(["hold", "config", "apply", "gutenberg", "--set", "embed=p2p"]) == 0
    capsys.readouterr()
    by_cli = _file().read_bytes()
    _file().write_bytes(original)
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    from app.api import app
    with TestClient(app) as client:
        client.headers["X-Admin-Token"] = "sekrit"
        r = client.post("/admin/library/gutenberg/embed-mode", json={"mode": "p2p"})
    assert r.status_code == 200 and r.json()["share_enabled"] is True
    assert _file().read_bytes() == by_cli


def test_parse_assignments_names_every_key():
    with pytest.raises(collection_file.SetRefused) as e:
        collection_file.parse_assignments(["nope=1"])
    for key in collection_file.SETTABLE:
        assert key in str(e.value)
    assert collection_file.parse_assignments(["embed_mode = off", "share=true"]) == {
        "embed": "off", "share": "true"}

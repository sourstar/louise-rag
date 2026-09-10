"""tests/hold/test_vectors_archive.py — `hold vectors import --archive`.

Step 8 needs it: `pubmed` draws its 35.3M vectors from `medcpt-pubmed`
and `wikipedia` its 12.7M from `upstash-wikipedia-2024-06`, and until
this verb existed there was no way to do either under the new tree.

IT WRAPS, and that is the plan's rule rather than a shortcut. A bundle is
09 §9.7's four files and goes through `app/hold/admit.py`'s ladder; an
archive is millions of vectors in someone else's format, fetched shard by
shard, and `app/bundles/genesis.py` already does that resumably with a
same-function proof per archive. What the verb ADDS is 03 §3.5's archive
refusal, which the old path never had.
"""
import pytest

from app import louise
from app.hold import codex as cx, collection as col
from app.hold.codex import CodexError
from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed
from tests.hold.test_template import recipe


def _root(tmp_path, archives=()):
    root = tmp_path / "collections"
    from app.hold import template as tpl
    col.create_from_recipe(
        root, "gutenberg", recipe(), platform_image=PLATFORM,
        embed=fake_embed, name="gutenberg", key=KEY, now=NOW,
        credits=tpl.expand_archives(archives))
    return root


# ---- the verb ------------------------------------------------------------

def test_bundle_and_archive_are_mutually_exclusive():
    """07 §7.3 gives `import` one shape for both, but a single run is one
    or the other — a command given both has not said what it wants."""
    p = louise.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["hold", "vectors", "import", "g",
                      "--bundle", "b", "--archive", "medcpt-pubmed"])
    with pytest.raises(SystemExit):
        p.parse_args(["hold", "vectors", "import", "g"])   # neither


def test_the_archive_form_parses():
    a = louise.build_parser().parse_args(
        ["hold", "vectors", "import", "g", "--archive", "medcpt-pubmed",
         "--shards", "0", "1"])
    assert a.archive == "medcpt-pubmed" and a.shards == ["0", "1"]
    assert a.bundle is None


# ---- 03 §3.5's archive refusal, which is what the verb adds -------------

def test_an_archive_this_collection_does_not_credit_is_REFUSED(isolated_config,
                                                               tmp_path, capsys):
    """06 §6.6: "an archive whose entry is not already in `credits` is
    REFUSED, not credited late — the Codex is written once". `credits` is
    hashed into `collection_id`, so this cannot be satisfied after the
    fact by editing anything, which is the point."""
    root = _root(tmp_path)                       # no archives credited
    assert louise.main(["hold", "vectors", "import", "gutenberg",
                        "--root", str(root),
                        "--archive", "medcpt-pubmed"]) == 2
    err = capsys.readouterr().err
    assert "does not credit" in err and "06 §6.6" in err


def test_an_unknown_archive_names_the_ones_there_are(isolated_config, tmp_path,
                                                     capsys):
    root = _root(tmp_path)
    assert louise.main(["hold", "vectors", "import", "gutenberg",
                        "--root", str(root), "--archive", "nope"]) == 2
    assert "no archive 'nope'" in capsys.readouterr().err


def test_a_credited_archive_passes_the_gate(isolated_config, tmp_path,
                                            monkeypatch, capsys):
    """The other half: a collection that DID declare the archive at
    creation gets past §3.5's refusal and on to the import itself."""
    root = _root(tmp_path, archives=("medcpt-pubmed",))
    codex = col.read(root, "gutenberg")
    assert "medcpt-pubmed" in {c.archive for c in cx.archive_credits(codex)}

    seen = {}

    def fake_import(con, collection, archive, **kw):
        seen["archive"] = archive.archive_id
        seen["shards"] = kw.get("shards")
        return {"shards": [{"shard": "0", "ok": True, "docs": 3, "chunks": 9}],
                "docs": 3, "chunks": 9}

    monkeypatch.setattr("app.bundles.genesis.import_archive", fake_import)
    monkeypatch.setenv("EMBED_URL", "http://example.invalid")
    assert louise.main(["hold", "vectors", "import", "gutenberg",
                        "--root", str(root), "--archive", "medcpt-pubmed"]) == 0
    assert seen["archive"] == "medcpt-pubmed"
    out = capsys.readouterr().out
    assert "1 of 1 admitted" in out and "9 chunks over 3 documents" in out


def test_shards_are_passed_through(isolated_config, tmp_path, monkeypatch):
    """`--shards` exists so a 959-shard archive can be proved on ONE
    before the rest is run (migration plan, step 8)."""
    root = _root(tmp_path, archives=("medcpt-pubmed",))
    seen = {}

    def fake_import(con, collection, archive, **kw):
        seen["shards"] = kw.get("shards")
        return {"shards": [], "docs": 0, "chunks": 0}

    monkeypatch.setattr("app.bundles.genesis.import_archive", fake_import)
    monkeypatch.setenv("EMBED_URL", "http://example.invalid")
    louise.main(["hold", "vectors", "import", "gutenberg", "--root", str(root),
                 "--archive", "medcpt-pubmed", "--shards", "7"])
    assert seen["shards"] == ["7"]


def test_no_encoder_defers_rather_than_failing(isolated_config, tmp_path,
                                               monkeypatch, capsys):
    """The archive route's same-function proof needs the encoder, and
    genesis DEFERS a shard rather than admitting it unproven. 03 §3.1's
    third outcome, and 07 §7.7's exit 3."""
    monkeypatch.delenv("EMBED_URL", raising=False)
    root = _root(tmp_path, archives=("medcpt-pubmed",))
    assert louise.main(["hold", "vectors", "import", "gutenberg",
                        "--root", str(root),
                        "--archive", "medcpt-pubmed"]) == 3
    assert "deferred" in capsys.readouterr().err


def test_a_refused_shard_exits_1_and_names_it(isolated_config, tmp_path,
                                              monkeypatch, capsys):
    root = _root(tmp_path, archives=("medcpt-pubmed",))

    def fake_import(con, collection, archive, **kw):
        return {"shards": [{"shard": "0", "ok": True, "docs": 1, "chunks": 1},
                           {"shard": "1", "ok": False,
                            "detail": "same-function proof failed"}],
                "docs": 1, "chunks": 1}

    monkeypatch.setattr("app.bundles.genesis.import_archive", fake_import)
    monkeypatch.setenv("EMBED_URL", "http://example.invalid")
    assert louise.main(["hold", "vectors", "import", "gutenberg",
                        "--root", str(root), "--archive", "medcpt-pubmed"]) == 1
    err = capsys.readouterr().err
    assert "refused" in err and "same-function proof failed" in err

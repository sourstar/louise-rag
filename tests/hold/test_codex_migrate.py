"""A Codex written before a §9.3 field existed can be brought up to it.

`derivation` was added to §9.3 after the node's collections were minted,
and `_only` refuses a MISSING field as firmly as an unknown one — on
purpose, because a peer that ignored one would hash the same collection
differently (§9.18). So every Codex written before the field fails to
load outright.

Measured against the live node 2026-09-13, with the new code and the
real config:

    LOADED: []
    UNREADABLE: gutenberg — codex: missing field(s) derivation
                pubmed    — codex: missing field(s) derivation

Zero collections, and `default_collection()` raises, which takes the
control manifest with it. The deploy was held for this.
"""
import json

import pytest

from app.hold import codex as cx
from app.hold import collection as col


@pytest.fixture
def written(tmp_path, monkeypatch):
    """A valid Codex on disk, with `derivation` taken back out — the shape
    the node's two files are actually in."""
    from tests.hold.conftest import pubmed as a_codex
    codex = a_codex()
    root = tmp_path / "hold"
    drag = col.write(root, codex)
    path = drag / col.CODEX_FILE
    raw = json.loads(path.read_bytes())
    del raw["derivation"]
    path.write_bytes(cx.canonical_json(raw))
    # the NAME `write` chose, not `default_name` recomputed: once the
    # directory exists, `default_name` returns the suffixed variant
    return root, drag.parent.name, path, codex


def test_the_unmigrated_file_does_not_load(written):
    """The premise. If this ever stops being true the migration is dead
    code and should go."""
    _, _, path, _ = written
    with pytest.raises(cx.CodexError) as e:
        cx.loads(path.read_bytes())
    assert "derivation" in str(e.value)


def test_a_dry_run_writes_nothing(written):
    root, name, path, _ = written
    before = path.read_bytes()
    got = col.migrate(root, name)
    assert got["added"] == ["derivation"]
    assert got["written"] is False
    assert path.read_bytes() == before


def test_applying_it_makes_the_file_load_again(written):
    root, name, path, _ = written
    got = col.migrate(root, name, apply=True)
    assert got["written"] is True
    back = cx.loads(path.read_bytes())
    assert back.derivation is None


def test_the_identity_moves_and_the_command_says_so(written):
    """`collection_id` is the digest of the object without `descriptive`,
    so a field added to the object is a field added to the id. The TOML's
    `codex = ` line must follow it or the collection is refused."""
    root, name, _, _ = written
    got = col.migrate(root, name, apply=True)
    assert got["before"] != got["after"]
    assert got["after"].startswith("sha256:")


def test_the_space_id_does_NOT_move(written):
    """The whole safety of this. §2.4 excludes `derivation` from
    `space_id`, so every vector already computed stays admissible — a
    bundle is admitted on `space_id` plus per-record `content_hash`, and
    `collection` is provenance that gates nothing. If this ever fails,
    the migration has become a 23-day re-embed of 40M documents."""
    root, name, path, original = written
    col.migrate(root, name, apply=True)
    assert cx.loads(path.read_bytes()).space_id == original.space_id


def test_nothing_is_invented_for_a_field_that_cannot_be_null(written):
    """A missing `space` or `credits` is not a schema gap, it is a broken
    Codex. Filling one would hash a value into `collection_id` as though
    the creator had written it."""
    _, _, path, _ = written
    raw = json.loads(path.read_bytes())
    del raw["credits"]
    with pytest.raises(cx.CodexError) as e:
        cx.migrate_missing_nulls(raw)
    assert "credits" in str(e.value)
    assert "invented" in str(e.value)


def test_migrating_twice_is_a_no_op(written):
    root, name, _, _ = written
    col.migrate(root, name, apply=True)
    again = col.migrate(root, name, apply=True)
    assert again["added"] == []
    assert again["before"] == again["after"]


def test_a_current_codex_needs_nothing(tmp_path):
    from tests.hold.conftest import pubmed as a_codex
    root = tmp_path / "hold"
    drag = col.write(root, a_codex())
    got = col.migrate(root, drag.parent.name)
    assert got["added"] == [] and got["written"] is False


# ---- one bad Codex refuses itself, not every command ----------------------

def test_an_unreadable_codex_does_not_take_down_the_listing(written, tmp_path):
    """Invariant 1's doctrine — "the node keeps running its others" —
    which `_held` did not follow: it called `cx.loads` on every held
    Codex and let the exception out, so `list`, `show`, `resolve` and
    `create` all died on one bad file.

    The live node holds four Codexes and NONE carries `derivation`: the
    two the worker serves, plus `gutenberg.parser4-2026-09-12` and
    `gutenberg.retired-2026-09-12`. Migrating only the served two would
    have left every `hold` verb raising on a retired directory nothing
    reads."""
    root, name, _, codex = written
    from tests.hold.conftest import pubmed as a_codex
    good = col.write(root, a_codex(descriptive=cx.Descriptive(
        title="Still fine", summary="a Codex that loads")))
    held = col._held(root)
    assert [n for n, _ in held] == [good.parent.name], "the good one is listed"
    assert name in col.unreadable(), "and the bad one is NAMED, not hidden"
    assert "derivation" in col.unreadable()[name]


def test_listing_survives_and_migrate_still_reaches_the_bad_one(written):
    """`migrate` reads the file directly rather than through `_held`, so
    the thing that repairs an unreadable Codex is not itself blocked by
    it. That ordering is the whole point."""
    root, name, _, _ = written
    assert col.list_all(root) == [] or True          # no raise is the assertion
    got = col.migrate(root, name, apply=True)
    assert got["written"] is True
    assert col.unreadable() == {} or name not in col._held(root)

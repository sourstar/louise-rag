"""The snapshot arc end to end: record, change, record, diff, ship vectors.

The point of the last step is that a VECTOR delta is an ordinary bundle. It
carries a signature, per-uid raw hashes binding vectors to a text revision,
and everything the verification ladder and genesis admission already know
how to check — none of which would have been worth rebuilding for a new
artifact type.
"""
import pytest

from app import cli, collections as collections_mod, db, snapshots
from tests.conftest import codex_collection


def _write_pinned(data_dir, config_dir, name: str = "wp", snapshot: str | None = "20260701"):
    """A Codex collection (the test gutenberg) pinned to `snapshot`."""
    codex_collection(name, louise_root=data_dir, config_dir=config_dir,
                     toml_extra=f'snapshot = "{snapshot}"\n' if snapshot else "")
    return collections_mod.by_name(name)


@pytest.fixture
def wiki(isolated_config, data_dir, config_dir):
    """A pinned collection with three documents. gutenberg is green-class,
    so nothing needs acknowledging; the yellow-class refusal has its own
    test below."""
    collection = _write_pinned(data_dir, config_dir)
    con = db.connect(collection)
    for i, h in ((1, "aa"), (2, "bb"), (3, "cc")):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "txt_hash,chunks) VALUES(?,'gutenberg',?,'/x',1,?,4)",
                    (f"gutenberg:{i}", str(i), h))
    return collection


def _run(*argv) -> int:
    return cli.main(list(argv))


def test_the_vectors_of_an_unacknowledged_collection_are_not_shipped(isolated_config, data_dir,
                                                                     config_dir, tmp_path, capsys,
                                                                     monkeypatch):
    """Invariant 9, "not exchanged": a vector delta is an ordinary signed,
    shareable bundle, and `snapshot vectors` never asked the gate
    (verification pass, 2026-09-13). The test Codex is green, so its class
    is made yellow here — with no terms acknowledged anywhere."""
    from app import licensing
    _write_pinned(data_dir, config_dir)
    monkeypatch.setattr(licensing, "license_class_for", lambda *a, **k: "yellow")
    assert _run("snapshot", "vectors", "wp", "--delta",
                str(tmp_path / "none.delta.jsonl")) == 1
    err = capsys.readouterr().err
    assert "REFUSED" in err and "invariant 9" in err


def test_record_then_diff_reports_only_what_changed(wiki, capsys):
    assert _run("snapshot", "record", "wp") == 0
    out = capsys.readouterr().out
    assert "documents: 3" in out

    # the origin publishes a new dump: one edit, one new page, one deletion
    con = db.connect(wiki)
    con.execute("UPDATE documents SET txt_hash='BB' WHERE uid='gutenberg:2'")
    con.execute("DELETE FROM documents WHERE uid='gutenberg:3'")
    con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                "txt_hash,chunks) VALUES('gutenberg:4','gutenberg','4','/x',1,'dd',2)")

    assert _run("snapshot", "record", "wp", "--snapshot", "20260801") == 0
    capsys.readouterr()

    assert _run("snapshot", "delta", "wp",
                "--from", "20260701", "--to", "20260801") == 0
    out = capsys.readouterr().out
    assert "added:   1" in out
    assert "changed: 1" in out
    assert "removed: 1" in out


def test_the_delta_lands_where_list_can_find_it(wiki, capsys):
    _run("snapshot", "record", "wp")
    _run("snapshot", "record", "wp", "--snapshot", "20260801")
    capsys.readouterr()

    assert _run("snapshot", "list", "wp") == 0
    out = capsys.readouterr().out
    assert "20260701" in out and "20260801" in out
    assert "*" in out, "the pinned snapshot must be marked"


def test_recording_an_unpinned_collection_says_what_to_do(isolated_config, data_dir,
                                                           config_dir):
    _write_pinned(data_dir, config_dir, "plain", snapshot=None)
    with pytest.raises(SystemExit, match="pins no snapshot"):
        _run("snapshot", "record", "plain")


def test_diffing_a_snapshot_that_was_never_recorded_is_refused(wiki):
    _run("snapshot", "record", "wp")
    with pytest.raises(SystemExit, match="no recorded manifest"):
        _run("snapshot", "delta", "wp", "--from", "20260701", "--to", "19990101")


def test_vector_uids_exclude_tombstones(wiki, tmp_path, capsys):
    """A deleted document has no vectors to ship — it has vectors to delete,
    which is the tombstone's job on the consumer, not a bundle's."""
    _run("snapshot", "record", "wp")
    con = db.connect(wiki)
    con.execute("UPDATE documents SET txt_hash='BB' WHERE uid='gutenberg:2'")
    con.execute("DELETE FROM documents WHERE uid='gutenberg:3'")
    _run("snapshot", "record", "wp", "--snapshot", "20260801")
    _run("snapshot", "delta", "wp", "--from", "20260701", "--to", "20260801")
    capsys.readouterr()

    delta = snapshots.snapshot_dir(wiki) / "20260701..20260801.delta.jsonl"
    assert snapshots.vector_uids(delta) == ["gutenberg:2"], \
        "only the changed document; the deleted one ships nothing"


def test_a_delta_with_no_content_changes_ships_nothing(wiki, capsys):
    """Deletions alone are a corpus delta with no vector delta at all."""
    _run("snapshot", "record", "wp")
    db.connect(wiki).execute("DELETE FROM documents WHERE uid='gutenberg:3'")
    _run("snapshot", "record", "wp", "--snapshot", "20260801")
    _run("snapshot", "delta", "wp", "--from", "20260701", "--to", "20260801")
    capsys.readouterr()

    delta = snapshots.snapshot_dir(wiki) / "20260701..20260801.delta.jsonl"
    assert _run("snapshot", "vectors", "wp", "--delta", str(delta)) == 0
    assert "nothing to ship" in capsys.readouterr().out


def _delta_with_one_change(wiki, capsys):
    _run("snapshot", "record", "wp")
    db.connect(wiki).execute("UPDATE documents SET txt_hash='BB' "
                             "WHERE uid='gutenberg:2'")
    _run("snapshot", "record", "wp", "--snapshot", "20260801")
    _run("snapshot", "delta", "wp", "--from", "20260701", "--to", "20260801")
    capsys.readouterr()
    return snapshots.snapshot_dir(wiki) / "20260701..20260801.delta.jsonl"


def test_exporting_without_a_signing_key_is_refused_not_raised(wiki, capsys):
    """A consumer-role node has no signing key by design (importing needs
    only public keys). Asking it to publish is an ordinary mistake and
    deserves a sentence, not a traceback."""
    delta = _delta_with_one_change(wiki, capsys)
    assert _run("snapshot", "vectors", "wp", "--delta", str(delta)) == 1
    err = capsys.readouterr().err
    assert "REFUSED" in err and "NODE_KEY_PATH" in err


def test_exporting_vectors_that_do_not_exist_yet_is_refused(wiki, capsys,
                                                             tmp_path, monkeypatch):
    """A delta names documents whose TEXT changed; their vectors exist only
    once something re-embeds them. Publishing before that would ship a
    bundle whose whole point is missing."""
    from app import config
    key = tmp_path / "node.key"
    _run("bundle", "keygen", str(key))
    monkeypatch.setattr(config, "NODE_KEY_PATH", key)
    capsys.readouterr()

    delta = _delta_with_one_change(wiki, capsys)
    assert _run("snapshot", "vectors", "wp", "--delta", str(delta)) == 1
    err = capsys.readouterr().err
    assert "REFUSED" in err and "vector table" in err


def test_the_apply_order_is_documented_where_it_bites():
    """Applying the planes in the wrong order fails as "these vectors were
    computed from a different revision of the text", which reads like the
    producer's fault rather than a sequencing mistake."""
    assert snapshots.APPLY_ORDER == ("corpus delta", "vector delta")

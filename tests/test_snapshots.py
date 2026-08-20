"""Snapshot manifests and delta chains.

The properties under test are the ones whose absence is SILENT: a delta
applied to the wrong base, a deletion that never propagates, a chain with a
hole in it. Each of those produces a corpus that looks fine and agrees with
nobody.
"""
import json
from pathlib import Path

import pytest

from app import snapshots
from app.snapshots import ADDED, CHANGED, REMOVED, Change, Entry, SnapshotError


def _manifest(path: Path, entries) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        Entry(uid=u, content=c, chunks=n).as_line() + "\n"
        for u, c, n in entries), encoding="utf-8", newline="\n")
    return path


def _kinds(changes):
    return sorted((c.kind, c.uid) for c in changes)


# ---------------- writing from a live manifest ----------------

def test_write_manifest_streams_in_uid_order(isolated_config, tmp_path):
    from app import db
    con = db.connect()
    for uid, h in (("w:3", "cc"), ("w:1", "aa"), ("w:2", "bb")):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "txt_hash,chunks) VALUES(?,'w',?,'/x',1,?,7)",
                    (uid, uid.split(":")[1], h))

    out = tmp_path / "snap.jsonl"
    digest = snapshots.write_manifest(con, out)
    entries = list(snapshots.read_manifest(out))

    assert [e.uid for e in entries] == ["w:1", "w:2", "w:3"], "sorted in SQL"
    assert [e.content for e in entries] == ["aa", "bb", "cc"]
    assert entries[0].chunks == 7
    assert digest == snapshots.manifest_digest(out), \
        "the digest returned must be the digest of what was written"


def test_out_of_scope_documents_are_not_claimed(isolated_config, tmp_path):
    """A document the collection's scope excludes is not part of what this
    node holds, and advertising it to a peer would be a lie."""
    from app import db
    con = db.connect()
    con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,txt_hash) "
                "VALUES('w:1','w','1','/x',1,'aa')")
    con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,txt_hash) "
                "VALUES('w:2','w','2','/x',0,'bb')")

    out = tmp_path / "snap.jsonl"
    snapshots.write_manifest(con, out)
    assert [e.uid for e in snapshots.read_manifest(out)] == ["w:1"]


def test_an_unsorted_manifest_is_refused_not_merged(tmp_path):
    """The diff is a two-pointer merge, so an unsorted manifest would yield
    a plausible-looking WRONG delta rather than an error."""
    bad = _manifest(tmp_path / "bad.jsonl",
                    [("w:2", "bb", 1), ("w:1", "aa", 1)])
    with pytest.raises(SnapshotError, match="not sorted"):
        list(snapshots.read_manifest(bad))


def test_a_malformed_line_names_its_line_number(tmp_path):
    path = tmp_path / "x.jsonl"
    path.write_text('{"uid":"w:1","content":"a"}\nnot json\n', encoding="utf-8")
    with pytest.raises(SnapshotError, match=r":2: malformed"):
        list(snapshots.read_manifest(path))


# ---------------- the diff ----------------

def test_diff_finds_added_changed_and_removed(tmp_path):
    parent = _manifest(tmp_path / "p.jsonl", [
        ("w:1", "aa", 1), ("w:2", "bb", 1), ("w:3", "cc", 1)])
    child = _manifest(tmp_path / "c.jsonl", [
        ("w:1", "aa", 1),          # untouched
        ("w:2", "BB", 2),          # edited
        ("w:4", "dd", 1)])         # new  (w:3 deleted upstream)

    assert _kinds(snapshots.diff(parent, child)) == [
        (ADDED, "w:4"), (CHANGED, "w:2"), (REMOVED, "w:3")]


def test_unchanged_documents_produce_nothing(tmp_path):
    """The entire economic argument: a delta must cost what CHANGED, not
    what exists."""
    # sorted() because uids order LEXICOGRAPHICALLY, not numerically:
    # "w:10" precedes "w:9". SQL and read_manifest agree on byte order, so
    # real manifests are consistent — a hand-built one has to say so.
    entries = sorted((f"w:{i}", f"h{i}", 1) for i in range(500))
    parent = _manifest(tmp_path / "p.jsonl", entries)
    child = _manifest(tmp_path / "c.jsonl", entries)
    assert list(snapshots.diff(parent, child)) == []


def test_deletions_are_first_class(tmp_path):
    """discovery never prunes unseen documents (see plugins/cursor.py), so
    without tombstones a retracted article stays in the corpus forever."""
    parent = _manifest(tmp_path / "p.jsonl", [("w:1", "aa", 1), ("w:2", "bb", 1)])
    child = _manifest(tmp_path / "c.jsonl", [("w:1", "aa", 1)])
    changes = list(snapshots.diff(parent, child))
    assert [(c.kind, c.uid) for c in changes] == [(REMOVED, "w:2")]


def test_an_empty_parent_is_all_additions(tmp_path):
    """The first snapshot: a delta from nothing is the reference itself."""
    parent = _manifest(tmp_path / "p.jsonl", [])
    child = _manifest(tmp_path / "c.jsonl", [("w:1", "aa", 1), ("w:2", "bb", 1)])
    assert _kinds(snapshots.diff(parent, child)) == [(ADDED, "w:1"), (ADDED, "w:2")]


# ---------------- delta files ----------------

def _build(tmp_path, parent_entries, child_entries, **kw):
    parent = _manifest(tmp_path / "p.jsonl", parent_entries)
    child = _manifest(tmp_path / "c.jsonl", child_entries)
    out = tmp_path / "d.jsonl"
    header = snapshots.build_delta(
        parent, child, out,
        collection=kw.get("collection", "wikipedia"), plugin="wikipedia",
        parent_snapshot="20260701", child_snapshot="20260801",
        chunk_version=kw.get("chunk_version", 1),
        cleaning_version=kw.get("cleaning_version", 1),
        created_at=1_787_000_000)
    return parent, child, out, header


def test_a_delta_round_trips_with_its_counts(tmp_path):
    _, _, out, header = _build(
        tmp_path,
        [("w:1", "aa", 1), ("w:2", "bb", 1), ("w:3", "cc", 1)],
        [("w:1", "aa", 1), ("w:2", "BB", 3), ("w:4", "dd", 2)])

    assert header.counts == {ADDED: 1, CHANGED: 1, REMOVED: 1}
    read_header, changes = snapshots.read_delta(out)
    assert read_header == header, "the header on disk must carry the counts"
    assert _kinds(changes) == [(ADDED, "w:4"), (CHANGED, "w:2"), (REMOVED, "w:3")]


def test_the_header_binds_the_delta_to_its_parent(tmp_path):
    parent, child, out, header = _build(
        tmp_path, [("w:1", "aa", 1)], [("w:1", "AA", 1)])
    assert header.parent_digest == snapshots.manifest_digest(parent)
    assert header.child_digest == snapshots.manifest_digest(child)
    assert header.parent_digest != header.child_digest


def test_an_unknown_format_is_refused(tmp_path):
    out = tmp_path / "d.jsonl"
    out.write_text(json.dumps({"format": "someone-elses/9", "collection": "w",
                                "plugin": "w", "parent_snapshot": None,
                                "parent_digest": None, "child_snapshot": "x",
                                "child_digest": "y", "chunk_version": 1,
                                "cleaning_version": 1}) + "\n", encoding="utf-8")
    with pytest.raises(SnapshotError, match="unknown delta format"):
        snapshots.read_delta(out)


# ---------------- admission ----------------

def test_a_delta_for_the_wrong_parent_is_refused(tmp_path):
    """The silent one: applying it produces a state no other peer has, and
    nothing downstream can detect it afterwards."""
    _, _, _, header = _build(tmp_path, [("w:1", "aa", 1)], [("w:1", "AA", 1)])
    with pytest.raises(SnapshotError, match="applying it would produce a state"):
        snapshots.check_applicable(
            header, local_digest="f" * 64, collection="wikipedia",
            chunk_version=1, cleaning_version=1)


def test_the_matching_parent_is_accepted(tmp_path):
    parent, _, _, header = _build(tmp_path, [("w:1", "aa", 1)], [("w:1", "AA", 1)])
    snapshots.check_applicable(
        header, local_digest=snapshots.manifest_digest(parent),
        collection="wikipedia", chunk_version=1, cleaning_version=1)


def test_a_delta_from_another_collection_is_refused(tmp_path):
    _, _, _, header = _build(tmp_path, [("w:1", "aa", 1)], [("w:1", "AA", 1)])
    with pytest.raises(SnapshotError, match="not 'pubmed'"):
        snapshots.check_applicable(header, local_digest=header.parent_digest,
                                    collection="pubmed", chunk_version=1,
                                    cleaning_version=1)


@pytest.mark.parametrize("field,value,match", [
    ("chunk_version", 2, "CHUNK_VERSION"),
    ("cleaning_version", 2, "cleaning version"),
])
def test_a_delta_from_a_different_segmentation_is_refused(tmp_path, field, value, match):
    """Applying it would mix two incompatible segmentations of the same
    text (invariant 3)."""
    _, _, _, header = _build(tmp_path, [("w:1", "aa", 1)], [("w:1", "AA", 1)])
    kwargs = {"chunk_version": 1, "cleaning_version": 1, field: value}
    with pytest.raises(SnapshotError, match=match):
        snapshots.check_applicable(header, local_digest=header.parent_digest,
                                    collection="wikipedia", **kwargs)


def test_a_reference_delta_has_no_parent_to_check(tmp_path):
    """A delta from nothing is the reference; it applies to a fresh node."""
    parent = _manifest(tmp_path / "p.jsonl", [])
    child = _manifest(tmp_path / "c.jsonl", [("w:1", "aa", 1)])
    header = snapshots.build_delta(
        parent, child, tmp_path / "d.jsonl", collection="wikipedia",
        plugin="wikipedia", parent_snapshot=None, child_snapshot="20260801",
        chunk_version=1, cleaning_version=1)
    snapshots.check_applicable(header, local_digest=None, collection="wikipedia",
                                chunk_version=1, cleaning_version=1)


# ---------------- chains ----------------

def _header(parent_digest, child_digest):
    return snapshots.DeltaHeader(
        format=snapshots.FORMAT, collection="wikipedia", plugin="wikipedia",
        parent_snapshot="p", parent_digest=parent_digest,
        child_snapshot="c", child_digest=child_digest,
        chunk_version=1, cleaning_version=1)


def test_a_chain_orders_parent_to_child_whatever_order_it_arrives_in():
    a, b, c = _header("A", "B"), _header("B", "C"), _header("C", "D")
    assert snapshots.chain([c, a, b]) == [a, b, c]


def test_a_gap_in_the_chain_is_refused():
    """Applying what is left would skip changes with nothing noticing."""
    a, c = _header("A", "B"), _header("C", "D")
    with pytest.raises(SnapshotError, match="do not form a single line"):
        snapshots.chain([a, c])


def test_a_forked_chain_is_refused():
    a, b = _header("A", "B"), _header("A", "C")
    with pytest.raises(SnapshotError, match="fork"):
        snapshots.chain([a, b])


def test_an_empty_chain_is_not_an_error():
    assert snapshots.chain([]) == []


def test_uid_order_is_lexicographic_not_numeric(isolated_config, tmp_path):
    """A trap for anything that builds a manifest by hand: "w:10" sorts
    BEFORE "w:9". write_manifest orders in SQL, whose default TEXT collation
    is byte order, and read_manifest verifies the same — so the two agree by
    construction and a hand-rolled numeric sort would be rejected."""
    from app import db
    con = db.connect()
    for i in (1, 2, 9, 10, 11):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "txt_hash) VALUES(?,'w',?,'/x',1,?)",
                    (f"w:{i}", str(i), f"h{i}"))

    out = tmp_path / "snap.jsonl"
    snapshots.write_manifest(con, out)
    uids = [e.uid for e in snapshots.read_manifest(out)]
    assert uids == ["w:1", "w:10", "w:11", "w:2", "w:9"]
    assert uids == sorted(uids), "SQL order and the verifier must agree"

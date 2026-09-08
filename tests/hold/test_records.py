"""tests/hold/test_records.py — the roots a snapshot publishes over a shard,
and the two things a build refuses.

docs/09-formats.md §9.4 (the shard entry), §9.5 (the record line and its
hashes), §9.14 (the relations shard); docs/02-collections.md §2.2, §2.3.
"""
import hashlib
import json

import pytest

from app.hold import records as rec
from app.hold.records import RecordsError


# ---- content_hash ---------------------------------------------------------

def test_content_hash_is_over_the_title_and_sections_and_nothing_else():
    """09 §9.5: the title and the sections exactly as the chunker sees them."""
    sections = [["Chapter 1", "It is a truth universally acknowledged"],
                ["Chapter 2", "Mr. Bennet was among the earliest"]]
    expected = hashlib.blake2b(
        json.dumps(["Pride and Prejudice", sections], ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8"),
        digest_size=16).hexdigest()
    assert rec.content_hash("Pride and Prejudice", sections) == expected
    assert len(rec.content_hash("t", [])) == 32


def test_content_hash_agrees_with_the_existing_pack_writer():
    """The shard writer computes the same value; two implementations of one
    hash is exactly how a corpus stops verifying against itself."""
    from app.pack import records as pack_records
    sections = [["h", "body"], ["h2", "more"]]
    assert rec.content_hash("T", sections) == pack_records.content_hash("T", sections)


def test_the_same_text_hashes_the_same_whatever_else_moved():
    a = rec.content_hash("T", [["h", "body"]])
    b = rec.content_hash("T", [["h", "body"]])
    assert a == b
    assert rec.content_hash("T", [["h", "bodyy"]]) != a


# ---- records_root ---------------------------------------------------------

def test_records_root_is_over_the_raw_digests_in_record_order():
    hashes = ["2c9e6f0b1a8d4e7f3b5c0a9d8e7f6a1b",
              "a3f0c1d2e3f4a5b6c7d8e9f0a1b2c3d4"]
    expected = "sha256:" + hashlib.sha256(
        bytes.fromhex(hashes[0]) + bytes.fromhex(hashes[1])).hexdigest()
    assert rec.records_root(hashes) == expected


def test_records_root_depends_on_order():
    a = ["2c9e6f0b1a8d4e7f3b5c0a9d8e7f6a1b", "a3f0c1d2e3f4a5b6c7d8e9f0a1b2c3d4"]
    assert rec.records_root(a) != rec.records_root(list(reversed(a)))


def test_records_root_refuses_a_hash_of_the_wrong_width():
    with pytest.raises(RecordsError, match="BLAKE2b-128, 32 hex"):
        rec.records_root(["deadbeef"])


def test_a_shard_holds_at_least_one_record():
    with pytest.raises(RecordsError, match="at least one record"):
        rec.records_root([])


# ---- chunks_root ----------------------------------------------------------

def test_chunks_root_is_the_id_then_the_hash_of_the_text():
    chunks = [("gutenberg:1342:0:2", "first chunk"),
              ("gutenberg:1342:1:2", "second chunk")]
    h = hashlib.sha256()
    for cid, text in chunks:
        h.update(cid.encode("utf-8"))
        h.update(hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest())
    assert rec.chunks_root(chunks) == "sha256:" + h.hexdigest()


def test_chunks_root_catches_a_boundary_move_that_keeps_the_count():
    """The whole reason it exists (09 §9.5): a chunker that moved every
    boundary without changing the number would pass a count comparison."""
    before = [("u:0:1", "aaaa bbbb"), ("u:1:1", "cccc dddd")]
    after = [("u:0:1", "aaaa"), ("u:1:1", "bbbb cccc dddd")]
    assert len(before) == len(after)
    assert rec.chunks_root(before) != rec.chunks_root(after)


def test_chunks_root_catches_a_version_bump_that_keeps_the_text():
    same_text = "the same words"
    assert rec.chunks_root([("u:0:1", same_text)]) != \
        rec.chunks_root([("u:0:2", same_text)])


def test_a_record_with_no_chunks_contributes_nothing_but_is_not_an_error():
    """06 §6.1: a document with no sections is still a record, with zero
    chunks."""
    assert rec.chunks_root([]) == "sha256:" + hashlib.sha256().hexdigest()


def test_the_chunk_id_is_uid_seq_version():
    assert rec.chunk_id("pubmed:38112034", 0, 1) == "pubmed:38112034:0:1"


# ---- ordering -------------------------------------------------------------

def test_numeric_and_byte_order_disagree_and_the_declaration_decides():
    ids = ["9", "10"]
    assert sorted(ids, key=lambda i: rec.order_key(i, "numeric")) == ["9", "10"]
    assert sorted(ids, key=lambda i: rec.order_key(i, "bytes")) == ["10", "9"]


def test_a_non_integer_id_under_numeric_order_is_a_build_error():
    """Not a silent fallback to bytes: the two orders disagree, and a
    fallback would cut two peers' shards differently."""
    with pytest.raises(RecordsError, match="not an integer"):
        rec.order_key("chr1", "numeric")
    assert rec.order_key("chr1", "bytes") == b"chr1"


def test_an_unknown_order_is_refused():
    with pytest.raises(RecordsError, match="bytes|numeric"):
        rec.order_key("1", "alphabetical")


def test_records_are_cut_in_native_id_order():
    rec.check_in_order(["1", "2", "10"], "numeric")
    rec.check_in_order(["chr1", "chr10", "chrX"], "bytes")
    with pytest.raises(RecordsError, match="sorts before"):
        rec.check_in_order(["10", "9"], "numeric")


def test_an_out_of_order_native_id_is_refused_naming_record_and_shard():
    """02 §2.2: refused rather than placed out of order, so shard ranges
    stay a partition of the id space (05 §5.5)."""
    with pytest.raises(RecordsError, match=r"'50'.*'100'.*year=1980/00003"):
        rec.refuse_out_of_order("50", "100", "numeric",
                                slice_="year=1980", n=3)


def test_a_record_at_or_after_the_open_shards_first_id_is_fine():
    rec.refuse_out_of_order("100", "100", "numeric", slice_="s", n=1)
    rec.refuse_out_of_order("101", "100", "numeric", slice_="s", n=1)


def test_the_first_shard_of_a_slice_has_nothing_to_sort_after():
    rec.refuse_out_of_order("1", None, "numeric", slice_="s", n=1)


def test_the_refusal_reads_the_declared_order_not_the_digits():
    """"50" sorts before "100" numerically and after it in bytes."""
    rec.refuse_out_of_order("50", "100", "bytes", slice_="s", n=1)
    with pytest.raises(RecordsError):
        rec.refuse_out_of_order("50", "100", "numeric", slice_="s", n=1)


# ---- origin_hash ----------------------------------------------------------

def test_a_record_without_an_origin_hash_is_refused():
    """Invariant 11: refused at build, never warned about."""
    with pytest.raises(RecordsError, match="no origin_hash"):
        rec.refuse_without_origin_hash("gutenberg:1", None)
    with pytest.raises(RecordsError, match="no origin_hash"):
        rec.refuse_without_origin_hash("gutenberg:1", "")


def test_an_origin_hash_is_blake2b_128_with_no_prefix():
    rec.refuse_without_origin_hash("gutenberg:1", "f1e2d3c4b5a69788796a5b4c3d2e1f00")
    for bad in ("sha256:" + "a" * 64, "F1E2D3C4B5A69788796A5B4C3D2E1F00", "abc"):
        with pytest.raises(RecordsError, match="32 lowercase hex"):
            rec.refuse_without_origin_hash("gutenberg:1", bad)


# ---- relations ------------------------------------------------------------

def test_an_edge_line_has_the_four_keys_in_order():
    line = rec.edge_line("eli:it/2020/1", "eli:it/1990/7", "eli:amends", "2026-08")
    assert line.endswith(b"\n")
    assert line.decode() == (
        '{"from":"eli:it/2020/1","to":"eli:it/1990/7",'
        '"type":"eli:amends","snapshot":"2026-08"}\n')
    assert list(json.loads(line)) == ["from", "to", "type", "snapshot"]


def test_an_edge_type_is_namespaced_by_its_plugin():
    with pytest.raises(RecordsError, match="<plugin>:<name>"):
        rec.edge_line("a", "b", "amends", "2026-08")


def test_a_plugin_without_relations_yields_nothing():
    class Bare:
        pass
    assert rec.edges_of(Bare(), object(), from_uid="u", snapshot_id="2026-08") == []


def test_the_platform_supplies_from_and_snapshot():
    """06 §6.1: the plugin supplies `to` and `type`; `from` is the document's
    own uid and `snapshot` is the one the edge is first listed in, neither of
    which the plugin can know."""
    from app.plugins.base import Edge

    class Citing:
        def relations(self, doc):
            yield Edge(to="pubmed:111", type="pubmed:cites")
            yield Edge(to="pubmed:222", type="pubmed:cites")

    lines = rec.edges_of(Citing(), object(), from_uid="pubmed:999",
                         snapshot_id="2026-W36")
    assert [json.loads(l) for l in lines] == [
        {"from": "pubmed:999", "to": "pubmed:111", "type": "pubmed:cites",
         "snapshot": "2026-W36"},
        {"from": "pubmed:999", "to": "pubmed:222", "type": "pubmed:cites",
         "snapshot": "2026-W36"}]


def test_an_edge_may_point_at_a_uid_this_collection_does_not_hold():
    """09 §9.14: a dangling edge is data, not an error (05 §5.5)."""
    line = rec.edge_line("pubmed:1", "doi:10.1000/xyz", "pubmed:cites", "2026-08")
    assert json.loads(line)["to"] == "doi:10.1000/xyz"

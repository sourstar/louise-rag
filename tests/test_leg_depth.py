"""A leg contributes its top records to the depth — RECORDS, not chunks
(05 §5.1, amended 2026-10-10; app/leg_depth.py).

Found live on 906f06b: each list asked the index for 50 CHUNKS, so the
vector leg brought a median of 38 books, not 50, and a text variant whose
first 50 chunks fell in three books gave the leg three records. Measured on
118 live queries: 50 vector chunks hold a median 43 records; the text leg's
top 50 records changed on 51 of 118 queries once its variants went deep
enough."""
from app import embeddings, fts, leg_depth


def chunks(spec: list[int]) -> list[dict]:
    """One hit per entry: the record number; seq counts within it."""
    seen: dict[int, int] = {}
    out = []
    for r in spec:
        seen[r] = seen.get(r, -1) + 1
        out.append({"chunk_id": f"g:{r}:{seen[r]}:1", "book_id": r, "source": "dense"})
    return out


class Index:
    def __init__(self, spec):
        self.all = chunks(spec)
        self.asked: list[int] = []

    def fetch(self, n):
        self.asked.append(n)
        return self.all[:n]


def test_a_list_deepens_until_it_holds_the_depth_in_records():
    # three chunks per book: 10 chunks hold 4 books
    idx = Index([b for b in range(1, 40) for _ in range(3)])
    got = leg_depth.to_records(idx.fetch, depth=10)
    assert idx.asked == [10, 20, 40]
    assert leg_depth.records_in(got) == 10
    assert got[-1]["chunk_id"] == "g:10:0:1", "the list ends at the chunk bringing the 10th"


def test_one_fetch_when_the_first_holds_enough():
    idx = Index(list(range(1, 200)))
    got = leg_depth.to_records(idx.fetch, depth=50, first=100)
    assert idx.asked == [100] and len(got) == 50


def test_an_index_that_runs_out_is_not_asked_again():
    idx = Index([1, 1, 2, 2, 3])
    got = leg_depth.to_records(idx.fetch, depth=10)
    assert idx.asked == [10] and leg_depth.records_in(got) == 3


def test_a_single_long_book_is_bounded():
    idx = Index([1] * 10_000)
    got = leg_depth.to_records(idx.fetch, depth=5)
    assert idx.asked[-1] == 5 * leg_depth.CHUNK_CAP
    assert max(idx.asked) <= 5 * leg_depth.CHUNK_CAP
    assert leg_depth.records_in(got) == 1


def test_a_native_id_with_a_colon_is_one_record():
    assert leg_depth.record_of_chunk("wikipedia:en:Rome:12:3") == "wikipedia:en:Rome"


def test_the_vector_leg_embeds_once_for_the_measured_case(monkeypatch):
    """Twice the depth held 50 records on every query measured (a median 59
    chunks needed, at most 82), so the query is embedded once."""
    asked = []
    spec = [b for b in range(1, 200) for _ in range(2)][:400]
    monkeypatch.setattr(embeddings, "search",
                        lambda q, k=20, langs=None, collection=None, book_ids=None:
                        asked.append(k) or chunks(spec)[:k])
    got = embeddings.search_records("q", 30)
    assert asked == [60] and leg_depth.records_in(got) == 30


def test_the_text_variant_passes_its_restriction_through(monkeypatch):
    asked = []
    monkeypatch.setattr(fts, "search",
                        lambda group, q, limit=20, collection=None, langs=None, book_ids=None:
                        asked.append((limit, langs, book_ids)) or chunks([1] * 3 + [2])[:limit])
    got = fts.search_records("en", "q", 2, langs=["en"], book_ids=[1, 2])
    assert asked == [(2, ["en"], [1, 2]), (4, ["en"], [1, 2])]
    assert leg_depth.records_in(got) == 2

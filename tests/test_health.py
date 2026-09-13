"""`louise health` — is the pipeline stuck, and if so why.

Operator, 2026-09-13: "I TOLD YOU TO ADD THIS CHECK TO YOUR FUCKING TEST
POOL". "pubmed is stuck" was reported three times in two days and was a
different thing each time — deleted shard files, documents stranded
behind a FINAL shard, and finally not stuck at all but waiting on an
encoder that `embed = "off"` had switched off. All three looked
identical from outside, because the worker's own summary of itself was
"idle — pipeline is caught up".

The distinction this has to get right, and the reason it is a check
rather than a page: a stage with no work is IDLE and fine; a stage with
work it cannot start is BLOCKED.
"""
import sqlite3

import pytest

from app import health


class Collection:
    def __init__(self, name="pubmed", archive=None):
        self.name = name
        self.genesis_archive = archive


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE documents (uid TEXT PRIMARY KEY, native_id TEXT, "
              "in_scope INTEGER DEFAULT 1, rdf_done INTEGER DEFAULT 0, "
              "fts_done INTEGER DEFAULT 0, embed_done INTEGER DEFAULT 0, "
              "error TEXT)")
    c.execute("CREATE TABLE control (key TEXT PRIMARY KEY, value TEXT)")
    # the real index, because the counts name it with `INDEXED BY` — which
    # raises outright when it is absent, and that is the point (a stage
    # that could not be counted must be reported, not skipped)
    c.execute("CREATE INDEX idx_documents_scope ON documents"
              "(in_scope, rdf_done, fts_done, embed_done)")
    return c


def _docs(con, n, *, rdf=1, fts=1, embed=0, error=None, start=1):
    for i in range(start, start + n):
        con.execute("INSERT INTO documents(uid, native_id, rdf_done, fts_done, "
                    "embed_done, error) VALUES (?,?,?,?,?,?)",
                    (f"u{i}", str(i), rdf, fts, embed, error))


def _scope(monkeypatch, embed_mode):
    monkeypatch.setattr(health.db, "get_scope", lambda con: {"embed_mode": embed_mode})


def test_work_waiting_on_a_switched_off_stage_is_BLOCKED(con, monkeypatch):
    """The 4,577,101 case. This is the whole point: the worker called
    itself caught up while millions of documents waited for a stage that
    would never start."""
    _docs(con, 5)
    _scope(monkeypatch, "off")
    got = health.inspect(con, Collection())
    embed = next(s for s in got.stages if s.name == "embed")
    assert embed.state == health.BLOCKED
    assert embed.waiting == 5
    assert "embed is OFF" in embed.reason
    assert "pubmed.toml" in embed.reason, "and it says where the switch is"
    assert got.blocked()


def test_a_switched_off_stage_with_NOTHING_to_do_is_not_blocked(con, monkeypatch):
    """"Off and finished" is not a problem. Reporting it would make the
    check cry wolf on every idle collection, and a check that cries wolf
    is the one nobody reads — which is how "idle — pipeline is caught up"
    got ignored for two days."""
    _docs(con, 5, embed=1)
    _scope(monkeypatch, "off")
    got = health.inspect(con, Collection())
    assert not got.blocked()
    assert all(s.state == health.IDLE for s in got.stages)


def test_work_waiting_on_a_stage_that_CAN_run_is_running_not_blocked(con, monkeypatch):
    _docs(con, 5)
    _scope(monkeypatch, "all")
    got = health.inspect(con, Collection())
    embed = next(s for s in got.stages if s.name == "embed")
    assert embed.state == health.RUNNING and not got.blocked()


def test_errored_documents_are_counted_only_when_asked(con, monkeypatch):
    """An error removes a document from every selection until a human
    intervenes (CLAUDE.md), so it must be visible — but counting them is
    the most expensive thing here, 43.56 s on pubmed to return zero,
    because `error` is in no index. Opt-in, and the stage counts include
    them rather than paying to exclude them (see `_WAITING`)."""
    _docs(con, 3)
    _docs(con, 2, error="boom", start=100)
    _scope(monkeypatch, "all")
    assert health.inspect(con, Collection()).errors == 0, "not counted by default"
    got = health.inspect(con, Collection(), errors=True)
    assert got.errors == 2
    embed = next(s for s in got.stages if s.name == "embed")
    assert embed.waiting == 5, "the five include the two errored: exact exclusion costs 23x"


def test_the_stages_are_reported_in_pipeline_order(con, monkeypatch):
    _docs(con, 1)
    _scope(monkeypatch, "all")
    got = health.inspect(con, Collection())
    assert [s.name for s in got.stages] == ["rdf", "fts", "embed"]


def test_a_stage_counts_only_what_cleared_the_previous_one(con, monkeypatch):
    """fts waits on rdf. A document that has not been parsed is not fts's
    backlog, and counting it there would blame the wrong stage."""
    _docs(con, 4, rdf=0, fts=0)
    _scope(monkeypatch, "all")
    got = {s.name: s.waiting for s in health.inspect(con, Collection()).stages}
    assert got["rdf"] == 4 and got["fts"] == 0


def test_an_unreadable_manifest_is_a_note_not_a_crash(monkeypatch):
    """A health check that dies is worse than no health check."""
    class Boom:
        def execute(self, *a):
            raise sqlite3.OperationalError("no such table: documents")
    got = health.inspect(Boom(), Collection())
    assert got.note and not got.blocked()


# ---- the archive gap, which is invisible from the stage counts ------------

class FakeArchive:
    archive_id = "medcpt-pubmed"
    local = False
    ids_per_shard = 1_000_000
    shards = ["0", "1"]

    def id_range_of(self, shard):
        n = int(shard)
        return (n * self.ids_per_shard, (n + 1) * self.ids_per_shard - 1)


@pytest.fixture
def archived(con, monkeypatch):
    from app.bundles import genesis
    monkeypatch.setitem(genesis.ARCHIVES, "medcpt-pubmed", FakeArchive())
    con.execute("CREATE TABLE genesis_imports (archive TEXT, shard TEXT, "
                "state TEXT, PRIMARY KEY (archive, shard))")
    for s in ("0", "1"):
        con.execute("INSERT INTO genesis_imports VALUES ('medcpt-pubmed',?,'final')", (s,))
    return con


def test_documents_the_archive_owes_are_reported_separately(archived):
    """THE ONE THAT HID TWICE. Those documents are waiting for embed, so
    they look like ordinary encoder backlog — while the vectors for them
    already exist and are free."""
    _docs(archived, 3, start=1_500_000)
    got = health.archive_gap(archived, Collection(archive="medcpt-pubmed"))
    assert got.state == health.BLOCKED and got.waiting == 3
    assert "genesis recheck pubmed" in got.reason


def test_no_gap_when_the_archive_has_served_everything(archived):
    _docs(archived, 3, embed=1, start=1_500_000)
    got = health.archive_gap(archived, Collection(archive="medcpt-pubmed"))
    assert got.state == health.IDLE and got.waiting == 0


def test_documents_outside_the_archive_range_are_not_its_debt(archived):
    """The 4,538,777 past PMID 36,999,999. They need the encoder, and
    blaming the archive for them would send an operator to re-run an
    import that has nothing left to give."""
    _docs(archived, 3, start=50_000_000)
    got = health.archive_gap(archived, Collection(archive="medcpt-pubmed"))
    assert got.state == health.IDLE


def test_a_collection_with_no_archive_reports_none(con):
    assert health.archive_gap(con, Collection()) is None


# ---- the check as a whole -------------------------------------------------

def test_check_returns_the_blocking_lines_across_collections(con, monkeypatch):
    _docs(con, 7)
    _scope(monkeypatch, "off")
    reports, blocking = health.check([Collection()], connect=lambda collection: con)
    assert len(reports) == 1
    assert any("pubmed/embed" in b and "embed is OFF" in b for b in blocking)


def test_an_all_clear_is_an_empty_list(con, monkeypatch):
    _docs(con, 7, embed=1)
    _scope(monkeypatch, "off")
    _, blocking = health.check([Collection()], connect=lambda collection: con)
    assert blocking == []


def test_a_collection_that_cannot_be_opened_is_noted_not_fatal(monkeypatch):
    def boom(collection):
        raise sqlite3.OperationalError("database is locked")
    reports, blocking = health.check([Collection()], connect=boom)
    assert reports[0].note.startswith("cannot open")
    assert blocking == []


def test_render_marks_the_blocked_stage(con, monkeypatch):
    _docs(con, 5)
    _scope(monkeypatch, "off")
    reports, _ = health.check([Collection()], connect=lambda collection: con)
    text = health.render(reports)
    assert "!!" in text and "embed is OFF" in text
    assert "pubmed" in text


def test_a_stage_that_cannot_be_counted_is_reported_not_skipped():
    """The one failure a health check must not have. A stage that
    silently vanished from the report would read as an all-clear — the
    "idle — pipeline is caught up" line again, in a new place. `INDEXED
    BY` raises outright when the index is absent, so this is reachable."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE documents (uid TEXT PRIMARY KEY, in_scope INTEGER, "
              "rdf_done INTEGER, fts_done INTEGER, embed_done INTEGER, error TEXT)")
    c.execute("CREATE TABLE control (key TEXT PRIMARY KEY, value TEXT)")
    # no idx_documents_scope: every count raises
    got = health.inspect(c, Collection())
    assert len(got.stages) == 3, "all three are still named"
    assert all(s.state == health.BLOCKED for s in got.stages)
    assert all("cannot be counted" in s.reason for s in got.stages)
    assert got.blocked(), "and it exits non-zero rather than looking clear"

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
        # green: a stand-in licensing cannot judge now reads as WITHHELD
        # (health fails closed); the license verdicts have their own tests
        self.plugin = "gutenberg"


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


@pytest.fixture(autouse=True)
def _judged_against_a_running_node(request, monkeypatch):
    """check() reads the REAL node's mode when none is given, and four of
    these tests once peeked /data/state/node.sqlite that way (review,
    2026-09-13). A test that uses isolated_config reads its own tmp store;
    every other one is judged against a running node, which holds nothing."""
    if "isolated_config" not in request.fixturenames:
        monkeypatch.setattr(health, "_node_mode", lambda: "running")


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


# ---- the node's mode: HELD is the operator's choice, not a fault ---------
#
# Live, 2026-09-13, minutes after the deploy: `rdf 24,012 running` on a node
# the Library held at acquire-only, where rdf stands down; and `run louise
# genesis recheck pubmed` for 38,324 documents whose 30 shards that very
# command had just reopened.

def test_acquire_only_holds_parsing_instead_of_calling_it_running(con, monkeypatch):
    _docs(con, 4, rdf=0, fts=0)
    _scope(monkeypatch, "all")
    got = health.inspect(con, Collection(), mode="acquire-only")
    rdf = next(s for s in got.stages if s.name == "rdf")
    assert rdf.state == health.HELD and "acquire-only" in rdf.reason
    assert not got.blocked(), "held is the operator's choice: never exit 1 for it"


def test_a_config_block_stays_blocked_whatever_the_mode(con, monkeypatch):
    """Resuming the node would not start an embed the TOML switched off."""
    _docs(con, 5)
    _scope(monkeypatch, "off")
    got = health.inspect(con, Collection(), mode="paused")
    embed = next(s for s in got.stages if s.name == "embed")
    assert embed.state == health.BLOCKED and "embed is OFF" in embed.reason


def test_a_running_node_holds_nothing(con, monkeypatch):
    _docs(con, 4, rdf=0, fts=0)
    _scope(monkeypatch, "all")
    got = health.inspect(con, Collection(), mode="running")
    assert next(s for s in got.stages if s.name == "rdf").state == health.RUNNING


def test_an_unknown_mode_invents_no_reason(con, monkeypatch):
    _docs(con, 4, rdf=0, fts=0)
    _scope(monkeypatch, "all")
    got = health.inspect(con, Collection(), mode="unknown")
    assert next(s for s in got.stages if s.name == "rdf").state == health.RUNNING


def _shard(con, shard, state):
    con.execute("INSERT OR REPLACE INTO genesis_imports VALUES ('medcpt-pubmed',?,?)",
                (shard, state))


def test_documents_under_a_REOPENED_shard_are_not_told_to_recheck(archived):
    _shard(archived, "1", "applied")        # what `genesis recheck` leaves
    _docs(archived, 3, start=1_500_000)
    got = health.archive_gap(archived, Collection(archive="medcpt-pubmed"), mode="running")
    assert got.state == health.RUNNING and got.waiting == 3
    assert "recheck" not in got.reason and "open shard" in got.reason


def test_open_shards_on_an_acquire_only_node_are_held(archived):
    """acquire-only fetches the shards and does not import them."""
    _shard(archived, "1", "applied")
    _docs(archived, 3, start=1_500_000)
    got = health.archive_gap(archived, Collection(archive="medcpt-pubmed"),
                             mode="acquire-only")
    assert got.state == health.HELD and "acquire-only" in got.reason


def test_a_rejected_shard_is_blocked_for_its_own_reason(archived):
    _shard(archived, "1", "rejected")
    _docs(archived, 2, start=1_500_000)
    got = health.archive_gap(archived, Collection(archive="medcpt-pubmed"))
    assert got.state == health.BLOCKED and "REJECTED" in got.reason
    assert "recheck" not in got.reason


def test_final_and_open_together_block_and_name_both(archived):
    _shard(archived, "1", "applied")
    _docs(archived, 2, start=500_000)       # under final shard 0
    _docs(archived, 3, start=1_500_000)     # under open shard 1
    got = health.archive_gap(archived, Collection(archive="medcpt-pubmed"), mode="running")
    assert got.state == health.BLOCKED and got.waiting == 5
    assert "2 under FINAL" in got.reason and "recheck pubmed" in got.reason
    assert "3 more under open" in got.reason


def test_check_reads_the_nodes_mode_and_creates_nothing(isolated_config, con, monkeypatch):
    from app import node_state
    _docs(con, 4, rdf=0, fts=0)
    _scope(monkeypatch, "all")
    reports, _ = health.check([Collection()], connect=lambda collection: con)
    assert not node_state.path().exists(), "a check creates nothing"
    assert reports[0].mode == "paused", "an absent store reads as the worker reads it"

    node_state.set_worker_state("acquire-only")
    reports, blocking = health.check([Collection()], connect=lambda collection: con)
    assert reports[0].mode == "acquire-only"
    assert next(s for s in reports[0].stages if s.name == "rdf").state == health.HELD
    assert blocking == []
    text = health.render(reports)
    assert text.startswith("node mode: acquire-only") and ".." in text


# ---- every gate is the worker's own (review of this module, 2026-09-13) ---

def test_a_stopped_archive_is_blocked_and_never_called_served(archived):
    """A shard rejected before any shard proved the archive stops it WHOLE:
    the worker fetches and imports nothing more (`genesis.archive_stopped`)
    and hands the ranges to the local encoder. "Being served", "running"
    and "held until you resume" were all false."""
    _shard(archived, "0", "rejected")
    _shard(archived, "1", "fetched")
    _docs(archived, 3, start=1_500_000)          # under the open shard only
    for mode in ("running", "acquire-only"):
        got = health.archive_gap(archived, Collection(archive="medcpt-pubmed"), mode=mode)
        assert got.state == health.BLOCKED, (mode, got)
        assert "STOPPED" in got.reason and "genesis retry pubmed" in got.reason
        assert "being served" not in got.reason and "held" not in got.reason


def test_an_unacknowledged_collection_is_withheld_not_running(con, monkeypatch):
    """Invariant 9: the worker indexes, embeds and imports nothing for a
    yellow or red collection without an acknowledgement, in any mode."""
    from app import licensing
    monkeypatch.setattr(licensing, "state_for", lambda c: licensing.MISSING)
    monkeypatch.setattr(licensing, "license_class_for", lambda c: "yellow")
    _docs(con, 3, rdf=0, fts=0)
    _scope(monkeypatch, "all")
    got = health.inspect(con, Collection(), mode="running")
    rdf = next(s for s in got.stages if s.name == "rdf")
    assert rdf.state == health.BLOCKED and "acknowledgement" in rdf.reason
    assert got.blocked()


def test_a_collection_refused_at_boot_is_reported_as_the_refusal(con, monkeypatch):
    """Invariant 1: a refused collection is not served — its backlog would
    only read as work in progress. Named, and exit 1."""
    monkeypatch.setattr(health, "_boot_refusals",
                        lambda cols: {"pubmed": "space_id mismatch (test)"})
    _docs(con, 3, rdf=0, fts=0)
    reports, blocking = health.check([Collection()], connect=lambda collection: con)
    assert [s.name for s in reports[0].stages] == ["boot"]
    assert reports[0].stages[0].state == health.BLOCKED
    assert any("fails the boot check" in b and "space_id mismatch" in b for b in blocking)


# ---- the verification pass of those fixes, 2026-09-13 --------------------

def test_the_stop_is_judged_on_the_ledgers_proof_columns(con, monkeypatch):
    """The fixture ledger above has no `proven_at`/`docs`, so the stopped-
    archive test passed with the old `SELECT shard, state` too. On the real
    schema a FINAL shard that imported nothing proves nothing; read without
    those columns it looks proven, and the stopped archive looks served."""
    from app.bundles import genesis
    monkeypatch.setitem(genesis.ARCHIVES, "medcpt-pubmed", FakeArchive())
    con.execute("CREATE TABLE genesis_imports (archive TEXT, shard TEXT, state TEXT, "
                "docs INTEGER DEFAULT 0, proven_at INTEGER, PRIMARY KEY (archive, shard))")
    for shard, state in (("0", "final"), ("1", "rejected"), ("2", "fetched")):
        con.execute("INSERT INTO genesis_imports VALUES ('medcpt-pubmed',?,?,0,NULL)",
                    (shard, state))
    _docs(con, 3, start=2_500_000)
    got = health.archive_gap(con, Collection(archive="medcpt-pubmed"), mode="running")
    assert got.state == health.BLOCKED and "STOPPED" in got.reason


def test_open_shards_beside_a_rejected_one_are_held_not_served_on_a_held_node(archived):
    _shard(archived, "1", "rejected")       # shard 0 is final: the archive is proven
    _shard(archived, "2", "fetched")
    _docs(archived, 2, start=1_500_000)     # under the rejected shard
    _docs(archived, 3, start=2_500_000)     # under the open one
    got = health.archive_gap(archived, Collection(archive="medcpt-pubmed"),
                             mode="acquire-only")
    assert got.state == health.BLOCKED and "REJECTED" in got.reason
    assert "being served" not in got.reason and "held" in got.reason


def test_a_declared_shard_not_fetched_yet_is_the_archives_debt(archived):
    """The worker keeps the encoder out of every declared shard's range
    until the archive delivers it; a document under a shard with no ledger
    row yet was reported as ordinary encoder backlog."""
    from app.bundles import genesis
    genesis.ARCHIVES["medcpt-pubmed"].shards = ["0", "1", "2"]
    _docs(archived, 3, start=2_500_000)
    got = health.archive_gap(archived, Collection(archive="medcpt-pubmed"), mode="running")
    assert got.state == health.RUNNING and got.waiting == 3 and "open shard" in got.reason


def test_a_real_boot_refusal_reaches_the_report_and_the_check_writes_nothing(
        isolated_config, monkeypatch):
    """The refusal test above stubs `_boot_refusals`; this drives the real
    wiring — spaces' own check, read-only — and proves the check changes
    nothing on the way (the report used to fill in the pinned width and log
    events)."""
    from app import collections as collections_mod, db, spaces
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("model-a", False))
    c = collections_mod.default_collection()
    spaces.validate_boot([c])                   # the worker's boot pins model-a
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("model-b", False))
    monkeypatch.setattr(spaces, "known_lance_dim", lambda d: 768)
    con = db.connect(c)

    def snapshot():
        return (tuple(con.execute("SELECT space_id, dim FROM space").fetchone()),
                con.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    before = snapshot()
    reports, blocking = health.check([c])
    assert [s.name for s in reports[0].stages] == ["boot"]
    assert reports[0].stages[0].state == health.BLOCKED
    assert any("fails the boot check" in b and "model-a" in b for b in blocking), blocking
    assert snapshot() == before, "a check writes nothing"

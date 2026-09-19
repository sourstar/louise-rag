"""tests/test_quarantine.py — a peer's own tests come before any use of a copy
(docs/03-checking.md §3.6; docs/07-commands.md §7.7, §7.8; docs/05-ask.md
§5.3; migration plan, step 14).

A copy is *refused*, in *quarantine* or *in use*, and the state is DERIVED
from what the peer holds — its own attestations, its calibration statements,
its acknowledgements — and written nowhere. Proved here:

  - each reason enters quarantine and is cleared by its own test;
  - nothing is written for the state, and two calls over the same holdings
    agree;
  - a run not yet tested re-quarantines every collection that reaches that
    encoder, and only those; the gate runs at once and clears it;
  - a run that FAILED leaves the text leg searchable and says so;
  - a legacy collection is always in use;
  - a quarantined copy is named under `withheld` with `shards: "all"`, enters
    no list, and exports nothing;
  - `hold collection ack --origin-unchecked` is refused with a test open, with
    the origin never tried, and with the origin check run — and accepted with
    the origin away, recording its time and the peer's key;
  - a later `records-vs-origin` failure re-quarantines, acknowledged or not;
  - IN THE REPORTING DEPLOY NOTHING IS WITHHELD.
"""
import json

import pytest
from fastapi import HTTPException

from app import (collections as collections_mod, copy_state, db, embeddings, health,
                 louise, platform_config, query_service, run_check, worker)
from app.hold import collection as col, drag as drag_mod, envelope as env, runs as hold_runs
from app.hold.codex import canonical_json
from app.verify import backend_gate
from tests.hold.conftest import KEY, NOW, PLATFORM
from tests.hold.test_template import SPACE, recipe as a_recipe
from tests.test_run_check import (DOC_URL, QUERY_URL, R1, R2, RAGE,  # noqa: F401
                                  chunks, node, opening, opening_query, rage, stored)

OTHER_URL = f"{RAGE}/upstream/another-encoder"


@pytest.fixture(autouse=True)
def fresh():
    copy_state.reset_cache()
    copy_state._switch.clear()
    worker._quarantine_nagged.clear()
    yield
    copy_state.reset_cache()


def enforce(tmp_path, how: str = "enforce") -> None:
    (tmp_path / "config" / "platform.toml").write_text(
        f'[node]\nquarantine = "{how}"\n', encoding="utf-8")
    copy_state._states.clear()


def attest(root, codex, check: str, result: str = "pass", *, name: str = "gutenberg",
           time: str = "2026-09-19T10:00:00Z", key=KEY, shards=()):
    """One of this peer's own attestations, as `hold check` writes them (09 §9.8)."""
    body = {"kind": "louise-attestation", "version": 3,
            "collection_id": codex.collection_id, "snapshot_id": "2026-09",
            "snapshot_hash": "sha256:" + "00" * 32, "check": check, "seed": None,
            "shards": list(shards), "result": result, "detail": {},
            "platform_image": PLATFORM, "encoder_engine": None, "encoder_weights": None,
            "key": env.key_text(key.verify_key), "time": time}
    d = root / name / "state" / "attestations" / drag_mod._hex(codex.collection_id)
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{drag_mod._hex(env.body_id(body))}.json"
    out.write_bytes(canonical_json(env.sign(env.ATTESTATION, body, key)))
    return out


def state_of(collection) -> copy_state.CopyState:
    copy_state._states.clear()
    return copy_state.of(collection)


def all_but_the_origin(root, collection, codex) -> None:
    attest(root, codex, "roots", name=collection.name)
    attest(root, codex, "chunks", name=collection.name)
    assert backend_gate.embed_allowed(db.connect(collection), collection) is True


def fully_tested(root, collection, codex) -> None:
    all_but_the_origin(root, collection, codex)
    attest(root, codex, "records-vs-origin", name=collection.name)


# ---- each reason, and the test that clears it -----------------------------------------

def test_a_new_copy_is_in_quarantine_and_each_test_clears_its_own_reason(node, rage):
    root, collection, codex = node
    s = state_of(collection)
    assert s.state == "quarantine" and s.origin_check == "pending"
    assert {t.name for t in s.tests if t.status == "open"} == \
        {"roots-and-chunks", "gate", "origin"}
    assert len(s.reasons) == 3 and all("cleared by" in r for r in s.reasons)

    attest(root, codex, "roots")
    assert state_of(collection).test("roots-and-chunks").status == "open", "chunks too"
    attest(root, codex, "chunks")
    assert state_of(collection).test("roots-and-chunks").status == "passed"

    assert backend_gate.embed_allowed(db.connect(collection), collection) is True
    s = state_of(collection)
    assert s.test("gate").status == "passed" and s.state == "quarantine"
    assert len(hold_runs.statements(root, "gutenberg", codex, "query")) == 1, \
        "an asymmetric space's query run is gated too, at once and by itself"

    attest(root, codex, "records-vs-origin")
    s = state_of(collection)
    assert s.state == "in-use" and s.reasons == () and s.origin_check == "passed"
    assert s.test("bundles").status == "passed"


def test_the_licence_is_one_of_the_five(node, rage):
    root, collection, codex = node
    fully_tested(root, collection, codex)
    missing = copy_state.evaluate(root, "gutenberg", codex, licence="missing",
                                  key=copy_state.own_key())
    assert missing.state == "quarantine"
    assert "hold collection ack gutenberg" in missing.reasons[0]
    for ok in ("acknowledged", "not_required"):
        assert copy_state.evaluate(root, "gutenberg", codex, licence=ok,
                                   key=copy_state.own_key()).state == "in-use"
    judged = copy_state.evaluate(root, "gutenberg", codex, licence="ValueError: broken",
                                 key=copy_state.own_key())
    assert judged.state == "quarantine", "a licence that cannot be judged fails closed"


def test_another_keys_attestations_are_not_this_peers_tests(node, rage):
    from nacl import signing
    root, collection, codex = node
    stranger = signing.SigningKey(b"\x09" * 32)
    for check in ("roots", "chunks", "records-vs-origin"):
        attest(root, codex, check, key=stranger)
    s = state_of(collection)
    assert s.test("roots-and-chunks").status == "open"
    assert s.test("origin").status == "open"


def test_a_peer_with_no_key_is_told_so(node, rage):
    root, collection, codex = node
    s = copy_state.evaluate(root, "gutenberg", codex, licence="not_required", key=None)
    assert s.state == "quarantine"
    assert any("no key" in r and "NODE_KEY_PATH" in r for r in s.reasons)


def test_a_refused_copy_is_refused_and_only_a_person_clears_it(node, rage):
    root, collection, codex = node
    fully_tested(root, collection, codex)
    s = copy_state.evaluate(root, "gutenberg", codex, licence="not_required",
                            refusal="the vector table is 1024 wide and the Codex says 768",
                            key=copy_state.own_key())
    assert s.state == "refused" and s.reasons == (
        "the vector table is 1024 wide and the Codex says 768",)


# ---- derived, written nowhere ------------------------------------------------------------

def _tree(root) -> dict:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_the_state_is_derived_and_nothing_is_written_for_it(node, rage):
    root, collection, codex = node
    fully_tested(root, collection, codex)
    before = _tree(root)
    first = state_of(collection)
    second = state_of(collection)
    third = copy_state.held(root, "gutenberg", codex)
    assert first == second
    assert (third.state, third.reasons, third.origin_check) == \
        (first.state, first.reasons, first.origin_check)
    assert _tree(root) == before, "evaluating the state wrote a file"


def test_an_evaluation_is_a_lookup_until_the_holdings_change(node, rage, monkeypatch):
    """A COST test (scheduling.md §6.7). Attestations accumulate for a
    collection's whole life, so an evaluation must not grow with them: warm,
    it verifies no signature and lists no directory of them — one `stat`."""
    import os
    root, collection, codex = node
    fully_tested(root, collection, codex)
    for i in range(60):
        attest(root, codex, "roots", time=f"2026-08-01T00:{i:02d}:00Z")
    held = root / "gutenberg" / "state" / "attestations" / drag_mod._hex(codex.collection_id)
    long_ago = held.stat().st_mtime - 60
    os.utime(held, (long_ago, long_ago))     # not a directory still being written to
    state_of(collection)

    verified, listed = {"n": 0}, {"n": 0}
    real_verify, real_scandir = env.verify, os.scandir

    def verify(domain, envelope):
        verified["n"] += 1
        return real_verify(domain, envelope)

    def scandir(path="."):
        listed["n"] += "attestations" in str(path)
        return real_scandir(path)

    monkeypatch.setattr(env, "verify", verify)
    monkeypatch.setattr(copy_state.os, "scandir", scandir)
    for _ in range(40):
        assert state_of(collection).state == "in-use"
    assert (verified["n"], listed["n"]) == (0, 0), \
        "an evaluation re-read the attestations it had already read"

    attest(root, codex, "records-vs-origin", "fail", time="2026-10-01T00:00:00Z")
    assert state_of(collection).state == "quarantine", "a new attestation was not read"
    assert verified["n"] > 0


# ---- a run not yet tested -------------------------------------------------------------------

@pytest.fixture
def three(node, tmp_path, rage):
    """Two collections that reach ONE encoder, and a third that reaches another."""
    root, first, codex = node
    cfg = tmp_path / "config" / "collections"
    made = {"gutenberg": codex}
    for name, recipe, url in (
            ("sister", a_recipe(work={"tokens": 6_000_000}), DOC_URL),
            ("elsewhere", a_recipe(work={"tokens": 7_000_000}), OTHER_URL)):
        made[name] = col.create_from_recipe(
            root, "gutenberg", recipe, platform_image=PLATFORM, embed=opening,
            embed_query=opening_query, name=name, key=KEY, now=NOW)
        (cfg / f"{name}.toml").write_text(
            f'codex = "{made[name].collection_id}"\nembed = "on-demand"\n'
            f'[encoder]\nurl = "{url}"\nquery_url = "{QUERY_URL}"\n', encoding="utf-8")
    assert made["sister"].space_id == codex.space_id
    assert made["sister"].collection_id != codex.collection_id
    loaded = {c.name: c for c in collections_mod.load_all()}
    for name, c in loaded.items():
        fully_tested(root, c, made[name])
    return root, loaded, made


def test_a_changed_run_requarantines_every_collection_of_that_encoder_and_only_those(
        three, rage):
    root, loaded, made = three
    assert all(state_of(c).state == "in-use" for c in loaded.values())

    rage.by_url[DOC_URL] = R2                # the model was relaunched with another flag
    with pytest.raises(hold_runs.RunUnvalidated):
        embeddings.upsert_book(7, "en", chunks(2), loaded["gutenberg"])

    for name in ("gutenberg", "sister"):
        s = state_of(loaded[name])
        assert s.state == "quarantine", name
        assert "has not tested" in s.test("gate").detail
    assert state_of(loaded["elsewhere"]).state == "in-use", \
        "a collection that reaches another encoder went with it"

    # the gate runs AT ONCE, with nothing to embed and nobody asking
    con = db.connect(loaded["sister"])
    worker._settle_the_run(con, loaded["sister"])
    assert R2 in hold_runs.statements(root, "sister", made["sister"])
    for name in ("gutenberg", "sister"):
        assert state_of(loaded[name]).state == "in-use", \
            "a pass validates the run for the SPACE: it counts for both"


def test_a_copy_that_arrived_whole_is_gated_without_any_embed_work(node, rage):
    root, collection, codex = node
    attest(root, codex, "roots"); attest(root, codex, "chunks")
    attest(root, codex, "records-vs-origin")
    assert state_of(collection).test("gate").status == "open"
    worker._settle_the_run(db.connect(collection), collection)
    assert state_of(collection).state == "in-use"

    cost = rage.texts
    for _ in range(10):
        worker._settle_the_run(db.connect(collection), collection)
    assert rage.texts == cost, "settling a settled run costs the encoder nothing"


def test_the_gate_comes_before_quarantine_at_the_embed_stage(node, rage, tmp_path):
    """The gate is one of the tests that CLEAR a quarantine; asked in the
    other order, an untested run would stay untested for ever."""
    root, collection, codex = node
    enforce(tmp_path)
    con = db.connect(collection)
    assert worker._embedding_allowed(con, collection) is False, "still in quarantine"
    assert R1 in hold_runs.statements(root, "gutenberg", codex), "and the gate has run"


# ---- a run that failed -------------------------------------------------------------------------

def test_a_failed_run_withholds_the_vector_leg_and_quarantines_nothing(node, rage, tmp_path):
    root, collection, codex = node
    rage.tag = b"another model"
    attest(root, codex, "roots"); attest(root, codex, "chunks")
    attest(root, codex, "records-vs-origin")
    assert backend_gate.embed_allowed(db.connect(collection), collection) is False

    s = state_of(collection)
    assert s.state == "in-use", "a failed test is a DONE test (02 §2.4)"
    assert s.vector_leg is False and "FAILED the gate" in s.vector_leg_reason
    assert copy_state.vector_leg_withheld(collection) is None, "reported only, until enforced"

    enforce(tmp_path)
    assert "text leg goes on" in copy_state.vector_leg_withheld(collection)
    assert query_service.withheld_reason(collection) is None, "the text leg is searchable"


def test_the_search_says_that_the_vector_leg_was_withheld(node, rage, tmp_path):
    from app import retrieval
    root, collection, codex = node
    rage.tag = b"another model"
    tested_but_failed = [attest(root, codex, c) for c in
                         ("roots", "chunks", "records-vs-origin")]
    assert tested_but_failed
    backend_gate.embed_allowed(db.connect(collection), collection)
    enforce(tmp_path)

    asked = rage.posts
    result = retrieval.search(db.connect(collection), "a lighthouse", mode="hybrid",
                              translate=False, collection=collection)
    assert result["legs_withheld"] == [{
        "collection": "gutenberg", "leg": "vector",
        "reason": copy_state.vector_leg_withheld(collection)}]
    assert rage.posts == asked, "the encoder was asked for a leg that is withheld"


def test_an_answer_from_an_untested_run_answers_no_query(node, rage, tmp_path):
    root, collection, codex = node
    fully_tested(root, collection, codex)
    embeddings.upsert_book(7, "en", chunks(3), collection)
    recipe = collections_mod.recipe_for(collection)

    rage.by_url[QUERY_URL] = R2
    assert embeddings.search("chunk 1", collection=collection), \
        "while the node only reports, the answer is used and the run is remembered"
    assert run_check.answering(collection)["query"] == R2
    assert state_of(collection).state == "quarantine"

    enforce(tmp_path)
    with pytest.raises(hold_runs.RunUnvalidated, match="discarded"):
        embeddings.search("chunk 1", collection=collection)
    assert recipe.query_url == QUERY_URL


# ---- everything older ----------------------------------------------------------------------------

def test_a_legacy_collection_is_always_in_use(isolated_config, tmp_path, monkeypatch):
    legacy = collections_mod.default_collection()
    assert legacy.codex is None
    assert copy_state.of(legacy) is copy_state.ALWAYS_IN_USE
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    (tmp_path / "platform.toml").write_text('[node]\nquarantine = "enforce"\n',
                                            encoding="utf-8")
    assert copy_state.enforced() is True
    assert copy_state.withholds(legacy) is None
    assert query_service.withheld_reason(legacy) is None


# ---- what quarantine withholds, and when ----------------------------------------------------------

def test_in_the_reporting_deploy_nothing_is_withheld(node, rage):
    root, collection, codex = node
    assert state_of(collection).state == "quarantine"
    assert copy_state.enforced() is False, "report is the default"
    assert copy_state.withholds(collection) is None
    assert query_service.withheld_reason(collection) is None
    assert query_service.withheld_from(None) == []
    assert collection in query_service.resolve_collections(None)

    con = db.connect(collection)
    assert worker._quarantine_allows(con, collection, "embedding") is True
    said = [r["message"] for r in con.execute(
        "SELECT message FROM events WHERE stage='control'")]
    assert any("REPORT ONLY" in m and "quarantine" in m for m in said), \
        "the first deploy SHOWS what the second would withhold"
    note = health._copy_state(collection)
    assert note[0] == "" and "REPORTED ONLY" in note[1]


def test_enforced_a_quarantined_copy_enters_no_list_and_is_named(node, rage, tmp_path):
    root, collection, codex = node
    enforce(tmp_path)
    assert collection not in query_service.resolve_collections(None)
    with pytest.raises(HTTPException) as e:
        query_service.resolve_collections(["gutenberg"])
    assert e.value.status_code == 403 and "quarantine" in e.value.detail

    named, = query_service.withheld_from(None)
    assert named["collection"] == "gutenberg" and named["shards"] == "all"
    assert named["collection_id"] == codex.collection_id
    assert named["reason"].startswith("quarantine: ")

    con = db.connect(collection)
    for use in ("embedding", "bundle import", "archive import"):
        assert worker._quarantine_allows(con, collection, use) is False
    assert health._copy_state(collection)[0].startswith("withheld: quarantine")


def test_enforced_nothing_of_a_quarantined_copy_is_exported(node, rage, tmp_path):
    from app.bundles import export
    root, collection, codex = node
    enforce(tmp_path)
    with pytest.raises(export.ExportError, match="nothing is exported — quarantine"):
        export.export_bundle(db.connect(collection), collection, ["g:1"], tmp_path / "out")
    with pytest.raises(copy_state.InQuarantine, match="exporting its vectors waits"):
        copy_state.require_in_use(root, "gutenberg", codex, use="exporting its vectors")


def test_a_tested_copy_is_used_like_any_other_when_enforced(node, rage, tmp_path):
    root, collection, codex = node
    fully_tested(root, collection, codex)
    enforce(tmp_path)
    assert query_service.withheld_reason(collection) is None
    assert worker._embedding_allowed(db.connect(collection), collection) is True
    copy_state.require_in_use(root, "gutenberg", codex, use="embedding for it")


def test_the_switch_is_report_or_enforce_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    assert platform_config.load().quarantine == "report"
    (tmp_path / "platform.toml").write_text('[node]\nquarantine = "yes"\n', encoding="utf-8")
    with pytest.raises(Exception, match="report.*enforce"):
        platform_config.load()
    assert copy_state.enforced() is False, "a file that cannot be read enforces nothing"


# ---- the origin: tried, away, decided -----------------------------------------------------------------

def ack(root, *extra) -> int:
    return louise.main(["hold", "collection", "ack", "gutenberg", "--root", str(root),
                        "--origin-unchecked", "--yes", *extra])


def test_origin_unchecked_is_refused_while_another_test_is_open(node, rage, capsys):
    root, collection, codex = node
    copy_state.note_origin_attempt(root, "gutenberg", codex, why="origin unreachable", now=NOW)
    assert ack(root) == 2
    assert "another test is still open" in capsys.readouterr().err
    assert not (root / "gutenberg" / "state" / copy_state.ORIGIN_ACK).exists()


def test_origin_unchecked_is_refused_when_the_check_was_never_tried(node, rage, capsys):
    root, collection, codex = node
    all_but_the_origin(root, collection, codex)
    assert ack(root) == 2
    assert "has not been tried" in capsys.readouterr().err


def test_origin_unchecked_is_accepted_with_the_origin_away(node, rage, capsys):
    root, collection, codex = node
    all_but_the_origin(root, collection, codex)
    copy_state.note_origin_attempt(root, "gutenberg", codex,
                                   why="https://www.gutenberg.org/ timed out", now=NOW)
    waiting = state_of(collection)
    assert waiting.state == "quarantine" and waiting.origin_check == "pending"
    assert "awaiting your decision" in waiting.reasons[0]
    assert "--origin-unchecked" in waiting.reasons[0] and "forget" in waiting.reasons[0]

    assert ack(root) == 0
    out = capsys.readouterr().out
    assert "cannot-run" in out and "timed out" in out, "it prints what could not run"
    record = json.loads((root / "gutenberg" / "state" / copy_state.ORIGIN_ACK).read_bytes())
    assert record["key"] == env.key_text(KEY.verify_key) and record["time"]
    assert record["last_tried"] == NOW and record["collection_id"] == codex.collection_id

    s = state_of(collection)
    assert s.state == "in-use" and s.reasons == (), "in use like any other, with no mark"
    assert s.origin_check == "acknowledged-unchecked"


def test_origin_unchecked_is_refused_when_the_check_has_in_fact_run(node, rage, capsys):
    root, collection, codex = node
    fully_tested(root, collection, codex)
    assert ack(root) == 2
    assert "HAS run" in capsys.readouterr().err


def test_a_later_origin_failure_requarantines_acknowledged_or_not(node, rage):
    root, collection, codex = node
    all_but_the_origin(root, collection, codex)
    copy_state.note_origin_attempt(root, "gutenberg", codex, why="away", now=NOW)
    copy_state.record_origin_ack(root, "gutenberg", codex, now=NOW)
    assert state_of(collection).state == "in-use"

    attest(root, codex, "records-vs-origin", "fail", time="2026-10-01T00:00:00Z",
           shards=("lang=en/00001",))
    s = state_of(collection)
    assert s.state == "quarantine" and s.origin_check == "failed"
    assert "lang=en/00001" in s.reasons[0] and "until a person decides" in s.reasons[0]

    attest(root, codex, "records-vs-origin", "pass", time="2026-10-02T00:00:00Z")
    assert state_of(collection).state == "in-use", "the latest of its own checks decides"


def test_a_roots_failure_after_a_pass_is_one_shards_fault_not_quarantine(node, rage):
    root, collection, codex = node
    fully_tested(root, collection, codex)
    attest(root, codex, "roots", "fail", time="2026-10-01T00:00:00Z", shards=("lang=en/00001",))
    assert state_of(collection).state == "in-use"


def test_hold_check_notes_an_origin_check_that_could_not_run(node, rage):
    root, collection, codex = node
    assert copy_state.origin_attempt(root, "gutenberg") is None
    copy_state.note_origin_attempt(root, "gutenberg", codex, why="x" * 900, now=NOW)
    tried = copy_state.origin_attempt(root, "gutenberg")
    assert tried["time"] == NOW and len(tried["why"]) == 400


# ---- show -------------------------------------------------------------------------------------------------

def test_hold_collection_show_prints_the_state_and_every_reason(node, rage, capsys):
    root, collection, codex = node
    assert louise.main(["hold", "collection", "show", "gutenberg", "--root", str(root)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert "refused" not in shown, "09 §9.3: replaced by state and reasons"
    assert shown["state"] == "quarantine" and shown["origin_check"] == "pending"
    assert len(shown["reasons"]) == 3

    fully_tested(root, collection, codex)
    copy_state.reset_cache()
    capsys.readouterr()                  # what the gate logged while it ran
    assert louise.main(["hold", "collection", "show", "gutenberg", "--root", str(root)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (shown["state"], shown["reasons"], shown["origin_check"]) == ("in-use", [], "passed")


def test_a_command_asks_the_same_licence_resolution_as_the_worker(tmp_path, monkeypatch):
    """Found on the reference node the day this was deployed: `hold collection
    show wikipedia` said "the licence is not acknowledged" while the worker
    said acknowledged. The command had no collection object to hand to
    `app/licensing.py` — the builder refuses a yellow collection with no
    `ack.json` of its own — and judged from the Codex's class instead. But a
    Codex collection is acknowledged by ANY record of its pinned terms on this
    peer (04 §4.4), which only THE resolution knows."""
    from app import licensing
    from tests.hold.conftest import pubmed
    codex = pubmed()
    assert codex.license.cls != "green" and codex.license.terms_sha256
    root = tmp_path / "collections"
    (root / "pubmed" / "state").mkdir(parents=True)
    monkeypatch.setenv("LOUISE_ROOT", str(tmp_path))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    licensing.reset_peer_acks()

    assert copy_state.held(root, "pubmed", codex).test("licence").status == "open"

    # the same terms, acknowledged under ANOTHER collection on this peer
    elsewhere = root / "pubmed-2025" / "state"
    elsewhere.mkdir(parents=True)
    (elsewhere / "ack.json").write_text(json.dumps(
        {"kind": "louise-ack", "version": 1, "terms_sha256": codex.license.terms_sha256,
         "time": NOW, "collection_id": "sha256:" + "11" * 32}), encoding="utf-8")
    licensing.reset_peer_acks()
    assert copy_state.held(root, "pubmed", codex).test("licence").status == "passed"
    licensing.reset_peer_acks()

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
from app.hold import collection as col, shard as shard_mod, envelope as env, runs as hold_runs
from app.hold.codex import canonical_json
from app.verify import backend_gate
from tests.hold.conftest import KEY, NOW, PLATFORM

#: this node's public key, as a statement names it: the only signer counted
ME = env.key_text(KEY.verify_key)
from tests.hold.test_template import SPACE, recipe as a_recipe
from tests.test_run_check import (DOC_URL, QUERY_URL, R1, R2, ENDPOINT,  # noqa: F401
                                  chunks, node, opening, opening_query, endpoint, stored)

OTHER_URL = f"{ENDPOINT}/upstream/another-encoder"


@pytest.fixture(autouse=True)
def fresh():
    copy_state.reset_cache()
    copy_state._switch.clear()
    worker._quarantine_nagged.clear()
    yield
    copy_state.reset_cache()


def enforce(tmp_path, how: str = "enforce", before=()) -> None:
    listed = ", ".join(f'"{c}"' for c in before)
    (tmp_path / "config" / "platform.toml").write_text(
        f'[node]\nquarantine = "{how}"\nquarantine_in_use_before = [{listed}]\n',
        encoding="utf-8")
    copy_state._states.clear()


def attest(root, codex, check: str, result: str = "pass", *, name: str = "gutenberg",
           time: str = "2026-09-19T10:00:00Z", key=KEY, shards=(), image=PLATFORM):
    """One of this peer's own attestations, as `hold check` writes them (09 §9.8)."""
    body = {"kind": "louise-attestation", "version": 3,
            "collection_id": codex.collection_id, "snapshot_id": "2026-09",
            "snapshot_hash": "sha256:" + "00" * 32, "check": check, "seed": None,
            "shards": list(shards), "result": result, "detail": {},
            "platform_image": image, "encoder_engine": None, "encoder_weights": None,
            "key": env.key_text(key.verify_key), "time": time}
    d = root / name / "state" / "attestations" / shard_mod._hex(codex.collection_id)
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{shard_mod._hex(env.body_id(body))}.json"
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

def test_a_new_copy_is_in_quarantine_and_each_test_clears_its_own_reason(node, endpoint):
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
    assert len(hold_runs.statements(root, "gutenberg", codex, "query", signer=ME)) == 1, \
        "an asymmetric space's query run is gated too, at once and by itself"

    attest(root, codex, "records-vs-origin")
    s = state_of(collection)
    assert s.state == "in-use" and s.reasons == () and s.origin_check == "passed"
    assert s.test("bundles").status == "passed"


def test_the_licence_is_one_of_the_five(node, endpoint):
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


def test_another_keys_attestations_are_not_this_peers_tests(node, endpoint):
    from nacl import signing
    root, collection, codex = node
    stranger = signing.SigningKey(b"\x09" * 32)
    for check in ("roots", "chunks", "records-vs-origin"):
        attest(root, codex, check, key=stranger)
    s = state_of(collection)
    assert s.test("roots-and-chunks").status == "open"
    assert s.test("origin").status == "open"


def test_a_peer_with_no_key_is_told_so(node, endpoint):
    root, collection, codex = node
    s = copy_state.evaluate(root, "gutenberg", codex, licence="not_required", key=None)
    assert s.state == "quarantine"
    assert any("no key" in r and "NODE_KEY_PATH" in r for r in s.reasons)


def test_a_refused_copy_is_refused_and_only_a_person_clears_it(node, endpoint):
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


def test_the_state_is_derived_and_nothing_is_written_for_it(node, endpoint):
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


def test_an_evaluation_is_a_lookup_until_the_holdings_change(node, endpoint, monkeypatch):
    """A COST test (scheduling.md §6.7). Attestations accumulate for a
    collection's whole life, so an evaluation must not grow with them: warm,
    it verifies no signature and lists no directory of them — one `stat`."""
    import os
    root, collection, codex = node
    fully_tested(root, collection, codex)
    for i in range(60):
        attest(root, codex, "roots", time=f"2026-08-01T00:{i:02d}:00Z")
    held = root / "gutenberg" / "state" / "attestations" / shard_mod._hex(codex.collection_id)
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
def three(node, tmp_path, endpoint):
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
            f'[encoder]\nurl = "{url}"\nquery_url = "{QUERY_URL}"\n'
            f'embed_model = "{url.rsplit("/", 1)[1]}"\nquery_model = "medcpt-query"\n',
            encoding="utf-8")
    assert made["sister"].space_id == codex.space_id
    assert made["sister"].collection_id != codex.collection_id
    loaded = {c.name: c for c in collections_mod.load_all()}
    for name, c in loaded.items():
        fully_tested(root, c, made[name])
    return root, loaded, made


def test_a_changed_run_requarantines_every_collection_of_that_encoder_and_only_those(
        three, endpoint):
    root, loaded, made = three
    assert all(state_of(c).state == "in-use" for c in loaded.values())

    endpoint.by_url[DOC_URL] = R2                # the model was relaunched with another flag
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
    assert R2 in hold_runs.statements(root, "sister", made["sister"], signer=ME)
    for name in ("gutenberg", "sister"):
        assert state_of(loaded[name]).state == "in-use", \
            "a pass validates the run for the SPACE: it counts for both"


def test_a_copy_that_arrived_whole_is_gated_without_any_embed_work(node, endpoint):
    root, collection, codex = node
    attest(root, codex, "roots"); attest(root, codex, "chunks")
    attest(root, codex, "records-vs-origin")
    assert state_of(collection).test("gate").status == "open"
    worker._settle_the_run(db.connect(collection), collection)
    assert state_of(collection).state == "in-use"

    cost = endpoint.texts
    for _ in range(10):
        worker._settle_the_run(db.connect(collection), collection)
    assert endpoint.texts == cost, "settling a settled run costs the encoder nothing"


def test_the_gate_comes_before_quarantine_at_the_embed_stage(node, endpoint, tmp_path):
    """The gate is one of the tests that CLEAR a quarantine; asked in the
    other order, an untested run would stay untested for ever."""
    root, collection, codex = node
    enforce(tmp_path)
    con = db.connect(collection)
    assert worker._embedding_allowed(con, collection) is False, "still in quarantine"
    assert R1 in hold_runs.statements(root, "gutenberg", codex, signer=ME), "and the gate has run"


# ---- a run that failed -------------------------------------------------------------------------

def test_a_failed_run_withholds_the_vector_leg_and_quarantines_nothing(node, endpoint, tmp_path):
    root, collection, codex = node
    endpoint.tag = b"another model"
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


def test_the_search_says_that_the_vector_leg_was_withheld(node, endpoint, tmp_path):
    from app import retrieval
    root, collection, codex = node
    endpoint.tag = b"another model"
    tested_but_failed = [attest(root, codex, c) for c in
                         ("roots", "chunks", "records-vs-origin")]
    assert tested_but_failed
    backend_gate.embed_allowed(db.connect(collection), collection)
    enforce(tmp_path)

    asked = endpoint.posts
    result = retrieval.search(db.connect(collection), "a lighthouse", mode="hybrid",
                              translate=False, collection=collection)
    assert result["legs_withheld"] == [{
        "collection": "gutenberg", "collection_id": codex.collection_id, "leg": "vector",
        "reason": copy_state.vector_leg_withheld(collection)}]
    assert endpoint.posts == asked, "the encoder was asked for a leg that is withheld"


def test_an_answer_from_an_untested_run_answers_no_query(node, endpoint, tmp_path):
    root, collection, codex = node
    fully_tested(root, collection, codex)
    embeddings.upsert_book(7, "en", chunks(3), collection)
    recipe = collections_mod.recipe_for(collection)

    endpoint.by_url[QUERY_URL] = R2
    assert embeddings.search("chunk 1", collection=collection), \
        "while the node only reports, the answer is used and the run is remembered"
    assert run_check.answering(collection)["query"] == R2
    assert state_of(collection).state == "quarantine"

    enforce(tmp_path)
    with pytest.raises(hold_runs.RunUnvalidated, match="discarded"):
        embeddings.search("chunk 1", collection=collection)
    assert recipe.query_url == QUERY_URL


# ---- what quarantine withholds, and when ----------------------------------------------------------

def test_in_the_reporting_deploy_nothing_is_withheld(node, endpoint):
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


def test_enforced_a_quarantined_copy_enters_no_list_and_is_named(node, endpoint, tmp_path):
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


def test_enforced_nothing_of_a_quarantined_copy_is_exported(node, endpoint, tmp_path):
    from app.bundles import export
    root, collection, codex = node
    enforce(tmp_path)
    with pytest.raises(export.ExportError, match="nothing is exported — quarantine"):
        export.export_bundle(db.connect(collection), collection, ["g:1"], tmp_path / "out")
    with pytest.raises(copy_state.InQuarantine, match="exporting its vectors waits"):
        copy_state.require_in_use(root, "gutenberg", codex, use="exporting its vectors")


def test_a_tested_copy_is_used_like_any_other_when_enforced(node, endpoint, tmp_path):
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


def test_origin_unchecked_is_refused_while_another_test_is_open(node, endpoint, capsys):
    root, collection, codex = node
    copy_state.note_origin_attempt(root, "gutenberg", codex, why="origin unreachable", now=NOW)
    assert ack(root) == 2
    assert "another test is still open" in capsys.readouterr().err
    assert not (root / "gutenberg" / "state" / copy_state.ORIGIN_ACK).exists()


def test_origin_unchecked_is_refused_when_the_check_was_never_tried(node, endpoint, capsys):
    root, collection, codex = node
    all_but_the_origin(root, collection, codex)
    assert ack(root) == 2
    assert "has not been tried" in capsys.readouterr().err


def test_origin_unchecked_is_accepted_with_the_origin_away(node, endpoint, capsys):
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


def test_origin_unchecked_is_refused_when_the_check_has_in_fact_run(node, endpoint, capsys):
    root, collection, codex = node
    fully_tested(root, collection, codex)
    assert ack(root) == 2
    assert "HAS run" in capsys.readouterr().err


def test_a_later_origin_failure_requarantines_acknowledged_or_not(node, endpoint):
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


def test_a_roots_failure_after_a_pass_is_one_shards_fault_not_quarantine(node, endpoint):
    root, collection, codex = node
    fully_tested(root, collection, codex)
    attest(root, codex, "roots", "fail", time="2026-10-01T00:00:00Z", shards=("lang=en/00001",))
    assert state_of(collection).state == "in-use"


def test_hold_check_notes_an_origin_check_that_could_not_run(node, endpoint):
    root, collection, codex = node
    assert copy_state.origin_attempt(root, "gutenberg") is None
    copy_state.note_origin_attempt(root, "gutenberg", codex, why="x" * 900, now=NOW)
    tried = copy_state.origin_attempt(root, "gutenberg")
    assert tried["time"] == NOW and len(tried["why"]) == 400


# ---- show -------------------------------------------------------------------------------------------------

def test_hold_collection_show_prints_the_state_and_every_reason(node, endpoint, capsys):
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


# ---- in use before the peer enforced (operator, 2026-09-20) ----------------------------------------

def test_a_copy_in_use_before_enforcement_stays_in_use_and_says_by_whose_decision(
        node, endpoint, tmp_path):
    """"Assume that the current collections have already passed it. The next
    collection to be created will be our guinea pig." The copies a peer used
    before it enforced cannot run two of the tests yet; the decision is
    desired state, by collection id, and NOTHING IS PRETENDED: no test reads
    passed, no attestation appears, the origin check stays pending."""
    root, collection, codex = node
    assert backend_gate.embed_allowed(db.connect(collection), collection) is True
    before = _tree(root)
    enforce(tmp_path, before=[codex.collection_id])

    s = state_of(collection)
    assert s.state == "in-use" and s.origin_check == "pending"
    assert {t.name: t.status for t in s.tests} == {
        "roots-and-chunks": "in-use-before", "bundles": "passed", "gate": "passed",
        "licence": "not-required", "origin": "in-use-before"}
    assert len(s.reasons) == 2 and all(
        "operator's decision" in r and "still open" in r and "cleared by" in r
        for r in s.reasons), "in use BY A DECISION is said, and what clears each test"
    assert _tree(root) == before, "a decision wrote a file: it is desired state, in platform.toml"

    assert copy_state.withholds(collection) is None
    assert collection in query_service.resolve_collections(None)
    assert worker._quarantine_allows(db.connect(collection), collection, "embedding") is True
    assert health._copy_state(collection) == ("", "")
    held = copy_state.held(root, "gutenberg", codex)
    assert (held.state, held.reasons) == (s.state, s.reasons), "the command reads the same"

    # the real tests still count: once they pass, nothing is left to say
    fully_tested(root, collection, codex)
    s = state_of(collection)
    assert s.state == "in-use" and s.reasons == () and s.origin_check == "passed"


def test_the_guinea_pig_is_tested_for_real(node, endpoint, tmp_path):
    """Every copy NOT in the list — another collection's id is in it here."""
    root, collection, codex = node
    assert backend_gate.embed_allowed(db.connect(collection), collection) is True
    enforce(tmp_path, before=["sha256:" + "ab" * 32])
    s = state_of(collection)
    assert s.state == "quarantine" and len(s.reasons) == 2
    assert copy_state.withholds(collection).startswith("quarantine: ")
    assert collection not in query_service.resolve_collections(None)


def test_the_decision_stands_in_for_no_failure_no_gate_and_no_licence(node, endpoint, tmp_path):
    root, collection, codex = node
    enforce(tmp_path, before=[codex.collection_id])

    # the gate has not run: that is a test the peer CAN run, and does by itself
    s = state_of(collection)
    assert s.state == "quarantine" and s.test("gate").status == "open"
    assert len(s.reasons) == 1 and "encoder" in s.reasons[0], \
        "only what the decision does not cover is a reason to withhold"
    assert backend_gate.embed_allowed(db.connect(collection), collection) is True
    assert state_of(collection).state == "in-use"

    # the licence (invariant 9): no list of ids acknowledges a licence
    mine = copy_state.own_key()
    assert copy_state.evaluate(root, "gutenberg", codex, licence="missing", key=mine,
                               before=True).state == "quarantine"

    # a test that FAILED: roots, then the origin
    attest(root, codex, "roots", "fail", shards=["records-0001"])
    s = state_of(collection)
    assert s.state == "quarantine" and s.test("roots-and-chunks").status == "failed"
    attest(root, codex, "roots", time="2026-09-19T11:00:00Z")
    attest(root, codex, "chunks")
    assert state_of(collection).state == "in-use"
    attest(root, codex, "records-vs-origin", "fail", time="2026-09-19T12:00:00Z",
           shards=["records-0001"])
    s = state_of(collection)
    assert s.state == "quarantine" and s.origin_check == "failed"

    # and a refusal is a refusal
    assert copy_state.evaluate(root, "gutenberg", codex, licence="not_required", key=mine,
                               refusal="the vector table is 1024 wide", before=True
                               ).state == "refused"


def test_the_origin_acknowledgement_has_nothing_to_add_to_a_copy_in_use_before(
        node, endpoint, tmp_path):
    root, collection, codex = node
    all_but_the_origin(root, collection, codex)
    enforce(tmp_path, before=[codex.collection_id])
    why = copy_state.origin_ack_refusal(state_of(collection))
    assert why and "quarantine_in_use_before" in why and "records-vs-origin" in why


def test_the_list_holds_collection_ids_and_a_name_is_refused(tmp_path, monkeypatch):
    """By id, never by name: a name can be given to another collection, and an
    entry that names nothing would leave its copy withheld while the operator
    reads it as listed. Refused, not skipped — and a file that is refused
    enforces NOTHING, so a typo cannot take every copy out of search."""
    from app.transfer import schedule
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    good = "sha256:" + "0f" * 32
    for bad in ('["gutenberg"]', f'"{good}"', '["sha256:0F"]', f'["{good.upper()}"]', "[7]"):
        (tmp_path / "platform.toml").write_text(
            f'[node]\nquarantine = "enforce"\nquarantine_in_use_before = {bad}\n',
            encoding="utf-8")
        with pytest.raises(schedule.ScheduleError, match="quarantine_in_use_before"):
            platform_config.load()
        copy_state._switch.clear()
        assert copy_state.enforced() is False
    (tmp_path / "platform.toml").write_text(
        f'[node]\nquarantine = "enforce"\nquarantine_in_use_before = ["{good}"]\n',
        encoding="utf-8")
    assert platform_config.load().quarantine_in_use_before == (good,)
    assert platform_config.PlatformConfig().quarantine_in_use_before == ()


# ---- 03 §3.4: a verdict belongs to the image that ran it ---------------------------

NEWER = "sha256:" + "2e" * 32


def test_an_origin_failure_under_another_image_flags_nothing(node, endpoint):
    """Live 2026-09-21: a deploy moved pubmed's parser from 4 to 8, the check
    re-parsed under the new image, every record differed — and the copy was
    quarantined as if its own toolchain had failed. 03 §3.4: "an
    equivalence failure of that image for this collection, not a defect in
    the collection ... it flags nothing"."""
    root, collection, codex = node
    all_but_the_origin(root, collection, codex)
    attest(root, codex, "records-vs-origin", "fail", image=NEWER,
           time="2026-10-01T00:00:00Z", shards=("lang=en/00001",))
    s = state_of(collection)
    origin = s.test("origin")
    assert origin.status == "open" and s.origin_check != "failed"
    assert "flags nothing" in origin.detail and NEWER[:19] in origin.detail


def test_the_references_own_failure_still_flags(node, endpoint):
    root, collection, codex = node
    all_but_the_origin(root, collection, codex)
    attest(root, codex, "records-vs-origin", "fail", image=NEWER, time="2026-10-01T00:00:00Z")
    attest(root, codex, "records-vs-origin", "fail", time="2026-10-02T00:00:00Z")
    s = state_of(collection)
    assert s.state == "quarantine" and s.origin_check == "failed"


def test_an_image_that_proved_equivalence_can_flag(node, endpoint):
    """Its own records-vs-origin AND chunks passed under it: it is a
    reference for this collection now, and its later failure counts."""
    root, collection, codex = node
    all_but_the_origin(root, collection, codex)
    attest(root, codex, "records-vs-origin", image=NEWER, time="2026-10-01T00:00:00Z")
    attest(root, codex, "chunks", image=NEWER, time="2026-10-01T00:00:00Z")
    assert state_of(collection).state == "in-use"
    attest(root, codex, "records-vs-origin", "fail", image=NEWER, time="2026-10-02T00:00:00Z")
    s = state_of(collection)
    assert s.state == "quarantine" and s.origin_check == "failed"


def test_half_an_equivalence_is_none(node, endpoint):
    """records-vs-origin passed under the image but chunks never ran there:
    not proven, so its later failure still flags nothing."""
    root, collection, codex = node
    all_but_the_origin(root, collection, codex)
    attest(root, codex, "records-vs-origin", image=NEWER, time="2026-10-01T00:00:00Z")
    attest(root, codex, "records-vs-origin", "fail", image=NEWER, time="2026-10-02T00:00:00Z")
    assert state_of(collection).origin_check != "failed"


def test_records_are_built_only_by_the_reference_or_an_equivalent_image(node, endpoint, monkeypatch):
    """03 §3.4: "does not build its records, create its snapshots or
    contribute vectors to it until its own records-vs-origin and chunks pass
    under the image it runs". Found live 2026-09-21: gutenberg's Codex image
    had been pruned and a build under the next one ran unrefused."""
    from app.hold import equivalence
    root, collection, codex = node
    # the PROOF path: an image whose versions differ from the Codex's (an image
    # with the same versions is equivalent by invariant 3 — the test below)
    monkeypatch.setattr(equivalence, "equivalent_by_versions", lambda c: False)
    monkeypatch.delenv("LOUISE_PLATFORM_IMAGE", raising=False)
    with pytest.raises(equivalence.NotEquivalent, match="does not know the image"):
        equivalence.require_reference(root, "gutenberg", codex, use="build its records")

    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", NEWER)
    with pytest.raises(equivalence.NotEquivalent, match="03 §3.4"):
        equivalence.require_reference(root, "gutenberg", codex, use="build its records")

    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", codex.toolchain.platform_image)
    assert equivalence.require_reference(root, "gutenberg", codex, use="x") \
        == codex.toolchain.platform_image

    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", NEWER)
    attest(root, codex, "records-vs-origin", image=NEWER, time="2026-10-01T00:00:00Z")
    attest(root, codex, "chunks", image=NEWER, time="2026-10-01T00:00:00Z")
    copy_state.reset_cache()
    assert equivalence.require_reference(root, "gutenberg", codex, use="x") == NEWER


def test_the_empty_bytes_placeholder_names_no_image(monkeypatch):
    from app.hold import equivalence
    from app.hold.codex import digest
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", digest(b""))
    assert equivalence.running_image() is None


def test_an_image_running_the_codexs_versions_is_equivalent_by_invariant_3(node, endpoint, monkeypatch):
    """03 §3.4, amended 2026-10-09 (RULE 1): the Codex's parser at its parser
    version and every layer's chunker at its version make the same records
    and chunks (invariant 3), so such an image needs no per-image proof; an
    image whose parser version differs still does."""
    from app import collections as collections_mod
    from app.hold import equivalence
    root, collection, codex = node
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", NEWER)
    assert equivalence.equivalent_by_versions(codex)
    assert equivalence.require_reference(root, "gutenberg", codex, use="build its records") == NEWER
    plugin = collections_mod.PLUGIN_REGISTRY[codex.plugin]
    monkeypatch.setattr(plugin, "PARSER_VERSION", codex.parser.version + 1)
    assert not equivalence.equivalent_by_versions(codex)
    with pytest.raises(equivalence.NotEquivalent, match="03 §3.4"):
        equivalence.require_reference(root, "gutenberg", codex, use="build its records")


def test_under_an_image_that_is_not_the_reference_the_origin_check_cannot_run(
        node, endpoint, monkeypatch):
    """03 §3.6: the fifth test CANNOT RUN when "this peer's platform image is
    neither the Codex's nor equivalent (§3.4), so that its failure would say
    nothing about the collection" — and the operator decides, with `hold
    collection ack --origin-unchecked`."""
    from app.hold import equivalence
    root, collection, codex = node
    monkeypatch.setattr(equivalence, "equivalent_by_versions", lambda c: False)  # versions differ
    all_but_the_origin(root, collection, codex)
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", NEWER)
    s = state_of(collection)
    assert s.state == "quarantine" and s.test("origin").status == "cannot-run"
    assert "neither the Codex's image" in s.test("origin").detail
    assert copy_state.origin_ack_refusal(s) is None, "the operator may decide"

    attest(root, codex, "records-vs-origin", "fail", image=NEWER,
           time="2026-10-01T00:00:00Z")
    s = state_of(collection)
    assert s.test("origin").status == "cannot-run" and s.origin_check != "failed"
    assert "flags nothing" in s.test("origin").detail

    # under the reference itself the check CAN run: open, not a decision
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", codex.toolchain.platform_image)
    assert state_of(collection).test("origin").status == "open"


# ---- the worker's re-check record, for the web process (operator, 2026-09-28) ------------

def _as_the_web(collection) -> None:
    """A process that has re-checked nothing itself: the web process."""
    run_check._rechecked.clear()


def test_the_web_relies_only_on_a_statement_the_worker_re_checked_since_it_started(
        node, endpoint, tmp_path):
    root, collection, codex = node
    enforce(tmp_path)
    recipe = collections_mod.recipe_for(collection)
    run_check.publish_rechecks()                       # the worker starts
    assert backend_gate.embed_allowed(db.connect(collection), collection) is True
    record = json.loads(run_check.record_path().read_text(encoding="utf-8"))
    assert {(e[1], e[2]) for e in record["checked"]} >= {("document", R1)}

    _as_the_web(collection)
    run_check.check_query(collection, recipe, {R1})    # the worker re-checked it

    run_check.publish_rechecks()                       # the worker restarts
    with pytest.raises(hold_runs.RunUnvalidated) as e:
        run_check.check_query(collection, recipe, {R1})
    assert e.value.failed is False and "has not re-checked" in str(e.value)


def test_a_statement_changed_after_the_worker_re_checked_it_is_not_relied_on(
        node, endpoint, tmp_path):
    """Re-signed with the node's own key — layer 1 passes it — but it is not
    the statement the worker re-checked: the record names its digest."""
    root, collection, codex = node
    enforce(tmp_path)
    recipe = collections_mod.recipe_for(collection)
    run_check.publish_rechecks()
    assert backend_gate.embed_allowed(db.connect(collection), collection) is True
    _as_the_web(collection)
    run_check.check_query(collection, recipe, {R1})

    path = hold_runs.path_for(root, "gutenberg", R1, "query")
    if not path.exists():
        path = hold_runs.path_for(root, "gutenberg", R1)
    body = dict(json.loads(path.read_bytes())["body"], time="2026-09-28T00:00:00Z")
    path.write_bytes(canonical_json(env.sign(env.CALIBRATION, body, KEY)))
    with pytest.raises(hold_runs.RunUnvalidated, match="has not re-checked"):
        run_check.check_query(collection, recipe, {R1})


def test_a_statement_the_command_line_keeps_is_re_checked_by_the_worker_without_a_restart(
        node, endpoint, tmp_path):
    """München, live 2026-10-21: `hold embedder check gutenberg` kept a new
    statement for the run the worker had re-checked; the worker knew the RUN
    as re-checked, never re-checked the new statement, and the web withheld
    the vector leg until the worker restarted (14:00-14:25). The worker now
    looks again as soon as a kept statement changes, re-checks it, and tells
    the web; writes wait for that re-check, a round at most."""
    root, collection, codex = node
    enforce(tmp_path)
    recipe = collections_mod.recipe_for(collection)
    con = db.connect(collection)
    run_check.publish_rechecks()                       # the worker starts
    assert backend_gate.embed_allowed(con, collection) is True
    run_check.check_write(collection, recipe, {R1})

    # the command line keeps a new statement for the same run (its own pass)
    path = hold_runs.path_for(root, "gutenberg", R1)
    body = dict(json.loads(path.read_bytes())["body"], time="2026-10-21T12:00:00Z")
    path.write_bytes(canonical_json(env.sign(env.CALIBRATION, body, KEY)))
    with pytest.raises(hold_runs.RunUnvalidated, match="not yet re-checked"):
        run_check.check_write(collection, recipe, {R1})      # a write waits, deferred

    assert backend_gate.embed_allowed(con, collection) is True   # the next round
    run_check.check_write(collection, recipe, {R1})
    _as_the_web(collection)
    run_check.check_query(collection, recipe, {R1})    # no restart: the leg is back


def test_a_command_line_process_does_not_replace_the_workers_record(node, endpoint, tmp_path):
    root, collection, codex = node
    run_check.publish_rechecks()
    assert backend_gate.embed_allowed(db.connect(collection), collection) is True
    before = run_check.record_path().read_bytes()
    run_check._publishing.clear()                      # another process: not the worker
    run_check.reset_cache()
    run_check.validate(root, "gutenberg", codex, collections_mod.recipe_for(collection),
                       key=KEY, now=NOW)
    assert run_check.record_path().read_bytes() == before


def test_a_decision_is_never_read_from_a_pipe(node, endpoint, capsys, monkeypatch):
    """Live, 2026-10-07: `ack --origin-unchecked` without `--yes`, run with
    `docker exec`, read stdin regardless and ended in an EOFError traceback.
    Without a terminal it is a refusal that names `--yes`, and nothing is
    written."""
    import io
    root, collection, codex = node
    all_but_the_origin(root, collection, codex)
    copy_state.note_origin_attempt(root, "gutenberg", codex, why="the origin is away", now=NOW)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert louise.main(["hold", "collection", "ack", "gutenberg", "--root", str(root), "--origin-unchecked"]) == 2
    assert "--yes is required when stdin is not a terminal" in capsys.readouterr().err
    assert not (root / "gutenberg" / "state" / copy_state.ORIGIN_ACK).exists()


def test_a_copy_with_no_snapshot_says_its_checks_wait_for_the_first_one(node, endpoint):
    """Roots, chunks and records-vs-origin draw over a snapshot's parts, and
    `hold check` refuses a copy that holds none: the status says so and
    names what makes the first snapshot, never only the check it would
    refuse (wikipedia, 2026-10-13)."""
    import shutil
    root, collection, codex = node
    snaps = root / "gutenberg" / "shard" / "snapshots"
    if snaps.is_dir():
        shutil.rmtree(snaps)
    s = state_of(collection)
    for t in ("roots-and-chunks", "origin"):
        test = s.test(t)
        assert "holds no snapshot yet" in test.detail and "hold snapshot create gutenberg" in test.clears
    snaps.mkdir(parents=True)
    (snaps / "2026-08.json").write_text("{}", encoding="utf-8")
    s = state_of(collection)
    assert "holds no snapshot" not in s.test("roots-and-chunks").detail

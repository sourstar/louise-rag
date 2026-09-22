"""tests/test_run_check.py — a vector is written only from an answer that
names a validated run (docs/03-checking.md §3.3; migration plan, step 13e).

    Validate once; then every answer says which run gave it.

What is proved here, against a stand-in for RAGE that states a run with
every answer and describes it at `/louise/runs/<id>`:

  - an answer naming a run this peer has not validated writes NO row and
    marks NOTHING: the document is deferred untouched, never errored;
  - the gate runs against that run AT ONCE, once per (space, run) and not
    once per answer, and work resumes under it when it passes;
  - a run that changes mid-document stops the write; what the validated run
    wrote stays, and the document stays selectable;
  - an endpoint that states no run serves a query and refuses a write;
  - a run that fails is kept as a failure, pauses the collection's embedding
    with its reason, and is not gated again;
  - a run whose batches differ from one text at a time is validated for
    single-text work only — the fault that corrupted 714 books (2026-07);
  - a collection older than Codex version 3 is not touched at all.
"""
import json

import httpx
import pytest

from app import collections as collections_mod, config, db, embeddings, run_check, worker
from app.hold import collection as col, embedder as emb, runs as hold_runs
from app.hold.codex import canonical_json, digest
from app.verify import backend_gate
from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed
from tests.hold.test_template import recipe as a_recipe

RAGE = "http://rage:30437"
DOC_URL = f"{RAGE}/upstream/medcpt-article"
QUERY_URL = f"{RAGE}/upstream/medcpt-query"


def a_run(flag: str = "") -> tuple[str, dict]:
    """A run as RAGE would state it: its id IS the hash of its description."""
    obj = {"driver": "kernel 6.18.0; xe 9F2C1A; libze-intel-gpu1 26.05.1",
           "engine": {"name": "llama.cpp", "build": "b10630", "variant": "sycl",
                      "image": "sha256:" + "8c" * 32,
                      "args": ["llama-server", "-m", "${WEIGHTS}", "--embedding"]
                              + ([flag] if flag else []),
                      "env": {}, "device": "Intel(R) Arc(TM) Pro B70 Graphics"},
           "weights": "sha256:" + "e2" * 32}
    return digest(canonical_json(obj)), obj


def opening(texts, tag: bytes = b""):
    """The stand-in encoder: a function of a text's OPENING. The client cuts a
    text to the encoder's window before it sends it (`_embed_batch`), so a
    stand-in that hashed the whole text would answer differently for the long
    calibration texts than it did when the collection was created — a
    difference no real encoder has."""
    return fake_embed([t[:120] for t in texts], tag=tag)


def opening_query(texts):
    return opening(texts, tag=b"query")


R1, R1_OBJ = a_run()
R2, R2_OBJ = a_run("--flash-attn")


class Rage:
    """The model container, as far as a client sees it: `/v1/embeddings`
    with `Louise-Run` on the answer, and `/louise/runs/<id>`."""

    def __init__(self) -> None:
        self.run: str | None = R1            # what the next answers name
        self.by_url: dict[str, str | None] = {}   # …unless this endpoint has its own
        self.known = {R1: R1_OBJ, R2: R2_OBJ}
        self.tag = b""                       # another function, when set
        self.batches_differ = False
        self.posts = 0
        self.texts = 0
        self.switch_after: int | None = None  # answers until the run changes
        self.levels: list[str | None] = []

    def post(self, url, *, json=None, timeout=None, headers=None):   # noqa: A002
        self.posts += 1
        texts = json["input"]
        self.texts += len(texts)
        self.levels.append((headers or {}).get("Louise-Level"))
        if self.switch_after is not None:
            if self.switch_after <= 0:
                self.run = R2
            self.switch_after -= 1
        if url.startswith(QUERY_URL):
            vectors = opening_query(texts)
        else:
            vectors = opening(texts, tag=self.tag)
        if self.batches_differ and len(texts) > 1:
            # the same direction to five decimals, other bytes: what a
            # batching fault looks like to every check but the fingerprint
            vectors = [[x * 1.0000001 + 1e-9 for x in v] for v in vectors]
        request = httpx.Request("POST", url)
        run = next((r for u, r in self.by_url.items() if url.startswith(u)), self.run)
        return httpx.Response(
            200, request=request,
            headers={"Louise-Run": run} if run else {},
            json={"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]})

    def get(self, url, *, timeout=None):
        request = httpx.Request("GET", url)
        run_id = url.rsplit("/", 1)[-1]
        if not url.startswith(f"{RAGE}/louise/runs/") or run_id not in self.known:
            return httpx.Response(404, request=request)
        return httpx.Response(200, request=request, json=self.known[run_id])


@pytest.fixture
def rage(monkeypatch):
    r = Rage()
    monkeypatch.setattr(embeddings.httpc, "post", r.post)
    monkeypatch.setattr(run_check.httpx, "get", r.get)
    return r


@pytest.fixture
def node(tmp_path, monkeypatch, rage):
    """A LOUISE_ROOT holding one VERSION-3 collection whose file reaches its
    encoder through RAGE."""
    root = tmp_path / "louise"
    (root / "collections").mkdir(parents=True)
    codex = col.create_from_recipe(
        root / "collections", "gutenberg", a_recipe(), platform_image=PLATFORM,
        embed=opening, embed_query=opening_query, name="gutenberg",
        key=KEY, now=NOW)
    assert codex.version >= 3

    cfg = tmp_path / "config" / "collections"
    cfg.mkdir(parents=True)
    (cfg / "gutenberg.toml").write_text(
        f'codex = "{codex.collection_id}"\nembed = "on-demand"\n'
        f'[encoder]\nurl = "{DOC_URL}"\nquery_url = "{QUERY_URL}"\n', encoding="utf-8")
    monkeypatch.setenv("LOUISE_ROOT", str(root))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(collections_mod.config, "DATA_DIR", root)
    from app.bundles import keys as node_keys
    monkeypatch.setattr(node_keys, "load_signing_key", lambda path=None: KEY)
    run_check.reset_cache()
    backend_gate.reset_cache()
    hold_runs._read.clear()
    collection = next(c for c in collections_mod.load_all() if c.name == "gutenberg")
    return root / "collections", collection, codex


def chunks(n: int, book: int = 7) -> list[dict]:
    return [{"id": f"g:{book}:{i}", "seq": i, "chapter": "", "text": f"chunk {i} of {book}"}
            for i in range(n)]


def stored(collection) -> int:
    dbh = embeddings._conn(collection)
    if embeddings.TABLE not in dbh.table_names():
        return 0
    return dbh.open_table(embeddings.TABLE).count_rows()


def statements_kept(root, role: str = "document") -> list[dict]:
    """This peer's statements about the DOCUMENT encoder's runs. The space of
    the fixture is asymmetric, so its query encoder is gated too, by itself
    (`test_the_query_encoder_is_a_run_of_its_own`)."""
    d = hold_runs.directory(root, "gutenberg")
    return [emb.read_statement(json.loads(p.read_bytes()))
            for p in sorted(d.glob(f"{role}-*.json"))] if d.is_dir() else []


def _doc(con, uid="g:7"):
    con.execute(
        "INSERT INTO documents(uid,source,native_id,path,in_scope,rdf_done,fts_done,"
        "embed_done,chunks) VALUES(?,?,?,?,1,1,1,0,3)", (uid, "g", uid.split(":")[1], "/x"))
    return con.execute("SELECT * FROM documents WHERE uid=?", (uid,)).fetchone()


# ---- the write is refused, and nothing is marked ---------------------------------

def test_an_unvalidated_run_writes_no_row(node, rage):
    root, collection, codex = node
    with pytest.raises(hold_runs.RunUnvalidated) as e:
        embeddings.upsert_book(7, "en", chunks(3), collection)
    assert e.value.run_id == R1 and e.value.failed is False
    assert stored(collection) == 0, "an unvalidated answer reached the table"


def test_the_worker_defers_the_document_and_marks_nothing(node, rage, monkeypatch):
    root, collection, codex = node
    con = db.connect(collection)
    row = _doc(con)
    monkeypatch.setattr(worker, "_load_chunks", lambda row, collection: chunks(3))

    assert worker._execute_step(con, collection, "embed", row) is False, \
        "a deferral is not work"
    after = con.execute("SELECT * FROM documents WHERE uid='g:7'").fetchone()
    assert after["error"] is None, "an error takes a document out of every selection"
    assert after["retries"] == 0 and after["embed_done"] == 0
    assert stored(collection) == 0


@pytest.mark.parametrize("exc", [
    hold_runs.RunUnvalidated("new", R1),
    hold_runs.RunUnvalidated("failed", R1, failed=True),
    hold_runs.RunRefused("no run"),
    emb.GateDeferred("unreachable"),
])
def test_the_runs_state_is_never_the_documents_fault(exc):
    assert worker.transient_failure(exc) is True


# ---- the gate: at once, and once ---------------------------------------------------

def test_the_gate_runs_at_once_and_work_resumes_under_the_run(node, rage):
    root, collection, codex = node
    con = db.connect(collection)

    assert backend_gate.embed_allowed(con, collection) is True
    kept = statements_kept(root)
    assert [b["run"] for b in kept] == [R1]
    body = kept[0]
    assert body["version"] == 2 and body["role"] == "document"
    assert body["space_id"] == codex.space_id
    assert body["engine"] == R1_OBJ["engine"] and body["driver"] == R1_OBJ["driver"]
    assert body["batch_invariant"] is True and body["fingerprint"].startswith("sha256:")

    assert embeddings.upsert_book(7, "en", chunks(3), collection) == 3
    assert stored(collection) == 3


def test_once_per_run_not_once_per_answer(node, rage):
    root, collection, codex = node
    con = db.connect(collection)
    assert backend_gate.embed_allowed(con, collection) is True
    gate_cost = rage.texts
    assert gate_cost >= 512, "the gate embeds the whole calibration set"

    # cached: asking again costs nothing at all
    assert backend_gate.embed_allowed(con, collection) is True
    assert rage.texts == gate_cost

    # twenty documents: twenty answers, no second gate
    for book in range(20):
        embeddings.upsert_book(book, "en", chunks(2, book), collection)
    assert rage.texts == gate_cost + 40

    # and a verdict forgotten costs ONE tiny request an encoder, not 512: the
    # statement is what validates the run, not the cache
    run_check.reset_cache()
    assert backend_gate.embed_allowed(con, collection) is True
    assert rage.texts == gate_cost + 40 + 2, "one probe each: document, query"
    assert len(statements_kept(root)) == 1


def test_the_query_encoder_is_a_run_of_its_own(node, rage):
    """03 §3.3: "An asymmetric space's query encoder is a run of its own,
    validated against the query reference vectors" — gated at once and by
    itself, from the same place, and never deciding whether documents may be
    embedded."""
    root, collection, codex = node
    con = db.connect(collection)
    rage.by_url[QUERY_URL] = R2
    assert backend_gate.embed_allowed(con, collection) is True
    document, = statements_kept(root)
    query, = statements_kept(root, "query")
    assert (document["run"], document["role"]) == (R1, "document")
    assert (query["run"], query["role"]) == (R2, "query")
    assert run_check.answering(collection) == {"document": R1, "query": R2}

    # a query side that states no run pauses nothing: it is only searched with
    rage.by_url[QUERY_URL] = None
    run_check.reset_cache()
    rage.run = R1
    assert backend_gate.embed_allowed(con, collection) is True


def test_backend_gate_off_does_not_switch_the_run_check_off(node, rage, monkeypatch):
    root, collection, codex = node
    monkeypatch.setattr(config, "BACKEND_GATE", "off")
    rage.run = None
    assert backend_gate.embed_allowed(db.connect(collection), collection) is False


def test_a_second_caller_does_not_start_a_second_gate(node, rage):
    """Two threads of one collection's stage: the one that finds the other
    looking gets False for this round — never a wait, never a second gate."""
    root, collection, codex = node
    con = db.connect(collection)
    lock = run_check._looking.setdefault(run_check._flight(collection),
                                         __import__("threading").Lock())
    assert lock.acquire(blocking=False)
    try:
        assert backend_gate.embed_allowed(con, collection) is False
        assert rage.posts == 0, "it asked the endpoint while another was gating"
    finally:
        lock.release()


# ---- a run that changes under the work ---------------------------------------------

def test_a_run_that_changes_mid_document_stops_the_write(node, rage, monkeypatch):
    root, collection, codex = node
    con = db.connect(collection)
    assert backend_gate.embed_allowed(con, collection) is True
    monkeypatch.setattr(config, "EMBED_SLICE_CHUNKS", 2)

    rage.switch_after = 2                  # two more answers from R1, then R2
    with pytest.raises(hold_runs.RunUnvalidated) as e:
        embeddings.upsert_book(7, "en", chunks(6), collection)
    assert e.value.run_id == R2
    assert stored(collection) == 2, "the validated run's slice stays; R2's is not written"

    # the verdict was forgotten with it, so the stage looks again: R2 is
    # gated at once, passes, and the document RESUMES from what is written
    rage.switch_after = None
    before = rage.texts
    assert backend_gate.embed_allowed(con, collection) is True
    assert sorted(b["run"] for b in statements_kept(root)) == sorted([R1, R2])
    gated = rage.texts
    assert gated - before >= 512
    assert embeddings.upsert_book(7, "en", chunks(6), collection) == 6
    assert stored(collection) == 6
    assert rage.texts - gated == 4, "the two chunks already written were embedded again"


def test_a_slice_answered_by_two_runs_is_not_written(node, rage, monkeypatch):
    """One slice, several requests: every answer counts, not the last one."""
    root, collection, codex = node
    assert backend_gate.embed_allowed(db.connect(collection), collection) is True
    rage.switch_after = 1
    with pytest.raises(hold_runs.RunUnvalidated):
        embeddings.upsert_book(7, "en", chunks(3), collection)
    assert stored(collection) == 0


# ---- an endpoint that states no run --------------------------------------------------

def test_a_runless_endpoint_serves_a_query_and_refuses_a_write(node, rage):
    root, collection, codex = node
    rage.run = None
    recipe = collections_mod.recipe_for(collection)

    vector = embeddings.embed_texts(["what is a lighthouse"], recipe=recipe, query=True)
    assert len(vector) == 1 and len(vector[0]) == codex.space.dim

    with pytest.raises(hold_runs.RunRefused, match="states no run identity"):
        embeddings.upsert_book(7, "en", chunks(3), collection)
    assert stored(collection) == 0

    con = db.connect(collection)
    assert backend_gate.embed_allowed(con, collection) is False
    state = backend_gate.gate_state(con)
    assert state["allowed"] is False and "no run identity" in state["reason"]
    assert statements_kept(root) == [], "there is no run to make a statement about"


def test_a_description_that_does_not_hash_to_the_run_is_refused(node, rage):
    root, collection, codex = node
    rage.known[R1] = R2_OBJ                 # the platform's two answers disagree
    with pytest.raises(hold_runs.RunRefused, match="does not hash"):
        run_check.stated_run(collections_mod.recipe_for(collection))
    assert backend_gate.embed_allowed(db.connect(collection), collection) is False
    assert statements_kept(root) == []


def test_the_platform_is_found_before_upstream_or_at_rage_url():
    assert hold_runs.base_url(DOC_URL) == RAGE
    assert hold_runs.base_url("http://enc:8080", "http://rage:30437/") == RAGE
    assert hold_runs.base_url("http://enc:8080") is None


# ---- a run that fails, and one that batches differently ---------------------------------

def test_a_failed_run_is_kept_pauses_and_is_not_gated_again(node, rage):
    root, collection, codex = node
    con = db.connect(collection)
    rage.tag = b"another model"
    assert backend_gate.embed_allowed(con, collection) is False
    kept = statements_kept(root)
    assert len(kept) == 1 and hold_runs.passing(kept[0], codex) is False, \
        "a failure is kept too, so the run is not retried against the same set"
    assert "FAILED the gate" in backend_gate.gate_state(con)["reason"]

    cost = rage.texts
    run_check.reset_cache()
    assert backend_gate.embed_allowed(con, collection) is False
    assert rage.texts == cost + 2, "the failed run was gated a second time"

    with pytest.raises(hold_runs.RunUnvalidated) as e:
        embeddings.upsert_book(7, "en", chunks(2), collection)
    assert e.value.failed is True and stored(collection) == 0


def test_a_run_whose_batches_differ_is_for_single_text_work_only(node, rage, monkeypatch):
    root, collection, codex = node
    monkeypatch.setattr(config, "EMBED_BATCH", 4)
    rage.batches_differ = True
    con = db.connect(collection)

    assert backend_gate.embed_allowed(con, collection) is False
    body = statements_kept(root)[0]
    assert hold_runs.passing(body, codex) is True, "every cosine is within tolerance"
    assert body["batch_invariant"] is False
    assert "single-text work only" in backend_gate.gate_state(con)["reason"]

    # the same run, the same statement, contributing one text at a time
    monkeypatch.setattr(config, "EMBED_BATCH", 1)
    run_check.reset_cache()
    assert backend_gate.embed_allowed(con, collection) is True
    assert embeddings.upsert_book(7, "en", chunks(2), collection) == 2


def test_the_gate_contributes_with_the_workers_concurrency(node, rage, monkeypatch):
    """`recipe.concurrency` documents in flight is a setting the answers can
    depend on, so the gate's second pass sends that many requests at once."""
    root, collection, codex = node
    from dataclasses import replace
    recipe = replace(collections_mod.recipe_for(collection), concurrency=4)
    assert run_check.batched(recipe) is True
    contribute = run_check.answers(recipe, how="contribute")
    texts = [f"text {i}" for i in range(16)]
    assert contribute(texts) == run_check.answers(recipe, how="one")(texts)
    assert contribute.runs == {R1}


# ---- stopping, and a statement nobody signed ------------------------------------------

def test_a_node_that_is_stopping_leaves_the_gate_and_keeps_nothing(node, rage):
    root, collection, codex = node
    recipe = collections_mod.recipe_for(collection)
    asked = {"n": 0}

    def stop() -> bool:
        asked["n"] += 1
        return asked["n"] > 3

    with pytest.raises(emb.GateDeferred, match="stopping"):
        run_check.validate(root, "gutenberg", codex, recipe, key=KEY, now=NOW, stop=stop)
    assert rage.texts < 512, "it went on embedding after it was told to stop"
    assert statements_kept(root) == []


def test_a_pass_whose_answers_came_from_two_runs_is_inconclusive(node, rage):
    root, collection, codex = node
    rage.switch_after = 100
    with pytest.raises(emb.GateDeferred, match="inconclusive"):
        run_check.validate(root, "gutenberg", codex, collections_mod.recipe_for(collection),
                           key=KEY, now=NOW)
    assert statements_kept(root) == []


def test_a_statement_nobody_signed_validates_nothing(node, rage):
    root, collection, codex = node
    assert backend_gate.embed_allowed(db.connect(collection), collection) is True
    path = hold_runs.path_for(root, "gutenberg", R1)
    forged = json.loads(path.read_bytes())
    forged["body"]["run"] = R2
    hold_runs.path_for(root, "gutenberg", R2).write_bytes(canonical_json(forged))

    hold_runs.check(root, "gutenberg", codex, {R1})
    with pytest.raises(hold_runs.RunUnvalidated):
        hold_runs.check(root, "gutenberg", codex, {R2})


def test_the_check_is_a_lookup_until_the_directory_changes(node, rage, monkeypatch):
    root, collection, codex = node
    assert backend_gate.embed_allowed(db.connect(collection), collection) is True
    reads = {"n": 0}
    real = emb.read_statement

    def counting(envelope):
        reads["n"] += 1
        return real(envelope)

    monkeypatch.setattr(emb, "read_statement", counting)
    for _ in range(50):
        hold_runs.check(root, "gutenberg", codex, {R1})
    assert reads["n"] == 0, "a signature was verified per answer"

    hold_runs.path_for(root, "gutenberg", R1).touch()
    path = hold_runs.path_for(root, "gutenberg", R1)
    path.write_bytes(path.read_bytes() + b"\n")
    hold_runs.check(root, "gutenberg", codex, {R1})
    assert reads["n"] == 1, "a changed directory is read again"


# ---- the command line -------------------------------------------------------------------

def test_vectors_build_checks_each_batch_before_its_write():
    from app.hold import snapshot as snap, vectors_build as vb
    written: list = []
    calls = {"n": 0}

    def before_write():
        calls["n"] += 1
        if calls["n"] == 2:
            raise hold_runs.RunRefused("no run")

    todo = [{"id": f"c{i}", "book_id": 1, "lang": "en", "seq": i, "chapter": "",
             "text": f"t{i}", "tokens": 1} for i in range(6)]
    shard = snap.Shard.__new__(snap.Shard)
    object.__setattr__(shard, "slice", "en")
    object.__setattr__(shard, "n", 0)
    import unittest.mock as mock
    with mock.patch.object(vb, "pending", lambda codex, records, vectors_for: todo):
        with pytest.raises(hold_runs.RunRefused):
            vb.build(None, shards=[shard], records_for=lambda s: [],
                     vectors_for=lambda ids: {}, embed=lambda t: [[0.0]] * len(t),
                     write=written.append, batch=2, before_write=before_write)
    assert len(written) == 1, "the batch whose answers were refused was written"


def test_the_command_line_gates_a_new_run_inline(node, rage):
    root, collection, codex = node
    recipe = collections_mod.recipe_for(collection)
    said: list[str] = []
    run_check.guard(root, "gutenberg", codex, recipe, {R1}, key=KEY, log=said.append)
    assert [b["run"] for b in statements_kept(root)] == [R1]
    assert any("new to this peer" in m for m in said)

    cost = rage.texts
    run_check.guard(root, "gutenberg", codex, recipe, {R1}, key=KEY)
    assert rage.texts == cost, "a validated run was gated again"

    with pytest.raises(hold_runs.RunRefused):
        run_check.guard(root, "gutenberg", codex, recipe, {None}, key=KEY)


# ---- everything older is untouched ---------------------------------------------------------

def test_a_collection_older_than_version_3_is_not_touched(isolated_config, monkeypatch):
    collection = collections_mod.default_collection()
    assert run_check.applies(collection) is False and embeddings._by_run(collection) is False

    def three_arguments(batch, timeout, recipe):       # the shape it always had
        return [[1.0, 0.0, 0.0, 0.0] for _ in batch]

    monkeypatch.setattr(embeddings, "_embed_batch", three_arguments)
    run_check.check_write(collection, None, {None})     # silent: not its business


# ---- a check runs under a validated run, or is inconclusive ---------------------------------

def test_a_check_runs_under_a_validated_run_or_is_inconclusive(node, rage):
    """03 §3.3, checking: "An answer that names another makes the check
    inconclusive, and an inconclusive check yields no attestation." Never a
    verdict: an encoder that is not the space's says nothing about vectors
    that are."""
    root, collection, codex = node
    recipe = collections_mod.recipe_for(collection)
    embed = run_check.checking(root, "gutenberg", codex, recipe, key=KEY)

    assert len(embed(["a text"])) == 1, "a new run is gated at once, inline, and answers"
    assert [b["run"] for b in statements_kept(root)] == [R1]

    rage.run = None
    with pytest.raises(emb.GateDeferred, match="inconclusive"):
        embed(["a text"])

    rage.run, rage.tag = R2, b"another model"
    with pytest.raises(emb.GateDeferred, match="inconclusive.*FAILED"):
        embed(["a text"])
    assert hold_runs.passing(next(b for b in statements_kept(root) if b["run"] == R2),
                             codex) is False


def test_an_older_collections_check_embeds_as_it_always_did(monkeypatch):
    from tests.hold.conftest import pubmed_v2
    asked = []
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts, **kw: asked.append(kw) or [[0.0] for _ in texts])
    embed = run_check.checking("/nowhere", "pubmed", pubmed_v2(), "a recipe")
    assert embed(["x", "y"]) == [[0.0], [0.0]]
    assert asked == [{"recipe": "a recipe", "query": False}], "no run is asked of it"


def test_t3_that_cannot_run_under_a_validated_run_is_skipped_never_failed():
    """A peer with no encoder at all may import (03 §3.5); one whose encoder
    is not a validated run is no worse placed."""
    from app.hold import admit
    from tests.hold.conftest import pubmed

    def not_validated(texts):
        raise emb.GateDeferred("inconclusive — the check did not run under a validated run")

    codex = pubmed()
    accepted = [(f"c{i}", [1.0] + [0.0] * (codex.space.dim - 1)) for i in range(32)]
    tier = admit.tier3(codex, accepted, text_of=lambda cid: "its text",
                       embed=not_validated, seed=b"\x00" * 32)
    assert tier.verdict == admit.SKIPPED and "no compliant encoder" in tier.why


def test_a_deferral_a_command_lets_through_is_exit_3_not_a_refusal(node, monkeypatch, capsys):
    from app import louise
    root, collection, codex = node

    def deferred(*a, **k):
        raise emb.GateDeferred("the encoder could not be asked")

    monkeypatch.setattr(louise.col, "show", deferred)
    assert louise.main(["hold", "collection", "show", "gutenberg", "--root", str(root)]) == 3
    assert "deferred" in capsys.readouterr().err

"""Louise's embedding path through VeGa (docs/notes/vega.md §2, §9): an
encoder declared `api = "vega"` takes a vector only with its receipt.

Against VeGa's REAL receipts service — its stores, its intake handler, its
read-only HTTP router, in process — and a LocalAI whose `vega-` model has
VeGa's wrapper in front of its backend: the tag parsed by app/vega/tag.py,
the receipt stored BEFORE the vector goes back, the floats written as Go
writes them (gofloat.py) or as base64. (tests/vega/localai_fake.py is that
LocalAI over real gRPC; grpcio is not in the development image, so this one
plays the wrapper in process.)

  - text and pair inputs go tagged, the pair as the tagged pair and never as
    `input_pairs`, asked as base64; a JSON body is read keeping `-0` and the
    halfway float32;
  - VeGa unreachable, VEGA_URL unset, a receipt missing, the wrapper unable
    to deliver one: a DEFERRAL — nothing written, no error on a document;
  - an input or a vector changed on the way: a REFUSAL — nothing written,
    surfaced as the run check's refusal is;
  - the receipts' run is the answer's run, held to the same check before a
    write as `Louise-Run` (two runs in a batch, an unvalidated run), and the
    gate reads what the run stands for from VeGa;
  - a query needs a receipt too; without one, a search does what it does
    when its encoder is unreachable;
  - an `api = "openai"` encoder never reads VeGa.
"""
from __future__ import annotations

import base64
import json
import math
import struct

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, db, embeddings, run_check, worker
from app import layers as layers_mod
from app.hold import calibrate, collection as col, embedder as emb, runs as hold_runs
from app.hold.codex import Chunker, Layer, canonical_json, digest
from app.rage.store import RunStore
from app.vega import client as vclient, service, tag as vtag
from app.verify import backend_gate
from tests.hold.conftest import KEY, NOW, PLATFORM, fake_embed, pubmed
from tests.hold.test_template import SPACE, recipe as a_recipe
from tests.vega.gofloat import go_json

VEGA_URL = "http://vega:8090"
LOCALAI = "http://localai:8080"
MODEL = "vega-bge-m3"
RECIPE = collections_mod.EmbedRecipe(url=LOCALAI, api="vega", model=MODEL, dtype="f32",
                                     ctx=512)
HALFWAY = struct.unpack("<f", bytes.fromhex("fd43ae15"))[0]
PAIR = {"form": "pair", "split": "first-section"}
ROW = {"uid": "pubmed:7", "native_id": 7}
SECTIONS = [("title", "BRCA2 and repair"), ("abstract", "First part."),
            ("conclusions", "Second part.")]


def a_run(flag: str = "") -> tuple[str, dict]:
    """A run as VeGa's wrapper describes it: its id IS the hash of it."""
    obj = {"driver": "kernel 6.18.0; xe 9F2C1A; libze-intel-gpu1 26.05.1",
           "engine": {"name": "localai/intel-sycl-f16-llama-cpp", "build": "v4.11.0",
                      "variant": None, "image": "sha256:" + "8c" * 32,
                      "args": ["/backends/vega-llama-cpp/run.sh", "--addr",
                               "127.0.0.1:${PORT}"] + ([flag] if flag else []),
                      "env": {}, "device": "Intel(R) Arc(TM) Pro B70 Graphics"},
           "weights": "sha256:" + "e2" * 32}
    return digest(canonical_json(obj)), obj


R1, R1_OBJ = a_run()
R2, R2_OBJ = a_run("--flash-attn")


def opening(texts, tag: bytes = b""):
    """The encoder: a function of a text's opening (tests/test_run_check.py
    says why), float32 values."""
    return fake_embed([t[:120] for t in texts], tag=tag)


def vector_of(form: str, content) -> list[float]:
    if form == vtag.TEXT:
        return opening([content])[0]
    return fake_embed([content[0] + "\x1f" + content[1]], tag=b"pair")[0]


class Vega:
    """VeGa's receipts service: its real stores, its real intake handler (one
    canonical JSON line each way) and its real read-only HTTP router."""

    def __init__(self, tmp_path) -> None:
        self.receipts = service.ReceiptStore()
        self.runs = service.Runs(RunStore(tmp_path / "vega-runs.sqlite"))
        app = FastAPI()
        app.include_router(service.router(self.receipts, self.runs))
        self.http = TestClient(app)
        self.down = False
        self.reads = 0
        for run_id, run in ((R1, R1_OBJ), (R2, R2_OBJ)):
            self.intake({"kind": "run", "run_id": run_id, "run": run})

    def intake(self, msg: dict) -> None:
        reply = service.handle(canonical_json(msg), self.receipts, self.runs)
        assert reply == {"ok": True}, reply

    def get(self, url, *, params=None, timeout=None):
        if self.down:
            raise httpx.ConnectError("[Errno 111] Connection refused",
                                     request=httpx.Request("GET", url))
        assert url.startswith(VEGA_URL + "/vega/"), url
        self.reads += 1
        return self.http.get(url[len(VEGA_URL):], params=params)


class LocalAI:
    """LocalAI's `/v1/embeddings` for a `vega-` model, VeGa's wrapper in front
    of its backend: each input's tag parsed (the wrapper refuses one without),
    the bare text or pair embedded, the receipt stored, THEN the vector back.
    The knobs make it the runner VeGa must not trust."""

    def __init__(self, vega: Vega) -> None:
        self.vega = vega
        self.run = R1                    # what each receipt names
        self.run_of = None               # input index -> run: a batch of two runs
        self.receipts = True             # False: a backend without the wrapper
        self.unavailable = False         # the wrapper could not deliver the receipt
        self.base64 = True               # False: JSON whatever was asked
        self.alter_vector = None         # after the receipt: LocalAI changed it
        self.alter_input = None          # before the backend: LocalAI changed it
        self.windows: list = []           # each input's window, as tagged
        self.vector = vector_of
        self.bodies: list[dict] = []
        self.texts = 0

    def post(self, url, *, json=None, timeout=None, headers=None):    # noqa: A002
        assert url == f"{LOCALAI}/v1/embeddings", url
        self.bodies.append(json)
        request = httpx.Request("POST", url)
        if self.unavailable:
            return httpx.Response(500, request=request, json={"error": {
                "code": 500, "type": "",
                "message": "rpc error: code = Unavailable desc = VeGa unavailable: "
                           "the receipt was not acknowledged"}})
        vectors = []
        for i, tagged in enumerate(json["input"]):
            tid, form, content, window = vtag.parse_window(tagged)
            self.windows.append(window)
            if self.alter_input is not None:
                content = self.alter_input(content)
            v = self.vector(form, content)
            self.texts += 1
            if self.receipts:
                run = self.run_of(i) if self.run_of else self.run
                self.vega.intake({"kind": "receipt", "id": tid, "run_id": run,
                                  "input_digest": vtag.input_digest(form, content, window),
                                  "vector_digest": vtag.vector_digest(v)})
            if self.alter_vector is not None:
                v = self.alter_vector(v)
            vectors.append(v)
        if self.base64 and json.get("encoding_format") == "base64":
            items = ['"' + base64.b64encode(struct.pack(f"<{len(v)}f", *v)).decode() + '"'
                     for v in vectors]
        else:
            items = ["[" + ",".join(go_json(x) for x in v) + "]" for v in vectors]
        data = ",".join(f'{{"embedding":{e},"index":{i},"object":"embedding"}}'
                        for i, e in enumerate(items))
        text = f'{{"object":"list","model":"{json.get("model")}","data":[{data}]}}'
        return httpx.Response(200, request=request, content=text.encode(),
                              headers={"content-type": "application/json"})


@pytest.fixture
def vega(tmp_path, monkeypatch):
    v = Vega(tmp_path)
    monkeypatch.setattr(config, "VEGA_URL", VEGA_URL)
    monkeypatch.setattr(embeddings.httpc, "get", v.get)
    monkeypatch.setattr(run_check.httpx, "get", v.get)
    return v


@pytest.fixture
def localai(vega, monkeypatch):
    la = LocalAI(vega)
    monkeypatch.setattr(embeddings.httpc, "post", la.post)
    monkeypatch.setattr(embeddings.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "EMBED_BATCH", 16)
    monkeypatch.setattr(config, "EMBED_CONCURRENCY", 1)
    return la


def parsed(body: dict) -> list[tuple[str, object]]:
    return [vtag.parse(s)[1:] for s in body["input"]]


# ---- what goes, and what comes back -------------------------------------------------

def test_text_inputs_go_tagged_as_base64_and_come_back_with_their_run(localai, vega):
    seen: set = set()
    vectors = embeddings.embed_calibration(RECIPE, ["one", "two"], runs=seen, raw=True)
    assert vectors == [vector_of("text", "one"), vector_of("text", "two")]
    assert seen == {R1}, "the receipts' run is the answer's run"
    body, = localai.bodies
    assert body["model"] == MODEL and body["encoding_format"] == "base64"
    assert "input_pairs" not in body
    assert parsed(body) == [("text", "one"), ("text", "two")]
    assert vega.reads == 1, "one receipts read for the batch"


def test_a_chunk_of_a_text_layer_goes_as_its_text(localai):
    codex = pubmed(version=5, layers=(Layer(Chunker("abstract", 1, {}), "default", None),))
    chunks = layers_mod.chunks_for_document(codex, ROW, SECTIONS)
    seen: set = set()
    vectors = embeddings.embed_chunks(codex, chunks, recipe=RECIPE, runs=seen, raw=True)
    assert vectors == [vector_of("text", chunks[0]["text"])] and seen == {R1}
    assert parsed(localai.bodies[0]) == [("text", chunks[0]["text"])]


def test_a_pair_goes_as_the_tagged_pair_never_as_input_pairs(localai):
    codex = pubmed(version=5, layers=(Layer(Chunker("abstract", 1, {}), "default", PAIR),))
    chunks = layers_mod.chunks_for_document(codex, ROW, SECTIONS)
    seen: set = set()
    vectors = embeddings.embed_chunks(codex, chunks, recipe=RECIPE, runs=seen, raw=True)
    pair = ("BRCA2 and repair", "First part.\n\nSecond part.")
    assert vectors == [vector_of("pair", pair)] and seen == {R1}
    body, = localai.bodies
    assert "input_pairs" not in body and parsed(body) == [("pair", pair)]


def test_a_json_body_is_read_keeping_negative_zero_and_the_halfway_float(localai):
    """LocalAI answering in JSON (Go's shortest float32 decimals) is read by
    `vclient.parse_response`: `response.json()` would turn `-0` into 0 and
    land 7.038531e-26 on the wrong float32, and refuse a correct vector."""
    localai.base64 = False
    localai.vector = lambda form, content: [-0.0, HALFWAY, 0.5, 0.1]
    seen: set = set()
    v, = embeddings.embed_calibration(RECIPE, ["x"], runs=seen, raw=True)
    assert seen == {R1}
    assert v[0] == 0.0 and math.copysign(1.0, v[0]) == -1.0, "the sign of zero is kept"
    assert struct.pack("<f", v[1]) == bytes.fromhex("fd43ae15"), "the backend's float32"
    assert v[3] == struct.unpack("<f", struct.pack("<f", 0.1))[0]


def test_a_vector_that_is_not_finite_is_asked_again_and_never_verified(localai, vega):
    calls = []

    def nan_once(form, content):
        calls.append(content)
        return [float("nan"), 1.0] if len(calls) == 1 else [0.5, 0.25]
    localai.vector = nan_once
    seen: set = set()
    assert embeddings.embed_calibration(RECIPE, ["x"], runs=seen, raw=True) == [[0.5, 0.25]]
    assert len(localai.bodies) == 2, "asked again, as the NaN retry always did"
    assert vega.reads == 1, "the NaN answer's receipts were never read"
    assert seen == {R1}


# ---- deferral: nothing written, nothing on a document --------------------------------

@pytest.mark.parametrize("case,match", [
    ("down", "VeGa is unreachable"),
    ("unset", "VEGA_URL is unset"),
    ("no receipt", "no receipt for 2 of 2 inputs"),
    ("unavailable", "could not deliver a receipt"),
])
def test_what_vega_cannot_vouch_for_yet_is_a_deferral(localai, vega, monkeypatch, case, match):
    if case == "down":
        vega.down = True
    elif case == "unset":
        monkeypatch.setattr(config, "VEGA_URL", "")
    elif case == "no receipt":
        localai.receipts = False
    else:
        localai.unavailable = True
    with pytest.raises(embeddings.ReceiptDeferred, match=match) as e:
        embeddings.embed_calibration(RECIPE, ["one", "two"], runs=set())
    assert isinstance(e.value, vclient.Deferral)
    assert worker.endpoint_unavailable(e.value), "read as the encoder's absence"
    assert worker.transient_failure(e.value)
    if case == "unset":
        assert localai.bodies == [], "nothing is embedded that could not be vouched for"


# ---- refusal: the answer is not what the run produced --------------------------------

@pytest.mark.parametrize("change,match", [
    ("vector", "input 0: the vector LocalAI returned is not the one the backend produced"),
    ("input", "input 0: the backend received another input"),
])
def test_an_answer_changed_on_the_way_is_a_refusal(localai, change, match):
    if change == "vector":
        localai.alter_vector = lambda v: [v[0] + 2 ** -10] + v[1:]
    else:
        localai.alter_input = lambda text: text + " "
    with pytest.raises(embeddings.ReceiptRefused, match=match) as e:
        embeddings.embed_calibration(RECIPE, ["one"], runs=set())
    assert isinstance(e.value, vclient.Refusal) and isinstance(e.value, hold_runs.RunRefused)


def test_a_body_that_is_not_an_embeddings_list_is_a_refusal(localai, monkeypatch):
    def garbage(url, **kw):
        return httpx.Response(200, request=httpx.Request("POST", url), content=b'{"data": 3}')
    monkeypatch.setattr(embeddings.httpc, "post", garbage)
    with pytest.raises(embeddings.ReceiptRefused, match="not an embeddings list"):
        embeddings.embed_calibration(RECIPE, ["one"])


# ---- the client's pieces ---------------------------------------------------------------

def test_the_client_reads_receipts_within_the_services_bound():
    assert vclient.READ_IDS == service.MAX_IDS
    asked = []

    class Answer:
        status_code = 200

        def __init__(self, ids):
            self.ids = ids

        def json(self):
            return {"receipts": [{"id": i} for i in self.ids]}

    def get(url, *, params, timeout):
        ids = params["ids"].split(",")
        asked.append(len(ids))
        return Answer(ids)
    ids = [f"{i:032x}" for i in range(service.MAX_IDS + 5)]
    assert len(vclient.fetch_receipts(VEGA_URL, ids, get=get)) == len(ids)
    assert asked == [service.MAX_IDS, 5]


def test_hold_calibrate_takes_a_vega_endpoint(localai, vega):
    assert "vega" in calibrate.APIS
    ep = calibrate.Endpoint("e.toml", LOCALAI, "vega", MODEL, "sha256:" + "e2" * 32, None)
    embed = calibrate.http_embed(512)
    vectors, runs = embed(ep, ["one"])
    assert len(vectors) == 1 and runs == {R1}
    vega.down = True
    with pytest.raises(calibrate.CalibrateUnreachable, match="VeGa is unreachable"):
        embed(ep, ["one"])


def test_an_openai_encoder_never_reads_vega(vega, monkeypatch):
    """RAGE's path, beside VeGa's until the cut-over: the run is the header's."""
    def rage(url, *, json=None, timeout=None, headers=None):    # noqa: A002
        assert "encoding_format" not in json and json["input"] == ["one"]
        return httpx.Response(200, request=httpx.Request("POST", url),
                              headers={"Louise-Run": R2},
                              json={"data": [{"index": 0, "embedding": [0.6, 0.8]}]})
    monkeypatch.setattr(embeddings.httpc, "post", rage)
    seen: set = set()
    recipe = collections_mod.EmbedRecipe(url="http://rage:30437/upstream/m", api="openai",
                                         model="m", dtype="f32", ctx=512)
    assert embeddings.embed_calibration(recipe, ["one"], runs=seen) == [[0.6, 0.8]]
    assert seen == {R2} and vega.reads == 0


# ---- a collection: the write, the gate, the worker, a search --------------------------

@pytest.fixture
def node(tmp_path, monkeypatch, localai):
    """A LOUISE_ROOT holding one collection of a symmetric space whose file
    reaches its encoder as `api = "vega"`."""
    root = tmp_path / "louise"
    (root / "collections").mkdir(parents=True)
    codex = col.create_from_recipe(
        root / "collections", "gutenberg", a_recipe(space=dict(SPACE, query={"instruction": ""})),
        platform_image=PLATFORM, embed=opening, name="gutenberg", key=KEY, now=NOW)
    assert hold_runs.applies(codex) and codex.space.query.encoder is None
    cfg = tmp_path / "config" / "collections"
    cfg.mkdir(parents=True)
    (cfg / "gutenberg.toml").write_text(
        f'codex = "{codex.collection_id}"\nembed = "on-demand"\n'
        f'[encoder]\nurl = "{LOCALAI}"\napi = "vega"\nembed_model = "{MODEL}"\nquery_model = "{MODEL}"\n', encoding="utf-8")
    monkeypatch.setenv("LOUISE_ROOT", str(root))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(collections_mod.config, "DATA_DIR", root)
    from app.bundles import keys as node_keys
    monkeypatch.setattr(node_keys, "load_signing_key", lambda path=None: KEY)
    run_check.reset_cache()
    backend_gate.reset_cache()
    hold_runs._read.clear()
    collection = next(c for c in collections_mod.load_all() if c.name == "gutenberg")
    assert collections_mod.recipe_for(collection).api == "vega"
    return root / "collections", collection, codex


def chunks(n: int, book: int = 7) -> list[dict]:
    return [{"id": f"g:{book}:{i}", "seq": i, "chapter": "", "text": f"chunk {i} of {book}"}
            for i in range(n)]


def stored(collection) -> int:
    dbh = embeddings._conn(collection)
    if embeddings.TABLE not in dbh.table_names():
        return 0
    return dbh.open_table(embeddings.TABLE).count_rows()


def statements_kept(root) -> list[dict]:
    d = hold_runs.directory(root, "gutenberg")
    return [emb.read_statement(json.loads(p.read_bytes()))
            for p in sorted(d.glob("document-*.json"))] if d.is_dir() else []


def _doc(con, uid="g:7"):
    con.execute(
        "INSERT INTO documents(uid,source,native_id,path,in_scope,rdf_done,fts_done,"
        "embed_done,chunks) VALUES(?,?,?,?,1,1,1,0,3)", (uid, "g", uid.split(":")[1], "/x"))
    return con.execute("SELECT * FROM documents WHERE uid=?", (uid,)).fetchone()


def test_an_unvalidated_run_writes_nothing_until_the_gate_reads_it_from_vega(node, localai):
    root, collection, codex = node
    with pytest.raises(hold_runs.RunUnvalidated) as e:
        embeddings.upsert_book(7, "en", chunks(3), collection)
    assert e.value.run_id == R1 and stored(collection) == 0

    # the gate at once: the run that answers, and what it stands for — read
    # from VeGa (`/vega/runs/<id>`), verified against its id
    assert backend_gate.embed_allowed(db.connect(collection), collection) is True
    body, = statements_kept(root)
    assert body["run"] == R1 and body["engine"] == R1_OBJ["engine"]
    assert body["driver"] == R1_OBJ["driver"] and body["batch_invariant"] is True
    assert embeddings.upsert_book(7, "en", chunks(3), collection) == 3
    assert stored(collection) == 3


def test_a_batch_answered_by_two_runs_is_held_to_both(node, localai):
    root, collection, codex = node
    con = db.connect(collection)
    assert backend_gate.embed_allowed(con, collection) is True          # R1 gated
    localai.run_of = lambda i: R1 if i == 0 else R2
    with pytest.raises(hold_runs.RunUnvalidated) as e:
        embeddings.upsert_book(7, "en", chunks(3), collection)
    assert e.value.run_id == R2 and stored(collection) == 0, "one request, two runs"

    # the header path's rule: every run validated, and the batch is written
    localai.run_of, localai.run = None, R2
    assert backend_gate.embed_allowed(con, collection) is True          # R2 gated
    assert sorted(b["run"] for b in statements_kept(root)) == sorted([R1, R2])
    localai.run_of = lambda i: R1 if i == 0 else R2
    assert embeddings.upsert_book(7, "en", chunks(3), collection) == 3


@pytest.mark.parametrize("case", ["no receipt", "vega down", "vector changed"])
def test_the_worker_defers_the_document_and_marks_nothing(node, localai, vega, monkeypatch,
                                                          case):
    root, collection, codex = node
    con = db.connect(collection)
    assert backend_gate.embed_allowed(con, collection) is True
    assert run_check._where(collection) in run_check._verdicts
    row = _doc(con)
    monkeypatch.setattr(worker, "_load_chunks", lambda row, collection: chunks(3))
    if case == "no receipt":
        localai.receipts = False
    elif case == "vega down":
        vega.down = True
    else:
        localai.alter_vector = lambda v: [v[0] + 2 ** -10] + v[1:]

    assert worker._execute_step(con, collection, "embed", row) is False, \
        "a deferral is not work"
    after = con.execute("SELECT * FROM documents WHERE uid='g:7'").fetchone()
    assert after["error"] is None, "an error takes a document out of every selection"
    assert after["retries"] == 0 and after["embed_done"] == 0
    assert stored(collection) == 0
    if case == "vector changed":
        # surfaced as the run check's refusal is: the verdict forgotten, and
        # the next look at the endpoint pauses the embedding with the reason
        assert run_check._where(collection) not in run_check._verdicts
        assert backend_gate.embed_allowed(con, collection) is False
        assert "not the one the backend produced" in backend_gate.gate_state(con)["reason"]


def test_a_reused_chunk_asks_nothing_and_an_embedded_one_still_needs_its_receipt(
        node, localai, vega):
    """RULE 1's reuse (`upsert_book`): a chunk whose id and text are stored
    keeps its vector and reaches no endpoint, so it needs no receipt; the
    one chunk whose text changed is embedded, and its receipt is verified
    before anything of the document is written."""
    root, collection, codex = node
    con = db.connect(collection)
    assert backend_gate.embed_allowed(con, collection) is True
    embeddings.upsert_book(7, "en", chunks(3), collection)
    posted = len(localai.bodies)

    vega.down = True
    assert embeddings.upsert_book(7, "en", chunks(3), collection) == 3
    assert len(localai.bodies) == posted, "every chunk kept: no request at all"
    vega.down = False

    changed = chunks(3)
    changed[1] = dict(changed[1], text="a new middle")
    localai.alter_vector = lambda v: [v[0] + 2 ** -10] + v[1:]
    with pytest.raises(embeddings.ReceiptRefused):
        embeddings.upsert_book(7, "en", changed, collection)
    table = embeddings._conn(collection).open_table(embeddings.TABLE)
    assert sorted(r["text"] for r in table.to_arrow().to_pylist()) == \
        ["chunk 0 of 7", "chunk 1 of 7", "chunk 2 of 7"], "a refused answer wrote nothing"


def test_a_partly_reused_document_reads_the_receipt_of_what_it_embedded(
        node, localai, vega):
    root, collection, codex = node
    con = db.connect(collection)
    assert backend_gate.embed_allowed(con, collection) is True
    embeddings.upsert_book(7, "en", chunks(3), collection)
    reads = vega.reads

    changed = chunks(3)
    changed[1] = dict(changed[1], text="a new middle")
    counts = embeddings.EmbedCounts()
    embeddings.upsert_book(7, "en", changed, collection, counts=counts)
    assert (counts.embedded, counts.reused) == (1, 2)
    assert parsed(localai.bodies[-1]) == [("text", "a new middle")]
    assert vega.reads > reads, "the embedded chunk's receipt was read"
    table = embeddings._conn(collection).open_table(embeddings.TABLE)
    assert sorted(r["text"] for r in table.to_arrow().to_pylist()) == \
        ["a new middle", "chunk 0 of 7", "chunk 2 of 7"]


def test_a_query_needs_a_receipt_too(node, localai, vega):
    from app import retrieval
    root, collection, codex = node
    con = db.connect(collection)
    assert backend_gate.embed_allowed(con, collection) is True
    embeddings.upsert_book(7, "en", chunks(3), collection)

    seen: set = set()
    vector = embeddings.embed_query(collection, "chunk 1 of 7", runs=seen)
    assert len(vector) == codex.space.dim and seen == {R1}
    assert parsed(localai.bodies[-1]) == [("text", "chunk 1 of 7")]
    hits = embeddings.search("chunk 1 of 7", collection=collection)
    assert hits and hits[0]["chunk_id"] == "g:7:1"

    # no receipt: the search goes on as it does with its encoder away
    localai.receipts = False
    with pytest.raises(embeddings.ReceiptDeferred):
        embeddings.search("chunk 1 of 7", collection=collection)
    result = retrieval.search(con, "a lighthouse", mode="hybrid", translate=False,
                              collection=collection)
    assert "legs_withheld" not in result
    events = [r["message"] for r in con.execute(
        "SELECT message FROM events WHERE stage='dense' ORDER BY id")]
    assert any("dense search failed" in m and "no receipt" in m for m in events)

    # a receipt that does not fit: the vector leg is withheld, and said so
    localai.receipts, localai.alter_vector = True, lambda v: [v[0] + 2 ** -10] + v[1:]
    result = retrieval.search(con, "a lighthouse", mode="hybrid", translate=False,
                              collection=collection)
    leg, = result["legs_withheld"]
    assert leg["leg"] == "vector" and "not the one the backend produced" in leg["reason"]


def test_a_long_input_goes_whole_with_the_spaces_ctx_as_its_window(localai):
    """docs/notes/char-cut-truncation.md: the platform cut a long input to
    about ctx x 4 CHARACTERS, a window that is not `ctx` tokens, and archive
    vectors of long inputs could not be reproduced through it. To a VeGa
    encoder it goes whole, and the tag names the window the encoder cuts at."""
    long_text = "word " * 5000                      # ten times the old character budget
    embeddings.embed_calibration(RECIPE, [long_text], raw=True)
    body, = localai.bodies
    assert parsed(body) == [("text", long_text)], "never cut by characters"
    assert localai.windows == [RECIPE.ctx]


def test_an_input_vega_cannot_cut_exactly_is_that_inputs_refusal(localai, monkeypatch):
    """The wrapper's refusal of one input (it cannot be cut at its window
    exactly) is that input's error, never a deferral retried for ever."""
    def refused(url, *, json=None, timeout=None, headers=None):   # noqa: A002
        return httpx.Response(500, request=httpx.Request("POST", url), json={"error": {
            "code": 500, "message": "rpc error: code = InvalidArgument desc = "
                                    f"{vtag.INPUT_MARK} cannot be cut at 512 tokens exactly"}})
    monkeypatch.setattr(embeddings.httpc, "post", refused)
    with pytest.raises(embeddings.InputRefused, match="cannot be cut at 512"):
        embeddings.embed_calibration(RECIPE, ["x"], raw=True)

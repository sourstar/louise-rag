"""The records build's child, faster and byte for byte the same
(docs/notes/python-hot-spots.md "C" and "F", 2026-11-02):

- C1: a task's lines are compressed together, in BATCHES, not one zstd call
  a record (a fixed 25-42 µs each), and the part's writer expands each
  batch once (`pack.build.ship_lines`, `_columns_of`, `_write_part_columns`);
- C2: the collection's layers are resolved once a task, not three times a
  record (`layers.Resolved`, `pack.build.compute_records`);
- F: held lines are parsed by orjson where it gives json.loads's object, by
  json.loads everywhere else (`pack.records.loads_line`).

The proof: for wikidata, wikipedia, pubmed and gutenberg, every staged part
— and every root, count and range the build database records of it — is
the bytes of a build BY THE BOOK (the writer encodes each line from the
whole record, json.loads parses, the layers are resolved a record at a
time): parsed and held, in-process and in a pool of spawned children, with
batches cut by the parts and parts cut by the batches, and with a child
killed by the kernel mid-build. And loads_line gives json.loads's object,
or json.loads's error, on every staged line and on generated ones."""
import bz2
import json
import math
import os
import shutil
import signal
import sqlite3
import sys
from html import escape
from pathlib import Path

import pytest

from app import collections as collections_mod
from app import config, parallel
from app import layers as layers_mod
from app.pack import build as pack_build
from app.pack import records

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
#: parts of three records: several parts a slice, and a task's batch cut by them
RPS = 3


# ---------------------------------------------------------------- the corpora

def _collection(tmp_path, name, plugin, corpus, **kw):
    from dataclasses import replace

    from app import licensing
    d = tmp_path / name
    c = collections_mod.Collection(
        name=name, plugin=plugin, profile=None, embed_mode_default=None, corpus_dir=Path(corpus),
        manifest_path=d / "manifest.db", fts_dir=d / "fts", lance_dir=d / "lancedb", **kw)
    if licensing.state_for(c) == licensing.MISSING:
        c = replace(c, license_ack=licensing.make_ack(c))
    return c


def _gutenberg(tmp_path, monkeypatch):
    from tests.conftest import gutenberg_books
    corpus = tmp_path / "gutenberg-corpus"
    corpus.mkdir()
    gutenberg_books(corpus, 13)
    return _collection(tmp_path, "gutenberg", "gutenberg", corpus)


def _pubmed(tmp_path, monkeypatch):
    corpus = tmp_path / "pubmed-corpus"
    shutil.copytree(FIXTURES / "pubmed-mini", corpus, ignore=shutil.ignore_patterns(".pack"))
    return _collection(tmp_path, "pubmed", "pubmed", corpus)


def _page_xml(pid: int, title: str, text: str) -> str:
    return (f"<page><title>{title}</title><ns>0</ns><id>{pid}</id>"
            f"<revision><id>{pid}00</id><timestamp>2026-07-31T12:02:55Z</timestamp>"
            f"<model>wikitext</model><format>text/x-wiki</format>"
            f'<text bytes="{len(text.encode())}" xml:space="preserve">{escape(text)}</text>'
            f"</revision></page>\n")


def _wikipedia(tmp_path, monkeypatch):
    """A multistream enwiki dump of the test articles, five pages a stream,
    every page with its Wikidata item (page_props)."""
    from tests import wikipedia_props
    from tests.test_plugins_wikipedia_source import ARTICLE_CHEESE, ARTICLE_MOON
    from tests.test_wikipedia_parser3 import ARTICLE as ARTICLE_P3
    from tests.test_wikipedia_promise9 import PAGE as PAGE_P9
    corpus = tmp_path / "wikipedia-corpus"
    d = corpus / "enwiki" / "latest"
    d.mkdir(parents=True)
    texts = [ARTICLE_P3, PAGE_P9, ARTICLE_MOON, ARTICLE_CHEESE] * 3
    pages = [(i + 1, f"Page {i + 1}", t) for i, t in enumerate(texts)]
    streams = [pages[i:i + 5] for i in range(0, len(pages), 5)]
    header = bz2.compress(b'<mediawiki xml:lang="en"><siteinfo><dbname>enwiki</dbname></siteinfo>\n')
    blobs = [bz2.compress("".join(_page_xml(*p) for p in s).encode()) for s in streams]
    dump = d / "enwiki-latest-pages-articles-multistream.xml.bz2"
    dump.write_bytes(header + b"".join(blobs) + bz2.compress(b"</mediawiki>\n"))
    index, offset = [], len(header)
    for s, blob in zip(streams, blobs):
        index += [f"{offset}:{pid}:{title}" for pid, title, _ in s]
        offset += len(blob)
    (d / "enwiki-latest-pages-articles-multistream-index.txt.bz2").write_bytes(
        bz2.compress(("\n".join(index) + "\n").encode()))
    wikipedia_props.write(dump, [(pid, "wikibase_item", f"Q{1000 + pid}") for pid, _, _ in pages])
    return _collection(tmp_path, "wikipedia", "wikipedia", corpus)


def _wikidata(tmp_path, monkeypatch):
    """The fixture dump with a label-only entity for every id it names, cut
    by `wikidata-text` at 24 tokens (most entities are windowed) with a
    word tokenizer found by its sha256 under TOKENIZERS_DIR — by the
    children too, which read the environment."""
    import re

    from tokenizers import Tokenizer, models, pre_tokenizers

    from app.plugins.wikidata import chunker
    from tests.test_plugins_wikidata_source import a_dump, entity_lines
    toks = tmp_path / "tokenizers"
    toks.mkdir()
    tok = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    tok.save(str(toks / "tok.json"))
    sha = chunker.sha256_of(toks / "tok.json")
    (toks / f"{sha[7:]}.json").write_bytes((toks / "tok.json").read_bytes())
    monkeypatch.setenv("TOKENIZERS_DIR", str(toks))
    monkeypatch.setattr(config, "TOKENIZERS_DIR", toks)
    monkeypatch.setitem(chunker._tokenizers, sha, chunker.tokenizer_for(sha))
    lines = entity_lines()
    have = {json.loads(line)["id"] for line in lines}
    named = sorted(set(re.findall(rb'"(?:id|property)":"([PQ][0-9]+)"', b"\n".join(lines)))
                   - {h.encode() for h in have})
    stubs = []
    for i in (x.decode() for x in named):
        stub = {"type": "property" if i.startswith("P") else "item", "id": i,
                "labels": {"en": {"language": "en", "value": f"label of {i}"}},
                "descriptions": {}, "aliases": {}, "claims": {}, "sitelinks": {}}
        if i.startswith("P"):
            stub["datatype"] = "string"
        stubs.append(json.dumps(stub).encode())
    corpus = tmp_path / "wikidata-corpus"
    a_dump(corpus, lines=lines + stubs)
    layer = layers_mod.Layer(chunker="wikidata-text", version=1, rule="default",
                             params={"tokenizer": sha, "max_tokens": 24})
    return _collection(tmp_path, "wikidata", "wikidata", corpus, layers=(layer,))


CORPORA = {"gutenberg": _gutenberg, "pubmed": _pubmed, "wikipedia": _wikipedia, "wikidata": _wikidata}


# ---------------------------------------------------------------- the build by the book

class _PerRecord:
    """The layers as the child resolved them before "C": `layer_index` and
    `chunker_for_document` for every record, each resolving `layers_for`."""

    def __init__(self, collection):
        self.collection = collection

    def index(self, native_id, row=None) -> int:
        self.native_id = native_id
        return layers_mod.layer_index(self.collection, native_id, row)

    def chunker_at(self, index: int):
        return layers_mod.chunker_for_document(self.collection, self.native_id)


def _by_the_book(m) -> None:
    """The writer encodes every line from the whole record (nothing shipped),
    json.loads parses held lines, the layers are resolved a record at a time."""
    m.setattr(pack_build, "with_line", lambda collection, got: got)
    m.setattr(records, "_orjson", None)
    m.setattr(pack_build, "_layers_of", _PerRecord)


def _build(c, pack_dir, **kw):
    return pack_build.build(c, pack_dir, records_per_shard=RPS, snapshot="2026-11-02", sign=False,
                            log=lambda *_: None, **kw)


def _staged(pack_dir: Path) -> dict[str, bytes]:
    """Every staged part, by its path."""
    return {p.relative_to(pack_dir).as_posix(): p.read_bytes()
            for p in sorted((pack_dir / pack_build.RECORDS_DIR).rglob("*")) if p.is_file()}


def _recorded(pack_dir: Path) -> list[tuple]:
    """What the build database records of each part, its time left out."""
    con = sqlite3.connect(pack_dir / pack_build.BUILD_DIR / pack_build.BUILD_DB)
    try:
        return con.execute(
            "SELECT slice, n, path, records, chunks, bytes, sha256, first_uid, last_uid, last_ord, "
            "id_min, id_max, layers, root, records_root, chunks_root, first_native_id, last_native_id "
            "FROM shards ORDER BY slice, n").fetchall()
    finally:
        con.close()


def _lines_of(blob_path: Path) -> list[bytes]:
    index = records.read_index(blob_path)
    return [line for k in range(len(index.frames)) for line in records.read_frame(blob_path, index, k)]


def _killing_a_child(monkeypatch, killed: list[int], kinds: list[str]) -> None:
    """At the first part submitted to a FRESH pool, its child is SIGKILLed —
    the kernel's OOM kill (operator, 2026-10-30: a kill recovers by itself,
    "not involving this agent"). A spawned child is still importing the
    app then, so the part cannot have been answered: the pool breaks."""
    real = parallel._submit

    def submit(ex, kind, collection, part):
        fut = real(ex, kind, collection, part)
        kinds.append(kind)
        if not killed:
            for p in list((getattr(ex, "_processes", None) or {}).values()):
                os.kill(p.pid, signal.SIGKILL)
                killed.append(p.pid)
                break
        return fut
    monkeypatch.setattr(parallel, "_submit", submit)


@pytest.mark.parametrize("corpus", sorted(CORPORA))
def test_every_staged_byte_is_a_build_by_the_book(corpus, isolated_config, tmp_path, monkeypatch):
    c = CORPORA[corpus](tmp_path, monkeypatch)
    from app import db, discovery
    con = db.connect(c)
    discovery.sync_collection(con, c)
    con.close()

    with monkeypatch.context() as m:
        _by_the_book(m)
        _build(c, tmp_path / "book")
    want, recorded = _staged(tmp_path / "book"), _recorded(tmp_path / "book")
    assert len(want) >= 2, f"{corpus}: several parts, so batches and parts cut each other"
    n_lines = sum(len(_lines_of(tmp_path / "book" / rel)) for rel in want)
    assert n_lines > RPS

    def same_as_the_book(pack_dir: Path, how: str) -> None:
        got = _staged(pack_dir)
        assert sorted(got) == sorted(want), f"{corpus}, {how}: the same parts"
        for rel in want:
            assert got[rel] == want[rel], f"{corpus}, {how}: {rel} differs from the build by the book"
        assert _recorded(pack_dir) == recorded, f"{corpus}, {how}: the same roots, counts and ranges"

    # in-process: two records a batch (several batches a part, each cut by
    # the parts), then one line a batch (the byte cap)
    with monkeypatch.context() as m:
        m.setattr(pack_build, "SHIP_BATCH_RECORDS", 2)
        same_as_the_book(_build(c, tmp_path / "serial-2").pack_dir, "batches of 2")
    with monkeypatch.context() as m:
        m.setattr(pack_build, "SHIP_BATCH_BYTES", 1)
        same_as_the_book(_build(c, tmp_path / "serial-1").pack_dir, "a line a batch")

    # held, in-process: the lines taken from the book's parts, parsed by orjson
    parts = pack_build.HeldRecords(tmp_path / "book", tuple(want))
    same_as_the_book(_build(c, tmp_path / "held-serial", held=parts).pack_dir, "held, in-process")
    with monkeypatch.context() as m:
        _by_the_book(m)
        same_as_the_book(_build(c, tmp_path / "held-book", held=parts).pack_dir, "held, by the book")

    # in a pool of two spawned children (the module's own constants: a
    # task's batch spans the parts of three records), parsed and held; the
    # held build loses a child to SIGKILL and finishes all the same
    monkeypatch.setattr(config, "PARSE_PROCESSES", 2)
    parallel.shutdown()
    parallel.reset_failures()
    try:
        kinds: list[str] = []
        real = parallel._submit
        monkeypatch.setattr(parallel, "_submit",
                            lambda ex, kind, col, part: kinds.append(kind) or real(ex, kind, col, part))
        same_as_the_book(_build(c, tmp_path / "pool").pack_dir, "parsed, in the pool")
        assert kinds and set(kinds) == {"record"}, "the records were computed by the children"
        monkeypatch.setattr(parallel, "_submit", real)
        killed: list[int] = []
        kinds.clear()
        broken: list[str] = []
        real_failure = parallel._note_failure
        monkeypatch.setattr(parallel, "_note_failure",
                            lambda exc, ex=None: broken.append(type(exc).__name__) or real_failure(exc, ex))
        parallel.shutdown()
        _killing_a_child(monkeypatch, killed, kinds)
        same_as_the_book(_build(c, tmp_path / "held-pool", held=parts).pack_dir,
                         "held, in the pool, a child killed")
        assert killed and set(kinds) == {"held-record"}, "a child of the held build was killed"
        assert broken, "the pool broke, and its unanswered parts were computed again"
    finally:
        parallel.shutdown()
        parallel.reset_failures()

    # and every staged line parses to json.loads's object
    for rel in want:
        for line in _lines_of(tmp_path / "book" / rel):
            _assert_same_parse(line)


def test_two_layers_cut_as_a_build_by_the_book_cuts_them(isolated_config, tmp_path, monkeypatch):
    """A collection of TWO layers (an archive's ids cut by `abstract`, the
    rest by `book`): the layer and the chunks of every record are the ones
    a resolution per record gives, parsed and held. In-process: the
    archive is this test's, which a spawned child would not see."""
    from app import db, discovery
    from app.bundles import genesis
    arc = genesis.GenesisArchive(archive_id="arc", base_url="", adapter="medcpt-npy",
                                 shards=("3000000",), uid_prefix="pubmed:", ids_per_shard=10)
    monkeypatch.setitem(genesis.ARCHIVES, "arc", arc)
    two = layers_mod.parse_layers([{"chunker": "abstract", "rule": "archive:arc"},
                                   {"chunker": "book"}], where="t")
    corpus = tmp_path / "pubmed-corpus"
    shutil.copytree(FIXTURES / "pubmed-mini", corpus, ignore=shutil.ignore_patterns(".pack"))
    c = _collection(tmp_path, "pubmed2", "pubmed", corpus, layers=two)
    con = db.connect(c)
    discovery.sync_collection(con, c)
    con.close()
    with monkeypatch.context() as m:
        _by_the_book(m)
        _build(c, tmp_path / "book")
    want, recorded = _staged(tmp_path / "book"), _recorded(tmp_path / "book")
    assert {k for row in recorded for k in json.loads(row[12])} == {"0", "1"}, "both layers hold records"
    with monkeypatch.context() as m:
        m.setattr(pack_build, "SHIP_BATCH_RECORDS", 2)
        d = _build(c, tmp_path / "new").pack_dir
    held = _build(c, tmp_path / "held", held=pack_build.HeldRecords(tmp_path / "book", tuple(want))).pack_dir
    for pack_dir in (d, held):
        assert _staged(pack_dir) == want and _recorded(pack_dir) == recorded


def test_a_task_resolves_its_layers_once_and_compresses_a_batch_at_a_time(isolated_config, tmp_path,
                                                                          monkeypatch):
    """C2 and C1, counted: one `layers_for` a task where there were three a
    record, and one batch (one compress call, one object pickled once) for
    every SHIP_BATCH_RECORDS lines."""
    import pickle
    from multiprocessing.reduction import ForkingPickler
    c = _gutenberg(tmp_path, monkeypatch)
    from app import db, discovery
    con = db.connect(c)
    discovery.sync_collection(con, c)
    rows = [{k: r[k] for k in ("uid", "native_id", "path", "lang")}
            for r in con.execute("SELECT * FROM documents WHERE error IS NULL ORDER BY uid")]
    con.close()
    assert len(rows) == 13
    calls: list[int] = []
    real = layers_mod.layers_for
    monkeypatch.setattr(layers_mod, "layers_for", lambda col: calls.append(1) or real(col))
    monkeypatch.setattr(pack_build, "SHIP_BATCH_RECORDS", 5)
    out = parallel._compute("record", c, rows)
    assert all(ok for ok, _ in out)
    assert len(calls) == 1, f"layers resolved {len(calls)} times for a task of {len(rows)} records"
    refs = [value[0][pack_build.LINE_KEY] for _ok, value in out]
    batches = {id(ref[0]) for ref in refs}
    assert len(batches) == math.ceil(len(rows) / 5), "a batch every five lines"
    crossed = pickle.loads(ForkingPickler.dumps(out))
    assert len({id(v[0][pack_build.LINE_KEY][0]) for _ok, v in crossed}) == len(batches), \
        "a batch crosses the pipe once, however many records it holds"
    # outside a task (the CLI, `hold check`), a record still resolves on its own
    calls.clear()
    pack_build._record_of(c, c.plugin_instance(), rows[0])
    assert len(calls) == 1


# ---------------------------------------------------------------- F: orjson parses, json.loads decides

def _same(a, b) -> bool:
    """The same object, strictly: types, key ORDER (a nested dict is written
    in its own order), and floats by repr (-0.0 is not 0.0). Iterative:
    the lines nest deeper than the interpreter's stack."""
    todo = [(a, b)]
    while todo:
        x, y = todo.pop()
        if type(x) is not type(y):
            return False
        if isinstance(x, dict):
            if list(x) != list(y):
                return False
            todo.extend((x[k], y[k]) for k in x)
        elif isinstance(x, list):
            if len(x) != len(y):
                return False
            todo.extend(zip(x, y))
        elif isinstance(x, float):
            if repr(x) != repr(y):
                return False
        elif x != y:
            return False
    return True


def _parse(fn, line):
    try:
        return "ok", fn(line)
    except RecursionError:
        return "error", ("RecursionError", "")
    except Exception as e:  # noqa: BLE001
        return "error", (type(e).__name__, str(e))


def _assert_same_parse(line) -> None:
    got, want = _parse(records.loads_line, line), _parse(json.loads, line)
    assert got[0] == want[0], (line[:120], got, want)
    if want[0] == "error":
        assert got[1] == want[1], (line[:120], got[1], want[1])
        return
    assert _same(got[1], want[1]), (line[:120], got[1], want[1])
    if isinstance(want[1], dict):
        assert records.canonical_line(got[1]) == records.canonical_line(want[1])


#: what orjson parses otherwise than json.loads, and where it refuses
EDGES: list[bytes] = [
    # integers past 64 bits: orjson returns a FLOAT for them, without raising
    b'{"extra":{"n":18446744073709551616}}', b'{"extra":{"n":-9223372036854775809}}',
    b'{"n":123456789012345678901234567890}', b'[18446744073709551615,-9223372036854775808]',
    b'{"n":9223372036854775807}', b'{"n":1000000000000000000}', b'{"n":999999999999999999}',
    b'{"id":"1234567890123456789012"}',                 # a long run inside a string
    # NaN and the infinities json.loads takes and orjson refuses
    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'[1e400]', b'[-1e400]',
    b'[1.7976931348623159e308]',
    # surrogates: escaped (lone and paired) and raw (CESU), and invalid UTF-8
    b'{"t":"\\ud800"}', b'{"t":"\\udc00x"}', b'{"t":"a\\ud83d\\ude00b"}', b'{"t":"\xed\xa0\x80"}',
    b'{"t":"\xff"}', b'{"t":"\xc0\xaf"}', b'{"t":"\xe2\x82"}',
    # duplicate keys, at the top and nested: the last value at the first place
    b'{"a":1,"b":2,"a":3}', b'{"extra":{"k":[1],"j":2,"k":{"z":0}}}', b'{"a":{"x":1},"a":{"y":2}}',
    # floats
    b'[-0.0,0.0,-0,0e0,-0e-0,1E5,1e-400,5e-324,2.2250738585072011e-308,0.30000000000000004]',
    b'[1.00000000000000011102230246251565404236316680908203125]', b'[1.7976931348623157e308]',
    b'[123456789.123456789,0.000000000000000001234]',
    # non-ASCII, raw and escaped, and the escapes
    "{\"t\":\"Città — 東京 😀 \u2028\u2029 \ufeff\"}".encode(), b'{"t":"\\u00e9\\u6771\\/\\b\\f\\n\\r\\t\\"\\\\"}',
    b'{"t":"\\u0000\x7f"}', b'{"t":"\x01"}', b'{"t":"\x1f"}',
    # framing: a BOM, whitespace, empties, not JSON at all
    b'\xef\xbb\xbf{"a":1}', b' \t\r\n{"a" : [ 1 , 2 ] }\r\n', b'{}', b'[]', b'""', b'',
    b'{"a":1}{"b":2}', b'{"a":1,}', b'[01]', b'{"a":.5}', b"{'a':1}", b'null', b'true',
]


def _nested(depth: int) -> bytes:
    return b'{"x":' * depth + b"1" + b"}" * depth


DEEP = [_nested(d) for d in (500, 1023, 1024, 1025, 3000)] + [b"[" * 1200 + b"]" * 1200]


@pytest.mark.parametrize("line", EDGES + DEEP, ids=[f"edge{i}" for i in range(len(EDGES))]
                         + ["nested500", "nested1023", "nested1024", "nested1025", "nested3000", "lists1200"])
def test_loads_line_is_json_loads_on_the_edges(line):
    pytest.importorskip("orjson")
    _assert_same_parse(line)
    _assert_same_parse(bytearray(line))
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return
    _assert_same_parse(text)          # not bytes: json.loads, as before


def test_loads_line_falls_back_where_orjson_would_differ_without_raising():
    """The one difference orjson does not raise for: an integer past 64
    bits comes back a float. Such a line never reaches it."""
    orjson = pytest.importorskip("orjson")
    line = b'{"n":18446744073709551616}'
    assert isinstance(orjson.loads(line)["n"], float), "the hazard this guards (orjson's behaviour)"
    assert records.loads_line(line) == {"n": 18446744073709551616}
    assert type(records.loads_line(line)["n"]) is int


def test_loads_line_is_json_loads_on_generated_lines():
    """Records-shaped lines and arbitrary JSON, generated: every key order,
    nesting, number and string json.dumps can write, ASCII-escaped or raw."""
    pytest.importorskip("orjson")
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    scalars = st.one_of(
        st.none(), st.booleans(),
        st.integers(),                                          # unbounded: past 64 bits too
        st.integers(min_value=-(2 ** 64), max_value=2 ** 64),
        st.floats(allow_nan=True, allow_infinity=True),
        st.floats(allow_nan=False, allow_infinity=False, width=64),
        st.text(),                                              # surrogates included
        st.text(alphabet=st.characters(codec="utf-8")),
        st.from_regex(r"[0-9]{15,25}", fullmatch=True),         # digit runs in strings
    )
    values = st.recursive(scalars, lambda inner: st.one_of(
        st.lists(inner, max_size=6), st.dictionaries(st.text(max_size=8), inner, max_size=6)),
        max_leaves=40)

    @settings(max_examples=1500, deadline=None, suppress_health_check=list(HealthCheck))
    @given(values, st.booleans(), st.sampled_from([(",", ":"), (", ", ": ")]))
    def check(value, ascii_only, separators):
        text = json.dumps(value, ensure_ascii=ascii_only, separators=separators)
        _assert_same_parse(text.encode("utf-8", "surrogatepass"))

    @settings(max_examples=300, deadline=None, suppress_health_check=list(HealthCheck))
    @given(st.fixed_dictionaries({
        "uid": st.text(), "native_id": st.from_regex(r"[0-9]{1,22}", fullmatch=True),
        "lang": st.text(max_size=3), "title": st.text(), "creators": st.lists(st.text(), max_size=3),
        "date": st.one_of(st.none(), st.text(max_size=10)), "subjects": st.lists(st.text(), max_size=3),
        "keys": st.lists(st.text(), max_size=3),
        "sections": st.lists(st.tuples(st.text(), st.text()).map(list), max_size=4),
        "extra": st.dictionaries(st.text(max_size=8), values, max_size=5),
        "origin": st.text(), "content_hash": st.text(), "origin_hash": st.text()}))
    def check_record(record):
        try:
            line = records.canonical_line(record).rstrip(b"\n")
        except UnicodeEncodeError:                              # a lone surrogate raw: never a line
            return
        _assert_same_parse(line)

    check()
    check_record()


def test_the_orjson_pin_is_the_image_s():
    """orjson is a dependency of the image, pinned exactly in both files
    (tests/test_image_constraints.py), and imported where it is installed."""
    root = Path(__file__).resolve().parents[2]
    req = [ln.strip() for ln in (root / "requirements.txt").read_text().splitlines()
           if ln.strip().startswith("orjson")]
    pins = [ln.strip() for ln in (root / "constraints.txt").read_text().splitlines()
            if ln.strip().startswith("orjson==")]
    assert len(req) == 1 and len(pins) == 1 and req[0] == pins[0], (req, pins)
    try:
        import orjson
    except ImportError:
        assert records._orjson is None
        return
    assert records._orjson is orjson and pins[0] == f"orjson=={orjson.__version__}"
    assert "orjson" not in sys.modules.get("app.pack.records").canonical_line.__code__.co_names

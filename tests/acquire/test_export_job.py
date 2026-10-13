"""An origin's asynchronous export as an acquisition file (app/acquire/
export_job.py; engine._export_fetch; 09 §9.17 amended 2026-10-13;
docs/notes/normattiva-full-corpus.md §4): a job started, confirmed, polled
and downloaded; the copy adopted when the update feed names no act of its
partition, re-exported when it does, left standing when the feed cannot be
read; a job that never ends is this pass's transient failure; a stop ends
the wait. And the spec: the exports under `archive`, never under `full`."""
import json
import time
import zipfile

import pytest

from app import db
from app.acquire import engine, export_job, specs
from app.acquire.spec import DeltaSpec, Endpoint, RemoteFile, Transport, TransportError
from tests.acquire.test_engine import _collection, _spec

O_MEMBERS = {"LEGGE_19900709_185/1990-07-14_090G0222_ORIGINALE_V0.xml": b"<akomaNtoso>originale</akomaNtoso>",
             "LEGGE_19900104_1/1990-01-08_090G0001_ORIGINALE_V0.xml": b"<akomaNtoso>n. 1</akomaNtoso>"}
V_MEMBERS = {"LEGGE_19900709_185/1990-07-14_090G0222_VIGENZA_2013-12-10_V0.xml": b"<akomaNtoso>vigente</akomaNtoso>"}


def _zip(path, members: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)


def _members(path) -> dict:
    with zipfile.ZipFile(path) as z:
        return {i.filename: z.read(i) for i in z.infolist() if not i.is_dir()}


def _is_o(body: dict) -> bool:
    return "dataVigenza" not in body


def _is_v(body: dict) -> bool:
    return "dataVigenza" in body


def _export_spec(mock_server):
    """Normattiva's shape: the download endpoint's URL, the exports beside
    the zips, the zip-directory delta (the hook runs before it)."""
    files = (RemoteFile(rel_path="akn/E/O/1990.zip", remote=export_job.encode({"year": 1990, "form": "O"})),
             RemoteFile(rel_path="akn/E/V/1990.zip", remote=export_job.encode({"year": 1990, "form": "V"})))
    return _spec(mock_server, delta=DeltaSpec(mode="zip-directory"), layout="container-files",
                 filters=("*.zip",),
                 endpoints=(Endpoint(transport=Transport.HTTPS,
                                     url=mock_server.base_url + "/collections/download",
                                     static_files=files),))


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(export_job, "POLL_S", 0.01)
    export_job.forget_feed()
    yield
    export_job.forget_feed()


def _warned(con):
    return [r[0] for r in con.execute("SELECT message FROM events WHERE stage='acquire' AND level='warn'")]


def test_a_job_is_started_confirmed_polled_and_downloaded(tmp_path, mock_server, quick):
    collection = _collection(tmp_path)
    con = db.connect(collection)
    _zip(mock_server.root / "o1990.zip", O_MEMBERS)
    _zip(mock_server.root / "v1990.zip", V_MEMBERS)
    t_o = mock_server.export_job("o1990.zip", polls=3, when=_is_o)
    t_v = mock_server.export_job("v1990.zip", polls=1, when=_is_v)
    spec = _export_spec(mock_server)

    got = engine.run_acquisition(con, collection, spec, "demo")
    assert got["fetched"] == 2 and got["skipped"] == 0 and got["failed"] == 0
    assert _members(collection.corpus_dir / "akn/E/O/1990.zip") == O_MEMBERS
    assert _members(collection.corpus_dir / "akn/E/V/1990.zip") == V_MEMBERS
    starts = mock_server.requests_to("nuova-ricerca")
    assert len(starts) == 2
    by_form = {("V" if "dataVigenza" in b else "O"): b for b in starts}
    assert by_form["O"]["formato"] == "AKN" and by_form["O"]["tipoRicerca"] == "A"
    assert by_form["O"]["parametriRicerca"]["dataInizioEmanazione"] == "1990-01-01"
    assert by_form["O"]["parametriRicerca"]["dataFineEmanazione"] == "1990-12-31"
    assert by_form["V"]["dataVigenza"] == time.strftime("%Y-%m-%d", time.gmtime())
    assert {b["token"] for b in mock_server.requests_to("conferma-ricerca")} == {t_o, t_v}
    assert mock_server.jobs[t_o]["polls_seen"] >= 3, "polled until the origin said done"
    rows = {r["rel_path"]: r for r in con.execute("SELECT rel_path, fetch_done, bytes_fetched, blake2 FROM fetch_files")}
    assert rows["akn/E/O/1990.zip"]["fetch_done"] == 1 and rows["akn/E/O/1990.zip"]["blake2"]
    assert rows["akn/E/O/1990.zip"]["bytes_fetched"] == (collection.corpus_dir / "akn/E/O/1990.zip").stat().st_size


def test_the_copy_is_adopted_while_the_feed_names_no_act_of_its_partition(tmp_path, mock_server, quick):
    collection = _collection(tmp_path)
    con = db.connect(collection)
    _zip(mock_server.root / "o1990.zip", O_MEMBERS)
    _zip(mock_server.root / "v1990.zip", V_MEMBERS)
    mock_server.export_job("o1990.zip", polls=1, when=_is_o)
    mock_server.export_job("v1990.zip", polls=1, when=_is_v)
    spec = _export_spec(mock_server)
    engine.run_acquisition(con, collection, spec, "demo")
    started = len(mock_server.requests_to("nuova-ricerca"))

    mock_server.set_updated([{"denominazioneAtto": "LEGGE", "annoProvvedimento": "1991"}])
    second = engine.run_acquisition(con, collection, spec, "demo")
    assert second["fetched"] == 2 and second["adopted"] == 2
    assert len(mock_server.requests_to("nuova-ricerca")) == started, "another year's act: no job"
    feed = mock_server.requests_to("aggiornati")
    assert feed and "dataInizioAggiornamento" in feed[-1] and "dataFineAggiornamento" in feed[-1]

    # an act of 1990 moved: both forms of the year are exported again
    export_job.forget_feed()
    mock_server.set_updated([{"denominazioneAtto": "LEGGE", "annoProvvedimento": "1990"}])
    _zip(mock_server.root / "o1990.zip", {**O_MEMBERS, "LEGGE_19900101_2/1990-01-05_090G0002_ORIGINALE_V0.xml": b"<akomaNtoso>new</akomaNtoso>"})
    mock_server.export_job("o1990.zip", polls=1, when=_is_o)
    mock_server.export_job("v1990.zip", polls=1, when=_is_v)
    third = engine.run_acquisition(con, collection, spec, "demo")
    assert third["fetched"] == 2 and third["adopted"] == 0
    assert len(mock_server.requests_to("nuova-ricerca")) == started + 2
    assert len(_members(collection.corpus_dir / "akn/E/O/1990.zip")) == 3


def test_an_unreadable_feed_leaves_the_copy_standing(tmp_path, mock_server, quick, monkeypatch):
    collection = _collection(tmp_path)
    con = db.connect(collection)
    _zip(mock_server.root / "o1990.zip", O_MEMBERS)
    _zip(mock_server.root / "v1990.zip", V_MEMBERS)
    mock_server.export_job("o1990.zip", polls=1, when=_is_o)
    mock_server.export_job("v1990.zip", polls=1, when=_is_v)
    spec = _export_spec(mock_server)
    engine.run_acquisition(con, collection, spec, "demo")
    started = len(mock_server.requests_to("nuova-ricerca"))

    def down(*a, **k):
        raise TransportError("aggiornati 503")
    monkeypatch.setattr(export_job, "updated_since", down)
    got = engine.run_acquisition(con, collection, spec, "demo")
    assert got["fetched"] == 0 and got["skipped"] == 2 and got["failed"] == 0, "deferred, not failed"
    assert len(mock_server.requests_to("nuova-ricerca")) == started
    assert _members(collection.corpus_dir / "akn/E/O/1990.zip") == O_MEMBERS, "the copy stands"
    assert any("update feed unread" in m for m in _warned(con))
    rows = {r["rel_path"]: r["error"] for r in con.execute("SELECT rel_path, error FROM fetch_files")}
    assert rows["akn/E/O/1990.zip"] is None, "the row is untouched"


def test_a_job_that_never_ends_is_this_pass_s_transient_failure(tmp_path, mock_server, quick, monkeypatch):
    collection = _collection(tmp_path)
    con = db.connect(collection)
    _zip(mock_server.root / "o1990.zip", O_MEMBERS)
    mock_server.export_job("o1990.zip", polls=10_000)
    monkeypatch.setattr(export_job, "JOB_BOUND_S", 0.05)
    spec = _spec(mock_server, delta=DeltaSpec(mode="zip-directory"), layout="container-files",
                 filters=("*.zip",),
                 endpoints=(Endpoint(transport=Transport.HTTPS, url=mock_server.base_url + "/collections/download",
                                     static_files=(RemoteFile(rel_path="akn/E/O/1990.zip",
                                                              remote=export_job.encode({"year": 1990, "form": "O"})),)),))
    got = engine.run_acquisition(con, collection, spec, "demo")
    assert got["failed"] == 1 and got["fetched"] == 0
    row = con.execute("SELECT fetch_done, error, retries FROM fetch_files WHERE rel_path='akn/E/O/1990.zip'").fetchone()
    assert row["fetch_done"] == 0 and row["retries"] == 1
    assert row["error"] is None, "a quiet deferral (engine.transient): retried next pass, never a dead row"
    assert engine.transient("export: job x not done in 1 s (connection timed out)")
    assert not (collection.corpus_dir / "akn/E/O/1990.zip").exists()


def test_a_stop_ends_the_wait_between_polls(tmp_path, monkeypatch):
    import threading
    from app.acquire.spec import TransferAborted

    class _Handle:
        stop = threading.Event()
    _Handle.stop.set()
    monkeypatch.setattr(export_job, "current_handle", lambda: _Handle)
    with pytest.raises(TransferAborted):
        export_job._stop_check()


def test_the_pieces_of_a_job(tmp_path):
    f = RemoteFile(rel_path="akn/E/V/2024.zip", remote=export_job.encode({"year": 2024, "form": "V"}))
    assert export_job.is_export(f) and not export_job.is_export(RemoteFile(rel_path="m.zip"))
    assert export_job.partition_of(f) == {"form": "V", "year": 2024}
    assert export_job.api_root("https://api.normattiva.it/t/normattiva.api/bff-opendata/v1/api/v1/collections/download") == \
        "https://api.normattiva.it/t/normattiva.api/bff-opendata/v1/api/v1"
    assert export_job.api_root("http://127.0.0.1:9/") == "http://127.0.0.1:9"
    o = export_job.body_for({"year": 1990, "form": "O", "type": "LEGGE"}, vigenza_day=None)
    assert o["parametriRicerca"]["denominazioneAtto"] == "LEGGE" and "dataVigenza" not in o
    v = export_job.body_for({"year": 1990, "form": "V"}, vigenza_day="2026-10-13")
    assert v["dataVigenza"] == "2026-10-13" and "denominazioneAtto" not in v["parametriRicerca"]
    with pytest.raises(ValueError):
        export_job.body_for({"year": 1990, "form": "V"}, vigenza_day=None)
    updated = {("LEGGE", 1990), ("DECRETO", 1991)}
    assert export_job.partition_touched({"year": 1990, "form": "O"}, updated)
    assert export_job.partition_touched({"year": 1990, "form": "O", "type": "LEGGE"}, updated)
    assert not export_job.partition_touched({"year": 1990, "form": "O", "type": "DECRETO"}, updated)
    assert not export_job.partition_touched({"year": 1992, "form": "V"}, updated)

    class _R:
        def __init__(self, text):
            self.text = text
        def json(self):
            return json.loads(self.text)
    assert export_job._token_of(_R("5d68d55a-1f34-4c0a-bef8-cd1bc7fa3b5b")) == "5d68d55a-1f34-4c0a-bef8-cd1bc7fa3b5b"
    assert export_job._token_of(_R('{"token": "5d68d55a-1f34-4c0a-bef8-cd1bc7fa3b5b"}')) == "5d68d55a-1f34-4c0a-bef8-cd1bc7fa3b5b"
    with pytest.raises(TransportError):
        export_job._token_of(_R('{"message": "Numero di atti superiore al limite consentito di 7000"}'))


def test_the_spec_keeps_full_to_the_23_collections_and_archive_adds_the_exports():
    from app.acquire.spec import resolve_profile
    files = list(specs.NORMATTIVA_SPEC.endpoints[0].static_files)
    exports = [f for f in files if export_job.is_export(f)]
    assert exports and exports[0].rel_path == "akn/E/O/1861.zip"
    this_year = time.gmtime().tm_year
    assert any(f.rel_path == f"akn/E/V/{this_year}.zip" for f in exports)
    assert len(exports) == 2 * (this_year - specs.EXPORT_FIRST_YEAR + 1)
    full = engine._apply_filters(files, specs.NORMATTIVA_SPEC.filters, resolve_profile(specs.NORMATTIVA_SPEC, "full"))
    assert len(full) == 23 and not any(export_job.is_export(f) for f in full), "the live collection's scope is unchanged"
    archive = engine._apply_filters(files, specs.NORMATTIVA_SPEC.filters, resolve_profile(specs.NORMATTIVA_SPEC, "archive"))
    assert len(archive) == 23 + len(exports)

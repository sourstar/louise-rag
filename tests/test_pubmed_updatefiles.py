"""PubMed's update files (docs/notes/pubmed-updatefiles.md): the
`updatefiles-dir` delta mode fetches NLM's daily files beside the baseline,
and the plugin applies them in NLM's order — new citations, revisions that
re-open the held record, DeleteCitation as the deletion signal.

The origin is a local directory tree served by a transport double with the
FTP transport's shape (a listing per directory, a fetch that resumes from a
byte offset, every chunk drawn from the budget); the real FtpTransport's
own changes are covered with ftplib mocked, as tests/acquire/test_transport_ftp.py
does. No real origin is contacted.
"""
from __future__ import annotations

import dataclasses
import gzip
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app import collections as collections_mod, db, discovery, licensing, plugin_io
from app.acquire import budget, engine, updatefiles
from app.acquire.spec import (Endpoint, LicenseAckRequiredError, RemoteFile, TransferAborted,
                              Transport)
from app.acquire.specs import PUBMED_SPEC
from app.plugins import cursor
from app.plugins.base import DiscoverState
from app.plugins.pubmed import SOURCE
from app.plugins.pubmed.source import (DELETE_MARKER, DELETE_REASON, UPDATES_SUBDIR,
                                       ordered_containers, split_locator)
from app.transfer.manager import Direction, get_manager, set_manager


# ---------------------------------------------------------------- the origin

def _article(pmid: int, title: str, abstract: str = "An abstract.") -> str:
    return (f"  <PubmedArticle>\n    <MedlineCitation Status=\"MEDLINE\" Owner=\"NLM\">\n"
            f"      <PMID Version=\"1\">{pmid}</PMID>\n"
            f"      <Article PubModel=\"Print\">\n        <Journal><ISOAbbreviation>J Test</ISOAbbreviation>"
            f"<JournalIssue><PubDate><Year>2026</Year></PubDate></JournalIssue></Journal>\n"
            f"        <ArticleTitle>{title}</ArticleTitle>\n"
            f"        <Abstract><AbstractText>{abstract}</AbstractText></Abstract>\n"
            f"      </Article>\n    </MedlineCitation>\n  </PubmedArticle>\n")


def _delete(*pmids: int) -> str:
    return ("  <DeleteCitation>\n" + "".join(f"    <PMID Version=\"1\">{p}</PMID>\n" for p in pmids)
            + "  </DeleteCitation>\n")


_stamp = [1_780_000_000]


def _container(path: Path, *children: str, md5: str | None = None) -> Path:
    """An NLM container, gzipped, with its `MD5(name)= hex` sidecar, at a
    later mtime than any written before it (as the origin publishes)."""
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<PubmedArticleSet>\n'
           + "".join(children) + "</PubmedArticleSet>\n").encode()
    data = gzip.compress(xml, mtime=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    sidecar = path.with_name(path.name + ".md5")
    sidecar.write_text(f"MD5({path.name})= {md5 or hashlib.md5(data).hexdigest()}\n")
    _stamp[0] += 100
    for p in (path, sidecar):
        os.utime(p, (_stamp[0], _stamp[0]))
    return path


def _origin(root: Path) -> Path:
    """NLM's layout: a baseline of two files, two update files numbered on.

      baseline   n0001: 31000001 (to be revised), 31000002 (to be deleted)
                 n0002: 31000003
      updates    n0003: 31000001 revised, 31000004 new
                 n0004: DeleteCitation 31000002, 31000005 new
    """
    base, upd = root / "baseline", root / "updatefiles"
    _container(base / "pubmed26n0001.xml.gz", _article(31000001, "Widgets, first version."),
               _article(31000002, "A duplicate citation."))
    _container(base / "pubmed26n0002.xml.gz", _article(31000003, "A third record."))
    _container(upd / "pubmed26n0003.xml.gz", _article(31000001, "Widgets, revised.", "Corrected."),
               _article(31000004, "A new citation."))
    _container(upd / "pubmed26n0004.xml.gz", _delete(31000002),
               _article(31000005, "Another new citation."))
    return root


class LocalOrigin:
    """An FTP-shaped transport over a local tree: `endpoint.url` is a
    directory; it lists files with their size and mtime, fetches by
    `remote` (else `rel_path`), resumes from a byte offset, and draws every
    chunk from the shared budget after writing it, as the real transports
    do (app/acquire/budget.py)."""

    CHUNK = 64

    def __init__(self):
        self.listed: list[str] = []
        self.requests: list[tuple[str, str, int]] = []
        self.abort_at: dict[str, int] = {}      # name -> stop once this many bytes are written
        self.corrupt_once: set[str] = set()     # names served with a flipped byte, once

    def list_files(self, endpoint: Endpoint, spec):
        d = Path(endpoint.url)
        self.listed.append(str(d))
        for p in sorted(d.iterdir()):
            if p.is_file():
                st = p.stat()
                yield RemoteFile(rel_path=p.name, size=st.st_size, mtime=int(st.st_mtime))

    def fetch_one(self, endpoint: Endpoint, remote: RemoteFile, dest: Path, resume_from: int) -> int:
        name = remote.remote or remote.rel_path
        data = (Path(endpoint.url) / name).read_bytes()
        if name in self.corrupt_once:
            self.corrupt_once.discard(name)
            data = data[:-1] + bytes([data[-1] ^ 0xFF])
        self.requests.append((endpoint.url, name, resume_from))
        part = dest.with_name(dest.name + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        start = resume_from if resume_from and part.is_file() else 0
        with open(part, "ab" if start else "wb") as f:
            pos = start
            while pos < len(data):
                chunk = data[pos:pos + self.CHUNK]
                f.write(chunk)
                f.flush()
                budget.acquire_bytes(len(chunk))
                pos += len(chunk)
                if name in self.abort_at and pos >= self.abort_at[name]:
                    del self.abort_at[name]
                    raise TransferAborted(f"stopped in {name} at {pos} (test)")
        os.replace(part, dest)
        return dest.stat().st_size


@pytest.fixture
def origin(tmp_path, monkeypatch):
    transport = LocalOrigin()
    monkeypatch.setitem(engine.REGISTRY, Transport.FTP, transport)
    set_manager(None)
    yield _origin(tmp_path / "ncbi" / "pubmed"), transport
    set_manager(None)


def _spec(root: Path):
    """PUBMED_SPEC, its one endpoint pointed at the local origin's baseline —
    the identifying half (filters, checksums, delta) exactly the plugin's."""
    return dataclasses.replace(
        PUBMED_SPEC, endpoints=(Endpoint(transport=Transport.FTP, url=str(root / "baseline")),),
        estimated_bytes={"full": 1})


def _collection(tmp_path, *, ack: bool = True) -> collections_mod.Collection:
    d = tmp_path / "pubmed"
    (d / "corpus").mkdir(parents=True, exist_ok=True)
    c = collections_mod.Collection(
        name="pubmed", plugin="pubmed", profile="full", embed_mode_default=None,
        corpus_dir=d / "corpus", manifest_path=d / "state" / "manifest.db",
        fts_dir=d / "fts", lance_dir=d / "lancedb", acquire_enabled=True, license_ack=None)
    return dataclasses.replace(c, license_ack=licensing.make_ack(c)) if ack else c


def _sync(c):
    con = db.connect(c)
    try:
        return discovery.sync_collection(con, c), con
    except Exception:
        con.close()
        raise


# ---------------------------------------------------------------- where the directory is

def test_the_update_directory_is_delta_subdir_resolved_against_url_by_rfc3986():
    """09 §9.17, amended: the live Codex's `…/pubmed/baseline` and 09's old
    worked example's `…/pubmed/` both name `…/pubmed/updatefiles`."""
    assert updatefiles.directory_url("ftp.ncbi.nlm.nih.gov/pubmed/baseline", "updatefiles") \
        == "ftp.ncbi.nlm.nih.gov/pubmed/updatefiles"
    assert updatefiles.directory_url("ftp://ftp.ncbi.nlm.nih.gov/pubmed/", "updatefiles") \
        == "ftp://ftp.ncbi.nlm.nih.gov/pubmed/updatefiles"
    assert PUBMED_SPEC.delta.updatefiles_subdir == UPDATES_SUBDIR, \
        "the plugin reads the update files where acquisition lands them"


def test_a_codex_collection_takes_the_update_directory_its_codex_names():
    """`delta` is in the identifying half (09 §9.17): a Codex collection
    fetches by its Codex's, as by its filters."""
    codex = SimpleNamespace(origin=SimpleNamespace(spec={
        "filters": ["*.xml.gz", "*.xml.gz.md5"], "delta": {"mode": "updatefiles-dir", "subdir": "daily"}}))
    spec = engine.effective_spec(SimpleNamespace(codex=codex, peer_source=None, profile="full"),
                                 PUBMED_SPEC)
    assert spec.delta.updatefiles_subdir == "daily"
    torrent = dataclasses.replace(spec, transport=Transport.TORRENT)
    assert updatefiles.subdir_of(torrent) is None, "the peer plane carries them at their paths"


def test_a_codex_pins_only_the_update_directory_never_another_mode():
    """Only `updatefiles-dir`'s subdir is pinned: a Codex older than its
    plugin's mode (normattiva's, written before `zip-directory`) keeps the
    plugin's mode, as it did before the pin existed."""
    from app.acquire.specs import NORMATTIVA_SPEC
    codex = SimpleNamespace(origin=SimpleNamespace(spec={
        "filters": list(NORMATTIVA_SPEC.filters), "delta": {"mode": "full-resync", "subdir": None}}))
    spec = engine.effective_spec(SimpleNamespace(codex=codex, peer_source=None, profile="full"),
                                 NORMATTIVA_SPEC)
    assert spec.delta == NORMATTIVA_SPEC.delta
    plain = SimpleNamespace(origin=SimpleNamespace(spec={
        "filters": ["*.xml.gz", "*.xml.gz.md5"], "delta": {"mode": "full-resync", "subdir": None}}))
    spec = engine.effective_spec(SimpleNamespace(codex=plain, peer_source=None, profile="full"),
                                 PUBMED_SPEC)
    assert spec.delta == PUBMED_SPEC.delta


# ---------------------------------------------------------------- acquisition

def test_update_files_are_listed_whole_fetched_beside_the_baseline_and_verified(tmp_path, origin):
    root, transport = origin
    c = _collection(tmp_path)
    con = db.connect(c)
    result = engine.run_acquisition(con, c, _spec(root), "full")
    assert result["fetched"] == 8 and result["failed"] == 0 and result["total"] == 8
    corpus = c.corpus_dir
    assert sorted(p.relative_to(corpus).as_posix() for p in corpus.rglob("*.xml.gz")) == [
        "pubmed26n0001.xml.gz", "pubmed26n0002.xml.gz",
        "updatefiles/pubmed26n0003.xml.gz", "updatefiles/pubmed26n0004.xml.gz"]
    assert transport.listed == [str(root / "baseline"), str(root / "updatefiles")], \
        "the listing contract: both directories, once each"
    asked = {name: url for url, name, _ in transport.requests}
    assert asked["pubmed26n0003.xml.gz"] == str(root / "updatefiles")
    assert asked["pubmed26n0003.xml.gz.md5"] == str(root / "updatefiles")
    assert asked["pubmed26n0001.xml.gz"] == str(root / "baseline")
    import json
    listing = json.loads(db.get_control(con, engine.LISTING_KEY))
    assert listing["files_total"] == 8, "the denominator counts the update files"
    # a second pass: nothing pending, nothing moves
    transport.requests.clear()
    again = engine.run_acquisition(con, c, _spec(root), "full")
    assert again["fetched"] == 0 and again["skipped"] == 8 and transport.requests == []


def test_every_byte_of_an_update_file_draws_from_the_one_budget(tmp_path, origin):
    """Invariant 5: the update files move through the same transfer path as
    the baseline, and the bucket accounts every byte."""
    root, _ = origin
    c = _collection(tmp_path)
    set_manager(None)
    before = sum(get_manager().bucket(Direction.DOWN).granted_snapshot()[1])
    engine.run_acquisition(db.connect(c), c, _spec(root), "full")
    moved = sum(p.stat().st_size for p in (root / "updatefiles").iterdir())
    granted = sum(get_manager().bucket(Direction.DOWN).granted_snapshot()[1]) - before
    assert granted >= moved + sum(p.stat().st_size for p in (root / "baseline").iterdir())


def test_an_md5_mismatch_is_refused_and_the_pair_fetched_again(tmp_path, origin):
    root, transport = origin
    c = _collection(tmp_path)
    con = db.connect(c)
    transport.corrupt_once.add("pubmed26n0004.xml.gz")
    first = engine.run_acquisition(con, c, _spec(root), "full")
    assert first["failed"] == 1
    landed = c.corpus_dir / UPDATES_SUBDIR / "pubmed26n0004.xml.gz"
    assert not landed.exists() and not Path(str(landed) + ".md5").exists(), \
        "refused, and its sidecar too: a stale sidecar would mismatch forever"
    second = engine.run_acquisition(con, c, _spec(root), "full")
    assert second["failed"] == 0 and second["fetched"] == 2
    assert landed.read_bytes() == (root / "updatefiles" / "pubmed26n0004.xml.gz").read_bytes()


def test_a_staged_file_is_checked_against_the_sidecar_at_its_destination(tmp_path, origin,
                                                                          monkeypatch):
    """The staging area (config.ACQUIRE_STAGING_DIR) held the data file while
    its sidecar had already been promoted: the check looked beside the
    staged copy, found nothing, and passed a corrupt file."""
    from app import config
    root, transport = origin
    monkeypatch.setattr(config, "ACQUIRE_STAGING_DIR", tmp_path / "staging")
    c = _collection(tmp_path)
    transport.corrupt_once.add("pubmed26n0003.xml.gz")
    result = engine.run_acquisition(db.connect(c), c, _spec(root), "full")
    assert result["failed"] == 1
    assert not (c.corpus_dir / UPDATES_SUBDIR / "pubmed26n0003.xml.gz").exists()


def test_a_stop_mid_file_resumes_from_the_byte_it_reached(tmp_path, origin):
    root, transport = origin
    c = _collection(tmp_path)
    con = db.connect(c)
    size = (root / "updatefiles" / "pubmed26n0003.xml.gz").stat().st_size
    transport.abort_at["pubmed26n0003.xml.gz"] = size // 2
    first = engine.run_acquisition(con, c, _spec(root), "full")
    assert first["aborted"] == 1 and first["failed"] == 0
    part = c.corpus_dir / UPDATES_SUBDIR / "pubmed26n0003.xml.gz.part"
    reached = part.stat().st_size
    assert 0 < reached < size
    row = con.execute("SELECT fetch_done, retries, error FROM fetch_files WHERE rel_path=?",
                      ("updatefiles/pubmed26n0003.xml.gz",)).fetchone()
    assert (row["fetch_done"], row["retries"], row["error"]) == (0, 0, None), \
        "a stop is not a failure: no retry burned, no error written"
    transport.requests.clear()
    second = engine.run_acquisition(con, c, _spec(root), "full")
    assert second["fetched"] == 1
    assert ("pubmed26n0003.xml.gz", reached) in {(n, r) for _, n, r in transport.requests}
    assert (c.corpus_dir / UPDATES_SUBDIR / "pubmed26n0003.xml.gz").read_bytes() == \
        (root / "updatefiles" / "pubmed26n0003.xml.gz").read_bytes()


def test_no_byte_moves_and_nothing_is_listed_before_the_acknowledgement(tmp_path, origin):
    """Invariant 9: before the first byte moves — the listing included."""
    root, transport = origin
    c = _collection(tmp_path, ack=False)
    with pytest.raises(LicenseAckRequiredError):
        engine.run_acquisition(db.connect(c), c, _spec(root), "full")
    assert transport.listed == [] and transport.requests == []
    assert not any(c.corpus_dir.iterdir())


def test_a_snapshot_cutoff_takes_the_update_files_published_before_it(tmp_path, origin):
    """06 §6.2's cutoff rule for an origin of update files: by the listing's
    own date (`records fetch --snapshot`)."""
    root, _ = origin
    c = _collection(tmp_path)
    cut = (root / "updatefiles" / "pubmed26n0004.xml.gz").stat().st_mtime
    engine.run_acquisition(db.connect(c), c, _spec(root), "full", until=cut)
    assert (c.corpus_dir / UPDATES_SUBDIR / "pubmed26n0003.xml.gz").is_file()
    assert not (c.corpus_dir / UPDATES_SUBDIR / "pubmed26n0004.xml.gz").exists()


def test_ftp_requests_a_file_by_its_name_in_the_directory_it_was_listed_in(tmp_path):
    from app.acquire.transport_ftp import FtpTransport
    fake = MagicMock()
    seen = []
    fake.retrbinary.side_effect = lambda cmd, cb, rest=None: (seen.append(cmd), cb(b"x"))
    endpoint = updatefiles.endpoint_for(Endpoint(transport=Transport.FTP,
                                                 url="ftp.example.org/pubmed/baseline"), "updatefiles")
    with patch("app.acquire.transport_ftp._connect", return_value=fake) as connect:
        FtpTransport().fetch_one(endpoint, RemoteFile(rel_path="updatefiles/a.xml.gz", remote="a.xml.gz"),
                                 tmp_path / "updatefiles" / "a.xml.gz", 0)
    assert connect.call_args[0][0].url == "ftp.example.org/pubmed/updatefiles"
    assert seen == ["RETR a.xml.gz"]


# ---------------------------------------------------------------- application

def _fetched(tmp_path, root, *, only=None):
    """The corpus as acquisition lands it — baseline at the root, update
    files under updatefiles/ — optionally only some of the update files."""
    c = _collection(tmp_path)
    for p in (root / "baseline").iterdir():
        _copy(p, c.corpus_dir / p.name)
    for p in (root / "updatefiles").iterdir():
        if only is None or any(p.name.startswith(n) for n in only):
            _copy(p, c.corpus_dir / UPDATES_SUBDIR / p.name)
    return c


def _copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())
    st = src.stat()
    os.utime(dest, (st.st_atime, st.st_mtime))


def _rows(con):
    return {r["uid"]: dict(r) for r in con.execute(
        "SELECT uid, path, error, txt_hash, rdf_done, fts_done, embed_done FROM documents")}


def _mark_done(con, *uids):
    con.executemany("UPDATE documents SET rdf_done=1, fts_done=1, embed_done=1 WHERE uid=?",
                    [(u,) for u in uids])


def test_the_files_are_enumerated_in_nlm_order(tmp_path, origin):
    root, _ = origin
    c = _fetched(tmp_path, root)
    assert [p.relative_to(c.corpus_dir).as_posix() for p, _ in ordered_containers(c.corpus_dir)] == [
        "pubmed26n0001.xml.gz", "pubmed26n0002.xml.gz",
        "updatefiles/pubmed26n0003.xml.gz", "updatefiles/pubmed26n0004.xml.gz"]


def test_a_new_pmid_appears_a_revision_reopens_and_a_delete_is_tombstoned(tmp_path, origin,
                                                                          monkeypatch):
    root, _ = origin
    c = _fetched(tmp_path, root, only=())          # the baseline alone, as the node holds it
    stats, con = _sync(c)
    assert stats["added"] == 3
    _mark_done(con, "pubmed:31000001", "pubmed:31000002", "pubmed:31000003")
    withdrawn: list = []
    from app import unindexable
    monkeypatch.setattr(unindexable, "withdraw_many",
                        lambda con_, coll, uids: withdrawn.extend(uids) or len(uids))
    for p in (root / "updatefiles").iterdir():
        _copy(p, c.corpus_dir / UPDATES_SUBDIR / p.name)
    stats = discovery.sync_collection(con, c)
    assert (stats["added"], stats["changed"], stats["withdrawn"]) == (2, 1, 1)
    rows = _rows(con)
    # new citations from both update files
    assert rows["pubmed:31000004"]["error"] is None and rows["pubmed:31000005"]["error"] is None
    # the revision replaced the held record and RE-OPENED it for every stage
    revised = rows["pubmed:31000001"]
    assert split_locator(revised["path"])[0].name == "pubmed26n0003.xml.gz"
    assert (revised["rdf_done"], revised["fts_done"], revised["embed_done"]) == (0, 0, 0)
    doc = SOURCE.parse(SOURCE.fetch(SimpleNamespace(
        source_id="pubmed", native_id="31000001", locator=revised["path"], extra={})))
    assert doc.title == "Widgets, revised." and revised["txt_hash"] == doc.raw_hash
    assert plugin_io.origin_locator(SOURCE, revised["path"], c.corpus_dir) == \
        "updatefiles/pubmed26n0003.xml.gz#0", "09 §9.5's own form of an update file's origin"
    # the DeleteCitation: out of every index, tombstoned, its marks reset
    gone = rows["pubmed:31000002"]
    assert withdrawn == ["pubmed:31000002"]
    assert gone["error"] == DELETE_MARKER and (gone["rdf_done"], gone["fts_done"], gone["embed_done"]) == (0, 0, 0)
    assert split_locator(gone["path"])[0].name == "pubmed26n0001.xml.gz", \
        "the row keeps the unit that last supplied the record"
    # untouched
    assert rows["pubmed:31000003"]["embed_done"] == 1


def test_the_deletion_signal_carries_the_origins_reason(tmp_path, origin):
    root, _ = origin
    c = _fetched(tmp_path, root)
    deletes = [r for r in SOURCE.discover(DiscoverState(corpus_root=c.corpus_dir))
               if r.extra.get("kind") == "delete"]
    assert [(r.native_id, r.deleted, r.error, r.extra["reason"]) for r in deletes] == [
        ("31000002", True, DELETE_MARKER, DELETE_REASON)]
    assert DELETE_REASON == "retracted"
    assert SOURCE.tombstone_reason(DELETE_MARKER) == "retracted"
    assert SOURCE.tombstone_reason("unparseable") is None


def test_a_file_applied_twice_changes_nothing_even_with_the_cursor_cleared(tmp_path, origin,
                                                                           monkeypatch):
    """Invariant 2. And the hard case: a cleared cursor (a relocation, the
    Library's rescan, a new manifest) re-enumerates every container in
    order — the baseline's 31000001 must not be applied over its revision,
    or the revised record is re-opened (and re-embedded) for nothing."""
    root, _ = origin
    c = _fetched(tmp_path, root)
    _, con = _sync(c)
    every = list(_rows(con))
    _mark_done(con, *[u for u in every if _rows(con)[u]["error"] is None])
    before = _rows(con)
    withdrawn: list = []
    from app import unindexable
    monkeypatch.setattr(unindexable, "withdraw_many", lambda con_, coll, uids: withdrawn.extend(uids))
    again = discovery.sync_collection(con, c)
    assert (again["added"], again["changed"], again["seen"]) == (0, 0, 0), "the cursor skips"
    cursor.clear(c.manifest_path.parent, "pubmed")
    rescan = discovery.sync_collection(con, c)
    assert (rescan["added"], rescan["changed"]) == (0, 0)
    assert rescan["superseded"] == 1, "the baseline's 31000001, older than its revision"
    assert _rows(con) == before and withdrawn == []


def test_update_n_plus_1_is_never_applied_before_n(tmp_path, origin):
    """n0004 lands first (parallel downloads land in any order): it waits,
    reported, until n0003 is held — and then both apply, in order."""
    root, _ = origin
    c = _fetched(tmp_path, root, only=("pubmed26n0004",))
    stats, con = _sync(c)
    rows = _rows(con)
    assert "pubmed:31000005" not in rows, "n0004's new citation waits"
    assert rows["pubmed:31000002"]["error"] is None, "and so does its DeleteCitation"
    assert stats["container_errors"] == 1
    said = [r["message"] for r in con.execute("SELECT message FROM events WHERE message LIKE '%waits%'")]
    assert any("pubmed26n0004.xml.gz waits" in m and "pubmed26n0003" in m for m in said)
    _copy(root / "updatefiles" / "pubmed26n0003.xml.gz",
          c.corpus_dir / UPDATES_SUBDIR / "pubmed26n0003.xml.gz")
    _copy(root / "updatefiles" / "pubmed26n0003.xml.gz.md5",
          c.corpus_dir / UPDATES_SUBDIR / "pubmed26n0003.xml.gz.md5")
    discovery.sync_collection(con, c)
    rows = _rows(con)
    assert rows["pubmed:31000005"]["error"] is None
    assert rows["pubmed:31000002"]["error"] == DELETE_MARKER
    assert split_locator(rows["pubmed:31000001"]["path"])[0].name == "pubmed26n0003.xml.gz"


def test_an_update_file_whose_md5_does_not_check_out_is_not_applied(tmp_path, origin):
    """And neither is any later one: the order is the origin's, not ours."""
    root, _ = origin
    c = _fetched(tmp_path, root)
    bad = c.corpus_dir / UPDATES_SUBDIR / "pubmed26n0003.xml.gz.md5"
    bad.write_text("MD5(pubmed26n0003.xml.gz)= " + "0" * 32 + "\n")
    _, con = _sync(c)
    rows = _rows(con)
    assert "pubmed:31000004" not in rows and "pubmed:31000005" not in rows
    assert split_locator(rows["pubmed:31000001"]["path"])[0].name == "pubmed26n0001.xml.gz"
    _copy(root / "updatefiles" / "pubmed26n0003.xml.gz.md5", bad)
    discovery.sync_collection(con, c)
    assert "pubmed:31000005" in _rows(con)


def test_a_stop_mid_file_resumes_and_ends_where_an_uninterrupted_scan_ends(tmp_path, origin,
                                                                           monkeypatch):
    """Invariant 2: the scan's unit is a batch of refs; the cursor never runs
    past a container whose rows are not committed, so a stop inside an
    update file re-enumerates it."""
    root, _ = origin
    whole = _fetched(tmp_path / "whole", root)
    _, con_whole = _sync(whole)
    expected = {u: (r["error"], r["txt_hash"], split_locator(r["path"])[0].name)
                for u, r in _rows(con_whole).items()}

    c = _fetched(tmp_path / "stopped", root)
    monkeypatch.setattr(discovery, "DISCOVER_BATCH_REFS", 1)

    class StopAfter:
        def __init__(self, n):
            self.n = n

        def is_set(self):
            self.n -= 1
            return self.n < 0
    con = db.connect(c)
    first = discovery.sync_collection(con, c, stop=StopAfter(4))
    assert first.get("interrupted")
    assert len(_rows(con)) < len(expected)
    discovery.sync_collection(con, c)
    got = {u: (r["error"], r["txt_hash"], split_locator(r["path"])[0].name)
           for u, r in _rows(con).items()}
    assert got == expected


def test_withdraw_many_takes_documents_out_of_every_index(tmp_path, monkeypatch):
    """The batched form of unindexable.withdraw: one commit per index."""
    from app import catalog, embeddings, fts, unindexable
    calls: dict = {}
    monkeypatch.setattr(fts, "delete_books", lambda g, ids, coll: calls.setdefault("fts", []).append((g, sorted(ids))))
    monkeypatch.setattr(fts, "delete_book", lambda lang, b, coll: calls.setdefault("mul", []).append(b))
    monkeypatch.setattr(embeddings, "delete_books", lambda books, coll, lock_con=None: calls.setdefault("vec", sorted(books)))
    monkeypatch.setattr(catalog, "delete_rows", lambda coll, rowids: calls.setdefault("cat", sorted(rowids)))
    c = _collection(tmp_path)
    con = db.connect(c)
    con.executemany("INSERT INTO documents(uid,source,native_id,lang,path,updated_at) "
                    "VALUES(?,?,?,?,?,0)",
                    [("pubmed:1", "pubmed", "1", "en", "a::0"), ("pubmed:2", "pubmed", "2", "en", "a::1"),
                     ("pubmed:3", "pubmed", "3", "mul", "a::2")])
    assert unindexable.withdraw_many(con, c, ["pubmed:2", "pubmed:1", "pubmed:3", "pubmed:9"]) == 3
    assert calls["fts"] == [(fts.group_for("en"), [1, 2])] and calls["mul"] == [3]
    assert calls["vec"] == [1, 2, 3] and len(calls["cat"]) == 3


def test_a_withdrawal_the_indexes_refuse_defers_the_batch(tmp_path, origin, monkeypatch):
    """Nothing written, the cursor short of the container: the next scan
    withdraws and tombstones it."""
    root, _ = origin
    c = _fetched(tmp_path, root, only=("pubmed26n0003",))
    _, con = _sync(c)
    _mark_done(con, "pubmed:31000002")
    for name in ("pubmed26n0004.xml.gz", "pubmed26n0004.xml.gz.md5"):
        _copy(root / "updatefiles" / name, c.corpus_dir / UPDATES_SUBDIR / name)
    from app import unindexable

    def busy(*_a):
        raise RuntimeError("index busy")
    monkeypatch.setattr(unindexable, "withdraw_many", busy)
    stats = discovery.sync_collection(con, c)
    assert stats.get("interrupted") and _rows(con)["pubmed:31000002"]["error"] is None
    monkeypatch.setattr(unindexable, "withdraw_many", lambda *_a: 1)
    discovery.sync_collection(con, c)
    assert _rows(con)["pubmed:31000002"]["error"] == DELETE_MARKER


# ---------------------------------------------------------------- the records build

def test_the_build_names_the_retracted_and_the_snapshot_tombstones_them_so(tmp_path, origin,
                                                                           isolated_config):
    """09 §9.4: a DeleteCitation is `retracted` ("the origin says the record
    should not have existed"), never the `withdrawn` of a record merely gone."""
    from app.hold import snapshot as sn
    from app.pack import build as pack_build
    root, _ = origin
    c = _fetched(tmp_path, root)
    _, con = _sync(c)
    con.close()
    pack_dir = tmp_path / "pack"
    pack_build.build(c, pack_dir, snapshot="2026-10-01", records_per_shard=10, sign=False,
                     log=lambda *_: None)
    reasons = sn.build_tombstone_reasons(pack_dir / pack_build.BUILD_DIR / pack_build.BUILD_DB)
    assert reasons == {"pubmed:31000002": "retracted"}
    import pyarrow as pa
    side = lambda *uids: pa.table({"uid": pa.array(list(uids), pa.string()),     # noqa: E731
                                   "h": pa.array([b"\0" * 16] * len(uids), pa.binary(16))})
    delta = sn._join_delta([side("pubmed:31000002", "pubmed:31000003", "pubmed:31000009")],
                           [side("pubmed:31000003")], name="pubmed", reasons=reasons)
    assert [(t.uid, t.reason) for t in delta.tombstoned] == [
        ("pubmed:31000002", "retracted"), ("pubmed:31000009", "withdrawn")]


def test_a_held_line_whose_unit_moved_is_parsed_again(tmp_path, origin, isolated_config):
    """RULE 1's shortcut (`--records-from…`) takes a held line unless the
    manifest proves its unit changed. The same bytes sent again in a later
    update file move the record's `origin`, which a build by the book
    writes: the line is parsed, not taken with the old one."""
    from app.pack import build as pack_build
    root, _ = origin
    c = _fetched(tmp_path, root)
    row = {"path": str(c.corpus_dir / "pubmed26n0002.xml.gz") + "::0"}
    line = {"origin": "pubmed26n0002.xml.gz#0"}
    assert not pack_build._origin_moved(c, SOURCE, row, line)
    moved = {"path": str(c.corpus_dir / UPDATES_SUBDIR / "pubmed26n0003.xml.gz") + "::0"}
    assert pack_build._origin_moved(c, SOURCE, moved, line)
    assert not pack_build._origin_moved(c, SOURCE, {"path": None}, line)

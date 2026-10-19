"""The origin-coverage guard (invariant 11; docs/notes/STATUS.md, 2026-10-11).

A record must be reproducible from the bytes its `origin_hash` names, so every
file a plugin's `parse()` reads must be one its `fetch()` read to compute that
hash — and touching a file parse opens beside its unit must change what
`discover()` reports, or the record is never rebuilt. Gutenberg's parser 8
broke both: its records took
creators and dates from `pg<id>.rdf` while the hash and the change signature
covered the book file alone, and records-vs-origin read a catalogue revision
as a parser fault.

`files_read(fn)` records the paths a call opens; each plugin's test module
asserts the rule over its own fixture corpus. A plugin that cannot follow it
says why in EXEMPT, and the registry test makes sure no plugin is silently
left out.
"""
from __future__ import annotations

import builtins
import io
import os
from contextlib import contextmanager
from pathlib import Path

#: plugin -> why its parse may read beyond what fetch hashed. Each reason is
#: an argument about records-vs-origin, never a convenience.
EXEMPT = {
    "deposit": "parse reads `.deposit.json` (the client's file name, the deposit time, a URL) "
               "beside the file fetch hashes. Louise writes it once, at the deposit, and a deposit "
               "collection publishes no record (02 §2.9): there is no origin to verify against.",
    "wikidata": "parse reads the dump's LABEL TABLE (app/plugins/wikidata/pack.py, labels.sqlite) beside "
                "the entity's own line that fetch hashes: an entity's text names other entities' labels, "
                "which are other lines of the SAME dump. 06 §6.4 (amended 2026-10-19): the table is a derived "
                "artefact of the pinned origin, `origin_hash` covers the entity's line — the unit a query "
                "returns — and a verifier rebuilds the table from the same dump (`origin.spec.files` pins "
                "it) before reproducing a record; `origin.form` says which part of the text comes from it.",
}

#: plugins whose module tests call `assert_covered` (tests/test_origin_coverage.py
#: checks this plus EXEMPT is the whole registry)
COVERED = {"gutenberg", "pubmed", "wikipedia", "normattiva", "embedded"}


@contextmanager
def _recording(seen: set[Path]):
    """Python's `open` and pyarrow's Parquet readers, which open files in C
    (the embedded and wikipedia-archive layers read Parquet)."""
    import pyarrow.parquet as pq
    real, real_pf_init, real_read_table = builtins.open, pq.ParquetFile.__init__, pq.read_table

    def note(file):
        if isinstance(file, (str, bytes, os.PathLike)):
            seen.add(Path(os.fsdecode(file)).resolve())

    def spy(file, *a, **k):
        note(file)
        return real(file, *a, **k)

    def pf_init(self, source, *a, **k):
        note(source)
        return real_pf_init(self, source, *a, **k)

    def read_table(source, *a, **k):
        note(source)
        return real_read_table(source, *a, **k)
    builtins.open, io.open = spy, spy
    pq.ParquetFile.__init__, pq.read_table = pf_init, read_table
    try:
        yield
    finally:
        builtins.open, io.open = real, real
        pq.ParquetFile.__init__, pq.read_table = real_pf_init, real_read_table


def files_read(fn, *args, **kwargs):
    """(result, the files `fn(*args, **kwargs)` opened)."""
    seen: set[Path] = set()
    with _recording(seen):
        out = fn(*args, **kwargs)
    return out, seen


def assert_covered(source, ref, corpus_root: Path, *, discover_state=None,
                   verify_only: tuple[str, ...] = ()) -> set[Path]:
    """For one document: everything `parse` reads under the corpus, `fetch`
    read too; and moving the mtime of any file parse opens changes the document's
    `discover()` signature (a fresh scan, no cursor). `verify_only`: suffixes
    of files fetch reads only to check the unit (pubmed's `.md5`), which no
    record byte comes from. Returns the files fetch read (a cache can make it
    none for a later document: a test asserts over all of them)."""
    from app.plugins.base import DiscoverState
    root = Path(corpus_root).resolve()
    raw, fetched = files_read(source.fetch, ref)
    _doc, parsed = files_read(source.parse, raw)
    parsed = {p for p in parsed if root in p.parents}
    fetched = {p for p in fetched if root in p.parents}
    assert parsed <= fetched, (
        f"{source.source_id}: parse() read {sorted(map(str, parsed - fetched))}, which fetch() did "
        f"not read — those bytes are in the record and not in its origin_hash")

    def signature():
        state = discover_state() if discover_state else DiscoverState(corpus_root=root)
        for r in source.discover(state):
            if r.native_id == ref.native_id and r.locator == ref.locator:
                return (r.mtime, r.size)
        raise AssertionError(f"{ref.native_id} not rediscovered")
    # THE SIGNATURE, over what parse reads from DISK: the unit fetch reads is
    # each plugin's own discovery tests' business — and its signature need not
    # be an mtime (normattiva's is the zip member's, pubmed reads a derived
    # `.pack.parquet`) — but a file parse opens beside it is exactly the
    # defect this guards: gutenberg's catalogue entry
    for p in sorted(parsed):
        assert not p.name.endswith(verify_only), f"{p.name} is verify-only and parse read it"
        before = signature()
        st = p.stat()
        os.utime(p, (st.st_atime, st.st_mtime + 1000))
        try:
            assert signature() != before, (
                f"{source.source_id}: {p.name} changed and discover() reports the same "
                f"signature — the record would never be rebuilt")
        finally:
            os.utime(p, (st.st_atime, st.st_mtime))
    return fetched

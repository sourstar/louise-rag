"""`louise hold records fetch <collection> --slice … [--snapshot ID]`
(07 §7.3, built 2026-10-25).

A slice is named as the SHARD's paths name it. The origin delivers whole the
slices its spec declares (`slice_profiles`: a language's dump, a language's
directory) and nothing finer; a slice under one is fetched as that one and
said; one the origin does not deliver apart is refused, before a byte moves,
naming what it does deliver. What a COMPLETE fetch took is recorded in the
collection's file — through the file's one writer — so the node's monthly
cycle continues it; a dry run, a refusal, a failure or a stop records
nothing. `--snapshot ID` runs the spec to that snapshot's cutoff by the
origin's cutoff rule (06 §6.2).
"""
from __future__ import annotations

import tomllib
from datetime import datetime, timezone

import pytest

from app import collections as collections_mod, db, louise
from app.acquire import engine
from app.acquire import spec as acquire_spec
from app.acquire.spec import (AcquisitionSpec, ChecksumSource, DeltaSpec, Endpoint, Filter,
                              RemoteFile, Transport)
from app.acquire.specs import ACQUIRE_REGISTRY, PUBMED_SPEC, WIKIPEDIA_SPEC
from app.hold import records_fetch, snapshot as sn
from app.plugins.base import LicenseClass


def _spec(mock_server, *, mode: str = "full-resync", mtimes: dict | None = None,
          **over) -> AcquisitionSpec:
    files = tuple(RemoteFile(rel_path=p, size=(mock_server.root / p).stat().st_size,
                             mtime=(mtimes or {}).get(p))
                  for p in ("en/a.txt", "it/b.txt"))
    base = dict(
        source_id="gutenberg", transport=Transport.HTTPS,
        endpoints=(Endpoint(transport=Transport.HTTPS, url=mock_server.base_url,
                            static_files=files),),
        layout="container-files", checksums=ChecksumSource(kind="none"),
        delta=DeltaSpec(mode=mode),
        profiles={"en": Filter(include=("en/*",)), "it": Filter(include=("it/*",)),
                  "first": Filter(max_files=1), "all": Filter()},
        profile_mode="additive",
        slice_profiles={"lang=en": "en", "lang=it": "it"},
        estimated_bytes={"en": 10, "it": 10, "first": 10, "all": 20},
        license_class=LicenseClass.GREEN)
    base.update(over)
    return AcquisitionSpec(**base)


@pytest.fixture
def origin(isolated_config, mock_server, monkeypatch):
    """The test node's gutenberg, its origin two languages on a mock server
    whose spec delivers each language whole."""
    for p in ("en/a.txt", "it/b.txt"):
        (mock_server.root / p).parent.mkdir(parents=True, exist_ok=True)
        (mock_server.root / p).write_bytes(b"x" * 10)
    monkeypatch.setattr(records_fetch.signal, "signal", lambda *a: None)

    def wire(**over):
        spec = _spec(mock_server, **over)
        monkeypatch.setitem(ACQUIRE_REGISTRY, "gutenberg", spec)
        return spec
    wire()
    return wire


def _file():
    return collections_mod.config_toml_path("gutenberg")


def _toml() -> dict:
    return tomllib.loads(_file().read_text(encoding="utf-8"))


def _corpus():
    c = collections_mod.by_name("gutenberg")
    return sorted(p.relative_to(c.corpus_dir).as_posix()
                  for p in c.corpus_dir.rglob("*.txt"))


def test_a_slice_is_fetched_and_recorded_so_the_cycle_continues_it(origin, capsys):
    before = collections_mod.by_name("gutenberg")
    assert before.acquire_enabled is False, \
        "the test file is in the shape before 07 §7.8: the node does not fetch it"
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--slice", "lang=it"]) == 0
    out = capsys.readouterr().out
    assert _corpus() == ["it/b.txt"]
    assert _toml()["slices"] == ["lang=it"] and "profile" not in _toml()
    c = collections_mod.by_name("gutenberg")
    assert (c.slices, c.profile, c.acquire_enabled) == (("lang=it",), "it", True)
    assert "recorded: slices = [\"lang=it\"]" in out and "continues it (profile it)" in out
    assert (c.corpus_dir, c.embed, c.codex) == (before.corpus_dir, before.embed, before.codex), \
        "the rest of the file carried"

    # a slice under one the origin delivers whole is fetched as that one, and said
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--slice", "lang=en/pages=0M"]) == 0
    captured = capsys.readouterr()
    assert "lang=en/pages=0M lies in lang=en" in captured.err
    assert _corpus() == ["en/a.txt", "it/b.txt"]
    c = collections_mod.by_name("gutenberg")
    assert (c.slices, c.profile) == (("lang=it", "lang=en"), "it,en"), "joined with the file's"

    # `all` is every slice, and it wins
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--slice", "all"]) == 0
    capsys.readouterr()
    c = collections_mod.by_name("gutenberg")
    assert (c.slices, c.profile, c.acquire_enabled) == ("all", "all", True)
    assert _toml()["slices"] == "all"


def test_a_slice_the_origin_does_not_deliver_apart_is_refused_first(origin, capsys):
    before = _file().read_bytes()
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--slice", "lang=fr"]) == 2
    err = capsys.readouterr().err
    assert "does not deliver it apart" in err and "lang=en, lang=it" in err and "--slice all" in err
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--slice", "all", "lang=it"]) == 2
    assert "name it alone" in capsys.readouterr().err
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--slice", "bad slice"]) == 2
    capsys.readouterr()
    assert _corpus() == [] and _file().read_bytes() == before


def test_an_origin_with_no_slices_of_its_own_takes_only_all(origin, capsys):
    """PubMed's update files may each hold any year: no year is delivered apart."""
    origin(slice_profiles={})
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--slice", "lang=it"]) == 2
    assert "every one of its files may hold records of any slice" in capsys.readouterr().err
    assert acquire_spec.profile_for_slices(PUBMED_SPEC, ["year=2020"]) is None


def test_a_copy_held_from_peers_is_not_fetched_from_the_origin(origin, capsys):
    """`source = "shard"`: the SHARD is the corpus, and the origin's files
    would land in it (the loader refuses acquiring into it for that reason)."""
    path = _file()
    path.write_text(path.read_text(encoding="utf-8").split("corpus_dir")[0]
                    + 'source = "shard"\n', encoding="utf-8")
    before = path.read_bytes()
    for argv in (["--slice", "lang=it"], ["--profile", "it"]):
        assert louise.main(["hold", "records", "fetch", "gutenberg", *argv]) == 2
        assert "held from peers" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_slice_and_profile_are_one_or_the_other(origin, capsys):
    with pytest.raises(SystemExit) as e:
        louise.main(["hold", "records", "fetch", "gutenberg", "--slice", "lang=it", "--profile", "it"])
    assert e.value.code == 2
    capsys.readouterr()


def test_a_dry_run_names_the_record_and_writes_nothing(origin, capsys):
    before = _file().read_bytes()
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--slice", "lang=it", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "profile: it" in out and "files:   1 pending / 1 total" in out
    assert 'records: slices = ["lang=it"] in the file once the fetch completes' in out
    assert _corpus() == [] and _file().read_bytes() == before


@pytest.mark.parametrize("result", [
    {"fetched": 0, "skipped": 0, "failed": 1, "aborted": 0, "adopted": 0, "total": 1},
    {"fetched": 0, "skipped": 0, "failed": 0, "aborted": 0, "adopted": 0, "total": 0,
     "stopped": "stopped in the listing"},
])
def test_an_incomplete_fetch_records_nothing(origin, monkeypatch, capsys, result):
    monkeypatch.setattr(engine, "run_acquisition", lambda *a, **k: result)
    before = _file().read_bytes()
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--slice", "lang=it"]) in (0, 1)
    assert "recorded:" not in capsys.readouterr().out
    assert _file().read_bytes() == before


def test_the_licence_gate_stays_first(origin, monkeypatch, capsys):
    """Invariant 9: a yellow collection without its acknowledgement moves no
    byte — and records no slice. (The class is the Codex's; the test Codex
    is green, so app/licensing.py, THE resolution, is told otherwise.)"""
    from app import licensing
    monkeypatch.setattr(licensing, "license_class_for", lambda *a, **k: "yellow")
    before = _file().read_bytes()
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--slice", "lang=it"]) == 2
    assert "REFUSED" in capsys.readouterr().err
    assert _corpus() == [] and _file().read_bytes() == before


def test_a_file_naming_a_profile_is_joined_by_what_it_spells(origin, capsys):
    path = _file()
    path.write_text(path.read_text(encoding="utf-8") + 'profile = "en"\n', encoding="utf-8")
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--slice", "lang=it"]) == 0
    capsys.readouterr()
    assert _toml()["slices"] == ["lang=en", "lang=it"] and "profile" not in _toml()
    # a profile no list of slices spells (a capped selection) is not dropped
    # to record one: refused before anything moves
    path.write_text(path.read_text(encoding="utf-8").replace('slices = ["lang=en", "lang=it"]',
                                                             'profile = "first"'), encoding="utf-8")
    before = path.read_bytes()
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--slice", "lang=it"]) == 2
    assert "which no list of slices spells" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_a_named_profile_is_recorded_as_the_slices_it_spells(origin, capsys):
    """RULE 2 (2026-10-31): wikipedia's dumps were fetched with `--profile all`
    and its file kept `slices = []` — the Library called it hand-populated. A
    profile is recorded as `--slice` is: what the node fetched, its file says."""
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--profile", "it",
                        "--dry-run"]) == 0
    assert 'records: slices = ["lang=it"] in the file once the fetch completes' in \
        capsys.readouterr().out
    assert "slices" not in _toml(), "a dry run writes nothing"
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--profile", "it"]) == 0
    capsys.readouterr()
    c = collections_mod.by_name("gutenberg")
    assert _toml()["slices"] == ["lang=it"]
    assert (c.slices, c.profile, c.acquire_enabled) == (("lang=it",), "it", True)
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--profile", "all"]) == 0
    capsys.readouterr()
    c = collections_mod.by_name("gutenberg")
    assert _toml()["slices"] == "all" and (c.slices, c.profile) == ("all", "all")


def test_a_profile_no_slices_spell_is_fetched_and_the_file_left_as_it_is(origin, capsys):
    before = _file().read_bytes()
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--profile", "first"]) == 0
    captured = capsys.readouterr()
    assert "names no slice" in captured.err and "recorded:" not in captured.out
    assert _corpus() and _file().read_bytes() == before, "fetched as asked, never refused"


# ---- --snapshot ID ----------------------------------------------------------

def _cut(snapshot_id: str) -> datetime:
    cx = collections_mod.by_name("gutenberg").codex
    return sn.cutoff_instant(snapshot_id, cadence=cx.update.cadence, day=cx.update.day,
                             clock=cx.origin.clock)


def test_update_files_are_fetched_to_the_cutoff(origin, capsys):
    """06 §6.2: for an origin of update files, the files published before the
    cutoff — by the listing's own date."""
    cut = _cut("2026-09").timestamp()
    origin(mode="updatefiles-dir", mtimes={"en/a.txt": int(cut) - 60, "it/b.txt": int(cut) + 60})
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--profile", "all",
                        "--snapshot", "2026-09", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "files:   1 pending / 1 total" in out and "cutoff:  files published before" in out
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--profile", "all",
                        "--snapshot", "2026-09"]) == 0
    capsys.readouterr()
    assert _corpus() == ["en/a.txt"]
    rows = db.connect(collections_mod.by_name("gutenberg")).execute(
        "SELECT rel_path FROM fetch_files WHERE fetch_done=1").fetchall()
    assert [r["rel_path"] for r in rows] == ["en/a.txt"]


def test_an_origin_with_only_its_present_says_where_the_cutoff_applies(origin, capsys):
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--slice", "lang=it",
                        "--snapshot", "2026-01"]) == 0
    err = capsys.readouterr().err
    assert "keeps only its current state" in err and "--snapshot 2026-01" in err
    assert _corpus() == ["it/b.txt"]


def test_a_dated_dump_must_be_pinned_and_before_the_cutoff(origin, monkeypatch, capsys):
    origin(mode="dated-dumps")
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--slice", "lang=it",
                        "--snapshot", "2026-09"]) == 2
    assert "pins no dump" in capsys.readouterr().err
    import dataclasses
    real = collections_mod._load_one
    for dump, rc in (("20261001", 2), ("20260901", 0)):
        monkeypatch.setattr(collections_mod, "_load_one",
                            lambda p, d=dump: dataclasses.replace(real(p), snapshot=d))
        monkeypatch.setattr(records_fetch, "fetch", lambda *a, **k: 0)
        assert louise.main(["hold", "records", "fetch", "gutenberg", "--snapshot", "2026-09"]) == rc
        err = capsys.readouterr().err
        assert ("not before that snapshot's cutoff" in err) == (rc == 2)


@pytest.mark.parametrize("bad", ["2026-13", "2026-W36", "26-09", "2026-9"])
def test_a_snapshot_id_of_another_form_is_refused(origin, capsys, bad):
    assert louise.main(["hold", "records", "fetch", "gutenberg", "--snapshot", bad]) == 2
    assert "is not a monthly snapshot id" in capsys.readouterr().err


def test_the_cutoff_is_the_inverse_of_the_snapshot_id():
    """09 §9.4: 00:00 in the origin's clock on `update.day` of the period
    after the one the id names."""
    got = sn.cutoff_instant("2026-09", cadence="monthly", day=3, clock="America/New_York")
    assert got.isoformat() == "2026-10-03T00:00:00-04:00"
    assert sn.snapshot_id_for(got, "monthly") == "2026-09"
    assert sn.cutoff_instant("2026-12", cadence="monthly", day=1, clock="UTC") == \
        datetime(2027, 1, 1, tzinfo=timezone.utc)
    week = sn.cutoff_instant("2026-W36", cadence="weekly", day=1, clock="UTC")
    assert week.date().isocalendar()[:2] == (2026, 37) and week.isoweekday() == 1
    assert sn.snapshot_id_for(week, "weekly") == "2026-W36"


# ---- the spec's half ----------------------------------------------------------

def test_wikipedia_delivers_a_language_whole():
    assert acquire_spec.delivered_slice(WIKIPEDIA_SPEC, "lang=it/pages=3M") == "lang=it"
    assert acquire_spec.delivered_slice(WIKIPEDIA_SPEC, "lang=italian") is None
    assert acquire_spec.profile_for_slices(WIKIPEDIA_SPEC, ["lang=it", "lang=en"]) == "it,en"
    assert acquire_spec.profile_for_slices(WIKIPEDIA_SPEC, "all") == "all"
    # `demo` is trwiki: a list of slices spells it
    assert acquire_spec.slices_for_profile(WIKIPEDIA_SPEC, "demo") == ["lang=tr"]
    assert acquire_spec.slices_for_profile(WIKIPEDIA_SPEC, "tr,it") == ["lang=tr", "lang=it"]
    assert acquire_spec.slices_for_profile(WIKIPEDIA_SPEC, "all") == "all"


def test_a_share_fetch_selection_still_derives_no_acquisition(isolated_config):
    """A list the origin does not deliver whole — parts taken from peers —
    keeps deriving no profile and no fetch, as before."""
    c = collections_mod.by_name("gutenberg")
    got = collections_mod.codex_defaults(c.codex, "gutenberg", ("lang=la",), ("records",))
    assert (got["profile"], got["acquire_enabled"]) == (None, False)

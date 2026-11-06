"""tests/test_louise_dev.py — `louise dev` (07 §7.3, §7.6): the operator's
instruments under the one command, each the tools module it wraps given
the rest of the line as its own flags (wrapped, not rewritten: `python -m
tools.<module>` keeps working until step 12); `migrate schema` over a
collection's state database by name."""
import sys

from app import louise


def test_an_instrument_gets_its_own_flags_whole(monkeypatch):
    import tools.sched_report as sr
    import tools.profile_embed as pe
    seen = {}
    monkeypatch.setattr(sr, "main", lambda argv=None: seen.setdefault("sched", argv) and 0)
    assert louise.main(["dev", "schedule-report", "--all", "--seconds", "5"]) == 0
    assert seen["sched"] == ["--all", "--seconds", "5"], "flags the CLI never parsed"

    def argvless():
        seen["profile"] = list(sys.argv)
        return 0
    monkeypatch.setattr(pe, "main", argvless)
    before = list(sys.argv)
    assert louise.main(["dev", "embedder-profile", "--url", "http://encoder", "--texts", "3"]) == 0
    assert seen["profile"] == ["louise dev embedder-profile", "--url", "http://encoder", "--texts", "3"]
    assert sys.argv == before, "sys.argv put back"
    assert set(louise.DEV_TOOLS) == {"schedule-report", "embedder-profile", "refs-sync", "wikidata-classes"}


def test_migrate_schema_runs_over_the_named_collection(isolated_config, monkeypatch, capsys):
    import tools.migrate_manifest as mm
    from app import collections as collections_mod
    calls = []
    monkeypatch.setattr(mm, "run", lambda db, source, dry, backup: calls.append((db, source, dry, backup)) or {"ok": 1})
    assert louise.main(["dev", "migrate", "schema", "gutenberg", "--dry-run"]) == 0
    c = collections_mod.by_name("gutenberg")
    assert calls == [(c.manifest_path, "gutenberg", True, True)]
    assert louise.main(["dev", "migrate", "schema", "nope"]) == 2

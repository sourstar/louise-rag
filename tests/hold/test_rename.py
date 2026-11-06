"""`hold collection rename` (app/hold/rename.py; 07 §7.3; operator,
2026-10-13: the complete Normattiva, created as `normattiva-1861`, becomes
`normattiva`). A name is this peer's own: the directory, the shared folders,
the manifest's stored paths, the pause and the held file move; nothing is
recomputed; an origin folder another collection links stays."""
import os
import sqlite3

import pytest

from app import node_state
from app.hold import collection as col, rename as rn
from app.hold.codex import CodexError


@pytest.fixture
def node(tmp_path, codex, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("LOUISE_ROOT", str(data))
    monkeypatch.setenv("SHARDS_ROOT", str(tmp_path / "shards"))
    monkeypatch.setenv("ORIGINS_ROOT", str(tmp_path / "corpora"))
    cfg = tmp_path / "config"
    (cfg / "collections").mkdir(parents=True)
    monkeypatch.setenv("CONFIG_DIR", str(cfg))
    root = data / "collections"
    col.create(root, codex, name="old")
    (cfg / "collections" / "old.toml").write_text(f'codex = "{codex.collection_id}"\n', encoding="utf-8")
    state = root / "old" / "state"
    state.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(state / "manifest.sqlite")
    con.execute("CREATE TABLE documents (uid TEXT, path TEXT)")
    con.execute("CREATE TABLE fetch_files (rel_path TEXT, local_path TEXT)")
    origin = f"{root}/old/origin/akn/M/codici.zip"
    con.executemany("INSERT INTO documents VALUES (?, ?)",
                    [("a:1", origin + "::x.xml"), ("a:2", "/elsewhere/old/y.xml"),
                     # a locator naming two files, wikipedia's dump + page_props
                     ("a:3", f"{root}/old/origin/d.xml.bz2::1::9|{root}/old/origin/p.sql.gz::4::5"),
                     ("a:4", f"{root}/older/origin/z.xml")])
    con.execute("INSERT INTO fetch_files VALUES ('akn/M/codici.zip', ?)", (origin,))
    con.commit(); con.close()
    (tmp_path / "corpora" / "old" / "akn").mkdir(parents=True)
    return tmp_path, root, cfg, codex


def test_a_rename_moves_everything_that_carries_the_name(node):
    tmp, root, cfg, codex = node
    node_state.set_paused("old", True)
    out = rn.rename(root, "old", "new")
    assert not (root / "old").exists() and (root / "new" / "state" / "manifest.sqlite").is_file()
    assert (cfg / "collections" / "new.toml").is_file() and not (cfg / "collections" / "old.toml").exists()
    assert os.readlink(root / "new" / "shard") == str(tmp / "shards" / "new")
    assert (tmp / "shards" / "new" / "codex.json").is_file() and not (tmp / "shards" / "old").exists()
    assert os.readlink(root / "new" / "origin") == str(tmp / "corpora" / "new")
    assert (tmp / "corpora" / "new" / "akn").is_dir()
    con = sqlite3.connect(root / "new" / "state" / "manifest.sqlite")
    paths = dict(con.execute("SELECT uid, path FROM documents"))
    assert paths["a:1"] == f"{root}/new/origin/akn/M/codici.zip::x.xml"
    assert paths["a:2"] == "/elsewhere/old/y.xml", "only paths under the collection's own directory"
    assert paths["a:3"] == f"{root}/new/origin/d.xml.bz2::1::9|{root}/new/origin/p.sql.gz::4::5", \
        "every file a locator names, not only the first (wikipedia's page_props, 2026-10-30)"
    assert paths["a:4"] == f"{root}/older/origin/z.xml", "a sibling whose name starts with the old one"
    assert con.execute("SELECT local_path FROM fetch_files").fetchone()[0] == f"{root}/new/origin/akn/M/codici.zip"
    assert out["done"]["paths"] == {"documents.path": 2, "fetch_files.local_path": 1}
    assert node_state.paused_collections() >= {"new"} and "old" not in node_state.paused_collections()
    assert col.resolve(root, "new")[1].collection_id == codex.collection_id, "the same Codex"
    assert not (root / "new" / "state" / rn.MARKER).exists()


def test_an_origin_another_collection_links_stays_where_it_is(node):
    tmp, root, cfg, codex = node
    (root / "sister").mkdir()
    (root / "sister" / "origin").symlink_to(tmp / "corpora" / "old", target_is_directory=True)
    out = rn.rename(root, "old", "new")
    assert out["origin_kept"] == {"folder": str(tmp / "corpora" / "old"), "also_linked_by": ["sister"]}
    assert os.readlink(root / "new" / "origin") == str(tmp / "corpora" / "old")
    assert (tmp / "corpora" / "old" / "akn").is_dir() and not (tmp / "corpora" / "new").exists()


def test_a_taken_name_or_a_bad_one_moves_nothing(node):
    tmp, root, cfg, codex = node
    (cfg / "collections" / "taken.toml").write_text("x", encoding="utf-8")
    with pytest.raises(CodexError, match="taken"):
        rn.rename(root, "old", "taken")
    with pytest.raises(CodexError, match="not a collection name"):
        rn.rename(root, "old", "no spaces")
    assert (root / "old").is_dir() and (cfg / "collections" / "old.toml").is_file()
    dry = rn.rename(root, "old", "new", dry_run=True)
    assert dry["dry_run"] and (root / "old").is_dir() and not (root / "new").exists()


def test_an_interrupted_rename_is_finished_by_running_it_again(node, monkeypatch):
    tmp, root, cfg, codex = node
    real = rn._rewrite_paths

    def boom(*a, **k):
        raise OSError("power cut")
    monkeypatch.setattr(rn, "_rewrite_paths", boom)
    with pytest.raises(OSError):
        rn.rename(root, "old", "new")
    assert (root / "new" / "state" / rn.MARKER).is_file(), "the plan survives the directory's move"
    monkeypatch.setattr(rn, "_rewrite_paths", real)
    out = rn.rename(root, "old", "new")
    assert out["done"]["directory"] == "already" and (cfg / "collections" / "new.toml").is_file()
    con = sqlite3.connect(root / "new" / "state" / "manifest.sqlite")
    assert con.execute("SELECT path FROM documents WHERE uid='a:1'").fetchone()[0].startswith(f"{root}/new/")


def test_a_cache_keyed_on_a_moved_folder_follows_it(node, monkeypatch):
    """2026-10-27: a cache is keyed on its origin's resolved path, so the
    origin's move orphaned wikidata's pack (~60 GB, hours from the bz2). Every
    cache keyed on a folder under what moves goes to the new path's key."""
    from app import config, derived
    tmp, root, cfg, codex = node
    monkeypatch.setattr(config, "CACHE_DIR", tmp / "cache")
    dump = tmp / "corpora" / "old" / "akn"
    pack = tmp / "cache" / "wikidata-pack" / derived.tag(dump)
    pack.mkdir(parents=True)
    (pack / "meta.json").write_text("{}", encoding="utf-8")
    other = tmp / "cache" / "wikidata-pack" / derived.tag(tmp / "corpora" / "elsewhere")
    other.mkdir()
    planned = rn.rename(root, "old", "new", dry_run=True)
    moved_to = tmp / "cache" / "wikidata-pack" / derived.tag(tmp / "corpora" / "new" / "akn")
    assert planned["caches"] == [[str(pack), str(moved_to)]] and pack.is_dir()
    out = rn.rename(root, "old", "new")
    assert (moved_to / "meta.json").is_file() and not pack.exists()
    assert other.is_dir(), "a cache of another origin stays"
    assert out["done"]["caches"] == {str(moved_to): "moved"}
    assert derived.place("wikidata-pack", tmp / "corpora" / "new" / "akn") == moved_to


def test_forget_takes_its_shard_folder_and_leaves_a_shared_origin(node):
    """With the shared roots, `forget` removed the directory and its links but
    left `<SHARDS_ROOT>/<name>` behind under the name; a collection renamed to
    that name then found it taken (2026-10-14: normattiva-1861 → normattiva
    after forgetting the parser-7 normattiva). Its SHARD folder goes; an
    origin folder another held collection links stays — the zips both read."""
    from app.hold import forget as forget_mod
    tmp, root, cfg, codex = node
    (root / "sister").mkdir()
    (root / "sister" / "origin").symlink_to(tmp / "corpora" / "old", target_is_directory=True)
    out = forget_mod.forget(root, "old")
    assert not (root / "old").exists() and not (tmp / "shards" / "old").exists()
    assert (tmp / "corpora" / "old" / "akn").is_dir(), "the shared origin stays"
    assert str(tmp / "shards" / "old") in out["removed"]
    alone = forget_mod.forget(root, "old")
    assert alone["gone"] is True


def test_forget_takes_its_own_origin_folder_when_nobody_else_links_it(node):
    from app.hold import forget as forget_mod
    tmp, root, cfg, codex = node
    forget_mod.forget(root, "old")
    assert not (tmp / "corpora" / "old").exists() and not (tmp / "shards" / "old").exists()


def test_the_bundles_folder_under_bundles_root_follows_a_rename_and_a_forget(tmp_path, codex, monkeypatch):
    """07 §7.8, 2026-10-19: `<name>/state/bundles` -> `<BUNDLES_ROOT>/<name>`
    moves with the name as the SHARD folder does, and goes with `forget`."""
    from app.hold import forget as forget_mod
    data = tmp_path / "data"
    monkeypatch.setenv("LOUISE_ROOT", str(data))
    monkeypatch.setenv("SHARDS_ROOT", str(tmp_path / "shards"))
    monkeypatch.setenv("BUNDLES_ROOT", str(tmp_path / "bundles"))
    cfg = tmp_path / "config"
    (cfg / "collections").mkdir(parents=True)
    monkeypatch.setenv("CONFIG_DIR", str(cfg))
    root = data / "collections"
    col.create(root, codex, name="old")
    (cfg / "collections" / "old.toml").write_text(f'codex = "{codex.collection_id}"\n', encoding="utf-8")
    bundle = root / "old" / "state" / "bundles" / codex.space_segment / "lang=af" / "00001" / ("c" * 64)
    bundle.mkdir(parents=True)
    (bundle / "header.json").write_bytes(b"{}")
    assert (tmp_path / "bundles" / "old" / codex.space_segment).is_dir(), "written through the link"
    out = rn.rename(root, "old", "new")
    assert out["done"]["bundles"] == "moved"
    assert os.readlink(root / "new" / "state" / "bundles") == str(tmp_path / "bundles" / "new")
    assert (tmp_path / "bundles" / "new" / codex.space_segment / "lang=af" / "00001" / ("c" * 64) / "header.json").is_file()
    assert not (tmp_path / "bundles" / "old").exists()
    gone = forget_mod.forget(root, "new")
    assert str(tmp_path / "bundles" / "new") in gone["removed"] and not (tmp_path / "bundles" / "new").exists()


def test_a_rename_carries_the_fast_drives_caches_with_what_it_moves(node, monkeypatch):
    """A cache is keyed on its origin's resolved path (app/derived.py): a
    moved origin found none and rebuilt it from the dump — wikidata's pack,
    hours (München, 2026-10-27). The rename carries every cache keyed on a
    folder or file under what it moves."""
    from app import config, derived
    tmp, root, cfg, codex = node
    cache = tmp / "cache"
    monkeypatch.setattr(config, "CACHE_DIR", cache)
    dump = tmp / "corpora" / "old" / "latest-all.json.bz2"
    dump.write_bytes(b"dump")
    pack = derived.place("wikidata-pack", dump)
    pack.mkdir(parents=True)
    (pack / "labels.sqlite").write_bytes(b"labels")
    other = derived.place("wikidata-pack", tmp / "elsewhere.bz2")
    other.mkdir(parents=True)
    moved = derived.place("wikidata-pack", tmp / "corpora" / "new" / "latest-all.json.bz2")
    planned = rn.rename(root, "old", "new", dry_run=True)
    assert planned["caches"] == [[str(pack), str(moved)]], "the dump FILE is a key, not only folders"
    out = rn.rename(root, "old", "new")
    assert (moved / "labels.sqlite").read_bytes() == b"labels" and not pack.exists()
    assert other.is_dir(), "a cache of another origin stays"
    assert out["done"]["caches"] == {str(moved): "moved"}
    assert rn.rename(root, "new", "new2", dry_run=True)["caches"], "and the next rename finds it again"


def test_no_fast_drive_no_cache_work(node, monkeypatch):
    from app import config
    tmp, root, cfg, codex = node
    monkeypatch.setattr(config, "CACHE_DIR", None)
    assert rn.rename(root, "old", "new", dry_run=True)["caches"] == []

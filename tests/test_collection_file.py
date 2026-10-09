"""07 §7.8's collection file: `codex`, `title`, `slices`, `kinds`, `embed`,
`share` and `[encoder]` — and nothing the Codex or the tree already says.

The files on disk were written before §7.8 spelled the file out, and they
carried a second answer to five questions the Codex already answers: which
archive (its `credits`), which dump (its `origin.spec.files`), the space's
window (its `space`), where the origin lives (beside the drag, operator
2026-09-15) and whether to fetch (the file's own `slices`). Each of those
is now DERIVED; the old keys are still read, so no deployed collection
changes on upgrade, and a rewrite keeps one only where the derivation
would give a different value.
"""
from pathlib import Path

import pytest

from app import collections as collections_mod, louise
from app.acquire import spec as acquire_spec
from app.acquire.specs import ACQUIRE_REGISTRY, WIKIPEDIA_SPEC
from app.hold import collection as col
from app.hold.codex import CodexError
from app.web import _write_collection_toml
from tests.hold.conftest import fake_query_embed  # noqa: E402,F811
from tests.hold.conftest import KEY, NOW, PLATFORM, a_credit, fake_embed
from tests.hold.test_template import recipe

ARCHIVE = "upstash-wikipedia-2024-06"


@pytest.fixture
def node(tmp_path, isolated_config, monkeypatch):
    """A node whose files live in CONFIG_DIR and whose collections live
    under LOUISE_ROOT — the reference node's two directories."""
    root = tmp_path / "louise"
    (root / "collections").mkdir(parents=True)
    cfg = tmp_path / "config"
    (cfg / "collections").mkdir(parents=True, exist_ok=True)
    # isolated_config's own gutenberg file names this same Codex; each test
    # here writes the file it is about
    (cfg / "collections" / "gutenberg.toml").unlink()
    monkeypatch.setenv("LOUISE_ROOT", str(root))
    monkeypatch.setenv("CONFIG_DIR", str(cfg))
    monkeypatch.setattr(collections_mod.config, "DATA_DIR", root)

    def make(credits=()):
        return col.create_from_recipe(
            root / "collections", "gutenberg", recipe(), platform_image=PLATFORM,
            embed=fake_embed, embed_query=fake_query_embed, name="gutenberg", key=KEY, now=NOW, credits=credits)

    def load(text):
        path = cfg / "collections" / "gutenberg.toml"
        path.write_text(text, encoding="utf-8")
        return path, collections_mod._load_one(path)

    return root, make, load


def rewrite(path, c):
    """As every rewrite path in the app calls the writer."""
    _write_collection_toml(
        path, codex=c.codex.collection_id, plugin=c.plugin, profile=c.profile,
        acquire_enabled=c.acquire_enabled, corpus_dir=c.corpus_dir,
        manifest=c.manifest_path, fts=c.fts_dir, lancedb=c.lance_dir,
        license_ack=c.license_ack, genesis_archive=c.genesis_archive,
        embed=c.embed, embed_mode=c.embed_mode_default, layers=c.layers,
        snapshot=c.snapshot, extras=c.extras, peer_source=c.peer_source,
        title=c.title, slices=c.slices, kinds=c.kinds, share=c.share)


# ---- what the file says, and what it derives ------------------------------

def test_the_file_says_only_what_the_codex_does_not(node):
    root, make, load = node
    cx = make()
    _, c = load(f'codex = "{cx.collection_id}"\nslices = "all"\n'
                'kinds = ["records", "vectors"]\nembed = "off"\nshare = false\n')
    assert c.slices == "all" and c.profile == "full", \
        "the acquisition spec's spelling of every slice"
    assert c.acquire_enabled is True
    assert c.corpus_dir == root / "collections" / "gutenberg" / "origin"
    assert c.genesis_archive is None, "this Codex credits no archive"
    assert (c.kinds, c.share, c.title) == (("records", "vectors"), False, None)


def test_the_archive_is_the_one_the_codex_credits(node):
    _, make, load = node
    cx = make(credits=(a_credit(ARCHIVE),))
    head = f'codex = "{cx.collection_id}"\nslices = "all"\n'
    assert load(head)[1].genesis_archive == ARCHIVE
    assert load(head + 'kinds = ["records"]\n')[1].genesis_archive is None, \
        "a peer that holds no vectors imports none"
    with pytest.raises(ValueError, match="03 §3.5"):
        load(f'codex = "{cx.collection_id}"\ngenesis_archive = "medcpt-pubmed"\n')


def test_holding_nothing_fetches_nothing(node):
    _, make, load = node
    _, c = load(f'codex = "{make().collection_id}"\nslices = []\n')
    assert (c.slices, c.profile, c.acquire_enabled) == ((), None, False)


def test_a_collection_holding_nothing_has_no_origin_to_miss(node):
    """No slice, nothing fetched, so no origin directory yet — which is not
    an error the discovery scan should log every round."""
    from app import db, discovery
    _, make, load = node
    _, c = load(f'codex = "{make().collection_id}"\nslices = []\n')
    con = db.connect(c)
    assert discovery.sync_collection(con, c)["seen"] == 0
    assert con.execute("SELECT 1 FROM events WHERE level='error'").fetchone() is None
    con.close()


def test_part_of_a_collection_is_a_list_of_slices(node):
    """`share fetch --slice` holds part (07 §7.3, step 9d): the file lists
    the slices, each once, and nothing is fetched from an origin for it."""
    _, make, load = node
    _, c = load(f'codex = "{make().collection_id}"\nslices = ["lang=en", "lang=de", "lang=en"]\n')
    assert (c.slices, c.profile, c.acquire_enabled) == (("lang=en", "lang=de"), None, False)
    with pytest.raises(ValueError, match="slices"):
        load(f'codex = "{make().collection_id}"\nslices = ["lang=en", "not a slice!"]\n')


def test_kinds_are_02s_closed_list(node):
    _, make, load = node
    with pytest.raises(ValueError, match="closed"):
        load(f'codex = "{make().collection_id}"\nkinds = ["records", "pdfs"]\n')


def test_slices_and_profile_are_one_answer(node):
    _, make, load = node
    with pytest.raises(ValueError, match="keep one"):
        load(f'codex = "{make().collection_id}"\nslices = "all"\nprofile = "full"\n')


# ---- the space is the Codex's --------------------------------------------

def test_the_encoder_table_names_an_endpoint_not_a_space(node):
    _, make, load = node
    cx = make()
    head = (f'codex = "{cx.collection_id}"\nslices = "all"\n\n'
            '[encoder]\nurl = "http://enc:8080"\nembed_model = "m"\nquery_model = "m"\n')
    r = collections_mod.recipe_for(load(head)[1])
    assert (r.url, r.dtype, r.ctx, r.dim) == \
        ("http://enc:8080", cx.space.dtype, cx.space.ctx, cx.space.dim)
    with pytest.raises(ValueError, match="02 §2.4"):
        load(head + f"ctx = {cx.space.ctx * 2}\n")


@pytest.mark.strict_encoder
def test_no_encoder_table_is_no_encoder(node):
    """No `[encoder]`, no endpoint: there is no default one to fall back on
    (operator, 2026-09-18) — the collection is non-compliant."""
    _, make, load = node
    cx = make()
    with pytest.raises(collections_mod.NoEncoder, match="non-compliant"):
        collections_mod.recipe_for(load(f'codex = "{cx.collection_id}"\n')[1])


# ---- the files already on disk --------------------------------------------

def test_the_old_shape_loads_as_it_did(node):
    """A file that never named `slices`, `kinds`, `share` or `title` keeps
    the old defaults for what it leaves out: nothing starts fetching,
    importing or moving on upgrade."""
    _, make, load = node
    cx = make(credits=(a_credit(ARCHIVE),))
    _, c = load(f'codex = "{cx.collection_id}"\nprofile = "full"\n'
                'corpus_dir = "/corpus"\n')
    assert c.corpus_dir == Path("/corpus")
    assert (c.acquire_enabled, c.genesis_archive) == (False, None)
    assert (c.slices, c.profile) == ("all", "full")


@pytest.mark.strict_encoder
def test_a_rewrite_drops_every_key_the_codex_answers(node):
    root, make, load = node
    cx = make(credits=(a_credit(ARCHIVE),))
    origin = root / "collections" / "gutenberg" / "origin"
    path, before = load(f'codex = "{cx.collection_id}"\nprofile = "full"\n'
                        'acquire_enabled = true\n'
                        f'genesis_archive = "{ARCHIVE}"\n'
                        f'corpus_dir = "{origin.as_posix()}"\n')
    rewrite(path, before)
    text = path.read_text(encoding="utf-8")
    assert text == (f'codex = "{cx.collection_id}"\nslices = "all"\n'
                    'kinds = ["records", "vectors"]\nshare = false\n')
    assert collections_mod._load_one(path) == before


def test_a_rewrite_keeps_what_the_codex_cannot_say(node):
    _, make, load = node
    path, before = load(f'codex = "{make().collection_id}"\nprofile = "full"\n'
                        'corpus_dir = "/corpus"\nacquire_enabled = false\n')
    rewrite(path, before)
    text = path.read_text(encoding="utf-8")
    assert 'corpus_dir = "/corpus"' in text and "acquire_enabled = false" in text
    assert collections_mod._load_one(path) == before


def test_the_new_shape_survives_a_rewrite(node):
    _, make, load = node
    cx = make()
    text = (f'codex = "{cx.collection_id}"\ntitle = "Gutenberg, plates"\n'
            'slices = "all"\nkinds = ["records"]\nembed = "on-demand"\n'
            'share = true\n\n[encoder]\nurl = "http://enc:8080"\n'
            'query_url = "http://enc:8081"\nembed_model = "m"\nquery_model = "q"\n')
    path, before = load(text)
    rewrite(path, before)
    assert path.read_text(encoding="utf-8") == text
    assert collections_mod._load_one(path) == before


# ---- `hold collection create` writes the file -----------------------------

def test_create_writes_where_the_node_reads(node):
    root, make, _ = node
    cx = make()
    held = louise._write_held_file(root / "collections", cx)
    assert held == collections_mod.config_toml_path("gutenberg")
    c = collections_mod._load_one(held)
    assert c.codex.collection_id == cx.collection_id
    assert (c.slices, c.acquire_enabled, c.embed_mode_default, c.share) == \
        ((), False, "off", False), "a new collection holds nothing and spends nothing"
    assert louise._write_held_file(root / "collections", cx) == held, \
        "the same Codex again: nothing to write"
    held.write_text('codex = "sha256:' + "0" * 64 + '"\n', encoding="utf-8")
    with pytest.raises(CodexError, match="another collection is another name"):
        louise._write_held_file(root / "collections", cx)


def test_a_create_in_another_root_writes_beside_its_drags(tmp_path, isolated_config,
                                                          monkeypatch):
    scratch = tmp_path / "scratch" / "collections"
    scratch.mkdir(parents=True)
    monkeypatch.setenv("LOUISE_ROOT", str(tmp_path / "node"))
    cx = col.create_from_recipe(scratch, "gutenberg", recipe(), platform_image=PLATFORM,
                                embed=fake_embed, embed_query=fake_query_embed, name="gutenberg", key=KEY, now=NOW)
    assert louise._write_held_file(scratch, cx) == scratch / "gutenberg.toml"


def test_create_names_both_sides_models(node):
    """07 §7.8 (amended 2026-10-08): the file `create` writes names
    `embed_model` and `query_model` — each URL's own model on the model
    container — and loads back as the node reads it."""
    root, make, _ = node
    cx = make()
    held = louise._write_held_file(root / "collections", cx,
                                   "http://rage:1/upstream/medcpt-article",
                                   "http://rage:1/upstream/medcpt-query")
    text = held.read_text(encoding="utf-8")
    assert 'embed_model = "medcpt-article"\nquery_model = "medcpt-query"\n' in text
    c = collections_mod._load_one(held)
    assert (c.embed.model, c.embed.query_model) == ("medcpt-article", "medcpt-query")


def test_create_model_keys_where_a_url_names_none():
    """07 §7.8 (amended 2026-10-09): a URL naming no model takes the model
    from its key, and without one is refused — never the space's label."""
    with pytest.raises(CodexError, match="--encoder .* names no model.*--embed-model"):
        louise.model_keys("http://localai:8080")
    assert louise.model_keys("http://localai:8080", embed_model="vega-medcpt") == \
        ("vega-medcpt", "vega-medcpt")
    assert louise.model_keys("http://localai:8080", embed_model="vega-a",
                             query_model="vega-q") == ("vega-a", "vega-q")
    assert louise.model_keys("http://rage:1/upstream/bge-m3") == ("bge-m3", "bge-m3")
    # a separate query endpoint naming no model needs --query-model
    with pytest.raises(CodexError, match="--query-encoder .* names no model.*--query-model"):
        louise.model_keys("http://rage:1/upstream/a", "http://query:2")
    assert louise.model_keys("http://rage:1/upstream/a", "http://query:2",
                             query_model="vega-q") == ("a", "vega-q")
    # a key that is not the model its URL names: the file would say two things
    with pytest.raises(CodexError, match="must agree"):
        louise.model_keys("http://rage:1/upstream/a", embed_model="b")
    with pytest.raises(CodexError, match="--api is one of"):
        louise.check_api("grpc")


def test_the_file_carries_api_and_the_named_models(node):
    root, make, _ = node
    cx = make()
    held = louise._write_held_file(root / "collections", cx, "http://localai:8080",
                                   api="vega", embed_model="vega-medcpt-article",
                                   query_model="vega-medcpt-query")
    c = collections_mod._load_one(held)
    assert (c.embed.url, c.embed.api, c.embed.model, c.embed.query_model) == (
        "http://localai:8080", "vega", "vega-medcpt-article", "vega-medcpt-query")


def test_the_file_never_names_the_label_as_a_model(node):
    root, make, _ = node
    cx = make()
    with pytest.raises(CodexError, match="names no model"):
        louise._write_held_file(root / "collections", cx, "http://localai:8080")
    assert not collections_mod.config_toml_path("gutenberg").exists()


# ---- the two derivations ---------------------------------------------------

@pytest.mark.parametrize("plugin,everything", [
    ("gutenberg", "full"), ("pubmed", "full"), ("wikipedia", "all"),
    ("embedded", "all"), ("normattiva", "full")])
def test_every_spec_spells_all(plugin, everything):
    assert acquire_spec.everything_profile(ACQUIRE_REGISTRY[plugin]) == everything


def test_the_dump_is_the_one_the_codex_pins():
    files = [{"rel_path": f.rel_path, "size": None, "checksum": None}
             for f in WIKIPEDIA_SPEC.snapshot_files("20260801")]
    assert acquire_spec.pinned_snapshot(WIKIPEDIA_SPEC, {"files": files}) == "20260801"
    assert acquire_spec.pinned_snapshot(WIKIPEDIA_SPEC, {"files": None}) is None
    with pytest.raises(ValueError, match="no dated snapshot"):
        acquire_spec.pinned_snapshot(WIKIPEDIA_SPEC, {"files": files[:3]})
    assert acquire_spec.pinned_snapshot(ACQUIRE_REGISTRY["gutenberg"],
                                        {"files": files}) is None


def test_the_command_line_refuses_a_url_naming_no_model_before_creating(
        tmp_path, isolated_config, monkeypatch, capsys):
    """`hold collection create --encoder URL` where the URL names no model:
    refused, exit 2, nothing created — `--embed-model` names it."""
    import json as _json
    root = tmp_path / "scratch" / "collections"
    root.mkdir(parents=True)
    monkeypatch.setenv("LOUISE_ROOT", str(tmp_path / "node"))
    f = tmp_path / "recipe.json"
    f.write_text(_json.dumps(recipe()), encoding="utf-8")
    base = ["hold", "collection", "create", "gutenberg", "--recipe", str(f), "--root", str(root),
            "--platform-image", PLATFORM, "--encoder", "http://localai:8080",
            "--query-encoder", "http://localai:8080"]
    assert louise.main(base) == 2
    assert "--embed-model" in capsys.readouterr().err
    assert not any(root.iterdir())
    assert louise.main(base + ["--api", "vega", "--embed-model", "vega-a"]) == 2
    assert "--query-model" in capsys.readouterr().err
    assert not any(root.iterdir())

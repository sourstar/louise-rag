"""How a source's profiles relate to one another (spec.profile_mode).

Reported live: after picking one language for the pre-embedded Wikipedia
corpus, the grow dialog only offered "all" — because profiles were treated
as an incremental ladder ordered by size, so the only thing "bigger" than
the largest language was everything. Languages are independent slices and
the plugin encodes the language into native_id, so they combine safely; an
EXCLUSIVE source's parts never do (a second part would mint the same ids),
and gutenberg's demo⊂full ladder is a genuine ladder.
"""
import pytest

from app import collections as collections_mod, web
from app.acquire.spec import (Filter, estimated_bytes_for, profile_parts,
                               resolve_profile)
from app.acquire.specs import ACQUIRE_REGISTRY, EMBEDDED_SPEC


def test_combined_profile_is_the_union_of_its_parts():
    f = resolve_profile(EMBEDDED_SPEC, "tr,it")
    assert set(f.include) == {"data/tr/*", "data/it/*"}
    # order-independent and duplicate-safe
    assert set(resolve_profile(EMBEDDED_SPEC, "it,tr,it").include) == set(f.include)


def test_combined_profile_sizes_add_up():
    tr = EMBEDDED_SPEC.estimated_bytes["tr"]
    it = EMBEDDED_SPEC.estimated_bytes["it"]
    assert estimated_bytes_for(EMBEDDED_SPEC, "tr,it") == tr + it
    assert estimated_bytes_for(EMBEDDED_SPEC, "tr") == tr


def test_unknown_part_is_refused_by_name():
    with pytest.raises(ValueError, match="klingon"):
        resolve_profile(EMBEDDED_SPEC, "tr,klingon")


def test_non_additive_sources_never_combine():
    """An EXCLUSIVE source (one part per collection: a second would mint the
    same ids) must not accept two, however they are spelled. Its worked case,
    genome, is boxed in attic/; the rule is the platform's."""
    from app.acquire.spec import (AcquisitionSpec, ChecksumSource, DeltaSpec,
                                   Endpoint, Transport)
    from app.plugins.base import LicenseClass
    exclusive = AcquisitionSpec(
        source_id="x", transport=Transport.HTTPS,
        endpoints=(Endpoint(transport=Transport.HTTPS, url="https://x.invalid"),),
        layout="container-files", checksums=ChecksumSource(kind="none"),
        delta=DeltaSpec(mode="full-resync"),
        profiles={"a": Filter(include=("a/*",)), "b": Filter(include=("b/*",))},
        profile_mode="exclusive", license_class=LicenseClass.GREEN)
    with pytest.raises(ValueError):
        resolve_profile(exclusive, "a,b")
    with pytest.raises(ValueError):
        resolve_profile(ACQUIRE_REGISTRY["gutenberg"], "demo,full")


def test_profile_parts_splits_and_trims():
    assert profile_parts("tr, it ,en") == ["tr", "it", "en"]
    assert profile_parts("") == []
    assert profile_parts("tr") == ["tr"]


def test_engine_filters_files_for_a_combined_profile(tmp_path, mock_server):
    """End to end through the engine: a combined profile selects exactly the
    union of its slices and nothing else."""
    from app import db
    from app.acquire import engine
    from app.acquire.spec import (AcquisitionSpec, ChecksumSource, DeltaSpec,
                                   Endpoint, RemoteFile, Transport)
    from app.plugins.base import LicenseClass

    for lang in ("tr", "it", "en"):
        path = mock_server.root / "data" / lang
        path.mkdir(parents=True, exist_ok=True)
        (path / "000.parquet").write_bytes(b"x" * 50)

    static = tuple(RemoteFile(rel_path=f"data/{l}/000.parquet", size=50)
                   for l in ("tr", "it", "en"))
    spec = AcquisitionSpec(
        source_id="embedded", transport=Transport.HTTPS,
        endpoints=(Endpoint(transport=Transport.HTTPS, url=mock_server.base_url,
                             static_files=static),),
        layout="container-files", checksums=ChecksumSource(kind="none"),
        delta=DeltaSpec(mode="full-resync"),
        profiles={l: Filter(include=(f"data/{l}/*",)) for l in ("tr", "it", "en")},
        profile_mode="additive",
        estimated_bytes={"tr": 50, "it": 50, "en": 50},
        license_class=LicenseClass.GREEN)
    collection = collections_mod.Collection(
        name="wv", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance",
        acquire_enabled=True)
    collection.corpus_dir.mkdir(parents=True, exist_ok=True)
    con = db.connect(collection)

    result = engine.run_acquisition(con, collection, spec, "tr,it")
    assert result["fetched"] == 2
    assert (collection.corpus_dir / "data" / "tr" / "000.parquet").is_file()
    assert (collection.corpus_dir / "data" / "it" / "000.parquet").is_file()
    assert not (collection.corpus_dir / "data" / "en").exists()


def test_grow_offers_every_slice_for_an_additive_source(isolated_config, tmp_path):
    """The reported bug: holding the largest language left only "all" on
    offer, so a second language could not be added."""
    c = collections_mod.Collection(
        name="wikivec", plugin="embedded", profile="en", embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")
    names = [o["name"] for o in web._grow_options(c)]
    assert "tr" in names and "it" in names, \
        "a smaller language must stay addable next to a larger one"
    assert "all" in names


def test_grow_still_ladders_for_incremental_sources(isolated_config, tmp_path):
    c = collections_mod.Collection(
        name="g", plugin="gutenberg", profile="starter", embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")
    names = [o["name"] for o in web._grow_options(c)]
    assert names[0] == "starter" and "demo" not in names


def _normattiva(tmp_path, profile):
    return collections_mod.Collection(
        name="normattiva", plugin="normattiva", profile=profile, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")


def test_grow_offers_nothing_beyond_what_all_names(isolated_config, tmp_path):
    """Operator, 2026-11-01: "Normattiva grow data, the entries are weird".
    `full` is what `slices = "all"` fetches; `archive` is normattiva-archive's
    (2026-10-13) and was offered — preselected — to the live collection."""
    opts = web._grow_options(_normattiva(tmp_path, "full"))
    assert [o["name"] for o in opts] == ["full"]
    assert opts[0]["label"].endswith("— current, re-sync")
    assert [o["name"] for o in web._grow_options(_normattiva(tmp_path, "starter"))] == ["starter", "full"]
    daughter = web._grow_options(_normattiva(tmp_path, "archive"))
    assert [o["name"] for o in daughter] == ["archive"], "the daughter re-syncs its own"


def test_a_profile_without_a_size_shows_none(isolated_config, tmp_path):
    """Operator, 2026-11-01: wikidata's families showed "?" for their size;
    where no size applies, nothing follows the entry's name."""
    c = collections_mod.Collection(
        name="wikidata", plugin="wikidata", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")
    labels = {o["name"]: o["label"] for o in web._grow_options(c)}
    assert labels["person"] == "person — people"
    assert labels["all"].startswith("all (~") and "?" not in "".join(labels.values())
    assert web._sized("x", None, files=3) == "x (3 files)"


def test_the_additive_dialog_marks_what_the_collection_holds(isolated_config, tmp_path):
    """Operator, 2026-11-01: wikipedia's grow showed no check on the languages
    held. `slices = "all"` holds every language (and checks "everything");
    a list holds the profiles of its delivered slices."""
    from dataclasses import replace
    c = collections_mod.Collection(
        name="wikipedia", plugin="wikipedia", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance", slices="all")
    held = web._profile_held(c)
    assert {"en", "it", "fa", "all"} <= set(held)
    some = replace(c, slices=("lang=it", "lang=en/pages=3M"))
    assert web._profile_held(some) == ["it", "en"]
    assert web._profile_held(replace(c, slices=None, profile="tr,it")) == ["tr", "it"]

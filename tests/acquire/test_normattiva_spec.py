"""Normattiva's acquisition spec: the precompiled collections as the origin
listed them on 2026-09-14, fetched by query into plain destination paths."""
from urllib.parse import parse_qs, urlsplit

from app.acquire import specs
from app.acquire.spec import Endpoint, RemoteFile, Transport
from app.acquire.transport_https import _url
from app.plugins.base import LicenseClass

SPEC = specs.NORMATTIVA_SPEC


def test_every_collection_is_requested_as_akn_in_one_form():
    files = SPEC.endpoints[0].static_files
    assert len(files) == len(specs._NORMATTIVA) == 23
    for f in files:
        q = parse_qs(urlsplit("x?" + f.remote.split("?", 1)[1]).query)
        assert q["formato"] == ["AKN"]
        assert q["formatoRichiesta"][0] in ("V", "O")
        assert f.rel_path == f"akn/{q['formatoRichiesta'][0]}/" + f.rel_path.split("/")[-1]
    assert len({f.rel_path for f in files}) == len(files), "one destination each"


def test_the_repealed_acts_are_fetched_and_findable_by_their_path():
    """The plugin reads `eli_in_force: False` from this collection's path:
    repeal is metadata, not deletion (eli-legal-plugins.md §5.7)."""
    rel = specs._normattiva_rel("Atti normativi abrogati (in originale)", "O")
    assert rel == "akn/O/atti-normativi-abrogati-in-originale.zip"
    assert rel in {f.rel_path for f in SPEC.endpoints[0].static_files}


def test_no_file_declares_a_size():
    """The origin rebuilds zips in place; a stale declared size would let a
    segmented download cut the new zip at the old length and record it
    complete."""
    assert all(f.size is None for f in SPEC.endpoints[0].static_files)


def test_the_query_is_what_is_requested_and_the_path_is_where_it_lands():
    ep = Endpoint(transport=Transport.HTTPS, url="https://h/collections/download")
    f = next(f for f in SPEC.endpoints[0].static_files if f.rel_path == "akn/V/codici.zip")
    assert _url(ep, f) == ("https://h/collections/download/collection-preconfezionata"
                           "?nome=Codici&formato=AKN&formatoRichiesta=V")
    assert _url(ep, RemoteFile(rel_path="a/b.zip")) == "https://h/collections/download/a/b.zip", \
        "every other spec still requests its rel_path"
    spaced = next(f for f in SPEC.endpoints[0].static_files if "leggi-costituzionali" in f.rel_path)
    assert "nome=Leggi%20costituzionali" in spaced.remote


def test_profiles_grow_and_the_licence_gate_is_yellow_with_terms():
    inc = {p: set(SPEC.profiles[p].include) for p in ("demo", "starter")}
    assert inc["demo"] <= inc["starter"]
    assert SPEC.estimated_bytes["demo"] < SPEC.estimated_bytes["starter"] < SPEC.estimated_bytes["full"]
    assert SPEC.license_class == LicenseClass.YELLOW
    assert SPEC.terms_text_path.is_file()
    assert b"\r" not in SPEC.terms_text_path.read_bytes(), "terms are byte-exact LF"
    assert specs.ACQUIRE_REGISTRY["normattiva"] is SPEC

"""The generic ELI layer (app/plugins/eli.py) — the rules every EU legal
plugin shares, docs/notes/eli-legal-plugins.md §6."""
import pytest

from app.plugins import eli


def test_native_id_is_the_work_key_hashed_under_its_date():
    """§5.2: date-sortable in the high digits, stable, inside int64."""
    a = eli.native_id("2003-11-15", "003G0333")
    assert a == eli.native_id("2003-11-15", "003G0333"), "a pure function of the key"
    assert a // 10**9 == 20031115
    assert a < 2**63
    assert eli.native_id("2003-11-16", "003G0333") > a, "later publication sorts later"


def test_the_same_codice_on_two_dates_is_two_acts():
    """ELI Italy p.8: 036U0886 names two acts, 1936-05-26 and 1937-01-16 —
    codice alone is not the key."""
    assert eli.native_id("1936-05-26", "036U0886") != eli.native_id("1937-01-16", "036U0886")


def test_a_date_that_is_not_iso_is_refused():
    with pytest.raises(ValueError):
        eli.native_id("15/11/2003", "003G0333")


def test_a_collision_is_reported_never_absorbed():
    guard = eli.CollisionGuard()
    assert guard.check(7, ("2003-11-15", "A")) is None
    assert guard.check(7, ("2003-11-15", "A")) is None, "the same act twice is not a collision"
    assert guard.check(7, ("2003-11-15", "B")) == ("2003-11-15", "A")


@pytest.mark.parametrize("returned, want", [
    ("eli/id/2018/06/08/18G00091/ORIGINAL", ("ORIGINAL", None)),
    ("eli/id/2003/11/15/003G0333/CONSOLIDATED/20180206", ("CONSOLIDATED", "2018-02-06")),
    ("http://www.normattiva.it/eli/id/2005/01/28/005G0020/CONSOLIDATED/ita/html",
     ("CONSOLIDATED", None)),
    ("eli/id/2008/06/19/008G0132/sg", (None, None)),
])
def test_the_expression_is_read_from_what_the_origin_returned(returned, want):
    """Rule 2. An undated CONSOLIDATED is the amendment history: no date."""
    assert eli.version_of(returned) == want


def test_a_profile_makes_absolute_uris_and_jurisdiction_qualified_keys():
    p = eli.Profile(jurisdiction="IT", authority="stato", base="http://www.normattiva.it/",
                    uri_template="…", work_key=("date_publication", "id_local"))
    assert p.absolute("eli/id/2003/11/15/003G0333") == \
        "http://www.normattiva.it/eli/id/2003/11/15/003G0333"
    assert p.absolute("http://x/eli/id/1") == "http://x/eli/id/1"
    assert p.key("id/2003/11/15/003G0333") == "eli:it:id/2003/11/15/003G0333"


def test_an_expression_id_sorts_by_the_start_of_validity_and_tells_versions_apart():
    """review §3.1: the version key is the origin's identifier of the
    Expression; an original and a consolidation dated the same day (ten
    acts in the archive) are two ids; later versions sort later."""
    work = ("1948-03-09", "048C0003")
    o = eli.expression_native_id("1948-03-09", work, "ORIGINAL")
    v11 = eli.expression_native_id("2001-02-01", work, "CONSOLIDATED/20010201")
    v12 = eli.expression_native_id("2001-02-16", work, "CONSOLIDATED/20010216")
    assert o // 10**9 == 19480309 and v11 // 10**9 == 20010201
    assert o < v11 < v12 < 2**63
    assert v11 == eli.expression_native_id("2001-02-01", work, "CONSOLIDATED/20010201")
    same_day_c = eli.expression_native_id("1929-05-08", ("1929-05-08", "029U0629"), "CONSOLIDATED/19290508")
    same_day_o = eli.expression_native_id("1929-05-08", ("1929-05-08", "029U0629"), "ORIGINAL")
    assert same_day_c != same_day_o and same_day_c // 10**9 == same_day_o // 10**9
    with pytest.raises(ValueError):
        eli.expression_native_id("1929", work, "ORIGINAL")


def test_the_force_status_key_is_the_documents():
    """09 §9.5 and 06 §6.1 name it `in_force`; the ELI keys stay prefixed."""
    assert eli.IN_FORCE == "in_force"
    assert "eli_in_force" not in eli.EXTRA_KEYS and "in_force" not in eli.EXTRA_KEYS

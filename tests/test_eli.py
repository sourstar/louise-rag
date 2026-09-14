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

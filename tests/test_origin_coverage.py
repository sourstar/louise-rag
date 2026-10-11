"""tests/test_origin_coverage.py — every plugin in the registry is under the
origin-coverage guard (tests/origin_coverage.py) or names why it is not, so a
new plugin cannot be added without either (docs/notes/STATUS.md, 2026-10-11)."""
from app import collections as collections_mod
from tests.origin_coverage import COVERED, EXEMPT


def test_every_plugin_is_covered_or_exempt_with_a_reason():
    registry = set(collections_mod.PLUGIN_REGISTRY)
    assert COVERED | set(EXEMPT) == registry, (
        f"not under the guard: {sorted(registry - COVERED - set(EXEMPT))}; "
        f"named and not registered: {sorted((COVERED | set(EXEMPT)) - registry)}")
    assert not COVERED & set(EXEMPT)
    assert all(len(why) > 40 for why in EXEMPT.values()), "an exemption is an argument"

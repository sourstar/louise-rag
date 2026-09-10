"""tests/test_asset_cache_busting.py — the stylesheet URL changes when the
stylesheet does.

`StaticFiles` sends an ETag and a Last-Modified and NO `Cache-Control`, so
a browser applies heuristic caching and may serve a stale copy for hours
without revalidating. Observed 2026-09-10: a licence badge gained
`.lic-yellow`, the node served the new CSS, and the browser kept a cached
stylesheet with no such rule — so the span matched nothing, inherited the
badge's green, and a shipped fix looked unshipped.
"""
from pathlib import Path

from app import web


def test_the_version_is_derived_from_the_static_files():
    v = web._asset_version()
    assert v and v != "0"
    assert all(c in "0123456789abcdef" for c in v)


def test_it_changes_when_a_static_file_changes(tmp_path, monkeypatch):
    """The whole point: same files, same tag; touched file, new tag."""
    before = web._asset_version()
    assert web._asset_version() == before, "not stable for unchanged files"

    css = Path(web.__file__).parent / "static" / "admin.css"
    original = css.stat()
    try:
        import os
        os.utime(css, ns=(original.st_atime_ns,
                          original.st_mtime_ns + 1_000_000_000))
        assert web._asset_version() != before
    finally:
        os.utime(css, ns=(original.st_atime_ns, original.st_mtime_ns))


def test_the_base_template_carries_it_on_both_assets():
    base = (Path(web.__file__).parent / "templates" / "base.html").read_text(
        encoding="utf-8")
    assert 'href="/static/admin.css?v={{ asset_v }}"' in base
    assert 'src="/static/vendor/htmx.min.js?v={{ asset_v }}"' in base


def test_the_global_is_available_to_templates():
    """Computed ONCE at import, not per render: `rglob` over the static
    tree on every page would be I/O for nothing, and on the node a deploy
    rewrites the files and restarts the process, so import time is exactly
    when it should be read. Asserted as a well-formed tag rather than
    against a live recomputation — the test above deliberately touches a
    file, and a suite that mutates the tree cannot also pin the constant.
    """
    v = web.TEMPLATES.env.globals.get("asset_v")
    assert v and all(c in "0123456789abcdef" for c in v)


def test_the_licence_class_has_its_own_colour_rule():
    """What the cache was hiding: the badge's border carries the
    acknowledgement and the class word carries the licence's own colour."""
    css = (Path(web.__file__).parent / "static" / "admin.css").read_text(
        encoding="utf-8")
    for cls in ("green", "yellow", "red", "unknown"):
        assert f".badge .lic-{cls}" in css, f"no colour rule for {cls}"
    library = (Path(web.__file__).parent / "templates" / "library.html").read_text(
        encoding="utf-8")
    assert 'class="lic-{{ c.license.class }}"' in library

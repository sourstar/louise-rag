"""09 §9.17: `origin.spec.files` entries are `{rel_path, size or null,
checksum or null}` — all three keys, always. A recipe is TOML, which has
no null, so an absent size or checksum is written out as null and an
unknown key is refused."""
import pytest

from app.hold import template

DUMP = "enwiki/20260801/enwiki-20260801-pages-articles-multistream.xml.bz2"


def _origin(files):
    return template._origin_from_recipe({
        "form": "the dated dumps", "unit": "index-page", "clock": "UTC",
        "cutoff_rule": "dated-dumps",
        "spec": {"source_id": "wikipedia", "files": files}})


def test_every_entry_carries_all_three_keys():
    o = _origin([{"rel_path": DUMP, "size": 26668484995}, {"rel_path": "x/y.bz2"}])
    assert o.spec["files"] == [
        {"rel_path": DUMP, "size": 26668484995, "checksum": None},
        {"rel_path": "x/y.bz2", "size": None, "checksum": None}]


def test_no_files_is_null():
    assert _origin([]).spec["files"] is None


@pytest.mark.parametrize("bad", [{"size": 1}, {"rel_path": DUMP, "sha1": "ab"}, "just/a/path"])
def test_a_malformed_entry_is_refused(bad):
    with pytest.raises(template.TemplateError):
        _origin([bad])

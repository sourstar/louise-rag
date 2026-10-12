"""app/vega/trees.py and `hold embedder tree-diff` (vega.md §12): two
per-file lists of a backend engine, diffed path by path — read from VeGa by
digest or from files — so a tree digest that moved names its byte."""
from __future__ import annotations

import json

import pytest

from app import louise
from app.hold.codex import canonical_json, digest
from app.vega import trees

BEFORE = {"run.sh": {"type": "file", "exec": True, "size": 10, "sha256": "ab" * 32},
          "lib": {"type": "dir"},
          "lib/libx.so.1": {"type": "link", "target": "libx.so.1.2.3"},
          "lib/libx.so.1.2.3": {"type": "file", "exec": False, "size": 13, "sha256": "cd" * 32},
          "gone": {"type": "file", "exec": False, "size": 1, "sha256": "ee" * 32}}
AFTER = {**{k: v for k, v in BEFORE.items() if k != "gone"},
         "lib/libx.so.1.2.3": {"type": "file", "exec": False, "size": 14, "sha256": "cf" * 32},
         "lib/libnew.so.1": {"type": "file", "exec": False, "size": 9, "sha256": "11" * 32}}


def test_a_diff_is_a_set_difference_on_paths_and_an_equality_on_entries():
    d = trees.diff(BEFORE, AFTER)
    assert d["added"] == {"lib/libnew.so.1": AFTER["lib/libnew.so.1"]}
    assert d["removed"] == {"gone": BEFORE["gone"]}
    assert d["changed"] == {"lib/libx.so.1.2.3": {"before": BEFORE["lib/libx.so.1.2.3"],
                                                  "after": AFTER["lib/libx.so.1.2.3"]}}
    assert d["same"] == 3
    assert trees.diff(BEFORE, BEFORE) == {"added": {}, "removed": {}, "changed": {}, "same": 5}
    lines = trees.lines(d)
    assert lines[0].startswith("+ lib/libnew.so.1  file 9 B sha256:111111111111")
    assert lines[1].startswith("- gone  file 1 B")
    assert lines[2].startswith("~ lib/libx.so.1.2.3  file 13 B sha256:cdcdcdcdcdcd… -> file 14 B")
    assert lines[-1] == "= 3 unchanged"


def test_a_tree_is_read_from_a_file_or_from_vega_by_its_digest(tmp_path):
    f = tmp_path / "before.json"
    f.write_text(json.dumps(BEFORE))
    assert trees.read(str(f)) == BEFORE
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"image": "x", "tree": BEFORE}))
    assert trees.read(str(wrapped)) == BEFORE, "an object wrapping the list under `tree`"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"a": 1}))
    with pytest.raises(ValueError, match="not a tree"):
        trees.read(str(bad))
    image = digest(canonical_json(AFTER))
    asked = []

    def get(url):
        asked.append(url)
        return AFTER
    assert trees.read(image, vega_url="http://vega:8090/", get=get) == AFTER
    assert asked == [f"http://vega:8090/vega/trees/{image}"]
    with pytest.raises(ValueError, match="no VeGa URL"):
        trees.read(image, vega_url=None)


def test_the_command_says_what_moved_and_exits_by_it(tmp_path, capsys, monkeypatch):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(BEFORE))
    b.write_text(json.dumps(AFTER))
    assert louise.main(["hold", "embedder", "tree-diff", str(a), str(a)]) == 0
    assert capsys.readouterr().out.strip().endswith("= 5 unchanged")
    assert louise.main(["hold", "embedder", "tree-diff", str(a), str(b)]) == 1
    out = capsys.readouterr().out
    assert "+ lib/libnew.so.1" in out and "- gone" in out and "~ lib/libx.so.1.2.3" in out
    assert louise.main(["hold", "embedder", "tree-diff", str(a), str(b), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["same"] == 3
    assert louise.main(["hold", "embedder", "tree-diff", str(a), str(tmp_path / "none.json")]) == 2
    monkeypatch.setattr("app.config.VEGA_URL", "")
    image = digest(canonical_json(AFTER))
    assert louise.main(["hold", "embedder", "tree-diff", str(a), image]) == 2, \
        "a digest needs VEGA_URL to be read from"

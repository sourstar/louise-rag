"""Where an export is built (operator, 2026-09-21: "export bundle should be
done in tank only").

EXPORT_DIR names bulk storage; every export that picks its own location —
the Library's download, `snapshot vectors`' default, a contribution — is
built under EXPORT_DIR/<name>/. An EXPORT_DIR that is not there is refused,
never created: inside a container an unmounted path is the container's own
storage, the one place the export must not land.
"""
import dataclasses
from pathlib import Path

import pytest

from app import config
from app.bundles import export as bundle_export


@dataclasses.dataclass
class _Collection:
    name: str
    manifest_path: Path


def test_without_export_dir_it_is_beside_the_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPORT_DIR", None)
    c = _Collection("pubmed", tmp_path / "state" / "manifest.sqlite")
    assert bundle_export.workspace(c) == tmp_path / "state"


def test_with_export_dir_it_is_the_collections_folder_there(tmp_path, monkeypatch):
    bulk = tmp_path / "bulk"
    bulk.mkdir()
    monkeypatch.setattr(config, "EXPORT_DIR", bulk)
    c = _Collection("pubmed", tmp_path / "state" / "manifest.sqlite")
    assert bundle_export.workspace(c) == bulk / "pubmed"
    assert (bulk / "pubmed").is_dir()


def test_an_absent_export_dir_is_refused_not_created(tmp_path, monkeypatch):
    bulk = tmp_path / "not-mounted"
    monkeypatch.setattr(config, "EXPORT_DIR", bulk)
    c = _Collection("pubmed", tmp_path / "state" / "manifest.sqlite")
    with pytest.raises(bundle_export.ExportError, match="EXPORT_DIR"):
        bundle_export.workspace(c)
    assert not bulk.exists()

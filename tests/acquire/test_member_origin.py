"""The origin as its zips' members (app/acquire/member_origin.py; 02 §2.3
`index-page`; review §3.10): a record's unit is its member, weighed and
fetched by Range, written as a one-member zip the plugin reads as the
corpus."""
import random
import zipfile
from pathlib import Path

import pytest

from app.acquire import member_origin, zipdelta
from app.acquire.spec import AcquisitionSpec, ChecksumSource, DeltaSpec, Endpoint, RemoteFile, Transport
from app.plugins.base import LicenseClass

MEMBERS = {f"D_1/2000-01-01_X{i:03d}_ORIGINALE_V0.xml": random.Random(i).randbytes(3000) for i in range(6)}


def _spec() -> AcquisitionSpec:
    return AcquisitionSpec(
        source_id="normattiva", transport=Transport.HTTPS,
        endpoints=(Endpoint(transport=Transport.HTTPS, url="https://h/download",
                            static_files=(RemoteFile(rel_path="akn/M/codici.zip", remote="x?nome=Codici"),)),),
        layout="container-files", filters=("*.zip",), checksums=ChecksumSource(kind="none"),
        delta=DeltaSpec(mode="zip-directory"), license_class=LicenseClass.GREEN)


def test_a_record_s_unit_is_its_member_and_it_is_fetched_as_a_one_member_zip(tmp_path):
    remote = tmp_path / "remote.zip"
    with zipfile.ZipFile(remote, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in MEMBERS.items():
            z.writestr(name, data)
    readers = []

    def reader_for(rel):
        assert rel == "akn/M/codici.zip"
        r = zipdelta.FileRangeReader(remote)
        readers.append(r)
        return r
    mo = member_origin.MemberOrigin(_spec(), reader_for=reader_for)
    member = "D_1/2000-01-01_X002_ORIGINALE_V0.xml"
    unit, size = mo.unit(f"akn/M/codici.zip#{member}")
    assert unit == f"akn/M/codici.zip#{member}"
    with zipfile.ZipFile(remote) as z:
        assert size == z.getinfo(member).compress_size
    assert mo.unit("akn/M/other.zip#x") == ("akn/M/other.zip#x", None), "a zip the spec does not list"
    assert mo.unit(f"akn/M/codici.zip#nope") == ("akn/M/codici.zip#nope", None)
    out = mo.materialise(f"akn/M/codici.zip#{member}")
    assert out == mo.base / "akn/M/codici.zip" and out.is_file()
    with zipfile.ZipFile(out) as z:
        assert [i.filename for i in z.infolist()] == [member]
        assert z.read(member) == MEMBERS[member] and z.testzip() is None
    assert len(readers) == 1, "one reader per zip, its directory read once"
    assert len(readers[0].reads) == 2, "one read for the directory, one for the member"
    with pytest.raises(KeyError):
        mo.materialise("akn/M/codici.zip#nope")


def test_only_an_index_page_codex_over_https_gets_a_member_origin():
    class Origin:
        unit = "index-page"

    class Codex:
        plugin = "normattiva"
        origin = Origin()
    mo = member_origin.for_codex(Codex())
    assert mo is not None and "akn/M/codici.zip" in mo.files and "akn/O/atti-normativi-abrogati-in-originale.zip" in mo.files
    Codex.origin.unit = "container-member"
    assert member_origin.for_codex(Codex()) is None
    Codex.origin.unit = "index-page"
    Codex.plugin = "gutenberg"
    assert member_origin.for_codex(Codex()) is None, "rsync, not a zip over HTTPS"
    Codex.plugin = "nope"
    assert member_origin.for_codex(Codex()) is None

"""app/refs/acquire.py — self-fetching reference dumps. No real network:
the transport is injected, exactly the seam the module exposes for this.
"""
import gzip
import time

import pytest

from app.plugins.base import RefSyncState
from app.refs import acquire as refs_acquire
from app.refs import connect, store as refs_store
from app.refs.acquire import REF_DUMPS, RefDump, _clinvar_transform, fetch_all, fetch_one


class FakeTransport:
    """Writes canned bytes per rel_path; counts calls."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.calls = []

    def fetch_one(self, endpoint, remote, dest, resume_from):
        self.calls.append(remote.rel_path)
        data = self.files[remote.rel_path]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return len(data)


HGNC_TSV = (b"hgnc_id\tsymbol\tname\tlocus_group\tlocation\n"
             b"HGNC:1101\tBRCA2\tBRCA2 DNA repair associated\tprotein-coding gene\t13q13.1\n")

CLINVAR_HEADER = ("#AlleleID\tType\tName\tGeneID\tGeneSymbol\tHGNC_ID\t"
                   "ClinicalSignificance\tClinSigSimple\tLastEvaluated\tRS# (dbSNP)\t"
                   "nsv/esv (dbVar)\tRCVaccession\tPhenotypeIDS\tPhenotypeList\tOrigin\t"
                   "OriginSimple\tAssembly\tChromosomeAccession\tChromosome\tStart\tStop\t"
                   "ReferenceAllele\tAlternateAllele\tCytogenetic\tReviewStatus\t"
                   "NumberSubmitters\tGuidelines\tTestedInGTR\tOtherIDs\tSubmitterCategories\t"
                   "VariationID\tPositionVCF\tReferenceAlleleVCF\tAlternateAlleleVCF\n")


def _clinvar_row(**overrides) -> str:
    """Build a variant_summary row positionally from the header — 34 columns,
    '-' everywhere not overridden, so the fixture can't silently misalign."""
    fields = CLINVAR_HEADER.strip().split("\t")
    row = {f: "-" for f in fields}
    row.update(overrides)
    return "\t".join(row[f] for f in fields) + "\n"


def _clinvar_gz() -> bytes:
    rows = [
        CLINVAR_HEADER,
        _clinvar_row(**{"#AlleleID": "15041", "Name": "NM_014855.3(AP5Z1):c.80_83del",
                         "GeneSymbol": "AP5Z1", "ClinicalSignificance": "Pathogenic",
                         "Assembly": "GRCh37", "VariationID": "100"}),
        # same VariationID on the other assembly — must dedupe
        _clinvar_row(**{"#AlleleID": "15041", "Name": "NM_014855.3(AP5Z1):c.80_83del",
                         "GeneSymbol": "AP5Z1", "ClinicalSignificance": "Pathogenic",
                         "Assembly": "GRCh38", "VariationID": "100"}),
        _clinvar_row(**{"#AlleleID": "15042", "Name": "NM_017547.4(FOXRED1):c.694C>T",
                         "GeneSymbol": "FOXRED1", "ClinicalSignificance": "Likely benign",
                         "Assembly": "GRCh38", "VariationID": "200"}),
    ]
    return gzip.compress("".join(rows).encode())


@pytest.fixture
def fake_transport():
    return FakeTransport({"hgnc_complete_set.txt": HGNC_TSV,
                           "variant_summary.txt.gz": _clinvar_gz()})


def test_fetch_all_downloads_transforms_and_is_freshness_gated(tmp_path, fake_transport):
    results = fetch_all(tmp_path, transport=fake_transport)
    assert results["hgnc"]["status"] == "fetched"
    assert results["clinvar-mini"]["status"] == "fetched"

    hgnc = (tmp_path / "hgnc.tsv").read_text()
    assert "BRCA2" in hgnc

    clinvar = (tmp_path / "clinvar_mini.tsv").read_text().splitlines()
    assert clinvar[0] == "variant_id\tgene_symbol\tsignificance\tdescription"
    assert len(clinvar) == 3, "assembly duplicates must collapse to one row"
    assert clinvar[1].startswith("100\tAP5Z1\tPathogenic")
    assert clinvar[2].startswith("200\tFOXRED1\tLikely benign")

    # second run: files are fresh — no network calls at all
    calls_before = len(fake_transport.calls)
    results = fetch_all(tmp_path, transport=fake_transport)
    assert all(r["status"] == "fresh" for r in results.values())
    assert len(fake_transport.calls) == calls_before

    # force refetches
    results = fetch_all(tmp_path, force=True, transport=fake_transport)
    assert all(r["status"] == "fetched" for r in results.values())


def test_fetched_dumps_flow_through_the_real_sync(tmp_path, fake_transport):
    fetch_all(tmp_path, transport=fake_transport)
    con = connect(tmp_path / "refs.db")
    results = refs_store.sync_all(con, tmp_path)
    assert results["hgnc"]["rows"] == 1
    assert results["clinvar-mini"]["rows"] == 2
    gene = con.execute("SELECT * FROM hgnc_gene WHERE symbol='BRCA2'").fetchone()
    assert gene["hgnc_id"] == "HGNC:1101" and gene["location"] == "13q13.1"
    var = con.execute("SELECT * FROM clinvar_variant WHERE variant_id='100'").fetchone()
    assert var["gene_symbol"] == "AP5Z1" and var["significance"] == "Pathogenic"


def test_refreshed_dump_resyncs_without_a_version_bump(tmp_path, fake_transport):
    fetch_all(tmp_path, transport=fake_transport)
    con = connect(tmp_path / "refs.db")
    refs_store.sync_all(con, tmp_path)
    assert refs_store.sync_all(con, tmp_path)["hgnc"]["skipped"] is True

    time.sleep(1.1)   # mtime granularity
    fake_transport.files["hgnc_complete_set.txt"] = HGNC_TSV.replace(
        b"13q13.1", b"13q13.2")
    fetch_all(tmp_path, force=True, transport=fake_transport)
    results = refs_store.sync_all(con, tmp_path)
    assert results["hgnc"]["skipped"] is not True
    gene = con.execute("SELECT location FROM hgnc_gene WHERE symbol='BRCA2'").fetchone()
    assert gene["location"] == "13q13.2"


def test_one_unreachable_origin_does_not_block_the_other(tmp_path):
    class HalfBroken(FakeTransport):
        def fetch_one(self, endpoint, remote, dest, resume_from):
            if "variant_summary" in remote.rel_path:
                from app.acquire.spec import TransportError
                raise TransportError("origin down")
            return super().fetch_one(endpoint, remote, dest, resume_from)

    results = fetch_all(tmp_path, transport=HalfBroken(
        {"hgnc_complete_set.txt": HGNC_TSV}))
    assert results["hgnc"]["status"] == "fetched"
    assert results["clinvar-mini"]["status"] == "error"


def test_link_columns_are_indexed_at_real_scale(tmp_path, fake_transport):
    fetch_all(tmp_path, transport=fake_transport)
    con = connect(tmp_path / "refs.db")
    refs_store.sync_all(con, tmp_path)
    plan = con.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM clinvar_variant WHERE gene_symbol='AP5Z1'"
    ).fetchall()
    assert any("idx_clinvar_variant_gene_symbol" in r["detail"] for r in plan)

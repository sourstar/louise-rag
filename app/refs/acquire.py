"""Self-fetching reference dumps — architecture-v2.md §5.1's
"assemblies/ClinVar/HGNC → HTTPS dumps", scoped to the sources the refs
layer serves today.

Downloads ride the SAME budgeted, Range-resumable HTTPS transport as corpus
acquisition (invariant 5: no code path moves bytes outside the transfer
budget). Raw dumps land under ref_root, an optional transform derives the
file the source's unchanged sync() reader expects, and freshness is
mtime-based so re-running is cheap and idempotent.

Both current dumps are license-green: HGNC's complete set is CC0; ClinVar
is public-domain NCBI data. A future yellow-class dump must add the same
acknowledgement gate corpus acquisition uses — do not fetch it without one.
"""
import csv
import gzip
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..acquire.spec import Endpoint, RemoteFile, Transport, TransportError
from ..acquire.transport_https import HttpsTransport


@dataclass(frozen=True)
class RefDump:
    source_id: str
    base_url: str
    rel_path: str
    raw_name: str                     # download target under ref_root
    final_name: str                   # what the ReferenceSource's sync() reads
    max_age_days: int = 30
    # transform(raw_path, final_path) — absent means the raw IS the final
    transform: Callable[[Path, Path], None] | None = None


def _clinvar_transform(raw: Path, final: Path) -> None:
    """variant_summary.txt.gz → the four-column clinvar_mini.tsv.

    The summary carries one row per assembly (GRCh37 + GRCh38) per
    variation; rows are deduplicated on VariationID keeping the first, since
    the mini schema stores no coordinates and the four kept columns are
    assembly-invariant. Streamed — the decompressed dump is hundreds of MB
    and never needs to be in memory at once.
    """
    seen: set[str] = set()
    tmp = final.with_suffix(".tsv.tmp")
    with gzip.open(raw, "rt", encoding="utf-8", errors="replace") as src, \
            open(tmp, "w", newline="", encoding="utf-8") as out:
        reader = csv.DictReader(src, delimiter="\t")
        writer = csv.DictWriter(
            out, fieldnames=("variant_id", "gene_symbol", "significance",
                              "description"), delimiter="\t")
        writer.writeheader()
        for row in reader:
            vid = row.get("VariationID") or ""
            if not vid or vid in seen:
                continue
            seen.add(vid)
            writer.writerow({
                "variant_id": vid,
                "gene_symbol": row.get("GeneSymbol") or "",
                "significance": row.get("ClinicalSignificance") or "",
                "description": row.get("Name") or "",
            })
    tmp.replace(final)


def _gnomad_transform(raw: Path, final: Path) -> None:
    """lof_metrics.by_gene.txt.bgz (bgz = gzip-compatible) → the seven-column
    gnomad_constraint.tsv the ref source reads. Values 'NA' become empty
    (SQLite NULL-ish empty strings would poison REAL columns — skip genes
    with no pLI at all, they carry no constraint signal)."""
    seen: set[str] = set()
    tmp = final.with_suffix(".tsv.tmp")
    with gzip.open(raw, "rt", encoding="utf-8", errors="replace") as src, \
            open(tmp, "w", newline="", encoding="utf-8") as out:
        reader = csv.DictReader(src, delimiter="\t")
        writer = csv.DictWriter(
            out, fieldnames=("gene_symbol", "transcript", "pli", "loeuf",
                              "oe_mis", "obs_lof", "exp_lof"), delimiter="\t")
        writer.writeheader()
        for row in reader:
            gene = row.get("gene") or ""
            pli = row.get("pLI") or ""
            if not gene or gene in seen or pli in ("", "NA"):
                continue
            seen.add(gene)
            def _num(key):
                v = row.get(key) or ""
                return v if v not in ("", "NA") else "0"
            writer.writerow({
                "gene_symbol": gene,
                "transcript": row.get("transcript") or "",
                "pli": pli,
                "loeuf": _num("oe_lof_upper"),
                "oe_mis": _num("oe_mis"),
                "obs_lof": _num("obs_lof"),
                "exp_lof": _num("exp_lof"),
            })
    tmp.replace(final)


def _homologene_transform(raw: Path, final: Path) -> None:
    """homologene.data (headerless TSV: group, taxid, gene_id, symbol,
    protein_gi, protein_acc) → the five keyed columns the ref source reads.
    The composite key makes (group, taxid, symbol) unique — TP53 and Trp53
    stay species-qualified (§9)."""
    tmp = final.with_suffix(".tsv.tmp")
    with open(raw, newline="", encoding="utf-8", errors="replace") as src, \
            open(tmp, "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out, delimiter="\t")
        writer.writerow(("key", "group_id", "taxid", "gene_id", "symbol"))
        for row in csv.reader(src, delimiter="\t"):
            if len(row) < 4:
                continue
            group, taxid, gene_id, symbol = row[0], row[1], row[2], row[3]
            if not (group.isdigit() and taxid.isdigit() and symbol):
                continue
            writer.writerow((f"{group}:{taxid}:{symbol}", group, taxid,
                              gene_id, symbol))
    tmp.replace(final)


REF_DUMPS: tuple[RefDump, ...] = (
    RefDump(
        source_id="hgnc",
        base_url="https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv",
        rel_path="hgnc_complete_set.txt",
        raw_name="hgnc.tsv",          # column superset of what sync() reads
        final_name="hgnc.tsv",
    ),
    RefDump(
        source_id="clinvar-mini",
        base_url="https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited",
        rel_path="variant_summary.txt.gz",
        raw_name="variant_summary.txt.gz",
        final_name="clinvar_mini.tsv",
        transform=_clinvar_transform,
    ),
    RefDump(
        source_id="homologene",
        base_url="https://ftp.ncbi.nih.gov/pub/HomoloGene/build68",
        rel_path="homologene.data",
        raw_name="homologene.data",
        final_name="homologene.tsv",
        max_age_days=3650,    # build 68 is frozen — refetch is pure hygiene
        transform=_homologene_transform,
    ),
    RefDump(
        source_id="gnomad-constraint",
        base_url=("https://storage.googleapis.com/gcp-public-data--gnomad/"
                   "release/2.1.1/constraint"),
        rel_path="gnomad.v2.1.1.lof_metrics.by_gene.txt.bgz",
        raw_name="gnomad.lof_metrics.by_gene.txt.bgz",
        final_name="gnomad_constraint.tsv",
        max_age_days=365,     # v2.1.1 is a frozen release; refetch is hygiene
        transform=_gnomad_transform,
    ),
)


def _is_fresh(path: Path, max_age_days: int) -> bool:
    if not path.is_file():
        return False
    return (time.time() - path.stat().st_mtime) < max_age_days * 86400


def fetch_one(dump: RefDump, ref_root: Path, *, force: bool = False,
              transport=None) -> dict:
    ref_root.mkdir(parents=True, exist_ok=True)
    final = ref_root / dump.final_name
    if not force and _is_fresh(final, dump.max_age_days):
        return {"status": "fresh", "path": str(final)}

    raw = ref_root / dump.raw_name
    transport = transport or HttpsTransport()
    endpoint = Endpoint(transport=Transport.HTTPS, url=dump.base_url)
    part = raw.with_name(raw.name + ".part")
    resume_from = part.stat().st_size if part.is_file() else 0
    size = transport.fetch_one(endpoint, RemoteFile(rel_path=dump.rel_path),
                                raw, resume_from)
    if dump.transform is not None:
        dump.transform(raw, final)
    return {"status": "fetched", "bytes": size, "path": str(final)}


def fetch_all(ref_root: Path, *, force: bool = False, transport=None) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for dump in REF_DUMPS:
        try:
            results[dump.source_id] = fetch_one(dump, ref_root, force=force,
                                                 transport=transport)
        except (TransportError, OSError) as e:
            # one unreachable origin must not block the other dumps
            results[dump.source_id] = {"status": "error", "error": str(e)}
    return results

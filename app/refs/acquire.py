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

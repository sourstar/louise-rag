"""gnomAD gene constraint — pLI / LOEUF per gene from the v2.1.1
lof_metrics.by_gene table (architecture-v2.md §5.1 "gnomAD → public
buckets"; license green). The numbers a clinician pins next to a gene:
pLI (probability of loss-of-function intolerance) and LOEUF (the
observed/expected upper bound — lower = more constrained).

Reads gnomad_constraint.tsv under ref_root; refs/acquire.py derives that
file from the published .bgz (gzip-compatible) dump.
"""
import csv

from ..plugins.base import RefColumn, RefLink, RefRecordBatch, RefSchema, RefSyncState, RefTable

TABLE = RefTable(
    name="gnomad_constraint",
    columns=(
        RefColumn("gene_symbol", "TEXT"),
        RefColumn("transcript", "TEXT"),
        RefColumn("pli", "REAL"),
        RefColumn("loeuf", "REAL"),
        RefColumn("oe_mis", "REAL"),
        RefColumn("obs_lof", "INTEGER"),
        RefColumn("exp_lof", "REAL"),
    ),
    primary_key="gene_symbol",
)

LINKS = (
    RefLink(name="constraint_to_gene", from_table="gnomad_constraint",
            from_column="gene_symbol", to_table="hgnc_gene", to_column="symbol"),
)


class GnomadConstraintSource:
    source_id = "gnomad-constraint"
    ref_version = 1

    def schema(self) -> RefSchema:
        return RefSchema(source_id=self.source_id, tables=(TABLE,), links=LINKS)

    def sync(self, state: RefSyncState):
        path = state.ref_root / "gnomad_constraint.tsv"
        if not path.is_file():
            return
        with open(path, newline="", encoding="utf-8") as f:
            rows = tuple(dict(r) for r in csv.DictReader(f, delimiter="\t"))
        if rows:
            yield RefRecordBatch(table=TABLE.name, rows=rows)

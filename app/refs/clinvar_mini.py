"""ClinVar-mini — a subset schema (variant id, gene, significance) from a
small TSV. See architecture-v2.md §4/§9.
"""
import csv

from ..plugins.base import RefColumn, RefLink, RefRecordBatch, RefSchema, RefSyncState, RefTable

TABLE = RefTable(
    name="clinvar_variant",
    columns=(
        RefColumn("variant_id", "TEXT"),
        RefColumn("gene_symbol", "TEXT"),
        RefColumn("significance", "TEXT"),
        RefColumn("description", "TEXT"),
    ),
    primary_key="variant_id",
)

LINKS = (
    RefLink(name="variant_to_gene", from_table="clinvar_variant", from_column="gene_symbol",
            to_table="hgnc_gene", to_column="symbol"),
)


class ClinvarMiniSource:
    source_id = "clinvar-mini"
    ref_version = 1

    def schema(self) -> RefSchema:
        return RefSchema(source_id=self.source_id, tables=(TABLE,), links=LINKS)

    def sync(self, state: RefSyncState):
        path = state.ref_root / "clinvar_mini.tsv"
        if not path.is_file():
            return
        with open(path, newline="", encoding="utf-8") as f:
            rows = tuple(dict(r) for r in csv.DictReader(f, delimiter="\t"))
        if rows:
            yield RefRecordBatch(table=TABLE.name, rows=rows)

"""HGNC gene symbols — symbols/ids/coordinates from a small TSV. See
architecture-v2.md §4/§8.
"""
import csv

from ..plugins.base import RefColumn, RefRecordBatch, RefSchema, RefSyncState, RefTable

TABLE = RefTable(
    name="hgnc_gene",
    columns=(
        RefColumn("hgnc_id", "TEXT"),
        RefColumn("symbol", "TEXT"),
        RefColumn("name", "TEXT"),
        RefColumn("location", "TEXT"),
    ),
    primary_key="hgnc_id",
)


class HgncSource:
    source_id = "hgnc"
    ref_version = 1

    def schema(self) -> RefSchema:
        return RefSchema(source_id=self.source_id, tables=(TABLE,))

    def sync(self, state: RefSyncState):
        path = state.ref_root / "hgnc.tsv"
        if not path.is_file():
            return
        with open(path, newline="", encoding="utf-8") as f:
            rows = tuple(dict(r) for r in csv.DictReader(f, delimiter="\t"))
        if rows:
            yield RefRecordBatch(table=TABLE.name, rows=rows)
